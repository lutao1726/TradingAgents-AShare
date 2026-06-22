"""
LLM 客户端模块：提供统一的大语言模型客户端接口。

核心组件：
- BaseLLMClient: LLM 客户端抽象基类
- create_llm_client(): 工厂函数，创建指定厂商的 LLM 客户端

支持的厂商：
- OpenAI（GPT-4o、GPT-4o-mini 等）
- Anthropic（Claude 系列）
- Google（Gemini 系列）
- xAI（Grok 系列）
- Ollama（本地模型）
- OpenRouter（模型路由）

使用方式：
    from tradingagents.llm_clients import create_llm_client
    
    client = create_llm_client(
        provider="openai",
        model="gpt-4o",
        api_key="sk-xxx"
    )
    llm = client.get_llm()
    response = llm.invoke(messages)
"""
from .base_client import BaseLLMClient
from .factory import create_llm_client

__all__ = ["BaseLLMClient", "create_llm_client"]
