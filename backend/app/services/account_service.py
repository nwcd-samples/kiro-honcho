"""AWS Account management service."""
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from app.models import AWSAccount, ICUser, KiroSubscription
from app.aws import AWSClient, IdentityCenterClient
from app.utils import get_encryption_service
from app.services.log_service import OperationLogService


class AccountService:
    """Service for managing AWS accounts."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
        self.encryption = get_encryption_service()
    
    async def get_dashboard_stats(self, account_id: Optional[int] = None) -> Dict[str, Any]:
        """获取 Dashboard 统计数据."""
        from app.models import ICUser, KiroSubscription, OperationLog
        
        if account_id:
            user_count = await self.session.scalar(
                select(func.count()).select_from(ICUser).where(ICUser.aws_account_id == account_id)
            )
            # 有活跃订阅的用户数（必须关联到 ICUser，且状态为 ACTIVE 或 PENDING）
            subscribed_user_count = await self.session.scalar(
                select(func.count()).select_from(KiroSubscription).where(
                    KiroSubscription.aws_account_id == account_id,
                    KiroSubscription.user_id.isnot(None),
                    func.upper(KiroSubscription.status).in_(["ACTIVE", "PENDING"])
                )
            )
            # 活跃订阅数（关联到 ICUser，状态为 ACTIVE）
            active_sub_count = await self.session.scalar(
                select(func.count()).select_from(KiroSubscription).where(
                    KiroSubscription.aws_account_id == account_id,
                    KiroSubscription.user_id.isnot(None),
                    func.upper(KiroSubscription.status) == "ACTIVE"
                )
            )
        else:
            user_count = await self.session.scalar(
                select(func.count()).select_from(ICUser)
            )
            subscribed_user_count = await self.session.scalar(
                select(func.count()).select_from(KiroSubscription).where(
                    KiroSubscription.user_id.isnot(None),
                    func.upper(KiroSubscription.status).in_(["ACTIVE", "PENDING"])
                )
            )
            active_sub_count = await self.session.scalar(
                select(func.count()).select_from(KiroSubscription).where(
                    KiroSubscription.user_id.isnot(None),
                    func.upper(KiroSubscription.status) == "ACTIVE"
                )
            )
        
        account_count = await self.session.scalar(
            select(func.count()).select_from(AWSAccount)
        )
        active_account_count = await self.session.scalar(
            select(func.count()).select_from(AWSAccount).where(AWSAccount.status == "active")
        )
        
        return {
            "total_users": user_count or 0,
            "subscribed_users": subscribed_user_count or 0,
            "active_subscriptions": active_sub_count or 0,
            "total_accounts": account_count or 0,
            "active_accounts": active_account_count or 0,
        }
    
    async def list_accounts(
        self,
        skip: int = 0,
        limit: int = 100
    ) -> tuple[int, List[AWSAccount]]:
        """List all AWS accounts with pagination."""
        # Count total
        count_query = select(func.count()).select_from(AWSAccount)
        total = await self.session.scalar(count_query)
        
        # Get accounts
        query = select(AWSAccount).offset(skip).limit(limit).order_by(AWSAccount.created_at.desc())
        result = await self.session.execute(query)
        accounts = list(result.scalars().all())
        
        return total, accounts
    
    async def get_account(self, account_id: int) -> Optional[AWSAccount]:
        """Get a single account by ID."""
        query = select(AWSAccount).where(AWSAccount.id == account_id)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def create_account(
        self,
        name: str,
        access_key_id: str,
        secret_access_key: str,
        sso_region: str = "us-east-2",
        kiro_region: str = "us-east-1",
        description: Optional[str] = None,
        operator: Optional[str] = None,
        sync_interval_minutes: int = 0,
        is_default: bool = False,
    ) -> AWSAccount:
        """
        Create a new AWS account.

        Credentials are encrypted before storage.
        """
        # 如果设为默认，先取消其他默认
        if is_default:
            from sqlalchemy import update
            await self.session.execute(
                update(AWSAccount).values(is_default=False)
            )
        
        # Encrypt credentials
        encrypted_key = self.encryption.encrypt(access_key_id)
        encrypted_secret = self.encryption.encrypt(secret_access_key)
        
        account = AWSAccount(
            name=name,
            description=description,
            access_key_id=encrypted_key,
            secret_access_key=encrypted_secret,
            sso_region=sso_region,
            kiro_region=kiro_region,
            status="pending",
            sync_interval_minutes=sync_interval_minutes,
            is_default=is_default,
        )
        
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        
        # 记录创建账号日志
        log_service = OperationLogService(self.session)
        await log_service.log_operation(
            account_id=account.id,
            operation="create_account",
            target=f"account:{name}",
            status="success",
            message=f"成功创建 AWS 账号: {name}",
            operator=operator,
        )
        
        return account
    
    async def update_account(
        self,
        account_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        sso_region: Optional[str] = None,
        kiro_region: Optional[str] = None,
        sync_interval_minutes: Optional[int] = None,
        is_default: Optional[bool] = None,
    ) -> Optional[AWSAccount]:
        """Update an AWS account."""
        account = await self.get_account(account_id)
        if not account:
            return None
        
        if name:
            account.name = name
        if description is not None:
            account.description = description
        if access_key_id:
            account.access_key_id = self.encryption.encrypt(access_key_id)
        if secret_access_key:
            account.secret_access_key = self.encryption.encrypt(secret_access_key)
        if sso_region:
            account.sso_region = sso_region
        if kiro_region:
            account.kiro_region = kiro_region
        if sync_interval_minutes is not None:
            account.sync_interval_minutes = sync_interval_minutes
        if is_default is not None:
            if is_default:
                from sqlalchemy import update
                await self.session.execute(update(AWSAccount).values(is_default=False))
            account.is_default = is_default
        
        account.updated_at = datetime.utcnow()
        await self.session.commit()
        await self.session.refresh(account)
        
        return account
    
    async def delete_account(self, account_id: int, operator: Optional[str] = None) -> bool:
        """Delete an AWS account."""
        account = await self.get_account(account_id)
        if not account:
            return False
        
        account_name = account.name
        await self.session.delete(account)
        await self.session.commit()
        
        # 记录删除账号日志 (日志本身也会被级联删除，这里只是为了记录操作)
        # 由于账号已删除，日志无法关联到该账号，所以跳过此日志
        
        return True
    
    def decrypt_credentials(self, account: AWSAccount) -> tuple[str, str]:
        """Decrypt and return account credentials."""
        access_key = self.encryption.decrypt(account.access_key_id)
        secret_key = self.encryption.decrypt(account.secret_access_key)
        return access_key, secret_key
    
    async def verify_account(self, account_id: int, operator: Optional[str] = None) -> Dict[str, Any]:
        """
        Verify AWS account credentials and permissions.

        Checks:
        1. Credentials are valid
        2. Can access Identity Center
        3. Can list Kiro subscriptions
        4. Gets instance info
        """
        account = await self.get_account(account_id)
        if not account:
            return {
                "success": False,
                "account_id": account_id,
                "message": "Account not found"
            }
        
        access_key, secret_key = self.decrypt_credentials(account)
        
        try:
            # Create AWS client
            aws_client = AWSClient(access_key, secret_key, account.sso_region)
            ic_client = IdentityCenterClient(aws_client, account.sso_region)
            
            # Check permissions
            permissions = {
                "has_identity_center_access": False,
                "has_kiro_access": False,
                "errors": []
            }
            
            # Test Identity Center access
            instance_info = None
            try:
                instance_info = ic_client.get_instance_info()
                permissions["has_identity_center_access"] = True
                account.instance_arn = instance_info["instance_arn"]
                account.identity_store_id = instance_info["identity_store_id"]
            except Exception as e:
                permissions["errors"].append(f"Identity Center: {str(e)}")
            
            # Test Kiro access (if Identity Center works)
            if instance_info:
                try:
                    from app.aws import KiroSubscriptionClient
                    kiro_client = KiroSubscriptionClient(
                        aws_client,
                        kiro_region=account.kiro_region,
                        sso_region=account.sso_region
                    )
                    result = kiro_client.list_subscriptions(instance_info["instance_arn"])
                    permissions["has_kiro_access"] = result["success"]
                    if not result["success"]:
                        permissions["errors"].append(f"Kiro: {result.get('message', 'Unknown error')}")
                except Exception as e:
                    permissions["errors"].append(f"Kiro: {str(e)}")
            
            account.permissions = permissions
            account.last_verified = datetime.utcnow()
            
            if permissions["has_identity_center_access"]:
                account.status = "active"
            else:
                account.status = "invalid"
            
            await self.session.commit()
            await self.session.refresh(account)
            
            # 记录验证结果日志
            log_service = OperationLogService(self.session)
            await log_service.log_operation(
                account_id=account_id,
                operation="verify_account",
                target=f"account:{account.name}",
                status="success" if account.status == "active" else "failed",
                message=f"账号验证{'成功' if account.status == 'active' else '失败'}: {account.name}",
                details=permissions,
                operator=operator,
            )
            
            return {
                "success": account.status == "active",
                "account_id": account_id,
                "status": account.status,
                "instance_arn": account.instance_arn,
                "identity_store_id": account.identity_store_id,
                "permissions": permissions,
                "message": "Account verified successfully" if account.status == "active" else "Verification failed"
            }
            
        except Exception as e:
            account.status = "invalid"
            account.last_verified = datetime.utcnow()
            await self.session.commit()
            
            # 记录验证失败日志
            log_service = OperationLogService(self.session)
            await log_service.log_operation(
                account_id=account_id,
                operation="verify_account",
                target=f"account:{account.name}",
                status="failed",
                message=f"账号验证失败: {str(e)}",
                operator=operator,
            )
            
            return {
                "success": False,
                "account_id": account_id,
                "status": "invalid",
                "message": f"Verification failed: {str(e)}"
            }
    
    async def sync_account_data(self, account_id: int, operator: Optional[str] = None) -> Dict[str, Any]:
        """Sync users and subscriptions from AWS account."""
        account = await self.get_account(account_id)
        if not account:
            return {"success": False, "message": "Account not found"}
        
        if account.status != "active":
            return {"success": False, "message": "Account not verified"}
        
        access_key, secret_key = self.decrypt_credentials(account)
        
        try:
            aws_client = AWSClient(access_key, secret_key, account.sso_region)
            ic_client = IdentityCenterClient(aws_client, account.sso_region)
            
            from app.aws import KiroSubscriptionClient
            kiro_client = KiroSubscriptionClient(
                aws_client,
                kiro_region=account.kiro_region,
                sso_region=account.sso_region
            )
            
            # Sync users - 使用内部 API 获取邮箱验证状态
            identity_store_id = account.identity_store_id
            # has_verification 标记本次数据是否包含可靠的邮箱验证状态。
            # 内部 API 失败时回退到标准 list_users（不含验证状态），
            # 此时必须保留数据库中已有的 email_verified，避免被错误重置。
            try:
                aws_users = ic_client.search_users_with_verification(identity_store_id)
                has_verification = True
            except Exception:
                aws_users = ic_client.list_users(identity_store_id)
                has_verification = False

            # 防御性检查：若未获取到任何用户，可能是 AWS API 异常。
            # 此时直接返回失败，绝不继续执行后续的"清理本地多余用户"逻辑，
            # 否则会误删全部本地用户及其订阅。
            if not aws_users:
                return {
                    "success": False,
                    "message": "未从 AWS 获取到任何用户（API 可能调用失败），已跳过同步以保护本地数据",
                }
            
            synced_users = 0
            for user_data in aws_users:
                # Check if user exists
                query = select(ICUser).where(
                    ICUser.aws_account_id == account_id,
                    ICUser.user_id == user_data["UserId"]
                )
                existing = await self.session.scalar(query)
                
                if existing:
                    # Update
                    existing.user_name = user_data.get("UserName", "")
                    existing.display_name = user_data.get("DisplayName")
                    existing.email = user_data.get("Email", "")
                    existing.given_name = user_data.get("GivenName")
                    existing.family_name = user_data.get("FamilyName")
                    existing.status = user_data.get("Status", "enabled")
                    # 仅在拥有可靠验证数据时更新，否则保留原值
                    if has_verification:
                        existing.email_verified = user_data.get("EmailVerified", False)
                    existing.last_synced = datetime.utcnow()
                else:
                    # Create
                    new_user = ICUser(
                        aws_account_id=account_id,
                        user_id=user_data["UserId"],
                        user_name=user_data.get("UserName", ""),
                        display_name=user_data.get("DisplayName"),
                        email=user_data.get("Email", ""),
                        given_name=user_data.get("GivenName"),
                        family_name=user_data.get("FamilyName"),
                        status=user_data.get("Status", "enabled"),
                        email_verified=user_data.get("EmailVerified", False),
                        last_synced=datetime.utcnow()
                    )
                    self.session.add(new_user)
                
                synced_users += 1
            
            await self.session.commit()
            
            # 清理本地存在但 AWS 已删除的用户
            aws_user_ids = {u["UserId"] for u in aws_users}
            local_users_query = select(ICUser).where(ICUser.aws_account_id == account_id)
            local_result = await self.session.execute(local_users_query)
            local_users = list(local_result.scalars().all())
            
            removed_users = 0
            for local_user in local_users:
                if local_user.user_id not in aws_user_ids:
                    # 同时清理关联的订阅
                    sub_query = select(KiroSubscription).where(
                        KiroSubscription.aws_account_id == account_id,
                        KiroSubscription.principal_id == local_user.user_id,
                    )
                    sub = await self.session.scalar(sub_query)
                    if sub:
                        await self.session.delete(sub)
                    await self.session.delete(local_user)
                    removed_users += 1
            
            if removed_users > 0:
                await self.session.commit()
            
            # Sync subscriptions
            subscriptions_result = kiro_client.list_subscriptions(account.instance_arn)
            synced_subs = 0
            
            if subscriptions_result["success"]:
                for sub_data in subscriptions_result["subscriptions"]:
                    principal_id = sub_data.get("principal", {}).get("user", "")
                    
                    # Find user by principal_id
                    user_query = select(ICUser).where(
                        ICUser.aws_account_id == account_id,
                        ICUser.user_id == principal_id
                    )
                    ic_user = await self.session.scalar(user_query)
                    
                    # Check if subscription exists
                    sub_query = select(KiroSubscription).where(
                        KiroSubscription.aws_account_id == account_id,
                        KiroSubscription.principal_id == principal_id
                    )
                    existing_sub = await self.session.scalar(sub_query)
                    
                    sub_type = sub_data.get("type", {}).get("amazonQ", "")
                    sub_status = sub_data.get("status", "active")
                    
                    # 跳过已取消的订阅
                    if sub_status.upper() in ("CANCELED", "CANCELLED"):
                        # 如果本地有记录，删除它
                        if existing_sub:
                            await self.session.delete(existing_sub)
                        continue
                    
                    if existing_sub:
                        existing_sub.subscription_type = sub_type
                        existing_sub.status = sub_status
                        existing_sub.last_synced = datetime.utcnow()
                    else:
                        new_sub = KiroSubscription(
                            aws_account_id=account_id,
                            user_id=ic_user.id if ic_user else None,
                            principal_id=principal_id,
                            subscription_type=sub_type,
                            status=sub_status,
                            last_synced=datetime.utcnow()
                        )
                        self.session.add(new_sub)
                    
                    synced_subs += 1
                
                await self.session.commit()
            
            # 只记录人工操作的日志，跳过系统自动同步
            if operator and not str(operator).startswith("system:"):
                log_service = OperationLogService(self.session)
                await log_service.log_operation(
                    account_id=account_id,
                    operation="sync_account_data",
                    target=f"account:{account.name}",
                    status="success",
                    message=f"同步完成: {synced_users} 用户, {synced_subs} 订阅",
                    details={"synced_users": synced_users, "synced_subscriptions": synced_subs},
                    operator=operator,
                )
            
            return {
                "success": True,
                "synced_users": synced_users,
                "synced_subscriptions": synced_subs
            }
            
        except Exception as e:
            # 只记录人工操作的失败日志
            if operator and not str(operator).startswith("system:"):
                log_service = OperationLogService(self.session)
                await log_service.log_operation(
                    account_id=account_id,
                    operation="sync_account_data",
                    target=f"account:{account.name}",
                    status="failed",
                    message=f"同步失败: {str(e)}",
                    operator=operator,
                )
            
            return {
                "success": False,
                "message": f"Sync failed: {str(e)}"
            }
