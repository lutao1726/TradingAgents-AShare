"""
反馈服务模块：用户侧的反馈 CRUD 操作。

核心功能：
1. 创建反馈：用户提交反馈
2. 反馈列表：分页查询用户的反馈
3. 反馈详情：获取单个反馈
4. 标记已读：标记反馈为已读
5. 未读计数：统计用户的未读回复数

管理员回复和邮件通知由独立的管理后台处理。
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import FeedbackDB, UserDB

logger = logging.getLogger(__name__)


def create_feedback(db: Session, user: UserDB, subject: str, content: str) -> FeedbackDB:
    """创建用户反馈。
    
    Args:
        db: 数据库会话
        user: 用户对象
        subject: 反馈主题
        content: 反馈内容
    
    Returns:
        创建的反馈对象
    """
    fb = FeedbackDB(
        id=str(uuid4()),
        user_id=user.id,
        user_email=user.email,
        subject=subject,
        content=content,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


def list_feedbacks(db: Session, user_id: str, page: int = 1, page_size: int = 20) -> tuple[list[FeedbackDB], int]:
    """分页查询用户的反馈列表。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        page: 页码（从 1 开始）
        page_size: 每页数量
    
    Returns:
        (反馈列表, 总数)
    """
    q = db.query(FeedbackDB).filter(FeedbackDB.user_id == user_id).order_by(FeedbackDB.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


def get_feedback(db: Session, feedback_id: str) -> Optional[FeedbackDB]:
    """获取单个反馈。
    
    Args:
        db: 数据库会话
        feedback_id: 反馈 ID
    
    Returns:
        反馈对象，不存在返回 None
    """
    return db.query(FeedbackDB).filter(FeedbackDB.id == feedback_id).first()


def mark_read(db: Session, feedback_id: str, user_id: str) -> Optional[FeedbackDB]:
    """标记反馈为已读。
    
    Args:
        db: 数据库会话
        feedback_id: 反馈 ID
        user_id: 用户 ID（用于权限校验）
    
    Returns:
        更新后的反馈对象，不存在返回 None
    """
    fb = db.query(FeedbackDB).filter(FeedbackDB.id == feedback_id, FeedbackDB.user_id == user_id).first()
    if fb:
        fb.is_read = True
        db.commit()
        db.refresh(fb)
    return fb


def unread_count(db: Session, user_id: str) -> int:
    """统计用户的未读回复数。
    
    条件：
    1. 有管理员回复
    2. 未读状态
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        未读回复数
    """
    return db.query(FeedbackDB).filter(
        FeedbackDB.user_id == user_id,
        FeedbackDB.admin_reply.isnot(None),
        FeedbackDB.is_read.is_(False),
    ).count()
