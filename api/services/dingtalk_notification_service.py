"""
钉钉自定义机器人通知服务模块。

协议参考：
https://open.dingtalk.com/document/orgapp/custom-robots-send-group-messages

核心功能：
1. 消息构建：构建研报摘要消息和预警消息
2. Webhook 校验：验证 access_token / URL 格式
3. 消息发送：通过钉钉 Webhook 发送文本消息
4. 异步重试：失败后自动重试一次

MVP 仅支持 keyword 安全模式（无需签名）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

if TYPE_CHECKING:
    from api.database import ReportDB

logger = logging.getLogger(__name__)

_DINGTALK_HOST = "oapi.dingtalk.com"
_DINGTALK_PATH = "/robot/send"


def _clip_text(text: str | None, limit: int = 1800) -> str:
    """截断文本（去除多余空白，限制长度）。"""
    if not text:
        return ""
    compact = " ".join(str(text).split()).strip()
    return compact[:limit]


def normalize_webhook_url(webhook_url: str) -> str:
    """标准化钉钉 Webhook URL。

    支持格式：
    - 纯 access_token：32 位以上字母数字
    - 完整 URL：https://oapi.dingtalk.com/robot/send?access_token=xxx

    Raises:
        ValueError: URL 格式不正确
    """
    normalized = str(webhook_url or "").strip()
    if not normalized:
        raise ValueError("钉钉 Webhook 不能为空")

    # 纯 access_token 格式
    if not normalized.startswith("http"):
        if not normalized.isalnum() or len(normalized) < 32:
            raise ValueError("钉钉 access_token 格式不正确，应为 32 位以上字母数字")
        return f"https://{_DINGTALK_HOST}{_DINGTALK_PATH}?access_token={normalized}"

    # 完整 URL 格式
    parsed = urlparse(normalized)

    if parsed.scheme != "https":
        raise ValueError("钉钉 Webhook 必须使用 HTTPS")

    if parsed.netloc != _DINGTALK_HOST or parsed.path != _DINGTALK_PATH:
        raise ValueError("仅支持钉钉自定义机器人的官方 Webhook 地址")

    if parsed.params or parsed.fragment:
        raise ValueError("钉钉 Webhook 地址格式不正确")

    query = parse_qs(parsed.query, keep_blank_values=False)
    tokens = query.get("access_token") or []
    if len(tokens) != 1 or not tokens[0].strip().isalnum():
        raise ValueError("钉钉 Webhook URL 必须包含有效的 access_token 参数")

    return f"https://{_DINGTALK_HOST}{_DINGTALK_PATH}?access_token={tokens[0].strip()}"


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
        f"标的：{report.symbol}",
        f"交易日：{report.trade_date}",
    ]

    if getattr(report, "decision", None):
        lines.append(f"决策：{report.decision}")
    if getattr(report, "direction", None):
        lines.append(f"方向：{report.direction}")
    if getattr(report, "confidence", None) is not None:
        lines.append(f"置信度：{report.confidence}%")

    summary = (
        _clip_text(getattr(report, "final_trade_decision", None), 1000)
        or _clip_text(getattr(report, "trader_investment_plan", None), 1000)
        or _clip_text(getattr(report, "investment_plan", None), 1000)
    )
    if summary:
        lines.append("")
        lines.append("摘要：")
        lines.append(summary)

    return "\n".join(lines)[:1800]


def build_test_message(content: str | None = None) -> str:
    """构建测试消息（用于 warmup）。"""
    base = (content or "").strip()
    if base:
        return f"[TradingAgents 连接测试] {base}"
    return "TradingAgents 钉钉连接测试成功"


def build_alert_message(
    symbol: str,
    trade_date: str,
    triggered_conditions: list[dict],
    quote: dict,
    position: Optional[dict] = None,
) -> str:
    """构建持仓预警消息。

    Args:
        symbol: 股票代码
        trade_date: 交易日期
        triggered_conditions: 触发的条件列表
        quote: 行情数据（current_price、change_pct 等）
        position: 持仓数据（average_cost、current_position 等）

    Returns:
        消息文本
    """
    lines = [
        "TradingAgents 持仓预警",
        f"标的：{symbol}",
        f"日期：{trade_date}",
    ]

    if quote.get("current_price"):
        lines.append(f"当前价：{quote['current_price']}")
    if quote.get("change_pct") is not None:
        lines.append(f"今日涨跌：{quote['change_pct']:+.2f}%")

    if position:
        if position.get("average_cost") and position.get("current_position"):
            pnl_pct = (
                (quote.get("current_price", 0) - position["average_cost"])
                / position["average_cost"]
                * 100
            )
            lines.append(f"持仓盈亏：{pnl_pct:+.2f}%")

    lines.append("")
    lines.append("触发条件：")
    for cond in triggered_conditions:
        desc = cond.get("description") or cond.get("trigger_type", "")
        lines.append(f"- {desc}")

    return "\n".join(lines)


def send_message(content: str, webhook_url: str) -> bool:
    """通过钉钉 Webhook 发送文本消息。

    Args:
        content: 消息内容
        webhook_url: Webhook URL 或 access_token

    Returns:
        是否发送成功
    """
    if not webhook_url:
        return False

    url = normalize_webhook_url(webhook_url)
    payload = {
        "msgtype": "text",
        "text": {"content": content},
    }

    try:
        response = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        # 钉钉成功返回 errcode == 0
        return int(body.get("errcode", -1)) == 0
    except Exception as exc:
        logger.warning("[dingtalk] send_message failed: %s", exc)
        return False


async def send_report_message_with_retry(report: "ReportDB", webhook_url: str) -> bool:
    """异步发送研报消息，失败后重试一次。

    Args:
        report: 研报对象
        webhook_url: Webhook URL

    Returns:
        是否发送成功
    """
    content = build_report_message(report)

    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[dingtalk] 发送成功 %s", report.symbol)
            return True
    except Exception as exc:
        logger.warning("[dingtalk] 第一次发送失败 %s: %s", report.symbol, exc)

    await asyncio.sleep(15)

    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[dingtalk] 重试发送成功 %s", report.symbol)
            return True
    except Exception as exc:
        logger.error("[dingtalk] 重试也失败 %s: %s", report.symbol, exc)

    return False


async def send_alert_message_with_retry(
    symbol: str,
    trade_date: str,
    triggered_conditions: list[dict],
    quote: dict,
    webhook_url: str,
    position: Optional[dict] = None,
) -> bool:
    """异步发送预警消息，失败后重试一次。

    Args:
        symbol: 股票代码
        trade_date: 交易日期
        triggered_conditions: 触发的条件列表
        quote: 行情数据
        webhook_url: Webhook URL
        position: 持仓数据

    Returns:
        是否发送成功
    """
    content = build_alert_message(symbol, trade_date, triggered_conditions, quote, position)

    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[dingtalk] 预警发送成功 %s", symbol)
            return True
    except Exception as exc:
        logger.warning("[dingtalk] 预警第一次发送失败 %s: %s", symbol, exc)

    await asyncio.sleep(15)

    try:
        ok = await asyncio.to_thread(send_message, content, webhook_url)
        if ok:
            logger.info("[dingtalk] 预警重试发送成功 %s", symbol)
            return True
    except Exception as exc:
        logger.error("[dingtalk] 预警重试也失败 %s: %s", symbol, exc)

    return False
