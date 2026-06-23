"""独立的定时任务调度器进程。

本文件实现了一个独立于 FastAPI API 服务器的定时任务调度器。
它每分钟检查一次数据库中是否有到期需要执行的定时分析任务，
并使用 asyncio.Semaphore 控制并发执行数量。

启动方式：
    python -m scheduler.main
    或
    uv run tradingagents-scheduler

重要说明：
    - 调度器是独立进程，不会随 API 服务器自动启动
    - 需要与 API 服务器同时运行才能使定时任务正常执行
    - 调度器只在 20:00 ~ 次日 8:00 之间检查任务（避开交易时段）
    - 非交易日（周末、节假日）不执行任务
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

# 加载 .env 文件中的环境变量（如 API Key、数据库路径等）
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════════
# 日志配置
# ═══════════════════════════════════════════════════════════════════════════════
# 日志级别可通过环境变量 LOG_LEVEL 控制，默认 INFO
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _log(msg: str):
    """便捷的日志输出函数"""
    logger.info(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# 并发控制
# ═══════════════════════════════════════════════════════════════════════════════
# 最大同时执行的定时任务数量，可通过环境变量 SCHEDULER_CONCURRENCY 配置
# 设为 0 表示不限制并发
SCHEDULER_CONCURRENCY = int(os.getenv("SCHEDULER_CONCURRENCY", "3"))

# 信号量，用于限制并发执行的任务数量
_semaphore: Optional[asyncio.Semaphore] = None
# 线程池执行器，用于运行阻塞的同步代码（如数据库操作、数据采集）
_executor: Optional[ThreadPoolExecutor] = None

# 保存所有后台任务的引用，防止被 Python 垃圾回收机制意外回收
# 如果不保存引用，asyncio.Task 可能在执行完成前被回收
_background_tasks: set = set()


def _create_tracked_task(coro, *, label: str = "Background task") -> asyncio.Task:
    """创建一个被追踪的 asyncio 任务。

    将任务添加到 _background_tasks 集合中以保持引用，
    任务完成时自动从集合中移除，并记录失败信息。

    Args:
        coro: 协程对象
        label: 任务标签，用于日志标识

    Returns:
        asyncio.Task 对象
    """
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task):
        """任务完成回调：从追踪集合中移除，并记录异常"""
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception():
            logger.error("%s failed: %s", label, t.exception())

    task.add_done_callback(_on_done)
    return task


# ═══════════════════════════════════════════════════════════════════════════════
# 从 API 和 tradingagents 模块导入依赖
# ═══════════════════════════════════════════════════════════════════════════════
# 数据库模型和工具
from api.database import (
    ScheduledAnalysisDB,  # 定时任务数据库表模型
    ReportDB,             # 研报数据库表模型
    UserDB,               # 用户数据库表模型
    init_db,              # 初始化数据库（创建表）
    get_db_ctx,           # 获取数据库会话上下文管理器
)
from api.job_store import get_job_store as _new_job_store
from api.services import (
    auth_service,         # 认证服务（获取用户配置、解密密钥等）
    report_service,       # 研报服务（恢复卡住的报告等）
    scheduled_service,    # 定时任务服务（查询待执行任务、标记执行结果等）
)

# 从 API 主模块导入关键函数（调度器复用 API 的分析执行逻辑）
from api.main import (
    _build_imported_user_context,      # 构建用户持仓上下文
    _build_scheduled_analyze_request,  # 构建定时分析请求
    _resolve_scheduled_trade_date,     # 解析交易日期（处理非交易日顺延）
    _run_job,                          # 执行分析任务的核心函数
    _set_job,                          # 设置任务状态
    _get_job,                          # 获取任务状态
    _emit_job_event,                   # 发送任务事件（SSE）
    get_job_store,                     # 获取任务存储实例
    _get_reverse_stock_map_cached_only,  # 股票代码→名称映射缓存
)

# 设置定时任务上下文标记（让数据采集层知道当前是定时任务触发的分析）
from tradingagents.dataflows.providers.cn_akshare_provider import set_scheduled_task_context


# ═══════════════════════════════════════════════════════════════════════════════
# 基于信号量的并发槽位管理
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def _concurrency_slot(job_id: str, symbol: str):
    """获取/释放一个并发执行槽位。

    使用 asyncio.Semaphore 限制同时执行的任务数量。
    当并发数达到上限时，新任务会等待直到有槽位释放。

    Args:
        job_id: 任务 ID
        symbol: 股票代码
    """
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(SCHEDULER_CONCURRENCY)

    # 如果并发限制设为 0，表示不限制并发，直接放行
    if SCHEDULER_CONCURRENCY <= 0:
        yield
        return

    _log(f"[Scheduler] Waiting for slot job={job_id} symbol={symbol}")
    await _semaphore.acquire()
    try:
        _log(f"[Scheduler] Acquired slot job={job_id} symbol={symbol}")
        yield
    finally:
        _semaphore.release()
        _log(f"[Scheduler] Released slot job={job_id} symbol={symbol}")


# ═══════════════════════════════════════════════════════════════════════════════
# 通知发送
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_scheduled_report_notifications(
    user_id: str, report_id: str, symbol: str
) -> None:
    """发送定时分析报告的通知（邮件 + 企业微信 + 钉钉）。

    分析完成后，根据用户的配置，通过邮件、企业微信 Webhook 和/或钉钉 Webhook
    发送报告通知。

    Args:
        user_id: 用户 ID
        report_id: 报告 ID
        symbol: 股票代码
    """
    try:
        # 延迟导入，避免循环依赖
        from api.services.email_report_service import send_report_email_with_retry
        from api.services.wecom_notification_service import send_report_message_with_retry
        from api.services.dingtalk_notification_service import send_report_message_with_retry as send_dingtalk_report_message_with_retry

        def _load_notification_targets():
            """从数据库加载通知目标配置（在同步线程中执行）"""
            email_user = None          # 邮件接收用户
            report_to_send = None      # 要发送的报告
            webhook_url = None         # 企业微信 Webhook URL
            wecom_report_enabled = True  # 企业微信通知是否启用
            dingtalk_webhook_url = None  # 钉钉 Webhook URL
            dingtalk_report_enabled = True  # 钉钉通知是否启用

            with get_db_ctx() as db:
                # 查询用户信息
                user = db.query(UserDB).filter(UserDB.id == user_id).first()
                # 查询报告信息
                report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
                # 获取用户的 LLM 配置（包含加密的 Webhook URL）
                user_cfg = auth_service.get_user_llm_config(db, user_id)
                # 解密企业微信 Webhook URL
                webhook_url = auth_service.decrypt_secret(
                    getattr(user_cfg, "wecom_webhook_encrypted", None)
                )
                # 解密钉钉 Webhook URL
                dingtalk_webhook_url = auth_service.decrypt_secret(
                    getattr(user_cfg, "dingtalk_webhook_encrypted", None)
                )
                # 将 ORM 对象从会话中分离，以便在会话关闭后仍可使用
                if report:
                    db.expunge(report)
                    report_to_send = report
                if user:
                    wecom_report_enabled = getattr(user, "wecom_report_enabled", True)
                    dingtalk_report_enabled = getattr(user, "dingtalk_report_enabled", True)
                    # 检查用户是否启用了邮件报告
                    if getattr(user, "email_report_enabled", True):
                        db.expunge(user)
                        email_user = user
            return email_user, report_to_send, webhook_url, wecom_report_enabled, dingtalk_webhook_url, dingtalk_report_enabled

        # 在线程池中加载通知配置（避免阻塞事件循环）
        email_user, report_to_send, webhook_url, wecom_report_enabled, dingtalk_webhook_url, dingtalk_report_enabled = (
            await asyncio.to_thread(_load_notification_targets)
        )

        # 发送邮件通知（异步后台任务）
        if email_user and report_to_send:
            _log(f"[Scheduler] Sending email report for {symbol} to {email_user.email}")
            std_symbol = symbol.strip().upper()
            code_to_name = _get_reverse_stock_map_cached_only()
            stock_name = code_to_name.get(std_symbol) or next(
                (name for code, name in code_to_name.items() if code.split(".")[0] == std_symbol.split(".")[0]),
                "",
            )
            _create_tracked_task(
                send_report_email_with_retry(email_user, report_to_send, stock_name=stock_name),
                label=f"Email notification task ({symbol})",
            )

        # 发送企业微信通知（异步后台任务）
        if report_to_send and webhook_url and wecom_report_enabled:
            _log(f"[Scheduler] Sending WeCom report for {symbol}")
            _create_tracked_task(
                send_report_message_with_retry(report_to_send, webhook_url),
                label=f"WeCom notification task ({symbol})",
            )

        # 发送钉钉通知（异步后台任务）
        if report_to_send and dingtalk_webhook_url and dingtalk_report_enabled:
            _log(f"[Scheduler] Sending DingTalk report for {symbol}")
            _create_tracked_task(
                send_dingtalk_report_message_with_retry(report_to_send, dingtalk_webhook_url),
                label=f"DingTalk notification task ({symbol})",
            )
    except Exception as e:
        logger.warning(f"[Scheduler] Notification send failed for {symbol}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 单个定时分析任务的执行
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_scheduled_analysis_once(
    task: dict,
    requested_trade_date: str,
    job_id: str,
    *,
    mark_schedule_run: bool,
) -> None:
    """执行一次定时分析任务。

    完整流程：
    1. 解析交易日期（如果当天是非交易日，顺延到下一个交易日）
    2. 构建分析请求（包含用户持仓上下文、分析周期等）
    3. 获取并发槽位（受 SCHEDULER_CONCURRENCY 限制）
    4. 调用 _run_job 执行实际的 Agent 分析流程
    5. 检查执行结果，标记成功或失败
    6. 发送通知（邮件 + 企业微信）

    Args:
        task: 任务快照字典，包含 id, user_id, symbol, horizon
        requested_trade_date: 请求的交易日期 (YYYY-MM-DD)
        job_id: 本次执行的唯一任务 ID
        mark_schedule_run: 是否标记为正式运行（True=定时触发，False=手动测试）
    """
    task_id = task["id"]
    user_id = task["user_id"]
    symbol = task["symbol"]
    horizon = task.get("horizon") or "short"

    # 解析实际交易日期（如果请求日期是非交易日，自动顺延到下一个交易日）
    actual_trade_date = _resolve_scheduled_trade_date(requested_trade_date)
    _log(f"[Scheduler] {symbol} trade_date={actual_trade_date} (requested={requested_trade_date})")

    # 设置定时任务上下文标记，让数据采集层知道这是定时任务触发的
    set_scheduled_task_context(True)

    def _build_request_sync():
        """构建分析请求（同步函数，在线程池中执行）"""
        with get_db_ctx() as db:
            # 获取用户的持仓上下文（用于传入分析流程）
            scheduled_user_context = task.get("manual_user_context") or _build_imported_user_context(
                db, user_id, symbol
            )
            return _build_scheduled_analyze_request(
                db=db,
                user_id=user_id,
                symbol=symbol,
                horizon=horizon,
                trade_date=actual_trade_date,
                scheduled_user_context=scheduled_user_context,
            )

    def _record_success_sync():
        """记录执行成功（同步函数，在线程池中执行）"""
        with get_db_ctx() as db:
            if mark_schedule_run:
                # 正式运行：标记定时任务为今日已执行
                scheduled_service.mark_run_success(db, task_id, requested_trade_date, job_id)
            else:
                # 手动测试：只记录测试结果，不消耗今日的执行配额
                scheduled_service.record_manual_test_result(db, task_id, "success", report_id=job_id)

    def _record_failure_sync():
        """记录执行失败（同步函数，在线程池中执行）"""
        with get_db_ctx() as db:
            if mark_schedule_run:
                scheduled_service.mark_run_failed(db, task_id, requested_trade_date)
            else:
                scheduled_service.record_manual_test_result(db, task_id, "failed")

    try:
        # 获取并发槽位（如果并发数已满，会在此等待）
        async with _concurrency_slot(job_id, symbol):
            # 在线程池中构建请求（因为涉及数据库查询）
            req = await asyncio.to_thread(_build_request_sync)

            # 执行实际的 Agent 分析流程（调用 API 服务器的 _run_job 函数）
            await _run_job(
                job_id,
                req,
                False,    # is_stream: 是否流式输出
                True,     # is_async: 是否异步执行
                user_id,
                "scheduled" if mark_schedule_run else "scheduled_manual",
            )

        # 检查任务执行结果
        job_state = _get_job(job_id)
        if job_state.get("status") == "failed":
            raise RuntimeError(job_state.get("error") or f"scheduled analysis job {job_id} failed")

        # 记录执行成功
        await asyncio.to_thread(_record_success_sync)
        _log(f"[Scheduler] Completed {symbol}")

        # 发送报告通知
        await _send_scheduled_report_notifications(user_id, job_id, symbol)

    except Exception as e:
        logger.error(f"[Scheduler] Failed {symbol}: {e}\n{traceback.format_exc()}")
        try:
            # 记录执行失败
            await asyncio.to_thread(_record_failure_sync)
        except Exception as db_exc:
            logger.error(f"[Scheduler] Could not record failure: {db_exc}")


async def _run_scheduled_job(task: dict, trade_date: str):
    """执行单个定时分析任务的入口函数。

    创建唯一的 job_id，调用 _run_scheduled_analysis_once 执行分析，
    完成后清理 job_store 中的临时数据。

    Args:
        task: 任务快照字典（使用普通 dict 而非 ORM 对象，
              避免在异步操作中出现 DetachedInstanceError）
        trade_date: 交易日期 (YYYY-MM-DD)
    """
    user_id = task["user_id"]
    symbol = task["symbol"]

    _log(f"[Scheduler] Running {symbol} for user={user_id}")
    # 生成唯一的任务 ID
    job_id = uuid4().hex
    try:
        await _run_scheduled_analysis_once(
            task,
            trade_date,
            job_id,
            mark_schedule_run=True,
        )
    finally:
        # 无论成功失败，都清理 job_store 中的临时数据
        get_job_store().delete_job(job_id)


# ═══════════════════════════════════════════════════════════════════════════════
# 调度器主循环
# ═══════════════════════════════════════════════════════════════════════════════

async def _scheduler_loop():
    """调度器主循环：每分钟检查一次是否有到期的定时任务需要执行。

    执行条件：
    1. 当天是交易日（排除周末和节假日）
    2. 当前时间在 20:00 ~ 次日 8:00 之间（避开交易时段）
    3. 任务的 trigger_time <= 当前时间
    4. 任务今天还没有执行过

    执行流程：
    1. 查询满足条件的待执行任务
    2. 将任务状态标记为 "running"（防止重复触发）
    3. 逐个启动异步执行（每个任务间隔 1 秒，避免瞬间压力）
    """
    # 延迟导入交易日历模块
    from tradingagents.dataflows.trade_calendar import is_cn_trading_day
    from zoneinfo import ZoneInfo

    _log("[Scheduler] Loop started.")

    while True:
        # 每 60 秒检查一次
        await asyncio.sleep(60)

        try:
            # 获取当前北京时间
            now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
            today = now.strftime("%Y-%m-%d")
            current_hhmm = now.strftime("%H:%M")

            # 条件 1：检查是否为交易日（非交易日跳过）
            if not is_cn_trading_day(today):
                continue

            # 条件 2：检查时间窗口（8:00 ~ 20:00 之间跳过）
            # 设计意图：定时任务应在收盘后（20:00）或开盘前（8:00）执行
            # 避免在交易时段内执行耗时的分析任务，影响实时行情数据采集
            time_val = now.hour * 60 + now.minute
            if 8 * 60 < time_val < 20 * 60:
                continue

            def _claim_pending_tasks():
                """查询并认领待执行的任务（同步函数，在线程池中执行）。

                使用"先标记后执行"的策略防止重复触发：
                1. 查询满足条件的任务
                2. 立即将状态标记为 "running"
                3. 提交事务
                4. 返回任务快照

                这样即使多个调度器实例同时运行（虽然不应该），
                也不会重复执行同一个任务。
                """
                with get_db_ctx() as db:
                    # 查询满足条件的任务：激活状态 + 今天未执行 + 触发时间已到
                    tasks = scheduled_service.get_pending_tasks(db, today, current_hhmm)
                    if not tasks:
                        return []

                    # 将任务状态标记为 "running"，防止下次循环重复触发
                    for task in tasks:
                        task.last_run_date = today
                        task.last_run_status = "running"
                    db.commit()

                    # 返回任务快照（普通 dict），避免 ORM 对象在异步操作中的问题
                    return [
                        {
                            "id": task.id,
                            "user_id": task.user_id,
                            "symbol": task.symbol,
                            "horizon": task.horizon,
                        }
                        for task in tasks
                    ]

            # 在线程池中执行数据库查询（避免阻塞事件循环）
            task_snapshots = await asyncio.to_thread(_claim_pending_tasks)
            if not task_snapshots:
                continue

            # 逐个启动任务执行（间隔 1 秒，避免瞬间并发压力过大）
            _log(f"[Scheduler] Launching {len(task_snapshots)} tasks (staggered)")
            for i, snap in enumerate(task_snapshots):
                if i > 0:
                    await asyncio.sleep(1)  # 任务间间隔 1 秒
                _create_tracked_task(_run_scheduled_job(snap, today))

        except Exception as e:
            logger.error(f"[Scheduler] Error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 卡住任务恢复
# ═══════════════════════════════════════════════════════════════════════════════

def _recover_stale_tasks():
    """恢复上次运行中卡住的任务。

    当调度器进程崩溃或被强制终止时，某些任务可能停留在 "running" 状态。
    启动时调用此函数，检查这些任务：

    1. 如果任务的报告已成功完成 → 标记为 "success"
    2. 如果任务的报告未完成或不存在 → 标记为 "stale"，等待重新执行

    同时恢复卡住的报告状态。
    """
    with get_db_ctx() as db:
        # 查询所有状态为 "running" 的任务
        stale = (
            db.query(ScheduledAnalysisDB)
            .filter(ScheduledAnalysisDB.last_run_status == "running")
            .all()
        )
        if stale:
            recovered_count = 0   # 已恢复（报告实际已完成）
            reset_count = 0       # 重置（报告未完成，等待重新执行）
            for item in stale:
                # 检查该任务对应的报告是否已完成
                has_report = (
                    item.last_report_id
                    and item.last_run_date
                    and db.query(ReportDB)
                    .filter(
                        ReportDB.id == item.last_report_id,
                        ReportDB.status == "completed",
                        ReportDB.created_at >= item.last_run_date,
                    )
                    .first()
                )
                if has_report:
                    # 报告已完成，标记任务为成功
                    item.last_run_status = "success"
                    recovered_count += 1
                else:
                    # 报告未完成，重置任务状态，允许重新执行
                    item.last_run_status = "stale"
                    item.last_run_date = None
                    reset_count += 1
            db.commit()
            _log(
                f"[Scheduler] Reset {len(stale)} stale 'running' tasks on startup "
                f"(recovered={recovered_count}, reset_to_stale={reset_count})."
            )

        # 同时恢复卡住的报告（状态为 "running" 但实际已超时的报告）
        report_reset = report_service.recover_stale_active_reports(db)
        if report_reset["total"]:
            _log(
                "[Reports] Recovered %s stale active reports on startup (marked failed)."
                % report_reset["total"]
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 启动流程
# ═══════════════════════════════════════════════════════════════════════════════

async def _startup():
    """调度器启动流程。

    按顺序执行以下初始化步骤：
    1. 配置 asyncio 默认线程池大小
    2. 初始化数据库
    3. 创建并发信号量
    4. 恢复卡住的任务
    5. 预加载交易日历
    6. 预加载股票名称映射表
    7. 进入调度器主循环（永不返回，除非被中断）
    """
    global _semaphore, _executor

    # ── 步骤 1：配置 asyncio 默认线程池 ──────────────────────────────────────
    # 每个定时任务的 _run_job 会发起大量 asyncio.to_thread 调用
    # （数据库写入、AKShare 数据采集、LLM 调用等）
    # Python 默认的线程池大小 min(32, cpu_count+4) 太小，会导致任务排队等待
    # 这里根据并发数动态调整：至少 64 个线程，或并发数 * 16
    try:
        loop = asyncio.get_running_loop()
        executor_workers = int(
            os.getenv("ASYNCIO_DEFAULT_EXECUTOR_WORKERS", str(max(64, SCHEDULER_CONCURRENCY * 16)))
        )
        loop.set_default_executor(
            ThreadPoolExecutor(
                max_workers=executor_workers,
                thread_name_prefix="ta-sched-asyncio",
            )
        )
        _log(f"[Scheduler] Default asyncio executor set to {executor_workers} workers.")
    except Exception as exc:
        _log(f"[Scheduler] Could not configure default asyncio executor: {exc}")

    # ── 步骤 2：初始化数据库 ────────────────────────────────────────────────
    init_db()
    _log("Database initialized.")

    # ── 步骤 3：创建并发信号量 ──────────────────────────────────────────────
    _semaphore = asyncio.Semaphore(SCHEDULER_CONCURRENCY)
    _log(f"[Scheduler] Concurrency limit set to {SCHEDULER_CONCURRENCY}")

    _executor = ThreadPoolExecutor(max_workers=SCHEDULER_CONCURRENCY + 2)

    # ── 步骤 4：恢复卡住的任务 ──────────────────────────────────────────────
    _recover_stale_tasks()

    # ── 步骤 5：预加载交易日历 ──────────────────────────────────────────────
    # 交易日历使用 mini_racer/V8 引擎解析，不是线程安全的
    # 所以在主线程中预加载，避免多线程竞争
    from tradingagents.dataflows.trade_calendar import _load_cn_trade_dates

    _load_cn_trade_dates()
    _log("Trade calendar pre-loaded.")

    # ── 步骤 6：预加载股票名称映射表 ────────────────────────────────────────
    # 用于将股票代码转换为中文名称（如 600519 → 贵州茅台）
    from api.main import _load_cn_stock_map

    await asyncio.to_thread(_load_cn_stock_map)
    _log("Stock map pre-loaded on startup.")

    # ── 步骤 7：启动预测回填后台任务 ────────────────────────────────────────
    # 每天执行一次预测回填（T+1/T+5/T+20），更新预测准确率
    async def _prediction_backfill_loop():
        """预测回填循环：每天执行一次。"""
        from tradingagents.dataflows.trade_calendar import is_cn_trading_day
        from zoneinfo import ZoneInfo

        _log("[PredictionBackfill] Loop started.")
        while True:
            now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
            # 每天 8:05 执行一次
            if now.hour == 8 and now.minute == 5:
                if is_cn_trading_day(now.strftime("%Y-%m-%d")):
                    _log("[PredictionBackfill] Running daily backfill...")
                    try:
                        from api.services.prediction_service import backfill_pending
                        stats = await asyncio.to_thread(backfill_pending, limit=200)
                        _log(f"[PredictionBackfill] Completed: {stats}")
                    except Exception as exc:
                        logger.error(f"[PredictionBackfill] Failed: {exc}")
                # 避免同一分钟内重复执行
                await asyncio.sleep(60)
            else:
                # 每 30 秒检查一次时间
                await asyncio.sleep(30)

    _create_tracked_task(_prediction_backfill_loop(), label="Prediction backfill loop")

    # ── 步骤 8：启动预警检查后台任务 ────────────────────────────────────────
    # 在交易时段内每 15 分钟检查一次持仓预警
    async def _alert_check_loop():
        """预警检查循环：交易时段内每 15 分钟执行一次。"""
        from tradingagents.dataflows.trade_calendar import is_cn_trading_day
        from zoneinfo import ZoneInfo

        _log("[AlertCheck] Loop started.")
        while True:
            now = datetime.now(tz=ZoneInfo("Asia/Shanghai"))
            today = now.strftime("%Y-%m-%d")
            current_hhmm = now.strftime("%H:%M")

            # 仅在交易时段（9:30-11:30, 13:00-15:00）检查
            hour_minute = now.hour * 60 + now.minute
            trading_windows = [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]
            in_trading_hours = any(start <= hour_minute <= end for start, end in trading_windows)

            if in_trading_hours and is_cn_trading_day(today):
                _log("[AlertCheck] Running alert check...")
                try:
                    from api.services.alert_service import check_alerts_for_user
                    # 获取所有有预警的用户
                    with get_db_ctx() as db:
                        from api.database import AlertDB
                        user_ids = {row[0] for row in db.query(AlertDB.user_id).filter(AlertDB.is_active == True).distinct()}
                    for uid in user_ids:
                        try:
                            await asyncio.to_thread(check_alerts_for_user, uid)
                        except Exception as exc:
                            logger.error(f"[AlertCheck] Failed for user {uid}: {exc}")
                    _log(f"[AlertCheck] Completed for {len(user_ids)} users")
                except Exception as exc:
                    logger.error(f"[AlertCheck] Failed: {exc}")
                # 避免重复执行，等 15 分钟
                await asyncio.sleep(15 * 60)
            else:
                # 每 60 秒检查一次时间
                await asyncio.sleep(60)

    _create_tracked_task(_alert_check_loop(), label="Alert check loop")

    # ── 步骤 9：进入调度器主循环 ────────────────────────────────────────────
    # 此函数不会返回，直到进程被中断
    await _scheduler_loop()


def main():
    """调度器进程入口函数。

    启动方式：
        python -m scheduler.main
        或
        uv run tradingagents-scheduler
    """
    _log("[Scheduler] Starting standalone scheduler process ...")
    try:
        asyncio.run(_startup())
    except KeyboardInterrupt:
        _log("[Scheduler] Stopped by user.")


# pyproject.toml 中的脚本入口点需要同步函数
sync_main = main


if __name__ == "__main__":
    main()
