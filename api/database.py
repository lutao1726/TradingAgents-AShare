"""数据库配置和会话管理模块。

核心功能：
1. 数据库引擎创建与连接池配置
2. SQLAlchemy ORM 模型定义（Report、User、Watchlist、Scheduled 等）
3. 数据库会话管理（FastAPI Depends 和手动上下文管理器）
4. 轻量级 Schema 迁移（无需 Alembic，适合开发阶段快速迭代）
5. 安全相关迁移（API Token 哈希化、密钥重加密）

数据库支持：
- SQLite（默认）：开发/单机部署，WAL 模式提升并发性能
- PostgreSQL/MySQL：生产环境，更大连接池

环境变量：
- DATABASE_URL：数据库连接字符串，默认 sqlite:///./tradingagents.db
"""

import logging
import os
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import Boolean, create_engine, Column, String, DateTime, Text, Integer, Float, JSON, UniqueConstraint, event, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# 数据库 URL - 默认使用 SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tradingagents.db")

# 创建数据库引擎
if DATABASE_URL.startswith("sqlite"):
    # SQLite 配置：check_same_thread=False 允许多线程访问
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},  # FastAPI 多线程必需
        echo=False,  # 不打印 SQL 语句
        pool_size=10,  # 连接池大小
        max_overflow=20,  # 最大溢出连接数
        pool_timeout=60,  # 获取连接超时时间（秒）
        pool_recycle=3600,  # 连接回收时间（秒）
    )

    def _can_use_wal() -> bool:
        """检查是否可以使用 WAL 模式。
        
        WAL（Write-Ahead Logging）模式需要数据库所在目录可写，
        因为会在数据库文件旁创建 -shm 和 -wal 文件。
        """
        import pathlib
        db_path = DATABASE_URL.replace("sqlite:///", "").replace("sqlite://", "")
        parent = pathlib.Path(db_path).resolve().parent
        return os.access(parent, os.W_OK)

    _use_wal = _can_use_wal()

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        """SQLite 连接初始化：设置 WAL 模式。
        
        WAL 模式优势：
        - 读写并发性能更好
        - 写操作不会阻塞读操作
        - 更好的崩溃恢复能力
        """
        cursor = dbapi_connection.cursor()
        if _use_wal:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
else:
    # PostgreSQL/MySQL 配置：更大连接池处理并发
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_size=20,  # 更大的连接池
        max_overflow=10,  # 最大溢出连接数
        pool_timeout=30,  # 获取连接超时时间（秒）
        pool_recycle=3600,  # 连接回收时间（秒）
    )

# 会话工厂 - 配置自动提交/刷新
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ORM 模型基类
Base = declarative_base()
logger = logging.getLogger(__name__)


