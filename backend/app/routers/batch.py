"""批量操作 API 路由 — 支持 SSE 实时进度."""
import csv
import io
import json
import asyncio
from typing import List, AsyncGenerator
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_session, async_session_maker
from app.models import AWSAccount, ICUser, KiroSubscription
from app.services import AccountService
from app.services.log_service import OperationLogService
from app.aws import AWSClient, IdentityCenterClient, KiroSubscriptionClient
from app.middleware import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/accounts/{account_id}/batch", tags=["Batch"])


class BatchUserItem(BaseModel):
    email: str
    given_name: str | None = None
    family_name: str | None = None
    display_name: str | None = None
    user_name: str | None = None
    subscription_type: str = "Q_DEVELOPER_STANDALONE_PRO"


class BatchCreateRequest(BaseModel):
    users: List[BatchUserItem]
    send_password_reset: bool = True


class BatchActionStreamRequest(BaseModel):
    """批量删除 / 重置密码的请求（按本地用户 ID）."""
    user_ids: List[int]
    mode: str = "email"  # 仅 reset-password 使用: email | otp
    concurrency: int = 5  # 同一时刻最多并发的 AWS 调用数（1-20）


class BatchChangePlanStreamRequest(BaseModel):
    """批量变更套餐请求（按本地用户 ID）."""
    user_ids: List[int]
    subscription_type: str = "Q_DEVELOPER_STANDALONE_PRO"
    concurrency: int = 5


