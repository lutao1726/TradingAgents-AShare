"""JobStore 抽象层：管理分析任务状态和 SSE 事件。

核心组成：
1. JobStore Protocol - 定义任务存储的统一接口
2. InMemoryJobStore - 内存版实现，适用于单实例开发/测试
3. get_job_store() - 工厂函数，根据环境变量自动选择实现

设计目标：
- 抽象化任务存储接口，支持内存版和 Redis 版无缝切换
- 线程安全：支持从事件循环线程和工作线程同时调用
- 内存安全：有界队列 + TTL 自动清理，防止内存泄漏
- 事件驱动：通过 asyncio.Queue 实现 SSE 事件推送

环境变量配置：
- TA_JOB_STORE: 存储类型，"redis" 或 "memory"（默认）
- REDIS_URL: Redis 连接 URL（当 TA_JOB_STORE=redis 时必需）
- JOB_EVENT_QUEUE_MAXSIZE: 事件队列最大长度（默认 2000）
- INMEMORY_JOB_TTL: 内存任务 TTL 秒数（默认 600，即 10 分钟）
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 终态状态集合：任务已完成或失败
_TERMINAL_STATUSES = frozenset({"completed", "failed"})

# 终态事件集合：收到这些事件时 SSE 订阅将终止
_TERMINAL_EVENTS = frozenset({"job.completed", "job.failed"})

# 单个任务的事件队列容量上限
# 防止 SSE 订阅者断开但 _run_job_inner 继续产生事件导致内存无限增长
# 达到上限时，最旧的事件将被丢弃
_QUEUE_MAXSIZE = int(os.environ.get("JOB_EVENT_QUEUE_MAXSIZE", "2000"))

# 已完成/失败任务的状态保留时间（秒）
# 轮询客户端和 SSE 订阅者在终态事件后仍需读取状态，因此不能立即删除
_INMEMORY_JOB_TTL = int(os.environ.get("INMEMORY_JOB_TTL", "600"))  # 10 分钟


def _utcnow_iso() -> str:
    """获取当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class JobStore(Protocol):
    """任务存储接口定义（Protocol 模式）。
    
    Protocol 允许静态类型检查时进行鸭子类型匹配，
    任何实现了这些方法的类都可以作为 JobStore 使用。
    """

    def set_job(self, job_id: str, **fields: Any) -> None:
        """创建或更新任务字段（合并语义，线程安全）。"""
        ...

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """获取任务字段，返回字典。不存在时返回空字典。"""
        ...

    def delete_job(self, job_id: str) -> None:
        """删除任务状态和关联的事件队列。"""
        ...

    def emit_event(self, job_id: str, event: str, data: Dict[str, Any]) -> None:
        """推送 SSE 事件（线程安全，支持从事件循环和工作线程调用）。"""
        ...

    def subscribe(self, job_id: str, *, poll_interval: float = 15.0) -> AsyncIterator[Dict[str, Any]]:
        """异步生成器，yield 事件。

        超时处理：
        - 任务仍在运行 → yield ping 事件
        - 任务已完成/失败 → 终止生成器
        
        终态事件：job.completed, job.failed
        """
        ...

    def clear(self) -> None:
        """重置所有状态（启动时调用）。"""
        ...


