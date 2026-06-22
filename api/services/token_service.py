"""
API Token 服务模块：管理用户的 API Token。

核心功能：
1. Token 创建：生成安全的随机 Token
2. Token 列表：获取用户的所有 Token
3. Token 删除：删除（吊销）Token
4. Token 验证：验证 Token 并返回关联的用户

安全设计：
- Token 使用 HMAC-SHA256 哈希存储（防止数据库泄露时被暴力破解）
- Token 格式：ta-sk-{随机字符串}
- Token 仅在创建时返回一次明文，后续只存储哈希
- 每个用户最多 10 个 Token

使用场景：
- 程序化访问 API（如脚本、交易机器人）
- 第三方集成
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from sqlalchemy.orm import Session
from api.database import UserTokenDB, UserDB


# Token 前缀
TOKEN_PREFIX = "ta-sk-"

# 每个用户的 Token 数量上限
MAX_TOKENS_PER_USER = 10


def _hmac_key() -> bytes:
    """获取 HMAC 密钥（使用应用密钥）。"""
    from api.services.auth_service import _secret_key
    return _secret_key().encode("utf-8")


def _hash_token(token_str: str) -> str:
    """使用 HMAC-SHA256 哈希 Token。
    
    安全优势：
    - 即使数据库泄露，攻击者也无法通过暴力破解获取原始 Token
    - 使用应用密钥作为 HMAC 密钥，增加破解难度
    """
    return hmac.new(_hmac_key(), token_str.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_token_string() -> str:
    """生成安全的随机 Token 字符串。
    
    格式：ta-sk-{URL 安全的随机字符串（64 字节）}
    """
    random_part = secrets.token_urlsafe(64)
    return f"{TOKEN_PREFIX}{random_part}"


def create_token(db: Session, user_id: str, name: str) -> dict:
    """创建新的 API Token。
    
    流程：
    1. 检查用户的 Token 数量是否达到上限
    2. 生成安全的随机 Token
    3. 哈希后存储到数据库
    4. 返回明文 Token（仅此一次）
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        name: Token 名称（用户自定义）
    
    Returns:
        Token 信息字典，包含：
        - id: Token ID
        - name: Token 名称
        - token: 明文 Token（仅创建时返回）
        - token_hint: Token 提示（最后 4 位）
        - last_used_at: 最后使用时间
        - created_at: 创建时间
    
    Raises:
        ValueError: Token 数量已达上限
    """
    # 检查数量上限
    count = db.query(UserTokenDB).filter(UserTokenDB.user_id == user_id).count()
    if count >= MAX_TOKENS_PER_USER:
        raise ValueError(f"每个用户最多只能创建 {MAX_TOKENS_PER_USER} 个 API Token")

    # 生成 Token
    plaintext = generate_token_string()
    
    # 创建数据库记录
    new_token = UserTokenDB(
        id=str(uuid4()),
        user_id=user_id,
        name=name,
        token=_hash_token(plaintext),  # 存储哈希后的 Token
        token_hint=plaintext[-4:],  # 保存最后 4 位作为提示
        created_at=datetime.now(timezone.utc),
    )
    db.add(new_token)
    db.commit()
    db.refresh(new_token)
    
    # 返回明文 Token（仅此一次）
    return {
        "id": new_token.id,
        "name": new_token.name,
        "token": plaintext,
        "token_hint": new_token.token_hint,
        "last_used_at": new_token.last_used_at,
        "created_at": new_token.created_at,
    }


def list_user_tokens(db: Session, user_id: str) -> List[UserTokenDB]:
    """获取用户的所有 Token（按创建时间降序）。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        Token 对象列表
    """
    return db.query(UserTokenDB).filter(UserTokenDB.user_id == user_id).order_by(UserTokenDB.created_at.desc()).all()


def delete_token(db: Session, user_id: str, token_id: str) -> bool:
    """删除（吊销）Token。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        token_id: Token ID
    
    Returns:
        是否删除成功
    """
    token_row = db.query(UserTokenDB).filter(
        UserTokenDB.id == token_id,
        UserTokenDB.user_id == user_id
    ).first()

    if not token_row:
        return False

    db.delete(token_row)
    db.commit()
    return True


def verify_token(db: Session, token_str: str) -> Optional[UserDB]:
    """验证 Token 并返回关联的用户。
    
    流程：
    1. 检查 Token 前缀
    2. 计算 Token 哈希
    3. 查询数据库中的 Token 记录
    4. 更新最后使用时间
    5. 返回关联的用户
    
    Args:
        db: 数据库会话
        token_str: 明文 Token 字符串
    
    Returns:
        用户对象，验证失败返回 None
    """
    # 检查前缀
    if not token_str.startswith(TOKEN_PREFIX):
        return None

    # 计算哈希
    token_hash = _hash_token(token_str)
    
    # 查询 Token 记录
    token_row = db.query(UserTokenDB).filter(
        UserTokenDB.token == token_hash,
        UserTokenDB.is_active == True
    ).first()

    if not token_row:
        return None

    # 更新最后使用时间
    token_row.last_used_at = datetime.now(timezone.utc)
    db.commit()

    # 返回关联的用户
    return db.query(UserDB).filter(UserDB.id == token_row.user_id).first()
