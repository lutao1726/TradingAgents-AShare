"""
LLM 客户端工厂模块：创建和配置 LLM 客户端实例。

核心功能：
1. 多厂商支持：OpenAI、Anthropic、Google、xAI、Ollama、OpenRouter
2. 统一接口：所有厂商返回相同的 BaseLLMClient 接口
3. 重试包装：自动为 LLM 添加指数退避重试机制

使用方式：
    from tradingagents.llm_clients import create_llm_client
    
    client = create_llm_client(
        provider="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-xxx"
    )
    llm = client.get_llm()  # 返回带重试的 LLM 实例
"""
from typing import Optional

from .base_client import BaseLLMClient
from .openai_client import OpenAIClient
from .anthropic_client import AnthropicClient
from .google_client import GoogleClient
from .retry_wrapper import RetryableLLM


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    enable_retry: bool = True,
    **kwargs,
) -> BaseLLMClient:
    """创建指定厂商的 LLM 客户端。
    
    Args:
        provider: LLM 厂商（openai, anthropic, google, xai, ollama, openrouter）
        model: 模型名称/标识
        base_url: API 端点基础 URL（可选）
        enable_retry: 是否启用重试包装器（默认 True）
        **kwargs: 其他厂商特定参数
    
    Returns:
        配置好的 BaseLLMClient 实例
    
    Raises:
        ValueError: 不支持的厂商
    """
    provider_lower = provider.lower()

    if provider_lower in ("openai", "ollama", "openrouter"):
        client = OpenAIClient(model, base_url, provider=provider_lower, **kwargs)
    elif provider_lower == "xai":
        client = OpenAIClient(model, base_url, provider="xai", **kwargs)
    elif provider_lower == "anthropic":
        client = AnthropicClient(model, base_url, **kwargs)
    elif provider_lower == "google":
        client = GoogleClient(model, base_url, **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")
    
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
