"""
Anthropic Claude 模型客户端

提供 Claude 系列模型的 LangChain 集成，支持扩展思考（Extended Thinking）功能。
当启用扩展思考时，Claude 返回的内容格式与标准格式不同，
需要进行标准化处理以确保下游代码的一致性。

主要组件：
1. _extract_text_from_content() - 从扩展思考的内容块中提取纯文本
2. NormalizedChatAnthropic - 标准化 ChatAnthropic，统一输出格式
3. AnthropicClient - Anthropic 客户端实现，继承自 BaseLLMClient
"""

from typing import Any, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk

from .base_client import BaseLLMClient
from .validators import validate_model


def _extract_text_from_content(content):
    """
    从扩展思考内容块中提取文本。
    
    当启用扩展思考功能时，Anthropic 返回的内容是一个列表格式：
        [{"type": "thinking", "thinking": "思考过程..."}, {"type": "text", "text": "实际回复"}]
    
    此函数只提取 type="text" 的内容块，并将其拼接成字符串。
    
    参数:
        content: 原始内容，可以是字符串或内容块列表
        
    返回:
        str: 提取并拼接后的纯文本内容
    """
    # 如果已经是字符串，直接返回
    if isinstance(content, str):
        return content
    
    # 如果是列表，遍历提取文本块
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # 提取 type="text" 的块
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") not in ("thinking",):
                    # 未知类型的块，尝试获取 text 字段
                    parts.append(block.get("text", ""))
            elif isinstance(block, str):
                # 如果块本身就是字符串，直接添加
                parts.append(block)
        return "".join(parts)
    
    # 其他类型转换为字符串
    return str(content)


class NormalizedChatAnthropic(ChatAnthropic):
    """
    标准化 ChatAnthropic 类
    
    解决 Claude 模型启用扩展思考后返回格式不一致的问题。
    将返回内容统一标准化为字符串，便于下游代码处理。
    
    继承自 ChatAnthropic，重写以下方法：
    - invoke() - 同步调用
    - ainvoke() - 异步调用
    - stream() - 同步流式输出
    - astream() - 异步流式输出
    """

    def _normalize_content(self, response):
        """
        标准化响应内容为字符串格式。
        
        参数:
            response: 原始 LLM 响应对象
            
        返回:
            response: 标准化后的响应对象（content 字段已转换为字符串）
        """
        response.content = _extract_text_from_content(response.content)
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

    async def ainvoke(self, input, config=None, **kwargs):
        """
        异步调用 LLM 并标准化返回内容。
        
        参数:
            input: 输入消息
            config: 可选的配置参数
            **kwargs: 其他参数
            
        返回:
            AIMessage: 标准化后的响应消息
        """
        return self._normalize_content(await super().ainvoke(input, config, **kwargs))

    def _normalize_chunk(self, chunk):
        """
        标准化流式输出的 chunk。
        
        参数:
            chunk: 流式输出的消息块
            
        返回:
            AIMessageChunk: 标准化后的消息块
        """
        if isinstance(chunk, AIMessageChunk) and isinstance(chunk.content, list):
            chunk.content = _extract_text_from_content(chunk.content)
        return chunk

    def stream(self, input, config=None, **kwargs):
        """
        同步流式输出并标准化每个 chunk。
        
        生成器函数，逐个产出标准化后的消息块。
        
        参数:
            input: 输入消息
            config: 可选的配置参数
            **kwargs: 其他参数
            
        生成:
            AIMessageChunk: 标准化后的消息块
        """
        for chunk in super().stream(input, config, **kwargs):
            yield self._normalize_chunk(chunk)

    async def astream(self, input, config=None, **kwargs):
        """
        异步流式输出并标准化每个 chunk。
        
        异步生成器函数，逐个产出标准化后的消息块。
        
        参数:
            input: 输入消息
            config: 可选的配置参数
            **kwargs: 其他参数
            
        生成:
            AIMessageChunk: 标准化后的消息块
        """
        async for chunk in super().astream(input, config, **kwargs):
            yield self._normalize_chunk(chunk)


class AnthropicClient(BaseLLMClient):
    """
    Anthropic Claude 模型客户端
    
    提供 Claude 系列模型的配置和实例化功能。
    支持自定义 base_url、API key、超时时间等参数。
    
    特殊处理：
    - base_url 末尾的 /v1 会被自动移除，因为 ChatAnthropic 会自动追加
    
    使用示例：
        client = AnthropicClient(model="claude-3-5-sonnet-20241022")
        llm = client.get_llm()
    """

    def __init__(self, model: str, base_url: Optional[str] = None, **kwargs):
        """
        初始化 Anthropic 客户端
        
        参数:
            model: Claude 模型名称，如 "claude-3-5-sonnet-20241022"
            base_url: 可选的 API 基础 URL，用于自定义端点
            **kwargs: 其他配置参数，如 api_key, timeout, max_tokens 等
        """
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """
        获取配置好的 ChatAnthropic 实例
        
        返回:
            NormalizedChatAnthropic: 标准化后的 Claude LLM 实例
            
        支持的 kwargs 参数：
        - api_key: Anthropic API 密钥
        - timeout: 请求超时时间（秒）
        - max_retries: 最大重试次数
        - max_tokens: 最大输出 token 数
        - callbacks: 回调函数列表
        """
        # 构建基础参数
        llm_kwargs = {"model": self.model}

        # 处理 base_url：移除末尾的 /v1
        if self.base_url:
            base = self.base_url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            llm_kwargs["base_url"] = base

        # 传递允许的参数
        for key in ("timeout", "max_retries", "api_key", "max_tokens", "callbacks"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """
        验证模型名称是否有效
        
        返回:
            bool: 模型有效返回 True，否则 False
        """
        return validate_model("anthropic", self.model)
