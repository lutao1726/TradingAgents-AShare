"""
模型名称验证器

用于验证各 LLM 提供商的模型名称是否有效。
只做名称验证，不强制限制参数。
让 LLM 提供商对未指定的参数使用自己的默认值。

支持的提供商：
1. OpenAI - GPT-5、GPT-4.1、o-series 等
2. Anthropic - Claude 4.5、Claude 4.x、Claude 3.7/3.5 等
3. Google - Gemini 3、Gemini 2.5、Gemini 2.0 等
4. xAI - Grok 4.1、Grok 4 等
5. ollama、openrouter - 接受任何模型名称（本地或代理服务）
"""

# 各提供商的有效模型列表
VALID_MODELS = {
    "openai": [
        # GPT-5 系列 (2025)
        "gpt-5.2",
        "gpt-5.1",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
        # GPT-4.1 系列 (2025)
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-4.1-nano",
        # o-series 推理模型
        "o4-mini",
        "o3",
        "o3-mini",
        "o1",
        "o1-preview",
        # GPT-4o 系列 (旧版但仍然支持)
        "gpt-4o",
        "gpt-4o-mini",
    ],
    "anthropic": [
        # Claude 4.5 系列 (2025)
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        # Claude 4.x 系列
        "claude-opus-4-1-20250805",
        "claude-sonnet-4-20250514",
        # Claude 3.7 系列
        "claude-3-7-sonnet-20250219",
        # Claude 3.5 系列 (旧版)
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
    ],
    "google": [
        # Gemini 3 系列 (预览版)
        "gemini-3-pro-preview",
        "gemini-3-flash-preview",
        # Gemini 2.5 系列
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # Gemini 2.0 系列
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ],
    "xai": [
        # Grok 4.1 系列
        "grok-4-1-fast",
        "grok-4-1-fast-reasoning",
        "grok-4-1-fast-non-reasoning",
        # Grok 4 系列
        "grok-4",
        "grok-4-0709",
        "grok-4-fast-reasoning",
        "grok-4-fast-non-reasoning",
    ],
}


def validate_model(provider: str, model: str) -> bool:
    """
    检查模型名称是否对给定提供商有效。
    
    对于 ollama 和 openrouter，接受任何模型名称。
    对于其他提供商，检查是否在预定义的模型列表中。
    
    参数:
        provider: LLM 提供商名称，如 "openai", "anthropic", "google", "xai"
        model: 模型名称，如 "gpt-4o", "claude-3-5-sonnet-20241022"
        
    返回:
        bool: 模型有效返回 True，否则 False
        
    示例：
        validate_model("openai", "gpt-4o")      # True
        validate_model("openai", "gpt-99")       # False
        validate_model("ollama", "any-model")    # True（ollama 接受任何模型）
    """
    provider_lower = provider.lower()

    # ollama 和 openrouter 接受任何模型名称
    # - ollama: 本地运行，支持任意模型
    # - openrouter: 代理服务，支持多种模型
    if provider_lower in ("ollama", "openrouter"):
        return True

    # 如果提供商不在预定义列表中，接受任何模型（向后兼容）
    if provider_lower not in VALID_MODELS:
        return True

    # 检查模型是否在有效列表中
    return model in VALID_MODELS[provider_lower]
