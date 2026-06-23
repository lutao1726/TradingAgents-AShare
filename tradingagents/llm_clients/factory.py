"""
LLM 客户端工厂模块：创建和配置 LLM 客户端实例。

核心功能：
1. 多厂商支持：OpenAI、Anthropic、Google、xAI、Ollama、OpenRouter
2. 统一接口：所有厂商返回相同的 BaseLLMClient 接口
3. 重试包装：自动为 LLM 添加指数退避重试机制
4. Key 池支持：支持多 Key 的轮询和故障转移

使用方式：
    from tradingagents.llm_clients import create_llm_client
    
    # 单 Key 模式
    client = create_llm_client(
        provider="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-xxx"
    )
    
    # Key 池模式
    client = create_llm_client(
        provider="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_pool=["sk-xxx1", "sk-xxx2", "sk-xxx3"]
    )
    
    llm = client.get_llm()  # 返回带重试的 LLM 实例
"""
from typing import Optional, List
import logging

from .base_client import BaseLLMClient
from .openai_client import OpenAIClient
from .anthropic_client import AnthropicClient
from .google_client import GoogleClient
from .retry_wrapper import RetryableLLM
from .key_pool_client import KeyPoolClientWrapper, create_key_pool_client

_logger = logging.getLogger(__name__)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    enable_retry: bool = True,
    api_key_pool: Optional[List[str]] = None,
    pool_id: Optional[str] = None,
    pool_strategy: str = "round_robin",
    **kwargs,
) -> BaseLLMClient:
    """创建指定厂商的 LLM 客户端。
    
    Args:
        provider: LLM 厂商（openai, anthropic, google, xai, ollama, openrouter）
        model: 模型名称/标识
        base_url: API 端点基础 URL（可选）
        enable_retry: 是否启用重试包装器（默认 True）
        api_key_pool: API Key 列表（用于 Key 池模式，可选）
        pool_id: Key 池 ID（可选，默认使用 provider 作为 ID）
        pool_strategy: Key 池选择策略（round_robin, weighted, random）
        **kwargs: 其他厂商特定参数
    
    Returns:
        配置好的 BaseLLMClient 实例
    
    Raises:
        ValueError: 不支持的厂商
    """
    provider_lower = provider.lower()

    # 如果提供了 Key 池，创建 Key 池客户端
    if api_key_pool and len(api_key_pool) > 1:
        effective_pool_id = pool_id or f"{provider_lower}_pool"
        
        # 从 kwargs 中移除 api_key，因为 Key 池会提供
        factory_kwargs = {k: v for k, v in kwargs.items() if k != 'api_key'}
        
        # 创建原始客户端工厂
        def client_factory(api_key: str, **extra_kwargs):
            """客户端工厂函数。"""
            # 合并参数，api_key 由 Key 池提供
            merged_kwargs = {**factory_kwargs, **extra_kwargs}
            
            if provider_lower in ("openai", "ollama", "openrouter"):
                return OpenAIClient(model, base_url, provider=provider_lower, api_key=api_key, **merged_kwargs)
            elif provider_lower == "xai":
                return OpenAIClient(model, base_url, provider="xai", api_key=api_key, **merged_kwargs)
            elif provider_lower == "anthropic":
                return AnthropicClient(model, base_url, api_key=api_key, **merged_kwargs)
            elif provider_lower == "google":
                return GoogleClient(model, base_url, api_key=api_key, **merged_kwargs)
            else:
                raise ValueError(f"Unsupported LLM provider: {provider_lower}")
        
        # 创建 Key 池客户端（不传递 kwargs，因为已经包含在 factory_kwargs 中）
        client = create_key_pool_client(
            pool_id=effective_pool_id,
            keys=api_key_pool,
            client_factory=client_factory,
            strategy=pool_strategy,
        )
        
        _logger.info(f"创建了 Key 池客户端，池 ID: {effective_pool_id}，Key 数量: {len(api_key_pool)}")
        
        # 如果启用重试，包装客户端
        if enable_retry:
            original_get_llm = client.get_llm
            
            def get_llm_with_retry():
                """获取带重试包装器的 LLM 实例。"""
                llm = original_get_llm()
                return RetryableLLM(llm)
            
            client.get_llm = get_llm_with_retry
        
        return client
    
    # 单 Key 模式（原有逻辑）
    if provider_lower in ("openai", "ollama", "openrouter"):
        client = OpenAIClient(model, base_url, provider=provider_lower, **kwargs)
    elif provider_lower == "xai":
        client = OpenAIClient(model, base_url, provider="xai", **kwargs)
    elif provider_lower == "anthropic":
        client = AnthropicClient(model, base_url, **kwargs)
    elif provider_lower == "google":
        client = GoogleClient(model, base_url, **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider_lower}")
    
    # 如果启用重试，包装 LLM 实例
    if enable_retry:
        original_get_llm = client.get_llm
        
        def get_llm_with_retry():
            """获取带重试包装器的 LLM 实例。"""
            llm = original_get_llm()
            return RetryableLLM(llm)
        
        # 替换 get_llm 方法
        client.get_llm = get_llm_with_retry
    
    return client
