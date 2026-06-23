"""
企业微信通知服务模块：发送分析报告到企业微信群机器人。

核心功能：
1. 消息构建：构建研报摘要消息和测试消息
2. Webhook 验证：验证企业微信 Webhook URL 格式
3. 消息发送：通过企业微信 Webhook 发送消息
4. 异步重试：异步发送消息，失败后自动重试

企业微信 Webhook 格式：
- 纯 Key：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
- 完整 URL：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx

消息格式：
- 文本消息（msgtype: text）
- 最大长度：1800 字符
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urlparse

import requests

if TYPE_CHECKING:
    from api.database import ReportDB

logger = logging.getLogger(__name__)

# 企业微信 Webhook 主机
_WECOM_WEBHOOK_HOST = "qyapi.weixin.qq.com"

# 企业微信 Webhook 路径
_WECOM_WEBHOOK_PATH = "/cgi-bin/webhook/send"


def _clip_text(text: str | None, limit: int = 720) -> str:
    """截断文本（去除多余空白，限制长度）。"""
    if not text:
        return ""
    compact = " ".join(str(text).split()).strip()
    return compact[:limit]


def build_report_message(report: "ReportDB") -> str:
    """构建研报摘要消息。
    
    消息内容：
    - 标题：TradingAgents 定时分析完成
    - 标的：股票代码
    - 交易日：分析日期
    - 决策：交易决策
    - 方向：分析方向
    - 置信度：置信度百分比
    - 摘要：最终交易决策/交易员方案/投资计划
    
    Args:
        report: 研报对象
    
    Returns:
        消息文本（最大 1800 字符）
    """
    lines = [
        "TradingAgents 定时分析完成",
    ]
    try:
        from api.main import _get_reverse_stock_map_cached_only
        code_to_name = _get_reverse_stock_map_cached_only()
        std_symbol = report.symbol.strip().upper()
        stock_name = code_to_name.get(std_symbol) or next(
            (name for code, name in code_to_name.items() if code.split(".")[0] == std_symbol.split(".")[0]),
            ""
        )
        if stock_name:
            lines.append(f"标的：{report.symbol}（{stock_name}）")
        else:
            lines.append(f"标的：{report.symbol}")
    except Exception:
        lines.append(f"标的：{report.symbol}")
    lines.append(f"交易日：{report.trade_date}")
    
    # 添加决策信息
    if getattr(report, "decision", None):
        lines.append(f"决策：{report.decision}")
    if getattr(report, "direction", None):
        lines.append(f"方向：{report.direction}")
    if getattr(report, "confidence", None) is not None:
        lines.append(f"置信度：{report.confidence}%")

    # 添加摘要（优先级：最终交易决策 > 交易员方案 > 投资计划）
    summary = (
        _clip_text(getattr(report, "final_trade_decision", None), 900)
        or _clip_text(getattr(report, "trader_investment_plan", None), 900)
        or _clip_text(getattr(report, "investment_plan", None), 900)
    )
    if summary:
        lines.append("")
        lines.append("摘要：")
        lines.append(summary)
    
    return "\n".join(lines)[:1800]


def build_test_message(content: str | None = None) -> str:
    """构建测试消息。
    
    Args:
        content: 自定义内容（可选）
    
    Returns:
        测试消息文本
    """
    custom = " ".join(str(content or "").split()).strip()
    message = custom or "TradingAgents Webhook Warmup\n这是一条企业微信机器人测试消息。"
    return message[:1800]


def normalize_webhook_url(webhook_url: str) -> str:
    """标准化企业微信 Webhook URL。
    
    支持格式：
    - 纯 Key：xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    - 完整 URL：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
    
    Args:
        webhook_url: Webhook URL 或 Key
    
    Returns:
        标准化后的完整 URL
    
    Raises:
        ValueError: URL 格式不正确
    """
    normalized = str(webhook_url or "").strip()
    if not normalized:
        raise ValueError("企业微信 Webhook 不能为空")

    # 纯 Key 格式
    if not normalized.startswith("http"):
        if not all(char.isalnum() or char == "-" for char in normalized):
            raise ValueError("企业微信 Webhook key 格式不正确")
        return f"https://{_WECOM_WEBHOOK_HOST}{_WECOM_WEBHOOK_PATH}?key={normalized}"

    # 完整 URL 格式
    parsed = urlparse(normalized)
    
    # 验证协议
    if parsed.scheme != "https":
        raise ValueError("企业微信 Webhook 必须使用 HTTPS")
    
    # 验证主机和路径
    if parsed.netloc != _WECOM_WEBHOOK_HOST or parsed.path != _WECOM_WEBHOOK_PATH:
        raise ValueError("仅支持企业微信机器人的官方 Webhook 地址")
    
    # 验证无额外参数
    if parsed.params or parsed.fragment:
        raise ValueError("企业微信 Webhook 地址格式不正确")

    # 验证查询参数
    query = parse_qs(parsed.query, keep_blank_values=False)
    if set(query.keys()) != {"key"}:
        raise ValueError("企业微信 Webhook 地址必须仅包含 key 参数")
    
    keys = query.get("key") or []
    if len(keys) != 1:
        raise ValueError("企业微信 Webhook 地址格式不正确")
    
    # 验证 Key 格式
    key = keys[0].strip()
    if not key or not all(char.isalnum() or char == "-" for char in key):
        raise ValueError("企业微信 Webhook key 格式不正确")

    return f"https://{_WECOM_WEBHOOK_HOST}{_WECOM_WEBHOOK_PATH}?{urlencode({'key': key})}"


def send_message(content: str, webhook_url: str) -> bool:
    """通过企业微信 Webhook 发送消息。
    
    Args:
        content: 消息内容
        webhook_url: Webhook URL
    
    Returns:
        是否发送成功
    """
    if not webhook_url:
        return False
    
    # 构建请求体
    payload = {
        "msgtype": "text",
        "text": {"content": content},
    }
    
    # 标准化 URL
    url = normalize_webhook_url(webhook_url)
    
    # 发送请求
    response = requests.post(
        url,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json;charset=utf-8"},
        timeout=10,
    )
    response.raise_for_status()
    
    # 解析响应
    try:
        body = response.json()
    except Exception:
        logger.warning(
            "[wecom] 非 JSON 响应 body=%s",
            _clip_text(getattr(response, "text", None), 240),
        )
        return False
    
    return int(body.get("errcode", -1)) == 0


async def send_report_message_with_retry(report: "ReportDB", webhook_url: str) -> bool:
    """异步发送研报消息，失败后自动重试。
    
    流程：
    1. 构建研报摘要消息
    2. 第一次尝试发送
    3. 失败后等待 15 秒
    4. 第二次尝试发送
    
    Args:
        report: 研报对象
        webhook_url: Webhook URL
    
    Returns:
        是否发送成功
    """
    content = build_report_message(report)
    
    # 第一次尝试
    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[wecom] 发送成功 %s", report.symbol)
            return True
    except Exception as exc:
        logger.warning("[wecom] 第一次发送失败 %s: %s", report.symbol, exc)

    # 等待 15 秒后重试
    await asyncio.sleep(15)
    
    # 第二次尝试
    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[wecom] 重试发送成功 %s", report.symbol)
            return True
    except Exception as exc:
        logger.error("[wecom] 重试也失败 %s: %s", report.symbol, exc)
    
    return False