def _sse(data: dict) -> str:
    """格式化为 SSE 数据帧."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _process_batch_stream(
    account_id: int,
    users: List[BatchUserItem],
    send_password_reset: bool,
    operator: str,
) -> AsyncGenerator[str, None]:
    """逐个处理用户，通过 SSE 返回实时进度."""
    
    def _event(data: dict) -> str:
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    
    total = len(users)
    success_count = 0
    failed_count = 0
    
    yield _event({"type": "start", "total": total, "message": f"开始处理 {total} 个用户..."})
    
    async with async_session_maker() as session:
        account_service = AccountService(session)
        account = await account_service.get_account(account_id)
        if not account or account.status != "active":
            yield _event({"type": "error", "message": "账号不存在或未验证"})
            yield _event({"type": "done", "success_count": 0, "failed_count": total})
            return
        
        access_key, secret_key = account_service.decrypt_credentials(account)
        aws_client = AWSClient(access_key, secret_key, account.sso_region)
        ic_client = IdentityCenterClient(aws_client, account.sso_region)
        kiro_client = KiroSubscriptionClient(
            aws_client, kiro_region=account.kiro_region, sso_region=account.sso_region
        )
        log_service = OperationLogService(session)
        
        for i, item in enumerate(users):
            progress = i + 1
            try:
                # Step 1: 检查用户是否已存在
                existing_id = ic_client.find_user_by_email(account.identity_store_id, item.email)
                if existing_id:
                    yield _event({
                        "type": "progress", "current": progress, "total": total,
                        "email": item.email, "step": "skip",
                        "message": f"[{progress}/{total}] {item.email} — 用户已存在",
                    })
                    success_count += 1
                    continue
                
                # Step 2: 创建 IC 用户
                yield _event({
                    "type": "progress", "current": progress, "total": total,
                    "email": item.email, "step": "creating",
                    "message": f"[{progress}/{total}] {item.email} — 创建 Identity Center 用户...",
                })
                
                given_name = item.given_name or item.email.split("@")[0]
                family_name = item.family_name or "Mr"
                display_name = item.display_name or f"{given_name} {family_name}"
                username = item.user_name or (item.given_name or item.email.split("@")[0])
                
                try:
                    user_id = ic_client.create_user(
                        identity_store_id=account.identity_store_id,
                        username=username, display_name=display_name,
                        given_name=given_name, family_name=family_name, email=item.email,
                    )
                except Exception as dup_e:
                    if "Duplicate" in str(dup_e) or "Conflict" in str(dup_e) or "duplicate" in str(dup_e):
                        username = item.email
                        user_id = ic_client.create_user(
                            identity_store_id=account.identity_store_id,
                            username=username, display_name=display_name,
                            given_name=given_name, family_name=family_name, email=item.email,
                        )
                    else:
                        raise dup_e
                
                yield _event({
                    "type": "progress", "current": progress, "total": total,
                    "email": item.email, "step": "created",
                    "message": f"[{progress}/{total}] {item.email} — ✅ IC 用户已创建",
                })
                
                # 保存到数据库
                ic_user = ICUser(
                    aws_account_id=account_id, user_id=user_id, user_name=username,
                    display_name=display_name, email=item.email,
                    given_name=given_name, family_name=family_name,
                    status="enabled", pending_subscription_type=None, email_verified=False,
                )
                session.add(ic_user)
                await session.commit()
                await session.refresh(ic_user)
                
                # Step 3: 发送邮件
                if send_password_reset:
                    ic_client.send_password_reset_email(user_id)
                    # 密码重置邮件已包含邮箱激活，无需单独发送验证邮件
                    yield _event({
                        "type": "progress", "current": progress, "total": total,
                        "email": item.email, "step": "email_sent",
                        "message": f"[{progress}/{total}] {item.email} — ✅ 邮件已发送",
                    })
                
                # Step 4: 分配订阅
                yield _event({
                    "type": "progress", "current": progress, "total": total,
                    "email": item.email, "step": "subscribing",
                    "message": f"[{progress}/{total}] {item.email} — 分配 Kiro 订阅...",
                })
                
                sub_result = kiro_client.create_assignment(
                    instance_arn=account.instance_arn,
                    principal_id=user_id,
                    subscription_type=item.subscription_type,
                )
                
                if sub_result["success"]:
                    new_sub = KiroSubscription(
                        aws_account_id=account_id, user_id=ic_user.id,
                        principal_id=user_id, subscription_type=sub_result.get("actual_type", item.subscription_type),
                        status="PENDING",
                    )
                    session.add(new_sub)
                    await session.commit()
                    yield _event({
                        "type": "progress", "current": progress, "total": total,
                        "email": item.email, "step": "subscribed",
                        "message": f"[{progress}/{total}] {item.email} — ✅ 订阅已分配",
                    })
                else:
                    ic_user.pending_subscription_type = item.subscription_type
                    await session.commit()
                    yield _event({
                        "type": "progress", "current": progress, "total": total,
                        "email": item.email, "step": "sub_failed",
                        "message": f"[{progress}/{total}] {item.email} — ⚠️ 订阅分配失败，后台将重试",
                    })
                
                await log_service.log_operation(
                    account_id=account_id, operation="batch_create_user",
                    target=f"user:{item.email}", status="success",
                    message=f"批量创建: {item.email}", operator=operator,
                )
                success_count += 1
                
            except Exception as e:
                failed_count += 1
                yield _event({
                    "type": "progress", "current": progress, "total": total,
                    "email": item.email, "step": "error",
                    "message": f"[{progress}/{total}] {item.email} — ❌ 失败: {str(e)[:100]}",
                })
            
            # 小延迟避免 API 限流
            await asyncio.sleep(0.1)
        
        yield _event({
            "type": "done",
            "success_count": success_count,
            "failed_count": failed_count,
            "message": f"完成！成功 {success_count}，失败 {failed_count}",
        })


@router.post("/users/stream")
async def batch_create_users_stream(
    account_id: int,
    request: BatchCreateRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量创建用户（SSE 实时进度）."""
    operator = current_user.get("username", "unknown")
    return StreamingResponse(
        _process_batch_stream(account_id, request.users, request.send_password_reset, operator),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/users/csv/stream")
async def batch_create_users_csv_stream(
    account_id: int,
    file: UploadFile = File(...),
    send_password_reset: bool = Form(True),
    current_user: dict = Depends(get_current_user),
):
    """CSV 批量创建用户（SSE 实时进度）."""
    if not file.filename or not file.filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="请上传 CSV 文件")

    content = await file.read()
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    users = []
    for row in reader:
        email = row.get("email", "").strip()
        if not email:
            continue
        users.append(BatchUserItem(
            email=email,
            given_name=row.get("given_name", "").strip() or None,
            family_name=row.get("family_name", "").strip() or None,
            display_name=row.get("display_name", "").strip() or None,
            user_name=row.get("user_name", "").strip() or None,
            subscription_type=row.get("subscription_type", "").strip() or "Q_DEVELOPER_STANDALONE_PRO",
        ))

    if not users:
        raise HTTPException(status_code=400, detail="CSV 文件中没有有效用户数据")

    operator = current_user.get("username", "unknown")
    return StreamingResponse(
        _process_batch_stream(account_id, users, send_password_reset, operator),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================
# 批量删除 / 变更套餐 / 重置密码 — SSE 实时进度
# ============================================================

async def _setup_account_clients(session: AsyncSession, account_id: int):
    """加载账号并构建 AWS 客户端。返回 (account, ic_client, kiro_client, log_service)。

    若账号不存在或未验证，account 为 None。
    """
    account_service = AccountService(session)
    account = await account_service.get_account(account_id)
    if not account or account.status != "active":
        return None, None, None, None

    access_key, secret_key = account_service.decrypt_credentials(account)
    aws_client = AWSClient(access_key, secret_key, account.sso_region)
    ic_client = IdentityCenterClient(aws_client, account.sso_region)
    kiro_client = KiroSubscriptionClient(
        aws_client, kiro_region=account.kiro_region, sso_region=account.sso_region
    )
    log_service = OperationLogService(session)
    return account, ic_client, kiro_client, log_service


# 默认并发上限：同一时刻最多同时发起的 sigv4_post（AWS 调用）数量
BATCH_CONCURRENCY = 5


async def _run_aws_concurrently(items, make_call, concurrency: int = BATCH_CONCURRENCY):
    """对每个 item 在线程池中执行阻塞的 make_call(item)，最多 concurrency 个并发。

    `sigv4_post` 基于同步阻塞的 requests，这里用 asyncio.to_thread 把它丢到线程池，
    再用 Semaphore 限制并发数。按**完成顺序**产出 (item, result, error)，
    以便调用方串行处理数据库写入与 SSE 进度。
    """
    if not items:
        return
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _worker(item):
        async with sem:
            try:
                return item, await asyncio.to_thread(make_call, item), None
            except Exception as e:  # noqa: BLE001
                return item, None, e

    tasks = [asyncio.create_task(_worker(it)) for it in items]
    for fut in asyncio.as_completed(tasks):
        yield await fut


async def _process_delete_stream(
    account_id: int, user_ids: List[int], operator: str,
    concurrency: int = BATCH_CONCURRENCY,
) -> AsyncGenerator[str, None]:
    """并发删除用户（取消订阅 + 移除 Identity Center），SSE 返回进度。

    AWS 调用并发执行（最多 concurrency 个），数据库写入在主协程内串行完成，
    避免并发使用同一个 Session（以及 SQLite 写锁冲突）。
    """
    total = len(user_ids)
    success_count = 0
    failed_count = 0
    completed = 0
    failures: list = []

    yield _sse({"type": "start", "total": total, "message": f"开始删除 {total} 个用户（并发 {concurrency}）..."})

    async with async_session_maker() as session:
        account, ic_client, kiro_client, log_service = await _setup_account_clients(session, account_id)
        if not account:
            yield _sse({"type": "error", "message": "账号不存在或未验证"})
            yield _sse({"type": "done", "success_count": 0, "failed_count": total})
            return

        instance_arn = account.instance_arn
        identity_store_id = account.identity_store_id

        result = await session.execute(
            select(ICUser).where(ICUser.aws_account_id == account_id, ICUser.id.in_(user_ids))
        )
        users_map = {u.id: u for u in result.scalars().all()}

        principals = [u.user_id for u in users_map.values()]
        sub_by_principal = {}
        if principals:
            sub_rows = await session.execute(
                select(KiroSubscription).where(
                    KiroSubscription.aws_account_id == account_id,
                    KiroSubscription.principal_id.in_(principals),
                )
            )
            sub_by_principal = {s.principal_id: s for s in sub_rows.scalars().all()}

        # 先即时报告「未找到」的用户，其余进入并发队列
        items = []
        for uid in user_ids:
            u = users_map.get(uid)
            if not u:
                failed_count += 1
                completed += 1
                failures.append({"email": f"user_id:{uid}", "reason": "用户未找到"})
                yield _sse({
                    "type": "progress", "current": completed, "total": total, "step": "error",
                    "message": f"[{completed}/{total}] 用户 ID {uid} — ❌ 未找到",
                })
                continue
            sub = sub_by_principal.get(u.user_id)
            items.append({
                "uid": uid, "email": u.email, "principal_id": u.user_id,
                "sub_type": sub.subscription_type if sub else None,
                "has_sub": sub is not None,
            })

        def _aws_delete(item):
            """纯 AWS 操作（在线程池中并发执行），不触碰数据库."""
            if item["has_sub"]:
                r = kiro_client.delete_assignment(
                    instance_arn=instance_arn, principal_id=item["principal_id"],
                    subscription_type=item["sub_type"],
                )
                if not r["success"]:
                    kiro_client.delete_assignment(
                        instance_arn=instance_arn, principal_id=item["principal_id"],
                    )
            ic_client.delete_user(identity_store_id, item["principal_id"])
            return True

        async for item, _res, err in _run_aws_concurrently(items, _aws_delete, concurrency):
            completed += 1
            email = item["email"]
            if err is not None:
                failed_count += 1
                failures.append({"email": email, "reason": str(err)[:200]})
                yield _sse({
                    "type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                    "message": f"[{completed}/{total}] {email} — ❌ 失败: {str(err)[:100]}",
                })
                continue
            # AWS 成功 → 在主协程内串行写库
            try:
                if item["has_sub"]:
                    sub = sub_by_principal.get(item["principal_id"])
                    if sub:
                        await session.delete(sub)
                await session.delete(users_map[item["uid"]])
                await session.commit()
                success_count += 1
                yield _sse({
                    "type": "progress", "current": completed, "total": total, "email": email, "step": "deleted",
                    "message": f"[{completed}/{total}] {email} — ✅ 已删除",
                })
            except Exception as e:
                await session.rollback()
                failed_count += 1
                failures.append({"email": email, "reason": f"AWS已删除但本地清理失败: {str(e)[:160]}"})
                yield _sse({
                    "type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                    "message": f"[{completed}/{total}] {email} — ⚠️ AWS 已删除但本地清理失败: {str(e)[:80]}",
                })

        await log_service.log_operation(
            account_id=account_id, operation="batch_delete_user", target=f"users:{total}",
            status="success" if failed_count == 0 else "failed",
            message=f"批量删除用户: 成功 {success_count}，失败 {failed_count}", operator=operator,
        )

    yield _sse({
        "type": "done", "success_count": success_count, "failed_count": failed_count,
        "failures": failures,
        "message": f"完成！成功 {success_count}，失败 {failed_count}",
    })


async def _process_change_plan_stream(
    account_id: int, user_ids: List[int], subscription_type: str, operator: str,
    concurrency: int = BATCH_CONCURRENCY,
) -> AsyncGenerator[str, None]:
    """并发变更套餐，SSE 返回进度。AWS 调用并发，数据库写入串行。"""
    total = len(user_ids)
    success_count = 0
    failed_count = 0
    completed = 0
    failures: list = []

    yield _sse({"type": "start", "total": total, "message": f"开始变更 {total} 个用户的套餐（并发 {concurrency}）..."})

    async with async_session_maker() as session:
        account, _ic_client, kiro_client, log_service = await _setup_account_clients(session, account_id)
        if not account:
            yield _sse({"type": "error", "message": "账号不存在或未验证"})
            yield _sse({"type": "done", "success_count": 0, "failed_count": total})
            return

        result = await session.execute(
            select(ICUser).where(ICUser.aws_account_id == account_id, ICUser.id.in_(user_ids))
        )
        users_map = {u.id: u for u in result.scalars().all()}

        sub_by_userid = {}
        if users_map:
            sub_rows = await session.execute(
                select(KiroSubscription).where(KiroSubscription.user_id.in_(list(users_map.keys())))
            )
            sub_by_userid = {s.user_id: s for s in sub_rows.scalars().all()}

        items = []
        for uid in user_ids:
            u = users_map.get(uid)
            if not u:
                failed_count += 1
                completed += 1
                failures.append({"email": f"user_id:{uid}", "reason": "用户未找到"})
                yield _sse({"type": "progress", "current": completed, "total": total, "step": "error",
                            "message": f"[{completed}/{total}] 用户 ID {uid} — ❌ 未找到"})
                continue
            sub = sub_by_userid.get(uid)
            if not sub:
                failed_count += 1
                completed += 1
                failures.append({"email": u.email, "reason": "无订阅"})
                yield _sse({"type": "progress", "current": completed, "total": total, "email": u.email, "step": "skip",
                            "message": f"[{completed}/{total}] {u.email} — ⚠️ 无订阅，跳过"})
                continue
            items.append({"uid": uid, "email": u.email, "principal_id": sub.principal_id})

        def _aws_update(item):
            return kiro_client.update_assignment(
                principal_id=item["principal_id"], subscription_type=subscription_type,
            )

        async for item, res, err in _run_aws_concurrently(items, _aws_update, concurrency):
            completed += 1
            email = item["email"]
            if err is not None:
                failed_count += 1
                failures.append({"email": email, "reason": str(err)[:200]})
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                            "message": f"[{completed}/{total}] {email} — ❌ 失败: {str(err)[:100]}"})
                continue
            if res["success"]:
                try:
                    sub = sub_by_userid.get(item["uid"])
                    if sub:
                        sub.subscription_type = subscription_type
                    await session.commit()
                except Exception:
                    await session.rollback()
                success_count += 1
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "updated",
                            "message": f"[{completed}/{total}] {email} — ✅ 已变更"})
            else:
                failed_count += 1
                failures.append({"email": email, "reason": res.get('message', '变更失败')[:200]})
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                            "message": f"[{completed}/{total}] {email} — ❌ {res.get('message', '变更失败')[:100]}"})

        await log_service.log_operation(
            account_id=account_id, operation="batch_change_plan", target=f"users:{total}",
            status="success" if failed_count == 0 else "failed",
            message=f"批量变更套餐: 成功 {success_count}，失败 {failed_count}", operator=operator,
        )

    yield _sse({"type": "done", "success_count": success_count, "failed_count": failed_count,
                "failures": failures,
                "message": f"完成！成功 {success_count}，失败 {failed_count}"})