class InMemoryJobStore:
    """内存版任务存储，使用 threading.Lock 和 asyncio.Queue。
    
    设计特点：
    - 线程安全：所有状态操作都通过 Lock 保护
    - 事件队列：每个任务一个 asyncio.Queue，支持异步订阅
    - 溢出处理：队列满时丢弃最旧事件，防止内存溢出
    - TTL 清理：终态任务在 _INMEMORY_JOB_TTL 后自动清理
    
    适用场景：
    - 单实例部署（开发、测试）
    - 不需要跨进程共享的环境
    """

    def __init__(self) -> None:
        """初始化内存存储。"""
        self._lock = threading.Lock()  # 线程锁，保护共享状态
        self._jobs: Dict[str, Dict[str, Any]] = {}  # 任务状态字典
        self._job_events: Dict[str, asyncio.Queue[Dict[str, Any]]] = {}  # 事件队列字典
        # 捕获事件循环，供工作线程通过 call_soon_threadsafe 调度
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # TTL 清理定时器，支持取消/替换（如任务重跑时）
        self._cleanup_handles: Dict[str, asyncio.TimerHandle] = {}

    def _capture_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """捕获当前运行的事件循环。"""
        loop, _ = self._resolve_loop()
        return loop

    def _resolve_loop(self) -> "tuple[Optional[asyncio.AbstractEventLoop], bool]":
        """获取事件循环。
        
        返回值：
        - loop: 运行中的事件循环，或缓存的循环
        - on_loop: 当前是否在事件循环线程中执行
        
        设计说明：
        - 首次调用时捕获事件循环并缓存
        - 后续调用返回缓存的循环（即使原始循环已关闭）
        - on_loop=True 表示可以直接调用 put_nowait
        - on_loop=False 表示需要通过 call_soon_threadsafe
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return self._loop, False
        # 首次捕获时加锁，确保所有工作线程指向同一个循环
        with self._lock:
            if self._loop is None:
                self._loop = running
        return running, True

    # ── 状态管理 ────────────────────────────────────────────────────────

    def set_job(self, job_id: str, **fields: Any) -> None:
        """创建或更新任务字段。
        
        自动处理 TTL 清理：
        - 进入终态（completed/failed）→ 安排清理定时器
        - 离开终态（如重跑，status="running"）→ 取消清理定时器
        """
        with self._lock:
            if job_id not in self._jobs:
                self._jobs[job_id] = {}
            self._jobs[job_id].update(fields)
            new_status = fields.get("status")
        
        # 根据新状态调整 TTL 清理
        if new_status in _TERMINAL_STATUSES:
            self._schedule_cleanup(job_id)  # 进入终态，安排清理
        elif new_status is not None:
            self._cancel_cleanup(job_id)  # 离开终态，取消清理

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """获取任务字段的副本（线程安全）。"""
        with self._lock:
            return dict(self._jobs.get(job_id, {}))

    def delete_job(self, job_id: str) -> None:
        """删除任务状态、事件队列和清理定时器。"""
        with self._lock:
            self._jobs.pop(job_id, None)
            self._job_events.pop(job_id, None)
            handle = self._cleanup_handles.pop(job_id, None)
        if handle is not None:
            handle.cancel()  # 取消待执行的清理定时器

    def _cancel_cleanup(self, job_id: str) -> None:
        """取消任务的清理定时器。"""
        with self._lock:
            handle = self._cleanup_handles.pop(job_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_cleanup(self, job_id: str) -> None:
        """安排任务的 TTL 清理。
        
        清理逻辑：
        - 在 _INMEMORY_JOB_TTL 秒后删除任务状态和事件队列
        - 支持取消/替换（如任务重跑时）
        
        容错处理：
        - 事件循环不可用时跳过（任务状态会泄漏直到进程重启）
        - 循环关闭时捕获 RuntimeError（进程关闭期间）
        """
        loop, on_loop = self._resolve_loop()
        if loop is None or not loop.is_running():
            # 事件循环不可用（如工作线程在捕获循环之前调用）
            # 最坏情况下任务状态泄漏直到进程重启
            return

        def _do_cleanup() -> None:
            """实际执行清理的回调函数。"""
            with self._lock:
                self._jobs.pop(job_id, None)
                self._job_events.pop(job_id, None)
                self._cleanup_handles.pop(job_id, None)

        with self._lock:
            existing = self._cleanup_handles.pop(job_id, None)
        if existing is not None:
            existing.cancel()  # 取消已存在的清理定时器

        def _arm() -> None:
            """安排清理定时器。"""
            handle = loop.call_later(_INMEMORY_JOB_TTL, _do_cleanup)
            with self._lock:
                self._cleanup_handles[job_id] = handle

        if on_loop:
            _arm()  # 在事件循环线程中直接调用
        else:
            try:
                loop.call_soon_threadsafe(_arm)  # 从工作线程安全调度
            except RuntimeError:
                # 循环已关闭（如进程关闭期间）
                # 不让清理安排失败传播上去并中止触发我们的状态写入
                logger.debug("无法为 %s 安排清理：循环已关闭", job_id)

    # ── 事件队列 ─────────────────────────────────────────────────────

    def _ensure_queue(self, job_id: str) -> asyncio.Queue[Dict[str, Any]]:
        """确保任务的事件队列存在，返回队列实例。"""
        with self._lock:
            q = self._job_events.get(job_id)
            if q is None:
                q = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
                self._job_events[job_id] = q
            return q

    @staticmethod
    def _put_with_overflow(q: "asyncio.Queue[Dict[str, Any]]", payload: Dict[str, Any]) -> None:
        """推送事件，队列满时丢弃最旧事件。
        
        溢出处理策略：
        - 队列满时先 get_nowait() 丢弃最旧事件
        - 然后 put_nowait() 推送新事件
        - 极端竞争情况下丢弃新事件（避免阻塞）
        
        设计目标：
        - 有界队列防止内存无限增长
        - 当 SSE 订阅者断开但 _run_job_inner 继续产生事件时保护系统
        """
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                q.get_nowait()  # 丢弃最旧事件
            except asyncio.QueueEmpty:
                pass
            try:
                q.put_nowait(payload)  # 推送新事件
            except asyncio.QueueFull:
                # 极端竞争：丢弃新事件
                pass

    def emit_event(self, job_id: str, event: str, data: Dict[str, Any]) -> None:
        """线程安全的事件发射器。
        
        调用路径：
        - 事件循环线程 → 直接调用 put_nowait
        - 工作线程 → 通过 call_soon_threadsafe 调度
        
        溢出处理：有界队列，最旧事件被丢弃，防止 SSE 消费者卡住时耗尽内存。
        """
        payload: Dict[str, Any] = {
            "event": event,
            "data": data,
            "timestamp": _utcnow_iso(),
        }
        q = self._ensure_queue(job_id)
        loop, on_loop = self._resolve_loop()

        if on_loop:
            self._put_with_overflow(q, payload)  # 直接推送
        elif loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._put_with_overflow, q, payload)  # 调度推送
            except RuntimeError:
                # 循环已关闭，回退到直接推送
                self._put_with_overflow(q, payload)
        else:
            # 最佳努力回退：直接推送
            # SSE 消费者在下一次 wait_for 循环中会收到
            self._put_with_overflow(q, payload)

    async def subscribe(
        self, job_id: str, *, poll_interval: float = 15.0
    ) -> AsyncIterator[Dict[str, Any]]:
        """异步生成器，yield 指定任务的事件。
        
        超时处理：
        - 任务仍在运行 → yield ping 事件保持连接
        - 任务已完成/失败 → 终止生成器
        
        终态事件（job.completed, job.failed）：yield 后终止
        
        清理逻辑：
        - 生成器退出时（终态事件或客户端断开），删除事件队列释放内存
        - 只删除当前队列实例（新订阅者可能已重新创建）
        """
        self._capture_loop()
        q = self._ensure_queue(job_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=poll_interval)
                    yield event
                    if event["event"] in _TERMINAL_EVENTS:
                        break  # 收到终态事件，终止
                except asyncio.TimeoutError:
                    with self._lock:
                        status = self._jobs.get(job_id, {}).get("status")
                    if status in _TERMINAL_STATUSES:
                        break  # 任务已结束，终止
                    # 任务仍在运行，发送 ping 保持连接
                    yield {
                        "event": "ping",
                        "data": {"timestamp": _utcnow_iso()},
                        "timestamp": _utcnow_iso(),
                    }
        finally:
            with self._lock:
                # 只删除当前队列实例（新订阅者可能已重新创建）
                if self._job_events.get(job_id) is q:
                    self._job_events.pop(job_id, None)

    # ── 生命周期管理 ────────────────────────────────────────────────────

    def clear(self) -> None:
        """清空所有状态（启动时调用）。
        
        清理内容：
        - 所有任务状态
        - 所有事件队列
        - 所有待执行的清理定时器
        """
        with self._lock:
            self._jobs.clear()
            self._job_events.clear()
            handles = list(self._cleanup_handles.values())
            self._cleanup_handles.clear()
        for handle in handles:
            handle.cancel()  # 取消所有定时器


def get_job_store() -> JobStore:
    """工厂函数：根据环境变量返回 JobStore 实现。
    
    选择逻辑：
    - REDIS_URL 环境变量存在 → 尝试创建 RedisJobStore
    - Redis 导入失败 → 回退到 InMemoryJobStore
    - REDIS_URL 不存在 → 使用 InMemoryJobStore
    
    环境变量：
    - REDIS_URL: Redis 连接 URL，如 redis://localhost:6379/0
    
    返回值：
    - RedisJobStore 或 InMemoryJobStore 实例
    """
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            from api.job_store_redis import RedisJobStore
            return RedisJobStore(redis_url)
        except ImportError:
            logger.warning(
                "REDIS_URL 已设置但 api.job_store_redis 模块不可用；"
                "回退到 InMemoryJobStore"
            )
    return InMemoryJobStore()
