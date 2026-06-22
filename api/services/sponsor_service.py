"""
赞助服务模块：提供公开的赞助者读取操作。

核心功能：
1. 赞助者列表：获取可见的赞助者列表（可按类型筛选）

设计说明：
- 赞助者记录由管理后台直接在数据库中管理
- 本服务仅提供公开的读取访问
- `amount` 字段故意排除在所有公开查询之外（隐私保护）
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from api.database import SponsorDB

logger = logging.getLogger(__name__)


def list_sponsors(db: Session, sponsor_type: Optional[str] = None) -> list[SponsorDB]:
    """获取可见的赞助者列表。
    
    Args:
        db: 数据库会话
        sponsor_type: 赞助类型筛选（money/token），None 表示全部
    
    Returns:
        赞助者列表（按排序顺序和日期降序）
    """
    q = db.query(SponsorDB).filter(SponsorDB.is_visible.is_(True))
    if sponsor_type:
        q = q.filter(SponsorDB.sponsor_type == sponsor_type)
    return q.order_by(SponsorDB.sort_order.asc(), SponsorDB.date.desc()).all()
