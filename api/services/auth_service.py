"""
认证服务模块：处理用户认证、JWT 签发、邮箱验证码、加密等。

核心功能：
1. JWT Token 管理：签发和验证访问令牌
2. 邮箱验证码：生成、发送、验证登录验证码
3. 数据加密：使用 Fernet 对称加密保护敏感数据（API Key、Webhook URL）
4. 用户管理：查询、创建、更新用户
5. LLM 配置：管理用户的 LLM 提供商、模型、API Key 等配置

安全设计：
- API Key 和 Webhook URL 使用 Fernet 加密存储
- JWT 使用 HS256 算法签名
- 验证码使用 SHA256 哈希存储
- 支持密钥迁移（默认密钥 → 自定义密钥）

环境变量：
- TA_APP_SECRET_KEY: 应用密钥（加密、JWT 签名）
- MAIL_HOST/MAIL_PORT/MAIL_USER/MAIL_PASS: SMTP 邮件配置
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Optional
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
import jwt
from jwt.exceptions import PyJWTError as JWTError
from sqlalchemy.orm import Session

from api.database import EmailVerificationCodeDB, UserDB, UserLLMConfigDB


# JWT 签名算法
ALGORITHM = "HS256"


def _utcnow() -> datetime:
    """获取当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """确保 datetime 对象带有时区信息（UTC）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# 默认密钥（仅用于开发环境，生产环境必须设置 TA_APP_SECRET_KEY）
_DEFAULT_SECRET = "tradingagents-ashare-dev-secret"


def _secret_key() -> str:
    """获取应用密钥。
    
    优先级：
    1. 环境变量 TA_APP_SECRET_KEY
    2. 硬编码默认值（仅开发环境）
    """
    return os.getenv("TA_APP_SECRET_KEY") or _DEFAULT_SECRET


def _fernet_from_key(key: str) -> Fernet:
    """从密钥创建 Fernet 加密实例。
    
    使用 SHA256 哈希密钥，然后 Base64 编码作为 Fernet 密钥。
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _fernet() -> Fernet:
    """获取当前密钥对应的 Fernet 实例。"""
    return _fernet_from_key(_secret_key())


def is_custom_secret_configured() -> bool:
    """检查是否配置了自定义密钥。"""
    return bool(os.getenv("TA_APP_SECRET_KEY"))


def encrypt_secret(value: str) -> str:
    """加密敏感数据（API Key、Webhook URL 等）。"""
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    """解密敏感数据。解密失败返回 None。"""
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def decrypt_secret_with_fallback(value: Optional[str]) -> Optional[str]:
    """解密敏感数据，支持密钥回退。
    
    解密顺序：
    1. 尝试当前密钥
    2. 尝试默认密钥（首次迁移场景：无密钥 → 自定义密钥）
    """
    if not value:
        return None
    # 尝试当前密钥
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        pass
    # 尝试默认密钥（首次迁移场景）
    if is_custom_secret_configured():
        try:
            return _fernet_from_key(_DEFAULT_SECRET).decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken:
            pass
    return None


def normalize_email(email: str) -> str:
    """标准化邮箱地址：去除空格，转小写。"""
    return email.strip().lower()


def generate_login_code() -> str:
    """生成 6 位随机登录验证码。"""
    return f"{secrets.randbelow(1000000):06d}"


def hash_code(email: str, code: str) -> str:
    """哈希验证码（使用 SHA256 + 密钥 + 邮箱）。"""
    return hashlib.sha256(f"{normalize_email(email)}:{code}:{_secret_key()}".encode("utf-8")).hexdigest()


