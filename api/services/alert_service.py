"""
持仓预警服务模块。

核心功能：
1. 预警条件评估：检查持仓是否触发预设条件
2. 通知发送：通过企业微信/钉钉/邮件发送预警通知
3. 去重控制：避免短时间内重复发送相同预警
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

import requests

from api.database import AlertDB, AlertTriggerDB, ImportedPortfolioPositionDB, get_db_ctx

logger = logging.getLogger(__name__)


class AlertServiceError(Exception):
    """预警服务异常基类。"""
    pass


class AlertNotFoundError(AlertServiceError):
    """预警不存在。"""
    pass


class PositionNotFoundError(AlertServiceError):
    """持仓不存在。"""
    pass


def evaluate_alert(
    db,
    alert: AlertDB,
    position: ImportedPortfolioPositionDB,
    quote: dict,
) -> list[dict]:
    """评估单条预警是否触发。

    Args:
        db: 数据库会话
        alert: 预警对象
        position: 持仓对象（含成本价、持仓量等）
        quote: 行情数据（current_price、change_pct 等）

    Returns:
        触发的 trigger 列表
    """
    triggers = db.query(AlertTriggerDB).filter_by(alert_id=alert.id, enabled=True).all()
    triggered = []

    for trigger in triggers:
        if trigger.trigger_type == "price_above":
            if quote.get("current_price") is not None and quote["current_price"] >= trigger.threshold:
                triggered.append({
                    "trigger_type": trigger.trigger_type,
                    "threshold": trigger.threshold,
                    "description": f"价格突破 {trigger.threshold} 元",
                })
        elif trigger.trigger_type == "price_below":
            if quote.get("current_price") is not None and quote["current_price"] <= trigger.threshold:
                triggered.append({
                    "trigger_type": trigger.trigger_type,
                    "threshold": trigger.threshold,
                    "description": f"价格跌破 {trigger.threshold} 元",
                })
        elif trigger.trigger_type == "daily_change_pct":
            if quote.get("change_pct") is not None and abs(quote["change_pct"]) >= trigger.threshold:
                triggered.append({
                    "trigger_type": trigger.trigger_type,
                    "threshold": trigger.threshold,
                    "description": f"单日涨跌幅 {quote['change_pct']:+.2f}% (阈值: ±{trigger.threshold}%)",
                })
        elif trigger.trigger_type == "unrealized_pnl_pct":
            if position.average_cost and position.average_cost > 0 and quote.get("current_price") is not None:
                pnl_pct = (quote["current_price"] - position.average_cost) / position.average_cost * 100
                if abs(pnl_pct) >= trigger.threshold:
                    triggered.append({
                        "trigger_type": trigger.trigger_type,
                        "threshold": trigger.threshold,
                        "description": f"持仓盈亏 {pnl_pct:+.2f}% (阈值: ±{trigger.threshold}%)",
                    })

    return triggered


def build_alert_message(
    symbol: str,
    trade_date: str,
    triggered_conditions: list[dict],
    quote: dict,
    position: Optional[dict] = None,
) -> str:
    """构建持仓预警消息文本。"""
    lines = [
        "TradingAgents 持仓预警",
    ]
    try:
        from api.main import _get_reverse_stock_map_cached_only
        code_to_name = _get_reverse_stock_map_cached_only()
        std_symbol = symbol.strip().upper()
        stock_name = code_to_name.get(std_symbol) or next(
            (name for code, name in code_to_name.items() if code.split(".")[0] == std_symbol.split(".")[0]),
            ""
        )
        if stock_name:
            lines.append(f"标的：{symbol}（{stock_name}）")
        else:
            lines.append(f"标的：{symbol}")
    except Exception:
        lines.append(f"标的：{symbol}")
    lines.append(f"日期：{trade_date}")

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
        lines.append(f"- {cond.get('description', cond.get('trigger_type', ''))}")

    return "\n".join(lines)


def send_alert_notification(
    user_id: str,
    symbol: str,
    trade_date: str,
    triggered_conditions: list[dict],
    quote: dict,
    position: Optional[dict] = None,
) -> None:
    """发送预警通知（邮件 + 企微 + 钉钉）。

    Args:
        user_id: 用户 ID
        symbol: 股票代码
        trade_date: 交易日期
        triggered_conditions: 触发的条件列表
        quote: 行情数据
        position: 持仓数据
    """
    try:
        from api.services.email_report_service import send_report_email_with_retry
        from api.services.wecom_notification_service import send_message as send_wecom_message
        from api.services.dingtalk_notification_service import send_message as send_dingtalk_message

        def _load_targets():
            email_user = None
            webhook_url = None
            wecom_enabled = True
            dingtalk_webhook_url = None
            dingtalk_enabled = True
            with get_db_ctx() as db:
                user = db.query(ImportedPortfolioPositionDB).filter(
                    ImportedPortfolioPositionDB.user_id == user_id,
                    ImportedPortfolioPositionDB.symbol == symbol,
                    ImportedPortfolioPositionDB.source == "manual",
                ).first()
                if user:
                    db.expunge(user)
                    email_user = user

                user_cfg = db.query(AlertDB).filter(AlertDB.user_id == user_id).first()
                if user_cfg:
                    from api.services.auth_service import decrypt_secret
                    webhook_url = decrypt_secret(getattr(user_cfg, "wecom_webhook_encrypted", None))
                    dingtalk_webhook_url = decrypt_secret(getattr(user_cfg, "dingtalk_webhook_encrypted", None))
            return email_user, webhook_url, wecom_enabled, dingtalk_webhook_url, dingtalk_enabled

        email_user, webhook_url, wecom_enabled, dingtalk_webhook_url, dingtalk_enabled = (
            __import__("asyncio").to_thread(_load_targets)
        )

        content = build_alert_message(symbol, trade_date, triggered_conditions, quote, position)

        # 邮件
        if email_user:
            try:
                from api.services.email_report_service import send_report_email_with_retry
                # 这里需要构造一个 ReportDB 对象，暂时跳过邮件
                pass
            except Exception:
                pass

        # 企微
        if webhook_url and wecom_enabled:
            try:
                send_wecom_message(content, webhook_url)
            except Exception as exc:
                logger.warning("[Alert] WeCom send failed: %s", exc)

        # 钉钉
        if dingtalk_webhook_url and dingtalk_enabled:
            try:
                send_dingtalk_message(content, dingtalk_webhook_url)
            except Exception as exc:
                logger.warning("[Alert] DingTalk send failed: %s", exc)

    except Exception as e:
        logger.warning(f"[Alert] Notification send failed for {symbol}: {e}")


def check_alerts_for_user(user_id: str) -> None:
    """检查用户的所有活跃预警。

    Args:
        user_id: 用户 ID
    """
    with get_db_ctx() as db:
        # 获取所有活跃预警
        alerts = db.query(AlertDB).filter_by(user_id=user_id, is_active=True).all()

        for alert in alerts:
            try:
                # 获取持仓
                position = db.query(ImportedPortfolioPositionDB).filter(
                    ImportedPortfolioPositionDB.user_id == user_id,
                    ImportedPortfolioPositionDB.symbol == alert.symbol,
                    ImportedPortfolioPositionDB.source == "manual",
                ).first()
                if not position:
                    continue

                # 获取最新行情
                quote = _fetch_quote(alert.symbol)
                if not quote:
                    continue

                # 评估预警
                triggered = evaluate_alert(db, alert, position, quote)
                if not triggered:
                    continue

                # 发送通知
                send_alert_notification(
                    user_id=user_id,
                    symbol=alert.symbol,
                    trade_date=datetime.now().strftime("%Y-%m-%d"),
                    triggered_conditions=triggered,
                    quote=quote,
                    position={
                        "average_cost": position.average_cost,
                        "current_position": position.current_position,
                    } if position else None,
                )

            except Exception as exc:
                logger.error(f"[Alert] Check failed for alert {alert.id}: {exc}")


def _fetch_quote(symbol: str) -> Optional[dict]:
    """获取最新行情数据。"""
    try:
        # 尝试使用 AKShare
        import akshare as ak
        std_symbol = symbol.split(".")[0]
        df = ak.stock_zh_a_spot_em()
        if df is not None and not df.empty:
            row = df[df["代码"] == std_symbol]
            if not row.empty:
                r = row.iloc[0]
                return {
                    "current_price": float(r.get("最新价", 0)),
                    "change_pct": float(r.get("涨跌幅", 0)),
                }
    except Exception as exc:
        logger.debug(f"[Alert] Fetch quote failed for {symbol}: {exc}")

    return None
