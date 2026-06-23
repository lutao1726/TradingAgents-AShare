"""
API Key 池客户端包装器：为 LLM 客户端添加 Key 池支持。

核心功能：
1. 自动管理 Key 池的生命周期
2. 每次请求自动获取下一个可用 Key
3. 请求完成后报告成功/失败状态
4. 支持故障转移和重试
5. 线程安全的 Key 访问

使用方式：
    from tradingagents.llm_clients.key_pool import create_pool_from_string
    from tradingagents.llm_clients.key_pool_client import KeyPoolClient
    
    # 创建 Key 池
    pool = create_pool_from_string("user123", "key1,key2,key3")
    
    # 包装普通客户端
    client = KeyPoolClient(original_client, "user123")
    llm = client.get_llm()  # 每次请求自动切换 Key
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .base_client import BaseLLMClient
from .key_pool import (
    get_key_from_pool,
    report_key_success,
    report_key_failure,
    get_key_pool,
    KeyPool,
)

_logger = logging.getLogger(__name__)


class KeyPoolClientWrapper:
    """Key 池客户端包装器。
    
    包装现有的 LLM 客户端，添加 Key 池管理功能。
    
    特性：
    - 自动 Key 切换
    - 请求成功率追踪
    - 自动故障转移
    - 线程安全
    """
    
    def __init__(
        self,
        original_client: BaseLLMClient,
        pool_id: str,
        strategy: str = "round_robin"
    ):
        """初始化 Key 池客户端包装器。
        
        Args:
            original_client: 原始 LLM 客户端
            pool_id: Key 池 ID
            strategy: 选择策略
        """
        self.original_client = original_client
        self.pool_id = pool_id
        self.strategy = strategy
        self._current_key: Optional[str] = None
        self._request_start_time: float = 0
    
    def get_llm(self) -> Any:
        """获取带 Key 池支持的 LLM 实例。
        
        返回一个包装器，在每次 invoke 时自动切换 Key。
        """
        original_llm = self.original_client.get_llm()
        return KeyPoolLLMWrapper(
            original_llm=original_llm,
            pool_id=self.pool_id,
            strategy=self.strategy
        )


class KeyPoolLLMWrapper:
    """Key 池 LLM 包装器。
    
    包装 LangChain LLM 实例，添加 Key 池管理。
    
    特性：
    - 每次 invoke 前自动获取新 Key
    - 请求完成后报告状态
    - 支持故障转移
    """
    
    def __init__(
        self,
        original_llm: Any,
        pool_id: str,
        strategy: str = "round_robin"
    ):
        """初始化 Key 池 LLM 包装器。
        
        Args:
            original_llm: 原始 LangChain LLM 实例
            pool_id: Key 池 ID
            strategy: 选择策略
        """
        self.original_llm = original_llm
        self.pool_id = pool_id
        self.strategy = strategy
        self._current_key: Optional[str] = None
        self._request_start_time: float = 0
    
    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """调用 LLM，自动管理 Key 池。
        
        流程：
        1. 从 Key 池获取下一个可用 Key
        2. 设置到 LLM 实例
        3. 执行请求
        4. 报告成功/失败
        5. 如果失败，尝试故障转移
        """
        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            # 1. 从 Key 池获取 Key
            self._current_key = get_key_from_pool(self.pool_id)
            if not self._current_key:
                _logger.error(f"[KeyPool] Key 池 {self.pool_id} 中没有可用的 Key")
                raise ValueError(f"Key 池 {self.pool_id} 中没有可用的 Key")
            
            # 脱敏显示 Key
            masked_key = self._mask_key(self._current_key)
            
            # 2. 设置 Key 到 LLM 实例
            self._set_api_key(self._current_key)
            
            # 3. 记录请求开始时间
            self._request_start_time = time.time()
            
            _logger.info(
                f"[KeyPool] 请求开始 | 池: {self.pool_id} | "
                f"Key: {masked_key} | 尝试: {attempt + 1}/{max_retries}"
            )
            
            try:
                # 4. 执行请求
                result = self.original_llm.invoke(input=input, config=config, **kwargs)
                
                # 5. 计算延迟
                latency_ms = (time.time() - self._request_start_time) * 1000
                
                # 6. 报告成功
                report_key_success(self.pool_id, self._current_key, latency_ms)
                
                _logger.info(
                    f"[KeyPool] 请求成功 | 池: {self.pool_id} | "
                    f"Key: {masked_key} | 延迟: {latency_ms:.1f}ms"
                )
                
                return result
                
            except Exception as e:
                # 7. 报告失败
                report_key_failure(self.pool_id, self._current_key, str(e))
                
                _logger.warning(
                    f"[KeyPool] 请求失败 | 池: {self.pool_id} | "
                    f"Key: {masked_key} | 尝试: {attempt + 1}/{max_retries} | "
                    f"错误: {e}"
                )
                
                last_error = e
                
                # 如果是最后一次尝试，抛出异常
                if attempt == max_retries - 1:
                    raise
                
                # 短暂等待后重试
                time.sleep(0.5 * (attempt + 1))
        
        # 不应该到达这里，但以防万一
        raise last_error
    
    def _mask_key(self, api_key: str) -> str:
        """脱敏显示 API Key。
        
        规则：
        - 如果 Key 长度 > 8，显示前4位 + **** + 后4位
        - 如果 Key 长度 <= 8，全部显示为 ****
        """
        if len(api_key) > 8:
            return f"{api_key[:4]}****{api_key[-4:]}"
        return "****"
    
    def _set_api_key(self, api_key: str):
        """设置 API Key 到 LLM 实例。"""
        # 根据 LangChain LLM 类型设置 API Key
        if hasattr(self.original_llm, 'openai_api_key'):
            self.original_llm.openai_api_key = api_key
        elif hasattr(self.original_llm, 'api_key'):
            self.original_llm.api_key = api_key
        elif hasattr(self.original_llm, 'anthropic_api_key'):
            self.original_llm.anthropic_api_key = api_key
        elif hasattr(self.original_llm, 'google_api_key'):
            self.original_llm.google_api_key = api_key
        else:
            _logger.warning("无法设置 API Key，LLM 实例不支持动态 Key 切换")
    
    def batch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        """批量调用 LLM。"""
        results = []
        for inp in inputs:
            result = self.invoke(inp, config=config, **kwargs)
            results.append(result)
        return results
    
    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """异步调用 LLM。"""
        # 简单实现：同步包装
        return self.invoke(input, config=config, **kwargs)
    
    async def abatch(self, inputs: list, config: Any = None, **kwargs: Any) -> list:
        """异步批量调用 LLM。"""
        results = []
        for inp in inputs:
            result = await self.ainvoke(inp, config=config, **kwargs)
            results.append(result)
        return results
    
    def stream(self, input: Any, config: Any = None, **kwargs: Any):
        """流式调用 LLM。"""
        # 从 Key 池获取 Key
        self._current_key = get_key_from_pool(self.pool_id)
        if not self._current_key:
            _logger.error(f"[KeyPool] Key 池 {self.pool_id} 中没有可用的 Key")
            raise ValueError(f"Key 池 {self.pool_id} 中没有可用的 Key")
        
        masked_key = self._mask_key(self._current_key)
        self._set_api_key(self._current_key)
        
        _logger.info(f"[KeyPool] 流式请求开始 | 池: {self.pool_id} | Key: {masked_key}")
        
        try:
            for chunk in self.original_llm.stream(input=input, config=config, **kwargs):
                yield chunk
            report_key_success(self.pool_id, self._current_key, latency_ms=0)
            _logger.info(f"[KeyPool] 流式请求完成 | 池: {self.pool_id} | Key: {masked_key}")
        except Exception as e:
            report_key_failure(self.pool_id, self._current_key, str(e))
            _logger.warning(f"[KeyPool] 流式请求失败 | 池: {self.pool_id} | Key: {masked_key} | 错误: {e}")
            raise
    
    async def astream(self, input: Any, config: Any = None, **kwargs: Any):
        """异步流式调用 LLM。"""
        # 从 Key 池获取 Key
        self._current_key = get_key_from_pool(self.pool_id)
        if not self._current_key:
            _logger.error(f"[KeyPool] Key 池 {self.pool_id} 中没有可用的 Key")
            raise ValueError(f"Key 池 {self.pool_id} 中没有可用的 Key")
        
        masked_key = self._mask_key(self._current_key)
        self._set_api_key(self._current_key)
        
        _logger.info(f"[KeyPool] 异步流式请求开始 | 池: {self.pool_id} | Key: {masked_key}")
        
        try:
            async for chunk in self.original_llm.astream(input=input, config=config, **kwargs):
                yield chunk
            report_key_success(self.pool_id, self._current_key, latency_ms=0)
            _logger.info(f"[KeyPool] 异步流式请求完成 | 池: {self.pool_id} | Key: {masked_key}")
        except Exception as e:
            report_key_failure(self.pool_id, self._current_key, str(e))
            _logger.warning(f"[KeyPool] 异步流式请求失败 | 池: {self.pool_id} | Key: {masked_key} | 错误: {e}")
            raise


def create_key_pool_client(
    pool_id: str,
    keys: list,
    client_factory: Callable,
    strategy: str = "round_robin",
    **client_kwargs
) -> KeyPoolClientWrapper:
    """创建带 Key 池支持的客户端。
    
    Args:
        pool_id: Key 池 ID
        keys: API Key 列表
        client_factory: 客户端工厂函数
        strategy: 选择策略
        **client_kwargs: 客户端参数
    
    Returns:
        KeyPoolClientWrapper 实例
    """
    # 创建 Key 池
    pool = get_key_pool(pool_id, keys, strategy)
    
    # 创建原始客户端（使用第一个 Key）
    original_client = client_factory(api_key=keys[0], **client_kwargs)
    
    # 包装客户端
    return KeyPoolClientWrapper(original_client, pool_id, strategy)


def create_key_pool_client_from_string(
    pool_id: str,
    keys_string: str,
    client_factory: Callable,
    strategy: str = "round_robin",
    **client_kwargs
) -> KeyPoolClientWrapper:
    """从字符串创建带 Key 池支持的客户端。
    
    Args:
        pool_id: Key 池 ID
        keys_string: 逗号分隔的 Key 字符串
        client_factory: 客户端工厂函数
        strategy: 选择策略
        **client_kwargs: 客户端参数
    
    Returns:
        KeyPoolClientWrapper 实例
    """
    keys = [k.strip() for k in keys_string.split(",") if k.strip()]
    if not keys:
        raise ValueError("Key 字符串中没有有效的 Key")
    
    return create_key_pool_client(pool_id, keys, client_factory, strategy, **client_kwargs)
