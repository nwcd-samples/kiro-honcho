"""User management API routes."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.database import get_session
from app.models import AWSAccount, ICUser, KiroSubscription
from app.schemas.user import (
    UserCreate,
    UserUpdate,
    UserResponse,
    UserListResponse,
    PasswordResetRequest,
    PasswordResetResponse,
    EmailVerificationResponse,
    GroupListResponse,
    AddUserToGroupRequest,
    AddUserToGroupResponse,
    BatchResetPasswordRequest,
    BatchDeleteRequest,
    BatchActionResult,
    BatchActionResponse,
)
from app.services import AccountService
from app.services.log_service import OperationLogService
from app.aws import AWSClient, IdentityCenterClient
from app.middleware import get_current_user

router = APIRouter(prefix="/accounts/{account_id}/users", tags=["Users"])


def _get_user_response(user: ICUser) -> UserResponse:
    """Helper to create UserResponse from ICUser."""
    return UserResponse(
        id=user.id,
        user_id=user.user_id,
        user_name=user.user_name,
        display_name=user.display_name,
        email=user.email,
        given_name=user.given_name,
        family_name=user.family_name,
        status=user.status,
        groups=user.groups,
        has_subscription=bool(user.subscription),
        subscription_type=user.subscription.subscription_type if user.subscription else None,
        subscription_status=user.subscription.status if user.subscription else None,
        pending_subscription_type=user.pending_subscription_type,
        email_verified=user.email_verified if user.email_verified else False,
        last_synced=user.last_synced,
        created_at=user.created_at
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    account_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    search: Optional[str] = Query(None, description="Search by email or name"),
    email_verified: Optional[bool] = Query(None, description="Filter by email verified"),
    subscription_status: Optional[str] = Query(None, description="Filter: ACTIVE, PENDING, NONE, UNVERIFIED"),
    sort_by: Optional[str] = Query(None, description="Sort field: email, status, subscription, created_at"),
    sort_order: Optional[str] = Query("desc", description="asc or desc"),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """List users for an AWS account with filtering and sorting."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    # Build query
    base_query = select(ICUser).options(selectinload(ICUser.subscription)).where(
        ICUser.aws_account_id == account_id
    )
    
    if search:
        base_query = base_query.where(
            ICUser.email.ilike(f"%{search}%") |
            ICUser.display_name.ilike(f"%{search}%") |
            ICUser.user_name.ilike(f"%{search}%")
        )
    
    # 邮箱验证过滤
    if email_verified is not None:
        base_query = base_query.where(ICUser.email_verified == email_verified)
    
    # 订阅状态过滤需要 join
    if subscription_status:
        upper_status = subscription_status.upper()
        if upper_status == "NONE":
            # 没有订阅的用户
            base_query = base_query.outerjoin(KiroSubscription, KiroSubscription.user_id == ICUser.id).where(
                KiroSubscription.id.is_(None)
            )
        elif upper_status == "UNVERIFIED":
            # 邮箱未验证
            base_query = base_query.where(ICUser.email_verified == False)
        elif upper_status in ("ACTIVE", "PENDING"):
            base_query = base_query.join(KiroSubscription, KiroSubscription.user_id == ICUser.id).where(
                func.upper(KiroSubscription.status) == upper_status
            )
    
    # Count
    count_query = select(func.count()).select_from(base_query.subquery())
    total = await session.scalar(count_query)
    
    # Sorting
    order_col = ICUser.created_at  # default
    if sort_by == "status":
        order_col = ICUser.email_verified
    elif sort_by == "subscription":
        order_col = ICUser.pending_subscription_type
    elif sort_by == "created_at":
        order_col = ICUser.created_at
    
    if sort_order == "asc":
        query = base_query.offset(skip).limit(limit).order_by(order_col.asc())
    else:
        query = base_query.offset(skip).limit(limit).order_by(order_col.desc())
    
    result = await session.execute(query)
    users = list(result.unique().scalars().all())
    
    return UserListResponse(
        total=total,
        users=[_get_user_response(u) for u in users]
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    account_id: int,
    request: UserCreate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Create a new user in Identity Center.
    
    用户创建后立即返回，邮件发送和订阅分配在后台异步执行。
    """
    import asyncio
    from app.database import async_session_maker

    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    if account.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account not verified"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        # Check if user exists
        existing = ic_client.find_user_by_email(account.identity_store_id, request.email)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"User with email {request.email} already exists"
            )
        
        # Create user in Identity Center
        given_name = request.given_name or request.email.split("@")[0]
        family_name = request.family_name or "Mr"
        display_name = request.display_name or f"{given_name} {family_name}"
        
        # username 优先级: user_name > given_name > 邮箱前缀
        if request.user_name:
            username = request.user_name
        elif request.given_name:
            username = request.given_name.lower()
        else:
            username = request.email.split("@")[0]
        
        # 创建用户，重复 username 时 fallback 到完整邮箱
        try:
            user_id = ic_client.create_user(
                identity_store_id=account.identity_store_id,
                username=username,
                display_name=display_name,
                given_name=given_name,
                family_name=family_name,
                email=request.email
            )
        except Exception as e:
            if "Duplicate" in str(e) or "Conflict" in str(e) or "duplicate" in str(e):
                username = request.email
                user_id = ic_client.create_user(
                    identity_store_id=account.identity_store_id,
                    username=username,
                    display_name=display_name,
                    given_name=given_name,
                    family_name=family_name,
                    email=request.email
                )
            else:
                raise
        
        # Save to database
        ic_user = ICUser(
            aws_account_id=account_id,
            user_id=user_id,
            user_name=username,
            display_name=display_name,
            email=request.email,
            given_name=given_name,
            family_name=family_name,
            status="enabled",
            email_verified=False,
            pending_subscription_type=request.subscription_type if request.auto_subscribe else None,
        )
        session.add(ic_user)
        await session.commit()
        await session.refresh(ic_user)
        
        # 记录创建用户日志
        log_service = OperationLogService(session)
        operator = current_user.get("username") if current_user else None
        await log_service.log_operation(
            account_id=account_id,
            operation="create_user",
            target=f"user:{request.email}",
            status="success",
            message=f"用户已创建: {request.email}，后台正在发送邮件和分配订阅",
            details={"user_id": user_id, "user_name": username},
            operator=operator,
        )
        
        # 后台异步执行：发送邮件 + 分配订阅
        # 保存需要的参数（不能在 task 里用 session）
        _account_id = account_id
        _user_id = user_id
        _ic_user_id = ic_user.id
        _email = request.email
        _send_reset = request.send_password_reset
        _auto_sub = request.auto_subscribe
        _sub_type = request.subscription_type
        _instance_arn = account.instance_arn
        _identity_store_id = account.identity_store_id
        _kiro_region = account.kiro_region
        _sso_region = account.sso_region
        _operator = operator

        async def _background_setup():
            """后台执行邮件发送和订阅分配."""
            try:
                # 发送邮件
                if _send_reset:
                    ic_client.send_password_reset_email(_user_id)
                    # 密码重置邮件已包含邮箱激活，无需单独发送验证邮件
                
                # 分配订阅
                if _auto_sub:
                    from app.aws import KiroSubscriptionClient
                    kiro_client = KiroSubscriptionClient(
                        aws_client, kiro_region=_kiro_region, sso_region=_sso_region
                    )
                    sub_result = kiro_client.create_assignment(
                        instance_arn=_instance_arn,
                        principal_id=_user_id,
                        subscription_type=_sub_type,
                    )
                    
                    async with async_session_maker() as bg_session:
                        from app.services.log_service import OperationLogService as BgLogService
                        bg_log = BgLogService(bg_session)
                        
                        if sub_result["success"]:
                            new_sub = KiroSubscription(
                                aws_account_id=_account_id,
                                user_id=_ic_user_id,
                                principal_id=_user_id,
                                subscription_type=_sub_type,
                                status="PENDING",
                            )
                            bg_session.add(new_sub)
                            
                            # 清除 pending 标记
                            from sqlalchemy import update
                            await bg_session.execute(
                                update(ICUser).where(ICUser.id == _ic_user_id).values(pending_subscription_type=None)
                            )
                            await bg_session.commit()
                            
                            await bg_log.log_operation(
                                account_id=_account_id,
                                operation="auto_subscribe",
                                target=f"user:{_email}",
                                status="success",
                                message=f"订阅分配成功: {_email} -> {_sub_type}，邮件已发送",
                                operator=_operator,
                            )
                        else:
                            await bg_log.log_operation(
                                account_id=_account_id,
                                operation="auto_subscribe",
                                target=f"user:{_email}",
                                status="failed",
                                message=f"订阅分配失败: {sub_result.get('message', '')}，后台将重试",
                                operator=_operator,
                            )
                elif _send_reset:
                    # 只发了邮件，记录日志
                    async with async_session_maker() as bg_session:
                        from app.services.log_service import OperationLogService as BgLogService
                        bg_log = BgLogService(bg_session)
                        await bg_log.log_operation(
                            account_id=_account_id,
                            operation="send_email",
                            target=f"user:{_email}",
                            status="success",
                            message=f"邮件已发送: {_email}（密码重置 + 邮箱验证）",
                            operator=_operator,
                        )
            except Exception as e:
                import logging
                logging.getLogger("create_user").error(f"后台任务失败 ({_email}): {e}")
                try:
                    async with async_session_maker() as bg_session:
                        from app.services.log_service import OperationLogService as BgLogService
                        bg_log = BgLogService(bg_session)
                        await bg_log.log_operation(
                            account_id=_account_id,
                            operation="create_user_background",
                            target=f"user:{_email}",
                            status="failed",
                            message=f"后台任务失败: {str(e)}",
                            operator=_operator,
                        )
                except Exception:
                    pass

        asyncio.create_task(_background_setup())
        
        # 立即返回用户信息
        refreshed_query = select(ICUser).options(selectinload(ICUser.subscription)).where(ICUser.id == ic_user.id)
        ic_user = await session.scalar(refreshed_query)
        return _get_user_response(ic_user)
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user: {str(e)}"
        )


@router.post("/batch-reset-password", response_model=BatchActionResponse)
async def batch_reset_password(
    account_id: int,
    request: BatchResetPasswordRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Batch send password reset (or OTP) emails to multiple users."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )

    # Load target users belonging to this account
    query = select(ICUser).where(
        ICUser.aws_account_id == account_id,
        ICUser.id.in_(request.user_ids),
    )
    result = await session.execute(query)
    users = {u.id: u for u in result.scalars().all()}

    access_key, secret_key = account_service.decrypt_credentials(account)
    aws_client = AWSClient(access_key, secret_key, account.sso_region)
    ic_client = IdentityCenterClient(aws_client, account.sso_region)
    log_service = OperationLogService(session)
    operator = current_user.get("username") if current_user else None

    results: list[BatchActionResult] = []
    success_count = 0
    failed_count = 0

    for uid in request.user_ids:
        ic_user = users.get(uid)
        if not ic_user:
            failed_count += 1
            results.append(BatchActionResult(user_id=uid, email=None, success=False, message="User not found"))
            continue
        try:
            if request.mode == "otp":
                res = ic_client.send_password_reset_otp(ic_user.user_id)
            else:
                res = ic_client.send_password_reset_email(ic_user.user_id)

            if res["success"]:
                success_count += 1
            else:
                failed_count += 1
            results.append(BatchActionResult(
                user_id=uid, email=ic_user.email,
                success=res["success"], message=res["message"],
            ))
        except Exception as e:
            failed_count += 1
            results.append(BatchActionResult(user_id=uid, email=ic_user.email, success=False, message=str(e)))

    await log_service.log_operation(
        account_id=account_id,
        operation="batch_reset_password",
        target=f"users:{len(request.user_ids)}",
        status="success" if failed_count == 0 else "failed",
        message=f"批量重置密码: 成功 {success_count}，失败 {failed_count}",
        operator=operator,
    )

    return BatchActionResponse(
        total=len(request.user_ids),
        success_count=success_count,
        failed_count=failed_count,
        results=results,
    )


@router.post("/batch-delete", response_model=BatchActionResponse)
async def batch_delete_users(
    account_id: int,
    request: BatchDeleteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Batch delete users: cancel subscriptions and remove from Identity Center."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)

    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )

    query = select(ICUser).where(
        ICUser.aws_account_id == account_id,
        ICUser.id.in_(request.user_ids),
    )
    result = await session.execute(query)
    users = {u.id: u for u in result.scalars().all()}

    access_key, secret_key = account_service.decrypt_credentials(account)
    aws_client = AWSClient(access_key, secret_key, account.sso_region)
    ic_client = IdentityCenterClient(aws_client, account.sso_region)

    from app.aws import KiroSubscriptionClient
    kiro_client = KiroSubscriptionClient(
        aws_client,
        kiro_region=account.kiro_region,
        sso_region=account.sso_region,
    )
    log_service = OperationLogService(session)
    operator = current_user.get("username") if current_user else None

    results: list[BatchActionResult] = []
    success_count = 0
    failed_count = 0

    for uid in request.user_ids:
        ic_user = users.get(uid)
        if not ic_user:
            failed_count += 1
            results.append(BatchActionResult(user_id=uid, email=None, success=False, message="User not found"))
            continue
        user_email = ic_user.email
        try:
            # Step 1: cancel Kiro subscription if any
            sub_query = select(KiroSubscription).where(
                KiroSubscription.aws_account_id == account_id,
                KiroSubscription.principal_id == ic_user.user_id,
            )
            subscription = await session.scalar(sub_query)
            if subscription:
                del_sub_result = kiro_client.delete_assignment(
                    instance_arn=account.instance_arn,
                    principal_id=ic_user.user_id,
                    subscription_type=subscription.subscription_type,
                )
                if not del_sub_result["success"]:
                    kiro_client.delete_assignment(
                        instance_arn=account.instance_arn,
                        principal_id=ic_user.user_id,
                    )
                await session.delete(subscription)
                await session.flush()

            # Step 2: remove from Identity Center
            ic_client.delete_user(account.identity_store_id, ic_user.user_id)

            # Step 3: remove local record
            await session.delete(ic_user)
            await session.commit()

            success_count += 1
            results.append(BatchActionResult(user_id=uid, email=user_email, success=True, message="Deleted"))
        except Exception as e:
            await session.rollback()
            failed_count += 1
            results.append(BatchActionResult(user_id=uid, email=user_email, success=False, message=str(e)))

    await log_service.log_operation(
        account_id=account_id,
        operation="batch_delete_user",
        target=f"users:{len(request.user_ids)}",
        status="success" if failed_count == 0 else "failed",
        message=f"批量删除用户: 成功 {success_count}，失败 {failed_count}",
        operator=operator,
    )

    return BatchActionResponse(
        total=len(request.user_ids),
        success_count=success_count,
        failed_count=failed_count,
        results=results,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    account_id: int,
    user_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Get a specific user."""
    query = select(ICUser).options(selectinload(ICUser.subscription)).where(
        ICUser.id == user_id,
        ICUser.aws_account_id == account_id
    )
    user = await session.scalar(query)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    
    return _get_user_response(user)


@router.delete("/{user_id}")
async def delete_user(
    account_id: int,
    user_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Delete a user from Identity Center."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    # Get user from database
    query = select(ICUser).where(
        ICUser.id == user_id,
        ICUser.aws_account_id == account_id
    )
    ic_user = await session.scalar(query)
    
    if not ic_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        # Step 1: 先取消 Kiro 订阅
        sub_query = select(KiroSubscription).where(
            KiroSubscription.aws_account_id == account_id,
            KiroSubscription.principal_id == ic_user.user_id,
        )
        subscription = await session.scalar(sub_query)
        
        if subscription:
            from app.aws import KiroSubscriptionClient
            kiro_client = KiroSubscriptionClient(
                aws_client,
                kiro_region=account.kiro_region,
                sso_region=account.sso_region,
            )
            del_sub_result = kiro_client.delete_assignment(
                instance_arn=account.instance_arn,
                principal_id=ic_user.user_id,
                subscription_type=subscription.subscription_type,
            )
            if not del_sub_result["success"]:
                # 尝试不指定 subscription_type 删除
                kiro_client.delete_assignment(
                    instance_arn=account.instance_arn,
                    principal_id=ic_user.user_id,
                )
            # 删除本地订阅记录
            await session.delete(subscription)
            await session.flush()
        
        # Step 2: 再从 Identity Center 删除用户
        ic_client.delete_user(account.identity_store_id, ic_user.user_id)
        
        # 记录删除用户日志
        user_email = ic_user.email
        log_service = OperationLogService(session)
        operator = current_user.get("username") if current_user else None
        await log_service.log_operation(
            account_id=account_id,
            operation="delete_user",
            target=f"user:{user_email}",
            status="success",
            message=f"成功删除用户: {user_email}（已取消订阅并从 Identity Center 移除）",
            operator=operator,
        )
        
        # 删除本地用户记录
        await session.delete(ic_user)
        await session.commit()
        
        return {"message": f"User {user_id} deleted (subscription cancelled, removed from Identity Center)"}
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete user: {str(e)}"
        )


@router.post("/{user_id}/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    account_id: int,
    user_id: int,
    request: PasswordResetRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Send password reset email to user."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    # Get user
    query = select(ICUser).where(
        ICUser.id == user_id,
        ICUser.aws_account_id == account_id
    )
    ic_user = await session.scalar(query)
    
    if not ic_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        if request.mode == "otp":
            result = ic_client.send_password_reset_otp(ic_user.user_id)
        else:
            result = ic_client.send_password_reset_email(ic_user.user_id)
        
        return PasswordResetResponse(
            success=result["success"],
            message=result["message"]
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send password reset: {str(e)}"
        )


@router.post("/{user_id}/verify-email", response_model=EmailVerificationResponse)
async def verify_email(
    account_id: int,
    user_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Send email verification to user."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    # Get user
    query = select(ICUser).where(
        ICUser.id == user_id,
        ICUser.aws_account_id == account_id
    )
    ic_user = await session.scalar(query)
    
    if not ic_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        result = ic_client.send_email_verification(
            ic_user.user_id,
            account.identity_store_id
        )
        
        return EmailVerificationResponse(
            success=result["success"],
            message=result["message"]
        )
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send verification: {str(e)}"
        )


@router.get("/groups", response_model=GroupListResponse)
async def list_groups(
    account_id: int,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """List all groups in Identity Center."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        groups = ic_client.list_groups(account.identity_store_id)
        
        return GroupListResponse(groups=groups)
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list groups: {str(e)}"
        )


@router.post("/{user_id}/add-to-group", response_model=AddUserToGroupResponse)
async def add_user_to_group(
    account_id: int,
    user_id: int,
    request: AddUserToGroupRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user)
):
    """Add user to a group."""
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    
    if not account:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AWS account {account_id} not found"
        )
    
    # Get user
    query = select(ICUser).where(
        ICUser.id == user_id,
        ICUser.aws_account_id == account_id
    )
    ic_user = await session.scalar(query)
    
    if not ic_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found"
        )
    
    access_key, secret_key = account_service.decrypt_credentials(account)
    
    try:
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        
        # Get group ID
        group_id = ic_client.get_group_id_by_name(
            account.identity_store_id,
            request.group_name
        )
        
        if not group_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Group '{request.group_name}' not found"
            )
        
        membership_id = ic_client.add_user_to_group(
            account.identity_store_id,
            ic_user.user_id,
            group_id
        )
        
        return AddUserToGroupResponse(
            success=True,
            membership_id=membership_id,
            message=f"User added to group {request.group_name}"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to add user to group: {str(e)}"
        )
