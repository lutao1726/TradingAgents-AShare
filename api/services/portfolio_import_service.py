"""
持仓导入服务模块：管理导入的持仓快照。

核心功能：
1. 持仓同步：全量替换持仓快照
2. 持仓追加：增量添加新标的（不清除旧标的）
3. 持仓删除：删除单个持仓
4. 持仓清空：清空所有持仓
5. 股票代码标准化：自动识别沪深北交所、基金、可转债等代码
6. 定时任务联动：导入持仓时自动创建定时分析任务

数据模型：
- ImportedPortfolioPositionDB：导入持仓数据库模型

使用场景：
- 手动输入持仓
- 截图识别持仓
- CSV 导入持仓
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import ImportedPortfolioPositionDB
from api.services import scheduled_service
from tradingagents.agents.utils.context_utils import normalize_user_context


logger = logging.getLogger(__name__)

# 股票代码正则：匹配 600519.SH 格式
_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def sync_positions(
    db: Session,
    user_id: str,
    positions: list[dict[str, Any]],
    source: str = "manual",
    auto_apply_scheduled: bool = True,
) -> dict[str, Any]:
    """全量替换持仓快照。
    
    流程：
    1. 标准化并去重股票代码
    2. 计算持仓占比（如果未提供）
    3. 删除该来源的旧持仓
    4. 插入新持仓
    5. 自动创建定时分析任务（可选）
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        positions: 持仓列表，每项至少包含 symbol（如 "600519.SH"）
                   可选字段：name, current_position, available_position,
                   average_cost, market_value, current_position_pct
        source: 导入来源（manual/image/csv）
        auto_apply_scheduled: 是否自动创建定时任务
    
    Returns:
        导入状态字典
    
    Raises:
        ValueError: positions 不是列表或无有效记录
    """
    if not isinstance(positions, list):
        raise ValueError("positions 必须为列表")

    source = (source or "manual").strip()
    now = datetime.now(timezone.utc)

    # 标准化并去重
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in positions:
        symbol = _normalize_code(raw.get("symbol"))
        if symbol is None:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        cleaned.append({
            "symbol": symbol,
            "name": (raw.get("name") or "").strip() or None,
            "current_position": _to_float(raw.get("current_position")),
            "available_position": _to_float(raw.get("available_position")),
            "average_cost": _to_float(raw.get("average_cost")),
            "market_value": _to_float(raw.get("market_value")),
            "current_position_pct": _to_float(raw.get("current_position_pct")),
        })

    # 计算持仓占比（如果未提供但有市值数据）
    total_mv = sum(p["market_value"] or 0 for p in cleaned if (p["market_value"] or 0) > 0)
    if total_mv > 0:
        for p in cleaned:
            if p["current_position_pct"] is None and p["market_value"] and p["market_value"] > 0:
                p["current_position_pct"] = round((p["market_value"] / total_mv) * 100, 4)

    if not cleaned:
        raise ValueError("没有有效的持仓记录，请检查输入格式")

    # 删除该来源的旧持仓
    db.query(ImportedPortfolioPositionDB).filter(
        ImportedPortfolioPositionDB.user_id == user_id,
        ImportedPortfolioPositionDB.source == source,
    ).delete()

    # 插入新持仓
    for p in cleaned:
        db.add(ImportedPortfolioPositionDB(
            id=uuid4().hex,
            user_id=user_id,
            source=source,
            symbol=p["symbol"],
            security_name=p["name"],
            current_position=p["current_position"],
            available_position=p["available_position"],
            average_cost=p["average_cost"],
            market_value=p["market_value"],
            current_position_pct=p["current_position_pct"],
            trade_points_json=[],
            trade_points_count=0,
            latest_trade_at=None,
            latest_trade_action=None,
            last_imported_at=now,
        ))

    # 自动创建定时分析任务
    scheduled_sync: dict[str, list] = {"created": [], "existing": [], "skipped_limit": []}
    if auto_apply_scheduled:
        ordered = [p["symbol"] for p in cleaned if (p["current_position"] or 0) > 0]
        scheduled_sync = scheduled_service.ensure_scheduled_for_symbols(
            db=db,
            user_id=user_id,
            symbols=ordered,
        )

    db.commit()
    return get_import_state(db, user_id, scheduled_sync=scheduled_sync)


def get_import_state(
    db: Session,
    user_id: str,
    scheduled_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """获取导入状态。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        scheduled_sync: 定时任务同步结果（可选）
    
    Returns:
        导入状态字典
    """
    positions = list_imported_positions(db, user_id)
    return {
        "auto_apply_scheduled": True,
        "last_synced_at": _latest_imported_at(positions),
        "last_error": None,
        "summary": {"positions": len(positions)},
        "scheduled_sync": scheduled_sync or {"created": [], "existing": [], "skipped_limit": []},
        "positions": positions,
    }


def list_imported_positions(db: Session, user_id: str) -> list[dict[str, Any]]:
    """获取用户的所有导入持仓（按市值降序）。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        持仓字典列表
    """
    rows = (
        db.query(ImportedPortfolioPositionDB)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .order_by(
            ImportedPortfolioPositionDB.market_value.desc(),
            ImportedPortfolioPositionDB.current_position.desc(),
            ImportedPortfolioPositionDB.symbol,
        )
        .all()
    )
    return [
        {
            "symbol": row.symbol,
            "name": row.security_name or row.symbol,
            "source": row.source,
            "current_position": row.current_position,
            "available_position": row.available_position,
            "average_cost": row.average_cost,
            "market_value": row.market_value,
            "current_position_pct": row.current_position_pct,
            "trade_points_count": row.trade_points_count or 0,
            "last_imported_at": row.last_imported_at.isoformat() if row.last_imported_at else None,
        }
        for row in rows
    ]


def build_scheduled_user_context(db: Session, user_id: str, symbol: str) -> dict[str, Any]:
    """为定时分析构建用户上下文。
    
    从导入的持仓数据中提取用户的持仓信息，用于分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbol: 股票代码
    
    Returns:
        用户上下文字典
    """
    row = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.symbol == (symbol or "").strip().upper(),
        )
        .first()
    )
    if not row:
        return {}

    payload: dict[str, Any] = {
        "objective": "持有处理" if (row.current_position or 0) > 0 else "观察",
        "current_position": row.current_position,
        "current_position_pct": row.current_position_pct,
        "average_cost": row.average_cost,
        "user_notes": f"来源：持仓导入（{row.source}）",
    }
    return normalize_user_context(payload)


def delete_position(db: Session, user_id: str, symbol: str) -> bool:
    """删除单个导入持仓。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbol: 股票代码
    
    Returns:
        是否删除成功
    """
    normalized = _normalize_code(symbol)
    if not normalized:
        return False
    deleted = (
        db.query(ImportedPortfolioPositionDB)
        .filter(
            ImportedPortfolioPositionDB.user_id == user_id,
            ImportedPortfolioPositionDB.symbol == normalized,
        )
        .delete()
    )
    db.commit()
    return deleted > 0


def append_positions(
    db: Session,
    user_id: str,
    positions: list[dict[str, Any]],
    source: str = "manual",
    auto_apply_scheduled: bool = True,
) -> dict[str, Any]:
    """追加新标的（不清除旧标的）。
    
    流程：
    1. 查询已存在的标的
    2. 过滤出新标的
    3. 标准化并去重
    4. 计算持仓占比
    5. 插入新标的
    6. 重新计算所有持仓占比
    7. 自动创建定时任务（可选）
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        positions: 持仓列表
        source: 导入来源
        auto_apply_scheduled: 是否自动创建定时任务
    
    Returns:
        操作结果字典
    """
    if not isinstance(positions, list):
        raise ValueError("positions 必须为列表")

    source = (source or "manual").strip()
    now = datetime.now(timezone.utc)

    # 查询已存在的标的
    existing_symbols = {
        row.symbol
        for row in db.query(ImportedPortfolioPositionDB.symbol)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .all()
    }

    added: list[str] = []
    skipped: list[str] = []
    cleaned: list[dict[str, Any]] = []

    # 过滤出新标的
    for raw in positions:
        symbol = _normalize_code(raw.get("symbol"))
        if symbol is None:
            continue
        if symbol in existing_symbols:
            skipped.append(symbol)  # 已存在，跳过
            continue
        if symbol in {c["symbol"] for c in cleaned}:
            continue  # 本次已处理，跳过
        existing_symbols.add(symbol)
        cleaned.append({
            "symbol": symbol,
            "name": (raw.get("name") or "").strip() or None,
            "current_position": _to_float(raw.get("current_position")),
            "available_position": _to_float(raw.get("available_position")),
            "average_cost": _to_float(raw.get("average_cost")),
            "market_value": _to_float(raw.get("market_value")),
            "current_position_pct": _to_float(raw.get("current_position_pct")),
        })

    if not cleaned:
        return {
            "added": added,
            "skipped": skipped,
            "message": "没有新标的可添加" if skipped else "没有有效的持仓记录",
            **get_import_state(db, user_id),
        }

    # 计算新标的的持仓占比
    total_mv = sum(p["market_value"] or 0 for p in cleaned if (p["market_value"] or 0) > 0)
    if total_mv > 0:
        for p in cleaned:
            if p["current_position_pct"] is None and p["market_value"] and p["market_value"] > 0:
                p["current_position_pct"] = round((p["market_value"] / total_mv) * 100, 4)

    # 插入新标的
    for p in cleaned:
        db.add(ImportedPortfolioPositionDB(
            id=uuid4().hex,
            user_id=user_id,
            source=source,
            symbol=p["symbol"],
            security_name=p["name"],
            current_position=p["current_position"],
            available_position=p["available_position"],
            average_cost=p["average_cost"],
            market_value=p["market_value"],
            current_position_pct=p["current_position_pct"],
            trade_points_json=[],
            trade_points_count=0,
            latest_trade_at=None,
            latest_trade_action=None,
            last_imported_at=now,
        ))
        added.append(p["symbol"])

    # 自动创建定时任务
    scheduled_sync: dict[str, list] = {"created": [], "existing": [], "skipped_limit": []}
    if auto_apply_scheduled:
        ordered = [p["symbol"] for p in cleaned if (p["current_position"] or 0) > 0]
        scheduled_sync = scheduled_service.ensure_scheduled_for_symbols(
            db=db,
            user_id=user_id,
            symbols=ordered,
        )

    db.commit()

    # 重新计算所有持仓占比（包括旧标的）
    _recalculate_position_pcts(db, user_id)

    result = get_import_state(db, user_id, scheduled_sync=scheduled_sync)
    result["added"] = added
    result["skipped"] = skipped
    result["message"] = f"成功添加 {len(added)} 只标的" + (f"，跳过 {len(skipped)} 只已存在的标的" if skipped else "")
    return result


def clear_imported_portfolio(db: Session, user_id: str) -> None:
    """清空用户的所有导入持仓（不分来源）。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    """
    db.query(ImportedPortfolioPositionDB).filter(
        ImportedPortfolioPositionDB.user_id == user_id,
    ).delete()
    db.commit()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _recalculate_position_pcts(db: Session, user_id: str) -> None:
    """重新计算所有持仓的占比。
    
    基于市值计算每只标的占总市值的百分比。
    """
    rows = (
        db.query(ImportedPortfolioPositionDB)
        .filter(ImportedPortfolioPositionDB.user_id == user_id)
        .all()
    )
    total_mv = sum(r.market_value or 0 for r in rows if (r.market_value or 0) > 0)
    for row in rows:
        if total_mv > 0 and row.market_value and row.market_value > 0:
            row.current_position_pct = round((row.market_value / total_mv) * 100, 4)
        else:
            row.current_position_pct = None
    db.commit()


def _normalize_code(value: Any) -> str | None:
    """标准化股票代码。
    
    支持格式：
    - 600519.SH（直接返回）
    - 600519（自动识别交易所）
    
    识别规则：
    - 6 开头：沪市（SH）
    - 0/3 开头：深市（SZ）
    - 5 开头：沪市基金（SH）
    - 15/16 开头：深市基金（SZ）
    - 11/13 开头：沪市可转债（SH）
    - 12 开头：深市可转债（SZ）
    - 4/8/9 开头：北交所（BJ）
    """
    text = str(value or "").strip().upper()
    if not text:
        return None
    if _CODE_RE.match(text):
        return text  # 已经是标准格式
    if re.match(r"^\d{6}$", text):
        if text.startswith("6"):
            return f"{text}.SH"
        if text.startswith(("0", "3")):
            return f"{text}.SZ"
        if text.startswith("5"):
            return f"{text}.SH"
        if text.startswith(("15", "16")):
            return f"{text}.SZ"
        if text.startswith(("11", "13")):
            return f"{text}.SH"
        if text.startswith("12"):
            return f"{text}.SZ"
        if text.startswith(("4", "8", "9")):
            return f"{text}.BJ"
    return None


def _to_float(value: Any) -> float | None:
    """安全转换为浮点数。"""
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _latest_imported_at(positions: list[dict[str, Any]]) -> str | None:
    """获取最新的导入时间。"""
    dates = [p["last_imported_at"] for p in positions if p.get("last_imported_at")]
    return max(dates) if dates else None
