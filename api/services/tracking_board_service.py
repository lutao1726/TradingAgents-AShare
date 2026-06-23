"""
跟踪看板服务模块：提供持仓跟踪看板的数据聚合。

核心功能：
1. 实时行情获取：通过数据源获取股票实时价格
2. 浮动盈亏计算：基于实时价格计算浮动盈亏和盈亏比例
3. 研报摘要：关联最新研报，提取交易建议摘要
4. 数据聚合：将持仓、行情、研报数据聚合为看板数据

数据流：
1. 获取用户的导入持仓
2. 获取实时行情数据
3. 获取关联的研报数据
4. 计算浮动盈亏和盈亏比例
5. 提取研报摘要
6. 返回聚合后的看板数据
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB, ReportDB
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.dataflows.trade_calendar import cn_today_str, previous_cn_trading_day


# 跟踪看板自动刷新间隔（秒）
REFRESH_INTERVAL_SECONDS = 20

logger = logging.getLogger(__name__)


def get_tracking_board(db: Session, user_id: str) -> dict[str, Any]:
    """获取跟踪看板数据。
    
    流程：
    1. 获取用户的导入持仓
    2. 获取实时行情数据
    3. 获取关联的研报数据
    4. 计算浮动盈亏和盈亏比例
    5. 提取研报摘要
    6. 返回聚合后的看板数据
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        看板数据字典，包含：
        - previous_trade_date: 上一个交易日
        - refresh_interval_seconds: 刷新间隔
        - items: 持仓列表（含行情、盈亏、研报摘要）
    """
    previous_trade_date = previous_cn_trading_day(cn_today_str())
    
    # 获取用户的导入持仓
    rows = _list_imported_position_rows(db, user_id)
    symbols = [row.symbol for row in rows]
    
    # 获取实时行情
    quotes = _fetch_live_quotes(symbols)
    
    # 获取关联的研报
    reports = _select_reports_for_symbols(db, user_id, symbols, previous_trade_date)

    items: list[dict[str, Any]] = []
    for row in rows:
        quote = quotes.get(row.symbol, {})
        live_price = _to_float(quote.get("price"))
        current_position = _to_float(row.current_position)
        average_cost = _to_float(row.average_cost)
        
        # 计算实时市值
        live_market_value = (
            round(live_price * current_position, 2)
            if live_price is not None and current_position is not None
            else _to_float(row.market_value)
        )
        
        # 计算浮动盈亏
        floating_pnl = (
            round((live_price - average_cost) * current_position, 2)
            if live_price is not None and average_cost is not None and current_position is not None
            else None
        )
        
        # 计算浮动盈亏比例
        floating_pnl_pct = (
            round(((live_price - average_cost) / average_cost) * 100, 2)
            if live_price is not None and average_cost not in (None, 0)
            else None
        )

        report = reports.get(row.symbol)
        analysis = _serialize_report_summary(report, previous_trade_date)
        if analysis is not None:
            analysis.update(_compare_position_with_analysis(current_position, report))

        items.append(
            {
                "symbol": row.symbol,
                "name": row.security_name or row.symbol,
                "current_position": _to_float(row.current_position),
                "available_position": _to_float(row.available_position),
                "average_cost": average_cost,
                "market_value": _to_float(row.market_value),
                "current_position_pct": _to_float(row.current_position_pct),
                "live_market_value": live_market_value,
                "floating_pnl": floating_pnl,
                "floating_pnl_pct": floating_pnl_pct,
                "live_price": live_price,
                "day_open": _to_float(quote.get("open")),
                "price_change": _to_float(quote.get("change")),
                "price_change_pct": _to_float(quote.get("change_pct")),
                "day_high": _to_float(quote.get("high")),
                "day_low": _to_float(quote.get("low")),
                "previous_close": _to_float(quote.get("previous_close")),
                "volume": _to_float(quote.get("volume")),
                "amount": _to_float(quote.get("amount")),
                "quote_time": quote.get("quote_time"),
                "quote_source": quote.get("source"),
                "last_imported_at": row.last_imported_at.isoformat() if row.last_imported_at else None,
                "analysis": analysis,
            }
        )

    return {
        "previous_trade_date": previous_trade_date,
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "items": items,
    }


def _list_imported_position_rows(db: Session, user_id: str) -> list[ImportedPortfolioPositionDB]:
    """获取用户的所有导入持仓（按市值降序）。"""
    return (
        db.query(ImportedPortfolioPositionDB)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .order_by(
            ImportedPortfolioPositionDB.market_value.desc(),
            ImportedPortfolioPositionDB.current_position.desc(),
            ImportedPortfolioPositionDB.symbol,
        )
        .all()
    )


def _select_reports_for_symbols(
    db: Session,
    user_id: str,
    symbols: list[str],
    previous_trade_date: str,
) -> dict[str, ReportDB]:
    """为每个标的选择最合适的研报。
    
    选择优先级：
    1. 上一个交易日的研报（精确匹配）
    2. 上一个交易日之前的最新研报
    3. 任意最新研报
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbols: 股票代码列表
        previous_trade_date: 上一个交易日
    
    Returns:
        股票代码到研报的映射字典
    """
    if not symbols:
        return {}

    # 查询所有已完成的研报
    rows = (
        db.query(ReportDB)
        .filter(
            ReportDB.user_id == user_id,
            ReportDB.symbol.in_(symbols),
            ReportDB.status == "completed",
        )
        .order_by(ReportDB.trade_date.desc(), ReportDB.created_at.desc())
        .all()
    )

    # 按优先级分类研报
    exact_previous: dict[str, ReportDB] = {}  # 精确匹配上一个交易日
    latest_before_previous: dict[str, ReportDB] = {}  # 上一个交易日之前的最新
    latest_any: dict[str, ReportDB] = {}  # 任意最新

    for row in rows:
        if row.symbol not in latest_any:
            latest_any[row.symbol] = row
        if row.trade_date == previous_trade_date and row.symbol not in exact_previous:
            exact_previous[row.symbol] = row
        if row.trade_date <= previous_trade_date and row.symbol not in latest_before_previous:
            latest_before_previous[row.symbol] = row

    # 按优先级选择研报
    selected: dict[str, ReportDB] = {}
    for symbol in symbols:
        report = exact_previous.get(symbol) or latest_before_previous.get(symbol) or latest_any.get(symbol)
        if report:
            selected[symbol] = report
    
    return selected


def _compare_position_with_analysis(current_position: float | None, report: ReportDB | None) -> dict[str, Any]:
    """对比当前持仓状态与最新分析建议。
    
    Args:
        current_position: 当前持仓数量
        report: 最新研报对象
    
    Returns:
        对比结果字典，包含 comparison / suggested_action / urgency / comparison_note
    """
    if report is None:
        return {
            "comparison": "neutral",
            "suggested_action": "hold",
            "urgency": "low",
            "comparison_note": None,
        }

    direction = (report.direction or "").upper()
    has_position = (current_position or 0) > 0

    buy_signals = {"BUY", "看多", "多", "BULLISH", "LEAN_BULLISH", "偏多"}
    sell_signals = {"SELL", "看空", "空", "BEARISH", "LEAN_BEARISH", "偏空"}

    if direction in sell_signals and has_position:
        return {
            "comparison": "mismatch_sell_but_holding",
            "suggested_action": "reduce",
            "urgency": "high",
            "comparison_note": "建议卖出，当前仍持有",
        }
    if direction in buy_signals and not has_position:
        return {
            "comparison": "mismatch_buy_but_missing",
            "suggested_action": "add",
            "urgency": "medium",
            "comparison_note": "建议买入，当前未持有",
        }
    if direction in sell_signals and not has_position:
        return {
            "comparison": "match",
            "suggested_action": "exit",
            "urgency": "low",
            "comparison_note": "建议空仓，当前未持有",
        }
    if direction in buy_signals and has_position:
        return {
            "comparison": "match",
            "suggested_action": "hold",
            "urgency": "low",
            "comparison_note": "建议持有，当前已持有",
        }

    return {
        "comparison": "neutral",
        "suggested_action": "hold",
        "urgency": "low",
        "comparison_note": None,
    }


def _serialize_report_summary(report: ReportDB | None, previous_trade_date: str) -> dict[str, Any] | None:
    """序列化研报摘要。
    
    Args:
        report: 研报对象
        previous_trade_date: 上一个交易日
    
    Returns:
        研报摘要字典，无研报返回 None
    """
    if report is None:
        return None

    return {
        "report_id": report.id,
        "trade_date": report.trade_date,
        "is_previous_trade_day": report.trade_date == previous_trade_date,
        "decision": report.decision,
        "direction": report.direction,
        "high_price": _to_float(report.target_price),
        "low_price": _to_float(report.stop_loss_price),
        "trader_advice_summary": _summarize_trader_advice(
            report.trader_investment_plan,
            fallback_text=report.final_trade_decision,
        ),
        "trader_investment_plan": report.trader_investment_plan,
        "final_trade_decision": report.final_trade_decision,
    }


def _summarize_trader_advice(text: str | None, fallback_text: str | None = None) -> str | None:
    """提取交易建议摘要。
    
    优先从文本中匹配关键字段：
    - 最终交易建议
    - 结论
    - 建议动作
    - 方向
    
    匹配失败时，提取第一行有意义的文本。
    
    Args:
        text: 主文本（交易员方案）
        fallback_text: 备用文本（最终交易决策）
    
    Returns:
        摘要文本，提取失败返回 None
    """
    for source in (text, fallback_text):
        if not source:
            continue

        # 尝试匹配关键字段
        for pattern in (
            r"最终交易建议[:：]\s*([^\n]+)",
            r"结论[:：]\s*([^\n]+)",
            r"建议动作[:：]\s*([^\n]+)",
            r"方向[:：]\s*([^\n]+)",
        ):
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                return _clip_summary(match.group(1))

        # 回退：提取第一行有意义的文本
        lines = [
            _clip_summary(line.strip(" -*\t"))
            for line in _strip_markdown(source).splitlines()
            if line.strip()
        ]
        for line in lines:
            if len(line) >= 6 and not re.match(r"^[一二三四五六七八九十0-9]+[、.)：:]?$", line):
                return line
    
    return None


def _strip_markdown(text: str) -> str:
    """去除 Markdown 格式。"""
    cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    cleaned = cleaned.replace("\r", "\n")
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*|__", "", cleaned)
    cleaned = re.sub(r"^\s*#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned)
    return cleaned


def _clip_summary(text: str | None) -> str | None:
    """截断摘要文本（最多 96 字符）。"""
    if text is None:
        return None
    compact = re.sub(r"\s+", " ", text).strip(" ，,;；。")
    if not compact:
        return None
    return compact[:96]


def _fetch_live_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """获取实时行情数据。
    
    Args:
        symbols: 股票代码列表
    
    Returns:
        股票代码到行情数据的映射字典
    """
    if not symbols:
        return {}
    try:
        result_json = route_to_vendor("get_realtime_quotes", symbols)
        return json.loads(result_json)
    except Exception as exc:
        logger.warning("[tracking-board] 实时行情获取失败: %s", exc)
        return {}


def _to_float(value: Any) -> float | None:
    """安全转换为浮点数（保留 4 位小数）。"""
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except Exception:
        return None
