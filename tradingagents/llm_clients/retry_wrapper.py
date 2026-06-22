"""
LLM 重试包装器：为 LLM 调用添加指数退避重试机制。

核心功能：
1. 指数退避：每次重试等待时间翻倍
2. 随机抖动：避免多个请求同时重试
3. 错误识别：只对可重试错误（如 429）进行重试
4. 可配置：支持通过环境变量配置重试参数

环境变量：
- TA_LLM_MAX_RETRIES: 最大重试次数（默认 5）
- TA_LLM_INITIAL_DELAY: 初始等待时间秒数（默认 1）
- TA_LLM_MAX_DELAY: 最大等待时间秒数（默认 60）
- TA_LLM_BACKOFF_MULTIPLIER: 退避倍数（默认 2）

使用方式：
    from tradingagents.llm_clients.retry_wrapper import RetryableLLM
    
    llm = ChatOpenAI(model="gpt-4o")
    retryable_llm = RetryableLLM(llm)
    response = retryable_llm.invoke(messages)
"""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# 可重试的错误关键词
RETRYABLE_ERROR_KEYWORDS = [
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
    "quota exceeded",
    "insufficient_quota",
    "overloaded",
    "capacity",
    "throttled",
    "throttling",
]

# 不可重试的错误关键词
NON_RETRYABLE_ERROR_KEYWORDS = [
    "401",  # 认证失败
    "403",  # 权限不足
    "404",  # 资源不存在
    "invalid_api_key",
    "authentication",
    "permission",
]


def _is_retryable_error(error: Exception) -> bool:
    """判断错误是否可重试。
    
    Args:
        error: 异常对象
    
    Returns:
        是否可重试
    """
    error_str = str(error).lower()
    
    # 检查是否为不可重试错误
    for keyword in NON_RETRYABLE_ERROR_KEYWORDS:
        if keyword in error_str:
            return False
    
    # 检查是否为可重试错误
    for keyword in RETRYABLE_ERROR_KEYWORDS:
        if keyword in error_str:
            return True
    
    # 网络错误通常可重试
    if "timeout" in error_str or "connection" in error_str:
        return True
    
    return False


def _get_retry_config() -> dict:
    """从环境变量获取重试配置。
    
    Returns:
        配置字典
    """
    return {
        "max_retries": int(os.getenv("TA_LLM_MAX_RETRIES", "5")),
        "initial_delay": float(os.getenv("TA_LLM_INITIAL_DELAY", "1")),
        "max_delay": float(os.getenv("TA_LLM_MAX_DELAY", "60")),
        "backoff_multiplier": float(os.getenv("TA_LLM_BACKOFF_MULTIPLIER", "2")),
    }


