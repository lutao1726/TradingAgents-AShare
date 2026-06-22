"""
自选股服务模块：处理用户自选股的数据库操作。

核心功能：
1. 自选股列表：获取用户的自选股列表（含定时任务状态）
2. 添加自选股：添加单个或多个股票到自选列表
3. 删除自选股：从自选列表中删除股票

数据模型：
- WatchlistItemDB：自选股数据库模型

业务规则：
- 每个用户最多 50 个自选股
- 每个用户对同一标的只能添加一次
"""

from typing import List
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import WatchlistItemDB, ScheduledAnalysisDB

# 每个用户的自选股数量上限
MAX_WATCHLIST_ITEMS = 50


def list_watchlist(db: Session, user_id: str) -> List[dict]:
    """获取用户的自选股列表（含定时任务状态）。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        自选股字典列表，每个字典包含：
        - id: 自选股 ID
        - symbol: 股票代码
        - sort_order: 排序顺序
        - created_at: 创建时间
        - has_scheduled: 是否有定时分析任务
    """
    items = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.user_id == user_id)
        .order_by(WatchlistItemDB.sort_order, WatchlistItemDB.created_at)
        .all()
    )
    
    # 查询该用户的定时分析任务标的
    scheduled_symbols = set(
        row.symbol for row in
        db.query(ScheduledAnalysisDB.symbol)
        .filter(ScheduledAnalysisDB.user_id == user_id)
        .all()
    )
    
    return [
        {
            "id": item.id,
            "symbol": item.symbol,
            "sort_order": item.sort_order,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "has_scheduled": item.symbol in scheduled_symbols,
        }
        for item in items
    ]


def add_watchlist_item(db: Session, user_id: str, symbol: str) -> dict:
    """添加股票到用户自选列表。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbol: 股票代码
    
    Returns:
        添加的自选股字典
    
    Raises:
        ValueError: 自选股数量已达上限或标的已在自选列表中
    """
    # 检查数量上限
    count = db.query(WatchlistItemDB).filter(WatchlistItemDB.user_id == user_id).count()
    if count >= MAX_WATCHLIST_ITEMS:
        raise ValueError(f"自选股数量已达上限 ({MAX_WATCHLIST_ITEMS})")

    # 检查是否已存在
    existing = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.user_id == user_id, WatchlistItemDB.symbol == symbol)
        .first()
    )
    if existing:
        raise ValueError(f"{symbol} 已在自选列表中")

    # 创建新记录
    item = WatchlistItemDB(id=uuid4().hex, user_id=user_id, symbol=symbol)
    db.add(item)
    db.commit()
    db.refresh(item)
    
    return {
        "id": item.id,
        "symbol": item.symbol,
        "sort_order": item.sort_order,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def add_watchlist_items(db: Session, user_id: str, symbols: List[str]) -> List[dict]:
    """批量添加股票到自选列表。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbols: 股票代码列表
    
    Returns:
        每个标的的操作结果列表
    """
    results: List[dict] = []
    for symbol in symbols:
        try:
            item = add_watchlist_item(db, user_id, symbol)
            results.append({
                "symbol": symbol,
                "status": "added",
                "item": item,
                "message": "已添加到自选列表",
            })
        except ValueError as exc:
            message = str(exc)
            status = "duplicate" if "已在自选列表" in message else "failed"
            results.append({
                "symbol": symbol,
                "status": status,
                "message": message,
            })
    return results


def delete_watchlist_item(db: Session, user_id: str, item_id: str) -> bool:
    """删除自选股。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_id: 自选股 ID
    
    Returns:
        是否删除成功
    """
    item = (
        db.query(WatchlistItemDB)
        .filter(WatchlistItemDB.id == item_id, WatchlistItemDB.user_id == user_id)
        .first()
    )
    if not item:
        return False
    db.delete(item)
    db.commit()
    return True