def create_access_token(user: UserDB, expires_days: int = 30) -> str:
    """创建 JWT 访问令牌。
    
    Args:
        user: 用户对象
        expires_days: 过期天数，默认 30 天
    
    Returns:
        JWT Token 字符串
    """
    now = _utcnow()
    payload = {
        "sub": user.id,  # 用户 ID
        "email": user.email,  # 用户邮箱
        "exp": now + timedelta(days=expires_days),  # 过期时间
        "iat": now,  # 签发时间
    }
    return jwt.encode(payload, _secret_key(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解码并验证 JWT 访问令牌。
    
    Returns:
        JWT payload 字典
    Raises:
        JWTError: Token 无效或已过期
    """
    return jwt.decode(token, _secret_key(), algorithms=[ALGORITHM])


def get_user_by_email(db: Session, email: str) -> Optional[UserDB]:
    """通过邮箱查询用户。"""
    return db.query(UserDB).filter(UserDB.email == normalize_email(email)).first()


def get_user_by_id(db: Session, user_id: str) -> Optional[UserDB]:
    """通过 ID 查询用户。"""
    return db.query(UserDB).filter(UserDB.id == user_id).first()


def upsert_login_code(db: Session, email: str, purpose: str = "login") -> str:
    """创建或更新登录验证码。
    
    流程：
    1. 使该邮箱的所有未消费验证码失效
    2. 生成新的 6 位验证码
    3. 哈希后存储到数据库
    4. 返回明文验证码（用于发送邮件）
    
    Args:
        db: 数据库会话
        email: 用户邮箱
        purpose: 验证码用途（login/register）
    
    Returns:
        明文验证码
    """
    email = normalize_email(email)
    code = generate_login_code()
    now = _utcnow()

    # 使该邮箱的所有未消费验证码失效
    db.query(EmailVerificationCodeDB).filter(
        EmailVerificationCodeDB.email == email,
        EmailVerificationCodeDB.purpose == purpose,
        EmailVerificationCodeDB.consumed_at.is_(None),
    ).update({"consumed_at": now})

    # 创建新的验证码记录
    row = EmailVerificationCodeDB(
        id=str(uuid4()),
        email=email,
        code_hash=hash_code(email, code),  # 存储哈希后的验证码
        purpose=purpose,
        expires_at=now + timedelta(minutes=10),  # 10 分钟过期
        created_at=now,
    )
    db.add(row)
    db.commit()
    return code


def verify_login_code(db: Session, email: str, code: str, purpose: str = "login", client_ip: Optional[str] = None) -> Optional[UserDB]:
    """验证登录验证码并完成登录（不自动注册）。

    流程：
    1. 查询该邮箱最新的未消费验证码
    2. 检查是否过期
    3. 验证码哈希比对
    4. 标记验证码为已消费
    5. **要求邮箱必须已存在于 users 表**（白名单），否则视为未授权
    6. 校验账户处于激活状态
    7. 更新用户的最后登录时间和 IP

    失败时统一返回 None（包含：验证码不存在/过期/错误、邮箱未注册、账户已停用），
    避免通过错误信息区分未注册与验证码错误，防止邮箱枚举攻击。

    Args:
        db: 数据库会话
        email: 用户邮箱
        code: 用户输入的验证码
        purpose: 验证码用途
        client_ip: 客户端 IP

    Returns:
        用户对象；验证失败或邮箱未注册返回 None
    """
    email = normalize_email(email)
    now = _utcnow()

    # 查询该邮箱最新的未消费验证码
    code_row = (
        db.query(EmailVerificationCodeDB)
        .filter(
            EmailVerificationCodeDB.email == email,
            EmailVerificationCodeDB.purpose == purpose,
            EmailVerificationCodeDB.consumed_at.is_(None),
        )
        .order_by(EmailVerificationCodeDB.created_at.desc())
        .first()
    )

    # 检查验证码是否存在且未过期
    expires_at = _as_utc(code_row.expires_at) if code_row else None
    if not code_row or not expires_at or expires_at < now:
        return None  # 验证码不存在或已过期

    # 验证码哈希比对
    if code_row.code_hash != hash_code(email, code):
        return None  # 验证码不匹配

    # 标记验证码为已消费
    code_row.consumed_at = now

    # 白名单：邮箱必须已存在于 users 表，否则拒绝登录
    user = get_user_by_email(db, email)
    if not user or not user.is_active:
        # 回滚验证码的消费标记，避免被锁定的验证码无法再次使用
        code_row.consumed_at = None
        db.commit()
        return None

    # 已有且激活的用户：更新登录信息
    user.last_login_at = now
    user.last_login_ip = client_ip
    user.updated_at = now

    db.commit()
    db.refresh(user)
    return user


def get_env_alias(keys: list[str], default: str = "") -> str:
    """获取环境变量，支持多个别名。
    
    Args:
        keys: 环境变量名列表（按优先级排序）
        default: 默认值
    
    Returns:
        第一个找到的环境变量值，或默认值
    """
    for k in keys:
        v = os.getenv(k)
        if v is not None:
            return v
    return default


def send_login_code(email: str, code: str) -> Optional[str]:
    """发送登录验证码邮件。
    
    流程：
    1. 检查 SMTP 配置
    2. 未配置时：控制台输出验证码（开发环境）或返回 None（生产环境）
    3. 已配置时：通过 SMTP 发送邮件
    
    Args:
        email: 接收邮箱
        code: 验证码
    
    Returns:
        发送失败时返回验证码（开发环境），成功返回 None
    """
    # 获取 SMTP 配置
    smtp_host = get_env_alias(["MAIL_HOST", "MAIL_SERVER", "SMTP_HOST"]).strip()
    if not smtp_host:
        # 未配置 SMTP：控制台输出验证码
        print(f"[auth] 登录验证码 {email}: {code}")
        if os.getenv("APP_ENV", "development") != "production":
            return code  # 开发环境返回验证码
        return None

    # SMTP 配置
    smtp_port = int(get_env_alias(["MAIL_PORT", "SMTP_PORT"]) or "587")
    smtp_user = get_env_alias(["MAIL_USER", "MAIL_USERNAME", "SMTP_USER"]).strip()
    smtp_password = get_env_alias(["MAIL_PASS", "MAIL_PASSWORD", "SMTP_PASSWORD"]).strip()
    smtp_from = get_env_alias(["MAIL_FROM", "SMTP_FROM"], smtp_user or "noreply@example.com").strip()
    
    # STARTTLS 配置（默认启用）
    smtp_starttls_str = get_env_alias(["MAIL_STARTTLS", "SMTP_TLS"], "1").strip().lower()
    smtp_starttls = smtp_starttls_str not in ("0", "false", "off", "no")
    
    # SSL/TLS 配置（默认禁用）
    smtp_ssl_tls_str = get_env_alias(["MAIL_SSL", "MAIL_SSL_TLS"], "0").strip().lower()
    smtp_ssl_tls = smtp_ssl_tls_str in ("1", "true", "on", "yes")

    # 构建邮件
    msg = EmailMessage()
    msg["Subject"] = "TradingAgents 登录验证码"
    msg["From"] = smtp_from
    msg["To"] = email
    msg.set_content(f"你的 TradingAgents 登录验证码是：{code}\n\n10 分钟内有效。")

    try:
        print(f"[auth] 连接到 {smtp_host}:{smtp_port} (SSL: {smtp_ssl_tls}, STARTTLS: {smtp_starttls})")
        smtp_cls = smtplib.SMTP_SSL if smtp_ssl_tls else smtplib.SMTP
        with smtp_cls(smtp_host, smtp_port, timeout=20) as server:
            if smtp_starttls and not smtp_ssl_tls:
                server.starttls()  # 启用 STARTTLS
            if smtp_user:
                server.login(smtp_user, smtp_password)  # 登录
            server.send_message(msg)  # 发送邮件
        return None  # 发送成功
    except Exception as e:
        print(f"[auth] 通过 {smtp_host} 发送邮件失败: {e}")
        print(f"[auth] 回退到控制台输出。验证码 {email}: {code}")
        if os.getenv("APP_ENV", "development") != "production":
            return code  # 开发环境返回验证码
        return None


def get_user_llm_config(db: Session, user_id: str) -> Optional[UserLLMConfigDB]:
    """获取用户的 LLM 配置。"""
    return db.query(UserLLMConfigDB).filter(UserLLMConfigDB.user_id == user_id).first()


def upsert_user_llm_config(
    db: Session,
    user_id: str,
    *,
    llm_provider: Optional[str] = None,
    backend_url: Optional[str] = None,
    quick_think_llm: Optional[str] = None,
    deep_think_llm: Optional[str] = None,
    max_debate_rounds: Optional[int] = None,
    max_risk_discuss_rounds: Optional[int] = None,
    api_key: Optional[str] = None,
    api_key_pool: Optional[str] = None,  # 新增：API Key 池（逗号分隔的多个 Key）
    wecom_webhook_url: Optional[str] = None,
    clear_api_key: bool = False,
    clear_api_key_pool: bool = False,  # 新增：是否清除 API Key 池
    clear_wecom_webhook: bool = False,
    default_analysts: Optional[list] = None,
) -> UserLLMConfigDB:
    """创建或更新用户的 LLM 配置。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        llm_provider: LLM 提供商（openai/anthropic/google 等）
        backend_url: LLM API 后端 URL
        quick_think_llm: 快速思考模型（如 gpt-4o-mini）
        deep_think_llm: 深度思考模型（如 gpt-4o）
        max_debate_rounds: 最大辩论轮次
        max_risk_discuss_rounds: 最大风控讨论轮次
        api_key: LLM API Key（将被加密存储）
        api_key_pool: API Key 池（逗号分隔的多个 Key，用于并发优化）
        wecom_webhook_url: 企业微信 Webhook URL（将被加密存储）
        clear_api_key: 是否清除 API Key
        clear_api_key_pool: 是否清除 API Key 池
        clear_wecom_webhook: 是否清除企业微信 Webhook
        default_analysts: 默认启用的分析师列表
    
    Returns:
        更新后的配置对象
    """
    row = get_user_llm_config(db, user_id)
    now = _utcnow()
    
    # 不存在则创建
    if not row:
        row = UserLLMConfigDB(user_id=user_id, created_at=now, updated_at=now)
        db.add(row)

    # 更新基本配置
    if llm_provider is not None:
        row.llm_provider = llm_provider
    if backend_url is not None:
        row.backend_url = backend_url
    if quick_think_llm is not None:
        row.quick_think_llm = quick_think_llm
    if deep_think_llm is not None:
        row.deep_think_llm = deep_think_llm
    if max_debate_rounds is not None:
        row.max_debate_rounds = max_debate_rounds
    if max_risk_discuss_rounds is not None:
        row.max_risk_discuss_rounds = max_risk_discuss_rounds

    # 处理 API Key（加密存储或清除）
    if clear_api_key:
        row.api_key_encrypted = None
    elif api_key:
        row.api_key_encrypted = encrypt_secret(api_key)

    # 处理 API Key 池（加密存储或清除）
    if clear_api_key_pool:
        row.api_key_pool_encrypted = None
    elif api_key_pool:
        row.api_key_pool_encrypted = encrypt_secret(api_key_pool)

    # 处理企业微信 Webhook（加密存储或清除）
    if clear_wecom_webhook:
        row.wecom_webhook_encrypted = None
    elif wecom_webhook_url:
        row.wecom_webhook_encrypted = encrypt_secret(wecom_webhook_url)

    # 更新默认分析师列表
    if default_analysts is not None:
        import json
        row.default_analysts = json.dumps(default_analysts)

    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row