class RetryableLLM:
    """带指数退避重试的 LLM 包装器。
    
    特性：
    - 指数退避：每次重试等待时间翻倍
    - 随机抖动：避免多个请求同时重试（惊群效应）
    - 错误分类：只对可重试错误进行重试
    - 详细日志：记录每次重试的详细信息
    
    使用方式：
        llm = ChatOpenAI(model="gpt-4o")
        retryable_llm = RetryableLLM(llm)
        response = retryable_llm.invoke(messages)
    """
    
    def __init__(
        self,
        llm: Any,
        max_retries: Optional[int] = None,
        initial_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
        backoff_multiplier: Optional[float] = None,
    ):
        """初始化重试包装器。
        
        Args:
            llm: 原始 LLM 实例
            max_retries: 最大重试次数（默认从环境变量读取）
            initial_delay: 初始等待时间秒数（默认从环境变量读取）
            max_delay: 最大等待时间秒数（默认从环境变量读取）
            backoff_multiplier: 退避倍数（默认从环境变量读取）
        """
        self.llm = llm
        config = _get_retry_config()
        self.max_retries = max_retries or config["max_retries"]
        self.initial_delay = initial_delay or config["initial_delay"]
        self.max_delay = max_delay or config["max_delay"]
        self.backoff_multiplier = backoff_multiplier or config["backoff_multiplier"]
    
    def _calculate_delay(self, attempt: int) -> float:
        """计算第 N 次重试的等待时间（指数退避 + 随机抖动）。
        
        Args:
            attempt: 当前重试次数（从 0 开始）
        
        Returns:
            等待时间秒数
        """
        # 指数退避：delay = initial_delay * (backoff_multiplier ^ attempt)
        delay = self.initial_delay * (self.backoff_multiplier ** attempt)
        
        # 限制最大等待时间
        delay = min(delay, self.max_delay)
        
        # 添加随机抖动（0% ~ 50% 的随机偏移）
        jitter = delay * random.uniform(0, 0.5)
        delay = delay + jitter
        
        return delay
    
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """调用 LLM（带重试逻辑）。
        
        Args:
            input: 输入内容（消息列表）
            config: 配置（可选）
            **kwargs: 其他参数
        
        Returns:
            LLM 响应
        
        Raises:
            Exception: 重试次数用尽后抛出最后一次异常
        """
        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                # 第一次尝试或重试
                if attempt > 0:
                    delay = self._calculate_delay(attempt - 1)
                    logger.warning(
                        f"[RetryableLLM] 重试 {attempt}/{self.max_retries}，"
                        f"等待 {delay:.1f} 秒..."
                    )
                    time.sleep(delay)
                
                # 调用原始 LLM
                return self.llm.invoke(input, config, **kwargs)
                
            except Exception as e:
                last_error = e
                
                # 判断是否可重试
                if not _is_retryable_error(e):
                    logger.error(
                        f"[RetryableLLM] 不可重试错误: {type(e).__name__}: {e}"
                    )
                    raise
                
                # 记录重试信息
                if attempt < self.max_retries:
                    logger.warning(
                        f"[RetryableLLM] 可重试错误: {type(e).__name__}: {e}"
                    )
                else:
                    logger.error(
                        f"[RetryableLLM] 重试次数用尽 ({self.max_retries} 次)，"
                        f"最后一次错误: {type(e).__name__}: {e}"
                    )
        
        # 所有重试都失败
        raise last_error
    
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """异步调用 LLM（带重试逻辑）。
        
        Args:
            input: 输入内容（消息列表）
            config: 配置（可选）
            **kwargs: 其他参数
        
        Returns:
            LLM 响应
        
        Raises:
            Exception: 重试次数用尽后抛出最后一次异常
        """
        import asyncio
        
        last_error = None
        
        for attempt in range(self.max_retries + 1):
            try:
                # 第一次尝试或重试
                if attempt > 0:
                    delay = self._calculate_delay(attempt - 1)
                    logger.warning(
                        f"[RetryableLLM] 异步重试 {attempt}/{self.max_retries}，"
                        f"等待 {delay:.1f} 秒..."
                    )
                    await asyncio.sleep(delay)
                
                # 调用原始 LLM
                return await self.llm.ainvoke(input, config, **kwargs)
                
            except Exception as e:
                last_error = e
                
                # 判断是否可重试
                if not _is_retryable_error(e):
                    logger.error(
                        f"[RetryableLLM] 不可重试错误: {type(e).__name__}: {e}"
                    )
                    raise
                
                # 记录重试信息
                if attempt < self.max_retries:
                    logger.warning(
                        f"[RetryableLLM] 可重试错误: {type(e).__name__}: {e}"
                    )
                else:
                    logger.error(
                        f"[RetryableLLM] 重试次数用尽 ({self.max_retries} 次)，"
                        f"最后一次错误: {type(e).__name__}: {e}"
                    )
        
        # 所有重试都失败
        raise last_error
    
    def __getattr__(self, name: str) -> Any:
        """代理其他属性到原始 LLM。
        
        Args:
            name: 属性名
        
        Returns:
            属性值
        """
        return getattr(self.llm, name)
