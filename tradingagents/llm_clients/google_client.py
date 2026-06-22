"""
Google Gemini 模型客户端

提供 Google Gemini 系列模型的 LangChain 集成。
支持 Gemini 2.5 和 Gemini 3 系列模型的不同 thinking_level 配置。

主要组件：
1. NormalizedChatGoogleGenerativeAI - 标准化 Gemini 输出格式
2. GoogleClient - Google Gemini 客户端实现，继承自 BaseLLMClient

Gemini 模型的 thinking_level 配置差异：
- Gemini 3 Pro/Flash: 使用 thinking_level 参数，支持 low/high（Pro 不支持 minimal）
- Gemini 2.5: 使用 thinking_budget 参数，0=禁用，-1=动态启用
"""

from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from .base_client import BaseLLMClient
from .validators import validate_model


class NormalizedChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """
    标准化 ChatGoogleGenerativeAI 类
    
    解决 Gemini 3 模型返回格式不一致的问题。
    Gemini 3 返回内容为列表格式：[{"type": "text", "text": "..."}]
    此类将其统一标准化为字符串，便于下游代码处理。
    
    继承自 ChatGoogleGenerativeAI，重写 invoke() 方法。
    """

    def _normalize_content(self, response):
        """
        标准化响应内容为字符串格式。
        
        参数:
            response: 原始 LLM 响应对象
            
        返回:
            response: 标准化后的响应对象（content 字段已转换为字符串）
        """
        content = response.content
        
        # 处理列表格式的内容
        if isinstance(content, list):
            # 提取所有 type="text" 的内容块
            texts = [
                item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
                else item if isinstance(item, str) else ""
                for item in content
            ]
            # 用换行符连接非空文本
            response.content = "\n".join(t for t in texts if t)
        
        return response

    def invoke(self, input, config=None, **kwargs):
        """
        同步调用 LLM 并标准化返回内容。
        
        参数:
            input: 输入消息
            config: 可选的配置参数
            **kwargs: 其他参数
            
        返回:
            AIMessage: 标准化后的响应消息
        """
        return self._normalize_content(super().invoke(input, config, **kwargs))


class GoogleClient(BaseLLMClient):
    """
    Google Gemini 模型客户端
    
    提供 Gemini 系列模型的配置和实例化功能。
    支持自定义 API key、超时时间等参数。
    
    thinking_level 配置说明：
    - Gemini 3 Pro: low, high（不支持 "minimal"，会自动降级为 "low"）
    - Gemini 3 Flash: minimal, low, medium, high
    - Gemini 2.5: 使用 thinking_budget，0=禁用，-1=动态启用
    
    使用示例：
        client = GoogleClient(model="gemini-2.5-pro")
        llm = client.get_llm()
    """

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        """
        初始化 Google Gemini 客户端
        
        参数:
            model: Gemini 模型名称，如 "gemini-2.5-pro"
            base_url: 可选的 API 基础 URL（Google API 通常不需要）
            **kwargs: 其他配置参数，如 api_key, timeout, thinking_level 等
        """
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """
        获取配置好的 ChatGoogleGenerativeAI 实例
        
        返回:
            NormalizedChatGoogleGenerativeAI: 标准化后的 Gemini LLM 实例
            
        支持的 kwargs 参数：
        - google_api_key 或 api_key: Google API 密钥
        - timeout: 请求超时时间（秒）
        - max_retries: 最大重试次数
        - callbacks: 回调函数列表
        - thinking_level: 思考级别配置
        """
        # 构建基础参数
        llm_kwargs = {"model": self.model}

        # 传递允许的参数
        for key in ("timeout", "max_retries", "google_api_key", "callbacks"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]
        
        # 如果提供了 api_key 但没有 google_api_key，使用 api_key
        if "api_key" in self.kwargs and "google_api_key" not in llm_kwargs:
            llm_kwargs["google_api_key"] = self.kwargs["api_key"]

        # 处理 thinking_level 配置
        # 不同模型系列使用不同的参数名和取值范围
        thinking_level = self.kwargs.get("thinking_level")
        if thinking_level:
            model_lower = self.model.lower()
            
            if "gemini-3" in model_lower:
                # Gemini 3 系列使用 thinking_level 参数
                # Gemini 3 Pro 不支持 "minimal"，降级为 "low"
                if "pro" in model_lower and thinking_level == "minimal":
                    thinking_level = "low"
                llm_kwargs["thinking_level"] = thinking_level
            else:
                # Gemini 2.5 系列使用 thinking_budget 参数
                # "high" -> -1（动态启用），其他 -> 0（禁用）
                llm_kwargs["thinking_budget"] = -1 if thinking_level == "high" else 0

        return NormalizedChatGoogleGenerativeAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """
        验证模型名称是否有效
        
        返回:
            bool: 模型有效返回 True，否则 False
        """
        return validate_model("google", self.model)
