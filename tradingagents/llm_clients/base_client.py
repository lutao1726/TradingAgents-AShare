"""
LLM 客户端抽象基类：定义所有 LLM 客户端的统一接口。

所有厂商的 LLM 客户端（OpenAI、Anthropic、Google 等）都必须继承此基类，
并实现 get_llm() 和 validate_model() 方法。

设计目的：
- 统一不同厂商的 LLM 客户端接口
- 提供可扩展的客户端架构
- 简化 Agent 层的 LLM 调用
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseLLMClient(ABC):
    """LLM 客户端抽象基类。
    
    所有厂商的 LLM 客户端都必须继承此类，并实现以下方法：
    - get_llm(): 返回配置好的 LLM 实例
    - validate_model(): 验证模型是否受支持
    
    属性：
    - model: 模型名称（如 "gpt-4o"、"claude-sonnet-4-20250514"）
    - base_url: API 端点基础 URL（可选）
    - kwargs: 其他配置参数
    """

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        """初始化 LLM 客户端。
        
        Args:
            model: 模型名称/标识
            base_url: API 端点基础 URL（可选）
            **kwargs: 其他配置参数（如 api_key、timeout 等）
        """
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    @abstractmethod
    def get_llm(self) -> Any:
        """返回配置好的 LLM 实例。
        
        Returns:
            LangChain 兼容的 LLM 实例（如 ChatOpenAI、ChatAnthropic）
        """
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """验证模型是否受此客户端支持。
        
        Returns:
            模型是否有效
        """
        pass
