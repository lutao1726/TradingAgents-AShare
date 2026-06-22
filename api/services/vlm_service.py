"""
通用视觉语言模型（VLM）服务模块。

核心功能：
1. 配置管理：从环境变量加载 VLM 配置
2. 图片识别：发送图片和文本提示到 VLM 并获取响应
3. 多厂商支持：支持 OpenAI 兼容接口和 Anthropic 接口

环境变量：
- TA_VLM_API_KEY: VLM API Key（必需）
- TA_VLM_BASE_URL: API 基础 URL（默认：https://open.bigmodel.cn/api/paas/v4）
- TA_VLM_MODEL: 模型名称（默认：glm-4.6v-flash）
- TA_VLM_PROVIDER: 提供商（openai/anthropic，默认：openai）
- TA_VLM_RAW_BASE64: 是否发送原始 base64（1=是，0=否，默认：1）

使用场景：
- 券商截图识别（持仓、自选股）
- 图片中的文本提取
- 图片内容分析
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def get_vlm_config() -> dict[str, str]:
    """从环境变量加载 VLM 配置。
    
    Returns:
        配置字典，包含：
        - provider: 提供商
        - api_key: API Key
        - base_url: API 基础 URL
        - model: 模型名称
    
    Raises:
        ValueError: 未配置 TA_VLM_API_KEY
    """
    api_key = os.getenv("TA_VLM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("未配置 VLM API Key（环境变量 TA_VLM_API_KEY）")
    return {
        "provider": os.getenv("TA_VLM_PROVIDER", "openai").strip(),
        "api_key": api_key,
        "base_url": os.getenv("TA_VLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").strip(),
        "model": os.getenv("TA_VLM_MODEL", "glm-4.6v-flash").strip(),
    }


def call_vlm(
    image_bytes: bytes,
    prompt: str,
    content_type: str = "image/png",
) -> str:
    """发送图片和文本提示到 VLM 并获取响应。
    
    Args:
        image_bytes: 图片字节数据
        prompt: 文本提示
        content_type: 图片 MIME 类型（默认：image/png）
    
    Returns:
        VLM 响应文本
    """
    config = get_vlm_config()
    provider = config.get("provider", "openai")
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    media_type = content_type or "image/png"

    # 根据提供商选择调用方式
    if provider == "anthropic":
        return _call_anthropic(base64_image, media_type, prompt, config)
    return _call_openai_compatible(base64_image, media_type, prompt, config)


def _call_openai_compatible(base64_image: str, media_type: str, prompt: str, config: dict) -> str:
    """调用 OpenAI 兼容接口的 VLM。
    
    Args:
        base64_image: Base64 编码的图片
        media_type: 图片 MIME 类型
        prompt: 文本提示
        config: VLM 配置
    
    Returns:
        VLM 响应文本
    """
    from openai import OpenAI
    client = OpenAI(
        api_key=config["api_key"],
        base_url=config.get("base_url") or None,
    )
    
    # 构建图片 URL（支持原始 base64 或 data URI 格式）
    raw_base64 = os.getenv("TA_VLM_RAW_BASE64", "1").strip() in ("1", "true", "yes")
    image_url = base64_image if raw_base64 else f"data:{media_type};base64,{base64_image}"

    # 调用 VLM
    response = client.chat.completions.create(
        model=config.get("model", "glm-4.6v-flash"),
        messages=[
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ],
        max_tokens=2000,
    )
    raw = response.choices[0].message.content or ""
    logger.info("[vlm] 响应（前 300 字符）: %s", raw[:300])
    return raw


def _call_anthropic(base64_image: str, media_type: str, prompt: str, config: dict) -> str:
    """调用 Anthropic 接口的 VLM。
    
    Args:
        base64_image: Base64 编码的图片
        media_type: 图片 MIME 类型
        prompt: 文本提示
        config: VLM 配置
    
    Returns:
        VLM 响应文本
    """
    import anthropic
    client = anthropic.Anthropic(api_key=config["api_key"])
    
    # 调用 VLM
    response = client.messages.create(
        model=config.get("model", "claude-sonnet-4-20250514"),
        max_tokens=2000,
        messages=[
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64_image}},
                {"type": "text", "text": prompt},
            ]},
        ],
    )
    raw = response.content[0].text if response.content else ""
    logger.info("[vlm] 响应（前 300 字符）: %s", raw[:300])
    return raw
