"""Redis 后端的 JobStore 实现。

核心设计：
1. 使用 Redis Hash 存储任务状态（job state）
2. 使用 Redis Pub/Sub 实现实时 SSE 事件推送
3. 支持多 worker API 部署，所有 uvicorn worker 共享任务状态和事件流

使用场景：
- 生产环境部署时，多个 API worker 进程需要共享任务状态
- 需要跨进程的实时事件推送（如分析进度、流式输出）
- 相比内存版 JobStore，提供持久化和分布式能力

启动方式：
    在 api/main.py 中通过环境变量 TA_JOB_STORE=redis 启用
    需要配置 REDIS_URL 环境变量（默认 redis://localhost:6379/0）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict

import redis

logger = logging.getLogger(__name__)

# 任务状态 Hash 的 TTL（秒），可通过环境变量 JOB_STATE_TTL 配置
# 默认 86400 秒 = 24 小时，过期后自动清理
_JOB_STATE_TTL: int = int(os.environ.get("JOB_STATE_TTL", "86400"))

# 终态事件集合：当收到这些事件时，SSE 订阅将终止
_TERMINAL_EVENTS = frozenset({"job.completed", "job.failed"})


def _utcnow_iso() -> str:
    """获取当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _serialize_value(v: Any) -> str:
    """将 Python 值序列化为 Redis Hash 字段的字符串格式。
    
    序列化规则：
    - None → 空字符串 ""
    - dict/list → JSON 字符串
    - 其他类型 → str() 转换
    """
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _deserialize_value(v: str) -> Any:
    """将 Redis Hash 字段的字符串值反序列化为 Python 对象。
    
    反序列化规则：
    - 空字符串 → None
    - 尝试 JSON 解析（支持 dict、list、JSON 编码的标量）
    - JSON 解析失败 → 返回原始字符串
    """
    if v == "":
        return None
    # 优先尝试 JSON 解析（覆盖 dict、list 和 JSON 编码的标量）
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


