"""
OpenAI 兼容客户端模块：支持 OpenAI、Ollama、OpenRouter、xAI 等厂商。

核心组件：
- UnifiedChatOpenAI: ChatOpenAI 子类，处理模型特定的参数兼容性问题
- OpenAIClient: OpenAI 兼容厂商的客户端实现

支持的厂商：
- OpenAI: GPT-4o、GPT-4o-mini、o1、o3 等
- xAI: Grok 系列
- Ollama: 本地部署的开源模型
- OpenRouter: 模型路由服务

特殊处理：
- 推理模型（o1、o3、gpt-5 等）自动禁用 temperature
- Moonshot/Kimi 模型强制 temperature=1
- DEBUG 模式下打印完整的 LLM 请求和响应
"""
import logging
import os
import time
from json import JSONDecodeError
from typing import Any, Optional

from langchain_openai import ChatOpenAI

_logger = logging.getLogger(__name__)

from .base_client import BaseLLMClient
from .validators import validate_model


class UnifiedChatOpenAI(ChatOpenAI):
    """统一的 ChatOpenAI 子类，处理模型特定的参数兼容性问题。
    
    特性：
    - 推理模型（o1、o3、gpt-5 等）自动移除 temperature 和 top_p
    - Moonshot/Kimi 模型强制 temperature=1
    - DEBUG 模式下打印完整的 LLM 请求和响应
    - 移除 response_parse_retries 参数，统一由重试包装器控制
    """

    def __init__(self, **kwargs):
        # 移除重试参数，由重试包装器统一控制
        kwargs.pop("response_parse_retries", None)
        kwargs.pop("response_parse_retry_delay", None)

        model = kwargs.get("model") or kwargs.get("model_name", "")
        base_url = kwargs.get("base_url")

        # LOG_LEVEL=DEBUG 时开启 LangChain verbose，打印完整的 LLM 请求和响应
        if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
            kwargs["verbose"] = True

        # 1. 推理模型（o1 等）通常不支持 temperature 参数
        if self._is_reasoning_model(model):
            kwargs.pop("temperature", None)
            kwargs.pop("top_p", None)

        # 2. Moonshot (Kimi) 模型严格要求 temperature=1
        if self._is_moonshot_model(model, base_url):
            kwargs["temperature"] = 1

        super().__init__(**kwargs)

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """调用 LLM 并在 DEBUG 模式下记录响应。"""
        result = super().invoke(input=input, config=config, **kwargs)
        if _logger.isEnabledFor(logging.DEBUG):
            content = result.content if hasattr(result, "content") else str(result)
            _logger.debug(f"[LLM Response] model={self.model_name} length={len(content)}\n{content}")
        return result

    @staticmethod
    def _is_reasoning_model(model: str) -> bool:
        """检查是否为推理模型。
        
        推理模型包括：o1、o3、gpt-5、r1、thinking、reasoning 等系列。
        这些模型通常不支持 temperature 和 top_p 参数。
        """
        model_lower = str(model).lower()
        return (
            model_lower.startswith("o1")
            or model_lower.startswith("o3")
            or "gpt-5" in model_lower
            or "-r1" in model_lower
            or "thinking" in model_lower
            or "reasoning" in model_lower
        )

    @staticmethod
    def _is_moonshot_model(model: str, base_url: Optional[str] = None) -> bool:
        """检查是否为 Moonshot (Kimi) 模型。
        
        Moonshot 模型严格要求 temperature=1。
        """
        m = str(model).lower()
        b = (base_url or "").lower()
        return "moonshot" in m or "kimi" in m or "moonshot" in b or "kimi" in b


class OpenAIClient(BaseLLMClient):
    """OpenAI 兼容厂商的客户端实现。
    
    支持的厂商：
    - OpenAI: GPT-4o、GPT-4o-mini、o1、o3 等
    - xAI: Grok 系列
    - Ollama: 本地部署的开源模型
    - OpenRouter: 模型路由服务
    
    配置特性：
    - 禁用内置重试，由 RetryableLLM 包装器统一控制
    - 超长超时（默认 600 秒），给足推理模型思考时间
    """

    def __init__(
        self,
        model: str,
        base_url: Optional[str] = None,
        provider: str = "openai",
        **kwargs,
    ):
        """初始化 OpenAI 兼容客户端。
        
        Args:
            model: 模型名称（如 "gpt-4o"、"grok-4"）
            base_url: API 端点基础 URL（可选）
            provider: 厂商标识（openai/xai/ollama/openrouter）
            **kwargs: 其他配置参数
        """
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """返回配置好的 ChatOpenAI 实例。
        
        配置策略：
        - 推理模型不设置 temperature
        - 禁用内置重试（由 RetryableLLM 统一控制）
        - 超长超时（默认 600 秒）
        - 根据 provider 设置对应的 API 端点和密钥
        """
        llm_kwargs = {"model": self.model}

        # 非推理模型设置 temperature
        if not UnifiedChatOpenAI._is_reasoning_model(self.model):
            llm_kwargs["temperature"] = self.kwargs.get("temperature", 0)

        # ── 稳定性配置 ──
        # 1. 内置重试：通过环境变量 TA_LLM_MAX_RETRIES 配置（默认 2）
        # 推荐设置较小值（如 2），避免推理模型重复扣费
        # 如需更多重试，建议通过 RetryableLLM 包装器控制
        llm_kwargs["max_retries"] = int(os.getenv("TA_LLM_MAX_RETRIES", "2"))
        
        # 2. 超长超时：默认 600 秒，给足推理模型思考时间
        llm_kwargs["timeout"] = self.kwargs.get("timeout", 600.0)
        
        # 根据 provider 设置 API 端点
        target_url = self.base_url or "https://api.openai.com/v1"
        if self.provider == "xai": 
            target_url = "https://api.x.ai/v1"
        elif self.provider == "openrouter": 
            target_url = "https://openrouter.ai/api/v1"
        elif self.provider == "ollama": 
            target_url = "http://localhost:11434/v1"
        
        print(f"[LLM Client] Init {self.provider} ({self.model}) at {target_url} (Retries={llm_kwargs['max_retries']}, Timeout={llm_kwargs['timeout']}s)")

        # 根据 provider 设置 API 密钥
        if self.provider == "xai":
            llm_kwargs["base_url"] = "https://api.x.ai/v1"
            api_key = os.environ.get("XAI_API_KEY")
            if api_key: 
                llm_kwargs["api_key"] = api_key
        elif self.provider == "openrouter":
            llm_kwargs["base_url"] = "https://openrouter.ai/api/v1"
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if api_key: 
                llm_kwargs["api_key"] = api_key
        elif self.provider == "ollama":
            llm_kwargs["base_url"] = "http://localhost:11434/v1"
            llm_kwargs["api_key"] = "ollama"  # Ollama 不需要真实密钥
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # 传递其他参数
        for key in ("api_key", "callbacks", "reasoning_effort"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return UnifiedChatOpenAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """验证模型是否受支持。"""
        return validate_model(self.provider, self.model)