def get_db() -> Generator[Session, None, None]:
    """获取数据库会话（FastAPI Depends 注入）。
    
    使用方式：
        @app.get("/api/data")
        def get_data(db: Session = Depends(get_db)):
            return db.query(SomeModel).all()
    
    特性：
    - 自动关闭会话（finally 块）
    - 适合 FastAPI 依赖注入
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class get_db_ctx:
    """手动数据库会话上下文管理器。
    
    使用方式：
        with get_db_ctx() as db:
            db.query(...)
    
    特性：
    - 异常时自动回滚
    - 适合非 FastAPI 场景（如脚本、服务层）
    """

    def __init__(self) -> None:
        self.db: Session | None = None

    def __enter__(self) -> Session:
        self.db = SessionLocal()
        return self.db

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.db is not None:
            if exc_type is not None:
                self.db.rollback()  # 异常时回滚
            self.db.close()  # 关闭会话


def init_db() -> None:
    """初始化数据库表。
    
    执行流程：
    1. 创建所有 ORM 模型对应的表
    2. 确保 reports 表 Schema 完整（轻量级迁移）
    3. 确保 users 表 Schema 完整（轻量级迁移）
    """
    Base.metadata.create_all(bind=engine)  # 创建表
    _ensure_report_schema()  # 确保报告表 Schema
    _ensure_user_schema()  # 确保用户表 Schema


def _ensure_report_schema() -> None:
    """为现有 SQLite 部署添加轻量级列迁移。
    
    功能：
    - 检查 reports 表是否缺少必要列
    - 缺少时自动添加，无需手动迁移
    
    新增列：
    - direction: 分析方向（看多、看空等）
    - status: 任务状态（pending, running, completed, failed）
    - error: 错误信息
    - analyst_traces: 分析师研判轨迹（JSON）
    - macro_report: 宏观分析师报告
    - smart_money_report: 主力资金分析师报告
    - game_theory_report: 博弈分析师报告
    - volume_price_report: 量价分析师报告
    """
    try:
        with engine.begin() as conn:
            # 获取 reports 表的所有列名
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(reports)"))}
            
            # 逐个检查并添加缺失的列
            if "direction" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN direction VARCHAR(50)"))
            if "status" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN status VARCHAR(20) DEFAULT 'completed'"))
            if "error" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN error TEXT"))
            if "analyst_traces" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN analyst_traces JSON"))
            if "macro_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN macro_report TEXT"))
            if "smart_money_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN smart_money_report TEXT"))
            if "game_theory_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN game_theory_report TEXT"))
            if "volume_price_report" not in columns:
                conn.execute(text("ALTER TABLE reports ADD COLUMN volume_price_report TEXT"))
    except Exception as e:
        logger.error("确保报告表 Schema 失败: %s", e)


def _ensure_user_schema() -> None:
    """为现有 SQLite 部署添加用户相关表和列的轻量级迁移。

    功能：
    - 检查 users 表和 user_llm_configs 表是否缺少必要列，缺少时自动添加
    - 创建 alerts 和 alert_triggers 表（如果不存在）
    - 执行 API Token 哈希化迁移
    - 执行 API Key 重加密迁移

    新增列：
    - users 表：last_login_ip, email_report_enabled, wecom_report_enabled, dingtalk_report_enabled
    - user_llm_configs 表：wecom_webhook_encrypted, default_analysts, api_key_pool_encrypted, dingtalk_webhook_encrypted
    """
    try:
        with engine.begin() as conn:
            # 检查 users 表列
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
            if "last_login_ip" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN last_login_ip VARCHAR(45)"))
            if "email_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN email_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            if "wecom_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN wecom_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            if "dingtalk_report_enabled" not in columns:
                conn.execute(text("ALTER TABLE users ADD COLUMN dingtalk_report_enabled BOOLEAN NOT NULL DEFAULT 1"))
            
            # 检查 user_llm_configs 表列
            llm_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(user_llm_configs)"))}
            if "wecom_webhook_encrypted" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN wecom_webhook_encrypted TEXT"))
            if "default_analysts" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN default_analysts TEXT"))
            if "api_key_pool_encrypted" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN api_key_pool_encrypted TEXT"))
            if "dingtalk_webhook_encrypted" not in llm_columns:
                conn.execute(text("ALTER TABLE user_llm_configs ADD COLUMN dingtalk_webhook_encrypted TEXT"))

            # 创建 alerts 表（如果不存在）
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    name VARCHAR(100),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_user_id ON alerts (user_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alerts_symbol ON alerts (symbol)"))

            # 创建 alert_triggers 表（如果不存在）
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS alert_triggers (
                    id VARCHAR(36) PRIMARY KEY,
                    alert_id VARCHAR(36) NOT NULL,
                    trigger_type VARCHAR(30) NOT NULL,
                    threshold FLOAT NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alert_triggers_alert_id ON alert_triggers (alert_id)"))
    except Exception as e:
        logger.error("确保用户表 Schema 失败: %s", e)

    # 执行安全相关迁移
    _migrate_tokens_to_hashed()  # API Token 哈希化
    _migrate_api_keys_reencrypt()  # API Key 重加密


def _migrate_tokens_to_hashed() -> None:
    """将明文 API Token 迁移为 HMAC-SHA256 哈希存储。
    
    迁移逻辑：
    1. 检测以 "ta-sk-" 开头的明文 Token
    2. 使用 HMAC-SHA256 哈希化
    3. 保存最后 4 位作为提示（token_hint）
    4. 更新数据库记录
    
    安全优势：
    - 数据库泄露时不会直接暴露 Token
    - 仍可通过提示识别 Token
    """
    import hashlib, hmac
    try:
        with engine.begin() as conn:
            # 检查 token_hint 列是否存在
            token_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(user_tokens)"))}
            if "token_hint" not in token_cols:
                conn.execute(text("ALTER TABLE user_tokens ADD COLUMN token_hint VARCHAR(8)"))

            # 检测未迁移的明文 Token
            rows = conn.execute(text("SELECT id, token FROM user_tokens WHERE token LIKE 'ta-sk-%'")).fetchall()
            if not rows:
                return  # 已经迁移过
            
            from api.services.auth_service import _secret_key
            key = _secret_key().encode("utf-8")
            
            for row_id, plaintext in rows:
                # HMAC-SHA256 哈希
                token_hash = hmac.new(key, plaintext.encode("utf-8"), hashlib.sha256).hexdigest()
                hint = plaintext[-4:]  # 保存最后 4 位
                
                # 更新数据库
                conn.execute(
                    text("UPDATE user_tokens SET token = :hash, token_hint = :hint WHERE id = :id"),
                    {"hash": token_hash, "hint": hint, "id": row_id},
                )
            logger.info("[安全] 已迁移 %s 个 API Token 从明文到哈希存储。", len(rows))
    except Exception as e:
        logger.error("Token 哈希迁移失败: %s", e)


def _migrate_api_keys_reencrypt() -> None:
    """当 TA_APP_SECRET_KEY 变更时重新加密用户密钥。
    
    迁移逻辑：
    1. 检查是否配置了自定义密钥
    2. 遍历所有用户的加密数据
    3. 尝试用当前密钥解密
    4. 如果失败，尝试用默认密钥解密（旧数据）
    5. 如果默认密钥成功，用当前密钥重新加密并更新
    
    适用场景：
    - 从开发环境迁移到生产环境（更换了 TA_APP_SECRET_KEY）
    - 密钥轮换
    """
    from api.services.auth_service import (
        is_custom_secret_configured, decrypt_secret,
        decrypt_secret_with_fallback, encrypt_secret,
    )
    
    # 只有配置了自定义密钥才执行迁移
    if not is_custom_secret_configured():
        return
    
    try:
        with engine.begin() as conn:
            # 查询所有加密数据
            rows = conn.execute(
                text(
                    """
                    SELECT user_id, api_key_encrypted, wecom_webhook_encrypted
                    FROM user_llm_configs
                    WHERE api_key_encrypted IS NOT NULL OR wecom_webhook_encrypted IS NOT NULL
                    """
                )
            ).fetchall()
            
            if not rows:
                return  # 无加密数据
            
            # 快速检查：如果第一条记录能正常解密，可能已经迁移过
            _, first_api_key, first_wecom_webhook = rows[0]
            first_secret = first_api_key or first_wecom_webhook
            if first_secret and decrypt_secret(first_secret) is not None and len(rows) < 50:
                # 小数据集，仍然验证所有；大数据集如果第一条 OK 则跳过
                pass
            
            migrated = 0
            for user_id, encrypted_api_key, encrypted_wecom_webhook in rows:
                for column_name, encrypted_value in (
                    ("api_key_encrypted", encrypted_api_key),
                    ("wecom_webhook_encrypted", encrypted_wecom_webhook),
                ):
                    if not encrypted_value:
                        continue
                    
                    # 尝试用当前密钥解密
                    if decrypt_secret(encrypted_value) is not None:
                        continue  # 已经是当前密钥加密的
                    
                    # 尝试用回退密钥解密（旧数据）
                    plaintext = decrypt_secret_with_fallback(encrypted_value)
                    if plaintext is None:
                        logger.warning(
                            "[安全] 无法用任何已知密钥解密用户的 %s（user_id=%s）。跳过。",
                            column_name,
                            user_id,
                        )
                        continue
                    
                    # 用当前密钥重新加密
                    new_encrypted = encrypt_secret(plaintext)
                    
                    # 更新数据库
                    if column_name == "api_key_encrypted":
                        conn.execute(
                            text("UPDATE user_llm_configs SET api_key_encrypted = :enc WHERE user_id = :uid"),
                            {"enc": new_encrypted, "uid": user_id},
                        )
                    elif column_name == "wecom_webhook_encrypted":
                        conn.execute(
                            text("UPDATE user_llm_configs SET wecom_webhook_encrypted = :enc WHERE user_id = :uid"),
                            {"enc": new_encrypted, "uid": user_id},
                        )
                    migrated += 1
            
            if migrated:
                logger.info("[安全] 已用新的 TA_APP_SECRET_KEY 重新加密 %s 个用户密钥。", migrated)
    except Exception as e:
        logger.error("用户密钥重加密迁移失败: %s", e)


# ─────────────────────────────────────────────────────────────────────
# ORM 模型定义
# ─────────────────────────────────────────────────────────────────────


class ReportDB(Base):
    """研报数据库模型。
    
    存储分析任务的完整结果，包括：
    - 任务生命周期信息（状态、错误）
    - 决策信息（方向、置信度、目标价、止损价）
    - 各分析师报告（技术、舆情、新闻、基本面、宏观、主力资金、量价）
    - 结构化数据（风险项、关键指标、分析师轨迹）
    
    使用场景：
    - 历史研报查询
    - 研报导出
    - 跟踪看板数据源
    """
    
    __tablename__ = "reports"
    
    # 主键与关联
    id = Column(String(36), primary_key=True, index=True)  # 任务 ID（UUID）
    user_id = Column(String(64), index=True, nullable=True)  # 用户 ID（预留多用户支持）
    symbol = Column(String(20), index=True, nullable=False)  # 股票代码（如 600519.SH）
    trade_date = Column(String(10), nullable=False)  # 交易日期（如 2026-05-26）
    
    # 任务生命周期信息
    status = Column(String(20), default="completed", index=True)  # 状态：pending, running, completed, failed
    error = Column(Text, nullable=True)  # 错误信息（失败时记录）
    
    # 决策信息
    decision = Column(String(50), nullable=True)  # 交易决策：BUY, SELL, HOLD 等
    direction = Column(String(50), nullable=True)  # 分析方向：看多、偏多、中性、偏空、看空
    confidence = Column(Integer, nullable=True)  # 置信度：0-100
    target_price = Column(Float, nullable=True)  # 目标价
    stop_loss_price = Column(Float, nullable=True)  # 止损价
    
    # 完整分析结果（JSON 格式）
    result_data = Column(JSON, nullable=True)

    # LLM 提取的结构化数据
    risk_items = Column(JSON, nullable=True)   # 风险项列表：[{"name": "...", "level": "high|medium|low", "description": "..."}]
    key_metrics = Column(JSON, nullable=True)  # 关键指标列表：[{"name": "...", "value": "...", "status": "good|neutral|bad"}]
    analyst_traces = Column(JSON, nullable=True) # 分析师研判轨迹：[{"agent": "...", "verdict": "...", "key_finding": "..."}]

    # 各分析师报告（独立字段，便于快速访问）
    market_report = Column(Text, nullable=True)  # 技术分析师报告
    sentiment_report = Column(Text, nullable=True)  # 舆情分析师报告
    news_report = Column(Text, nullable=True)  # 新闻分析师报告
    fundamentals_report = Column(Text, nullable=True)  # 基本面分析师报告
    macro_report = Column(Text, nullable=True)  # 宏观分析师报告
    smart_money_report = Column(Text, nullable=True)  # 主力资金分析师报告
    volume_price_report = Column(Text, nullable=True)  # 量价分析师报告
    game_theory_report = Column(Text, nullable=True)  # 博弈分析师报告
    investment_plan = Column(Text, nullable=True)  # 研究总监投资计划
    trader_investment_plan = Column(Text, nullable=True)  # 交易员交易方案
    final_trade_decision = Column(Text, nullable=True)  # 最终交易决策
    
    # 元数据
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间
    
    def to_dict(self) -> dict:
        """转换为字典格式。
        
        返回值：
        - 包含所有字段的字典
        - 日期时间字段转换为 ISO 格式字符串
        
        使用场景：
        - API 响应序列化
        - 前端数据展示
        """
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "decision": self.decision,
            "direction": self.direction,
            "confidence": self.confidence,
            "target_price": self.target_price,
            "stop_loss_price": self.stop_loss_price,
            "result_data": self.result_data,
            "risk_items": self.risk_items,
            "key_metrics": self.key_metrics,
            "analyst_traces": self.analyst_traces,
            "market_report": self.market_report,
            "sentiment_report": self.sentiment_report,
            "news_report": self.news_report,
            "fundamentals_report": self.fundamentals_report,
            "macro_report": self.macro_report,
            "smart_money_report": self.smart_money_report,
            "volume_price_report": self.volume_price_report,
            "game_theory_report": self.game_theory_report,
            "investment_plan": self.investment_plan,
            "trader_investment_plan": self.trader_investment_plan,
            "final_trade_decision": self.final_trade_decision,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PredictionSnapshotDB(Base):
    """预测快照数据库模型。
    
    存储每次分析完成的预测快照，用于后续回填实际结果和计算准确率。
    
    字段说明：
    - id: 快照唯一标识（与 report_id 一致，方便关联）
    - user_id: 用户 ID
    - report_id: 关联的研报 ID
    - symbol: 股票代码
    - trade_date: 预测日期
    - direction: 预测方向（BUY/SELL/HOLD）
    - confidence: 预测置信度（0-100）
    - target_price: 目标价
    - stop_loss_price: 止损价
    - analyst_traces: 各分析师 verdict（JSON）
    - risk_verdict: 风控裁决（pass/revise/reject）
    - actual_close_t1/t5/t20: T+1/T+5/T+20 实际收盘价
    - return_t1/t5/t20: T+1/T+5/T+20 收益率（%）
    - direction_correct: 方向是否正确
    - attribution: 归因分析（JSON）
    - backfilled_at: 回填时间
    """

    __tablename__ = "prediction_snapshots"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=True)
    report_id = Column(String(36), index=True, nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    trade_date = Column(String(10), nullable=False, index=True)

    # 预测内容
    direction = Column(String(20), nullable=False)
    confidence = Column(Integer, nullable=True)
    target_price = Column(Float, nullable=True)
    stop_loss_price = Column(Float, nullable=True)

    # 各分析师 verdict（JSON）
    analyst_traces = Column(JSON, nullable=True)

    # 风控裁决
    risk_verdict = Column(String(20), nullable=True)

    # 实际结果（延迟回填）
    actual_close_t1 = Column(Float, nullable=True)
    actual_close_t5 = Column(Float, nullable=True)
    actual_close_t20 = Column(Float, nullable=True)

    # 收益（延迟计算）
    return_t1 = Column(Float, nullable=True)
    return_t5 = Column(Float, nullable=True)
    return_t20 = Column(Float, nullable=True)

    # 准确率
    direction_correct = Column(Boolean, nullable=True)

    # 归因（延迟填写）
    attribution = Column(JSON, nullable=True)

    # 元数据
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    backfilled_at = Column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "report_id": self.report_id,
            "symbol": self.symbol,
            "trade_date": self.trade_date,
            "direction": self.direction,
            "confidence": self.confidence,
            "target_price": self.target_price,
            "stop_loss_price": self.stop_loss_price,
            "analyst_traces": self.analyst_traces,
            "risk_verdict": self.risk_verdict,
            "actual_close_t1": self.actual_close_t1,
            "actual_close_t5": self.actual_close_t5,
            "actual_close_t20": self.actual_close_t20,
            "return_t1": self.return_t1,
            "return_t5": self.return_t5,
            "return_t20": self.return_t20,
            "direction_correct": self.direction_correct,
            "attribution": self.attribution,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "backfilled_at": self.backfilled_at.isoformat() if self.backfilled_at else None,
        }


class AlertDB(Base):
    """预警数据库模型。

    存储用户设置的持仓预警条件。

    字段说明：
    - id: 预警唯一标识
    - user_id: 用户 ID
    - symbol: 股票代码
    - name: 用户自定义名称
    - is_active: 是否启用
    - created_at: 创建时间
    - updated_at: 更新时间
    """
    __tablename__ = "alerts"

    id = Column(String(36), primary_key=True, index=True)
    user_id = Column(String(64), index=True, nullable=False)
    symbol = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "name": self.name,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AlertTriggerDB(Base):
    """预警触发条件数据库模型。

    存储预警的具体触发条件。

    字段说明：
    - id: 触发条件唯一标识
    - alert_id: 关联的预警 ID
    - trigger_type: 触发类型（price_above / price_below / daily_change_pct / unrealized_pnl_pct）
    - threshold: 阈值
    - enabled: 是否启用
    - created_at: 创建时间
    """
    __tablename__ = "alert_triggers"

    id = Column(String(36), primary_key=True, index=True)
    alert_id = Column(String(36), index=True, nullable=False)
    trigger_type = Column(String(30), nullable=False)
    threshold = Column(Float, nullable=False)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "alert_id": self.alert_id,
            "trigger_type": self.trigger_type,
            "threshold": self.threshold,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class UserDB(Base):
    """用户数据库模型。
    
    存储用户基本信息和偏好设置。
    
    字段说明：
    - id: 用户唯一标识（UUID）
    - email: 用户邮箱（唯一，用于登录）
    - is_active: 账户是否激活
    - last_login_at: 最后登录时间
    - last_login_ip: 最后登录 IP
    - email_report_enabled: 是否启用邮件报告
    - wecom_report_enabled: 是否启用企业微信报告
    - dingtalk_report_enabled: 是否启用钉钉报告
    """
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, index=True)  # 用户 ID
    email = Column(String(255), unique=True, index=True, nullable=False)  # 邮箱（唯一）
    is_active = Column(Boolean, default=True, nullable=False)  # 账户激活状态
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间
    last_login_at = Column(DateTime, nullable=True)  # 最后登录时间
    last_login_ip = Column(String(45), nullable=True)  # 最后登录 IP（支持 IPv6）
    email_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")  # 邮件报告开关
    wecom_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")  # 企业微信报告开关
    dingtalk_report_enabled = Column(Boolean, default=True, nullable=False, server_default="1")  # 钉钉报告开关


class EmailVerificationCodeDB(Base):
    """邮箱验证码数据库模型。
    
    用于邮箱登录验证，支持验证码过期和消费状态追踪。
    
    字段说明：
    - email: 接收验证码的邮箱
    - code_hash: 验证码哈希（不存储明文）
    - purpose: 验证码用途（login, register 等）
    - expires_at: 过期时间
    - consumed_at: 消费时间（已使用时记录）
    """
    __tablename__ = "email_verification_codes"

    id = Column(String(36), primary_key=True, index=True)  # 验证码 ID
    email = Column(String(255), index=True, nullable=False)  # 邮箱
    code_hash = Column(String(255), nullable=False)  # 验证码哈希
    purpose = Column(String(50), default="login", nullable=False)  # 用途
    expires_at = Column(DateTime, nullable=False)  # 过期时间
    consumed_at = Column(DateTime, nullable=True)  # 消费时间
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间


class UserLLMConfigDB(Base):
    """用户 LLM 配置数据库模型。
    
    存储每个用户的 LLM 配置，包括：
    - LLM 提供商（OpenAI, Anthropic 等）
    - 后端 URL
    - 快速/深度思考模型
    - 辩论轮次配置
    - 加密的 API Key
    - 加密的企业微信 Webhook
    - 加密的钉钉 Webhook
    - 默认启用的分析师列表
    
    安全说明：
    - API Key 和 Webhook URL 使用 AES 加密存储
    - 启动时自动重加密（密钥变更时）
    """
    __tablename__ = "user_llm_configs"

    user_id = Column(String(36), primary_key=True, index=True)  # 用户 ID
    llm_provider = Column(String(50), nullable=True)  # LLM 提供商
    backend_url = Column(String(500), nullable=True)  # 后端 URL
    quick_think_llm = Column(String(255), nullable=True)  # 快速思考模型（如 gpt-4o-mini）
    deep_think_llm = Column(String(255), nullable=True)  # 深度思考模型（如 gpt-4o）
    max_debate_rounds = Column(Integer, nullable=True)  # 最大辩论轮次
    max_risk_discuss_rounds = Column(Integer, nullable=True)  # 最大风控讨论轮次
    api_key_encrypted = Column(Text, nullable=True)  # 加密的 API Key（单个 Key，向后兼容）
    api_key_pool_encrypted = Column(Text, nullable=True)  # 加密的 API Key 池（逗号分隔的多个 Key，用于并发优化）
    wecom_webhook_encrypted = Column(Text, nullable=True)  # 加密的企业微信 Webhook
    dingtalk_webhook_encrypted = Column(Text, nullable=True)  # 加密的钉钉 Webhook
    default_analysts = Column(Text, nullable=True)  # 默认启用的分析师列表（JSON），如 '["market","social",...]'
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间


class UserTokenDB(Base):
    """用户 API Token 数据库模型。
    
    存储用户生成的 API Token，用于程序化访问 API。
    
    字段说明：
    - name: Token 名称（用户自定义）
    - token: Token 哈希（HMAC-SHA256）
    - token_hint: Token 提示（最后 4 位，用于识别）
    - is_active: Token 是否激活
    - last_used_at: 最后使用时间
    
    安全说明：
    - Token 使用 HMAC-SHA256 哈希存储
    - 启动时自动迁移明文 Token
    """
    __tablename__ = "user_tokens"

    id = Column(String(36), primary_key=True, index=True)  # Token ID
    user_id = Column(String(36), index=True, nullable=False)  # 用户 ID
    name = Column(String(50), nullable=False)  # Token 名称
    token = Column(String(128), unique=True, index=True, nullable=False)  # Token 哈希
    token_hint = Column(String(8), nullable=True)  # Token 提示（最后 4 位）
    is_active = Column(Boolean, default=True, nullable=False)  # 激活状态
    last_used_at = Column(DateTime, nullable=True)  # 最后使用时间
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间


class VersionStatsDB(Base):
    """版本统计数据模型。
    
    记录系统版本使用情况，用于统计分析。
    
    字段说明：
    - version: 系统版本号
    - nonce: 随机数（防重复统计）
    - remote_ip: 客户端 IP
    """
    __tablename__ = "version_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)  # 自增主键
    version = Column(String(50), nullable=True)  # 版本号
    nonce = Column(String(64), nullable=True)  # 随机数
    remote_ip = Column(String(45), nullable=True, index=True)  # 客户端 IP
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间


class WatchlistItemDB(Base):
    """自选股数据库模型。
    
    存储用户的自选股列表。
    
    约束：
    - 每个用户对同一标的只能添加一次（唯一约束）
    """
    __tablename__ = "watchlist_items"

    id = Column(String(36), primary_key=True)  # 自选股 ID
    user_id = Column(String(64), index=True, nullable=False)  # 用户 ID
    symbol = Column(String(20), nullable=False)  # 股票代码
    sort_order = Column(Integer, default=0)  # 排序顺序
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间

    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='uq_watchlist_user_symbol'),)  # 唯一约束


class ScheduledAnalysisDB(Base):
    """定时分析任务数据库模型。
    
    存储用户的定时分析任务配置。
    
    字段说明：
    - symbol: 股票代码
    - horizon: 分析周期（short/medium）
    - trigger_time: 触发时间（HH:MM 格式）
    - is_active: 是否激活
    - last_run_date: 最后运行日期
    - last_run_status: 最后运行状态
    - last_report_id: 最后生成的研报 ID
    - consecutive_failures: 连续失败次数
    
    约束：
    - 每个用户对同一标的只能设置一个定时任务（唯一约束）
    """
    __tablename__ = "scheduled_analyses"

    id = Column(String(36), primary_key=True)  # 任务 ID
    user_id = Column(String(64), index=True, nullable=False)  # 用户 ID
    symbol = Column(String(20), nullable=False)  # 股票代码
    horizon = Column(String(10), default="short")  # 分析周期
    trigger_time = Column(String(5), default="20:00")  # 触发时间
    is_active = Column(Boolean, default=True)  # 激活状态
    last_run_date = Column(String(10), nullable=True)  # 最后运行日期
    last_run_status = Column(String(10), nullable=True)  # 最后运行状态
    last_report_id = Column(String(36), nullable=True)  # 最后研报 ID
    consecutive_failures = Column(Integer, default=0)  # 连续失败次数
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间

    __table_args__ = (UniqueConstraint('user_id', 'symbol', name='uq_scheduled_user_symbol'),)  # 唯一约束


class SponsorDB(Base):
    """赞助者数据库模型。
    
    存储项目赞助者信息，由管理员维护。
    
    字段说明：
    - sponsor_type: 赞助类型（money/token）
    - name: 赞助者名称
    - github: GitHub 用户名
    - avatar: 头像 URL
    - email: 邮箱
    - provider: Token 赞助时的提供商名称
    - amount: 金额（仅管理员可见，不暴露在公开 API）
    - date: 赞助日期
    - sort_order: 排序顺序
    - is_visible: 是否公开显示
    """
    __tablename__ = "sponsors"

    id = Column(String(36), primary_key=True, index=True)  # 赞助记录 ID
    sponsor_type = Column(String(20), nullable=False, index=True)  # 赞助类型：money | token
    name = Column(String(100), nullable=False)  # 名称
    github = Column(String(100), nullable=True)  # GitHub 用户名
    avatar = Column(String(500), nullable=True)  # 头像 URL
    email = Column(String(255), nullable=True)  # 邮箱
    provider = Column(String(100), nullable=True)  # Token 提供商
    amount = Column(Float, nullable=True)  # 金额（管理员专用）
    date = Column(String(10), nullable=False)  # 赞助日期
    sort_order = Column(Integer, default=0)  # 排序顺序
    is_visible = Column(Boolean, default=True, nullable=False)  # 是否可见
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间


class FeedbackDB(Base):
    """用户反馈数据库模型。
    
    存储用户提交的反馈和管理员回复。
    
    字段说明：
    - subject: 反馈主题
    - content: 反馈内容
    - admin_reply: 管理员回复
    - replied_at: 回复时间
    - is_read: 是否已读
    """
    __tablename__ = "feedbacks"

    id = Column(String(36), primary_key=True, index=True)  # 反馈 ID
    user_id = Column(String(64), index=True, nullable=False)  # 用户 ID
    user_email = Column(String(255), nullable=False)  # 用户邮箱
    subject = Column(String(200), nullable=False)  # 主题
    content = Column(Text, nullable=False)  # 内容
    admin_reply = Column(Text, nullable=True)  # 管理员回复
    replied_at = Column(DateTime, nullable=True)  # 回复时间
    is_read = Column(Boolean, default=False, nullable=False)  # 是否已读
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间


class ImportedPortfolioPositionDB(Base):
    """导入持仓数据库模型。
    
    存储用户导入的持仓快照和近期交易点位。
    
    字段说明：
    - source: 导入来源（manual/image/csv）
    - symbol: 股票代码
    - security_name: 证券名称
    - current_position: 当前持仓量
    - available_position: 可用持仓量
    - average_cost: 平均成本
    - market_value: 市值
    - current_position_pct: 持仓占比
    - trade_points_json: 交易点位（JSON 格式）
    - trade_points_count: 交易点位数量
    - latest_trade_at: 最近交易时间
    - latest_trade_action: 最近交易动作
    - last_imported_at: 最后导入时间
    
    约束：
    - 每个用户对同一来源的同一标的只能有一条记录（唯一约束）
    """
    __tablename__ = "imported_portfolio_positions"

    id = Column(String(36), primary_key=True)  # 记录 ID
    user_id = Column(String(64), index=True, nullable=False)  # 用户 ID
    source = Column(String(32), default="manual", nullable=False)  # 导入来源
    symbol = Column(String(20), nullable=False)  # 股票代码
    security_name = Column(String(80), nullable=True)  # 证券名称
    current_position = Column(Float, nullable=True)  # 当前持仓量
    available_position = Column(Float, nullable=True)  # 可用持仓量
    average_cost = Column(Float, nullable=True)  # 平均成本
    market_value = Column(Float, nullable=True)  # 市值
    current_position_pct = Column(Float, nullable=True)  # 持仓占比
    trade_points_json = Column(JSON, nullable=True)  # 交易点位（JSON）
    trade_points_count = Column(Integer, default=0, nullable=False)  # 交易点位数量
    latest_trade_at = Column(String(32), nullable=True)  # 最近交易时间
    latest_trade_action = Column(String(16), nullable=True)  # 最近交易动作
    last_imported_at = Column(DateTime, nullable=True)  # 最后导入时间
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # 创建时间
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))  # 更新时间

    __table_args__ = (
        UniqueConstraint('user_id', 'source', 'symbol', name='uq_imported_portfolio_user_source_symbol'),  # 唯一约束
    )