class RedisJobStore:
    """基于 Redis 的任务存储，使用 Hash 存储状态，Pub/Sub 推送事件。
    
    Key 命名规则（所有 key 都以 *prefix* 为前缀）：
        {prefix}job:{job_id}      -- Redis Hash，存储任务字段
        {prefix}events:{job_id}   -- Pub/Sub 频道，用于 SSE 事件推送
    
    典型使用流程：
        1. 创建任务：store.set_job(job_id, status="running", ticker="600519.SH")
        2. 更新进度：store.emit_event(job_id, "progress", {"step": "analyst", "agent": "market"})
        3. 查询状态：job = store.get_job(job_id)
        4. SSE 订阅：async for event in store.subscribe(job_id): ...
        5. 任务完成：store.set_job(job_id, status="completed", result={...})
    """

    def __init__(self, redis_url: str, prefix: str = "ta:") -> None:
        """初始化 Redis 连接。
        
        Args:
            redis_url: Redis 连接 URL，如 redis://localhost:6379/0
            prefix: Key 前缀，默认 "ta:"，用于隔离不同应用的数据
        """
        self._prefix = prefix
        self._r: redis.Redis = redis.Redis.from_url(
            redis_url, decode_responses=True  # 自动解码为字符串
        )
        # 启动时验证连接
        self._r.ping()
        logger.info("RedisJobStore 已连接到 %s (prefix=%r)", redis_url, prefix)

    # ── Key 辅助方法 ─────────────────────────────────────────────────────

    def _job_key(self, job_id: str) -> str:
        """生成任务状态的 Redis Key。格式：{prefix}job:{job_id}"""
        return f"{self._prefix}job:{job_id}"

    def _channel_key(self, job_id: str) -> str:
        """生成 Pub/Sub 频道的 Redis Key。格式：{prefix}events:{job_id}"""
        return f"{self._prefix}events:{job_id}"

    # ── 状态管理 ────────────────────────────────────────────────────────

    def set_job(self, job_id: str, **fields: Any) -> None:
        """创建或更新任务字段（合并语义）。
        
        特性：
        - 复杂值（dict/list）自动 JSON 序列化
        - None 值存储为空字符串
        - 每次调用都会刷新 TTL
        
        Args:
            job_id: 任务唯一标识
            **fields: 要更新的字段键值对，如 status="running", ticker="600519.SH"
        """
        if not fields:
            return
        key = self._job_key(job_id)
        mapping = {k: _serialize_value(v) for k, v in fields.items()}
        # 使用 pipeline 批量执行，减少网络往返
        pipe = self._r.pipeline()
        pipe.hset(key, mapping=mapping)  # 设置字段
        pipe.expire(key, _JOB_STATE_TTL)  # 刷新 TTL
        pipe.execute()

    def get_job(self, job_id: str) -> Dict[str, Any]:
        """获取任务的所有字段，返回字典。如果任务不存在返回空字典。"""
        raw = self._r.hgetall(self._job_key(job_id))
        if not raw:
            return {}
        return {k: _deserialize_value(v) for k, v in raw.items()}

    def delete_job(self, job_id: str) -> None:
        """删除任务状态 Hash。删除不存在的 key 不会报错。"""
        self._r.delete(self._job_key(job_id))

    # ── 事件 Pub/Sub ───────────────────────────────────────────────────

    def emit_event(self, job_id: str, event: str, data: Dict[str, Any]) -> None:
        """发布 SSE 事件到任务的 Pub/Sub 频道。
        
        Args:
            job_id: 任务 ID
            event: 事件类型，如 "progress", "token", "job.completed"
            data: 事件数据，将被 JSON 序列化
        """
        payload: Dict[str, Any] = {
            "event": event,
            "data": data,
            "timestamp": _utcnow_iso(),
        }
        self._r.publish(
            self._channel_key(job_id), 
            json.dumps(payload, ensure_ascii=False)
        )

    async def subscribe(
        self, job_id: str, *, poll_interval: float = 15.0
    ) -> AsyncIterator[Dict[str, Any]]:
        """异步生成器，yield 指定任务的事件。
        
        实现原理：
        1. 在后台守护线程中运行阻塞式 Redis Pub/Sub 监听器
        2. 通过 asyncio.Queue 桥接线程和异步事件循环
        3. 使用 loop.call_soon_threadsafe 线程安全地传递消息
        
        超时处理：
        - 超时且任务仍在运行 → yield ping 事件（保持连接）
        - 超时且任务已完成/失败 → 终止生成器
        
        Args:
            job_id: 要订阅的任务 ID
            poll_interval: 无事件时的轮询间隔（秒），默认 15 秒
        
        Yields:
            事件字典，包含 event、data、timestamp 字段
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        stop_event = threading.Event()  # 用于通知监听线程停止

        def _listener() -> None:
            """阻塞式监听器，在守护线程中运行。"""
            pubsub = self._r.pubsub()
            try:
                pubsub.subscribe(self._channel_key(job_id))
                while not stop_event.is_set():
                    # 每秒检查一次是否需要停止
                    msg = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg is not None and msg["type"] == "message":
                        try:
                            payload = json.loads(msg["data"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                        # 线程安全地将消息放入异步队列
                        loop.call_soon_threadsafe(queue.put_nowait, payload)
            except Exception:
                logger.debug("Redis pubsub 监听器 %s 退出", job_id, exc_info=True)
            finally:
                try:
                    pubsub.unsubscribe()
                    pubsub.close()
                except Exception:
                    pass

        # 启动守护线程
        thread = threading.Thread(target=_listener, daemon=True)
        thread.start()

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=poll_interval)
                    yield event
                    # 收到终态事件，终止订阅
                    if event["event"] in _TERMINAL_EVENTS:
                        break
                except asyncio.TimeoutError:
                    # 超时：检查任务状态
                    status = self.get_job(job_id).get("status")
                    if status in ("completed", "failed"):
                        break  # 任务已结束，终止订阅
                    # 任务仍在运行，发送 ping 保持连接
                    yield {
                        "event": "ping",
                        "data": {"timestamp": _utcnow_iso()},
                        "timestamp": _utcnow_iso(),
                    }
        finally:
            stop_event.set()  # 通知监听线程停止
            thread.join(timeout=3.0)  # 等待线程退出

    # ── 生命周期管理 ────────────────────────────────────────────────────

    def clear(self) -> None:
        """删除所有匹配前缀的任务状态 Key。
        
        使用 SCAN 命令避免 KEYS 阻塞 Redis 服务器。
        注意：只清理任务状态 Key，不清理 Pub/Sub 频道。
        """
        cursor = 0
        pattern = f"{self._prefix}job:*"
        while True:
            cursor, keys = self._r.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                self._r.delete(*keys)
            if cursor == 0:  # SCAN 完成
                break