async def _process_reset_password_stream(
    account_id: int, user_ids: List[int], mode: str, operator: str,
    concurrency: int = BATCH_CONCURRENCY,
) -> AsyncGenerator[str, None]:
    """并发发送密码重置/激活邮件，SSE 返回进度（纯 AWS 调用，无数据库写入）."""
    total = len(user_ids)
    success_count = 0
    failed_count = 0
    completed = 0
    failures: list = []

    yield _sse({"type": "start", "total": total, "message": f"开始向 {total} 个用户发送邮件（并发 {concurrency}）..."})

    async with async_session_maker() as session:
        account, ic_client, _kiro_client, log_service = await _setup_account_clients(session, account_id)
        if not account:
            yield _sse({"type": "error", "message": "账号不存在或未验证"})
            yield _sse({"type": "done", "success_count": 0, "failed_count": total})
            return

        result = await session.execute(
            select(ICUser).where(ICUser.aws_account_id == account_id, ICUser.id.in_(user_ids))
        )
        users_map = {u.id: u for u in result.scalars().all()}

        items = []
        for uid in user_ids:
            u = users_map.get(uid)
            if not u:
                failed_count += 1
                completed += 1
                failures.append({"email": f"user_id:{uid}", "reason": "用户未找到"})
                yield _sse({"type": "progress", "current": completed, "total": total, "step": "error",
                            "message": f"[{completed}/{total}] 用户 ID {uid} — ❌ 未找到"})
                continue
            items.append({"uid": uid, "email": u.email, "principal_id": u.user_id})

        def _aws_send(item):
            if mode == "otp":
                return ic_client.send_password_reset_otp(item["principal_id"])
            return ic_client.send_password_reset_email(item["principal_id"])

        async for item, res, err in _run_aws_concurrently(items, _aws_send, concurrency):
            completed += 1
            email = item["email"]
            if err is not None:
                failed_count += 1
                failures.append({"email": email, "reason": str(err)[:200]})
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                            "message": f"[{completed}/{total}] {email} — ❌ 失败: {str(err)[:100]}"})
            elif res["success"]:
                success_count += 1
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "sent",
                            "message": f"[{completed}/{total}] {email} — ✅ 邮件已发送"})
            else:
                failed_count += 1
                failures.append({"email": email, "reason": res.get('message', '发送失败')[:200]})
                yield _sse({"type": "progress", "current": completed, "total": total, "email": email, "step": "error",
                            "message": f"[{completed}/{total}] {email} — ❌ {res.get('message', '发送失败')[:100]}"})

        await log_service.log_operation(
            account_id=account_id, operation="batch_reset_password", target=f"users:{total}",
            status="success" if failed_count == 0 else "failed",
            message=f"批量重置密码: 成功 {success_count}，失败 {failed_count}", operator=operator,
        )

    yield _sse({"type": "done", "success_count": success_count, "failed_count": failed_count,
                "failures": failures,
                "message": f"完成！成功 {success_count}，失败 {failed_count}"})


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/delete/stream")
async def batch_delete_stream(
    account_id: int,
    request: BatchActionStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量删除用户（SSE 实时进度）."""
    operator = current_user.get("username", "unknown")
    concurrency = max(1, min(request.concurrency or 5, 20))
    return StreamingResponse(
        _process_delete_stream(account_id, request.user_ids, operator, concurrency),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/change-plan/stream")
async def batch_change_plan_stream(
    account_id: int,
    request: BatchChangePlanStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量变更套餐（SSE 实时进度）."""
    operator = current_user.get("username", "unknown")
    concurrency = max(1, min(request.concurrency or 5, 20))
    return StreamingResponse(
        _process_change_plan_stream(account_id, request.user_ids, request.subscription_type, operator, concurrency),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/reset-password/stream")
async def batch_reset_password_stream(
    account_id: int,
    request: BatchActionStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    """批量发送密码重置/激活邮件（SSE 实时进度）."""
    operator = current_user.get("username", "unknown")
    concurrency = max(1, min(request.concurrency or 5, 20))
    return StreamingResponse(
        _process_reset_password_stream(account_id, request.user_ids, request.mode, operator, concurrency),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

