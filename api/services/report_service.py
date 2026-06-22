"""
研报服务模块：处理研报的创建、查询、更新、删除等数据库操作。

核心功能：
1. 研报 CRUD：创建、查询、更新、删除研报记录
2. 结构化数据提取：使用 LLM 从报告文本中提取决策、置信度、目标价等
3. 正则表达式提取：LLM 不可用时的回退方案
4. 字段解析：从 result_data 中解析所有报告字段
5. 批量操作：批量删除研报

数据模型：
- ReportDB：研报数据库模型，存储完整分析结果
- StructuredReport：LLM 提取的结构化数据模型
- RiskItemSchema：风险项模型
- KeyMetricSchema：关键指标模型

使用场景：
- 分析任务完成后保存研报
- 历史研报查询和导出
- 跟踪看板数据源
"""

import json
import json_repair
import logging
import re

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Iterable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from api.database import ReportDB


# 研报摘要列（用于列表查询，避免加载完整报告内容）
REPORT_SUMMARY_COLUMNS = (
    ReportDB.id,
    ReportDB.user_id,
    ReportDB.symbol,
    ReportDB.trade_date,
    ReportDB.status,
    ReportDB.error,
    ReportDB.decision,
    ReportDB.direction,
    ReportDB.confidence,
    ReportDB.target_price,
    ReportDB.stop_loss_price,
    ReportDB.risk_items,
    ReportDB.key_metrics,
    ReportDB.analyst_traces,
    ReportDB.created_at,
    ReportDB.updated_at,
)

# 活跃报告状态（未完成的报告）
ACTIVE_REPORT_STATUSES = ("pending", "running")

# 中断报告的错误消息
STALE_REPORT_ERROR_MESSAGE = "分析任务已中断，请重新发起分析"


# ─── 结构化提取模型 ─────────────────────────────────────────────────────────

from pydantic import field_validator


class RiskItemSchema(BaseModel):
    """风险项模型。
    
    字段：
    - name: 风险名称（15字以内）
    - level: 风险等级（high/medium/low）
    - description: 一句话说明（30字以内）
    """
    name: str = Field(..., description="风险名称，15字以内")
    level: str = Field("medium", description="风险等级")
    description: str = Field("", description="一句话说明，30字以内")

    @field_validator("level", mode="before")
    @classmethod
    def _coerce_level(cls, v):
        """标准化风险等级为小写。"""
        if isinstance(v, str) and v.lower() in ("high", "medium", "low"):
            return v.lower()
        return "medium"


class KeyMetricSchema(BaseModel):
    """关键指标模型。
    
    字段：
    - name: 指标名称（如 PE、ROE、营收增速）
    - value: 指标值（包含单位，如 28.5x、15.2%）
    - status: 优劣判断（good/neutral/bad）
    """
    name: str = Field(..., description="指标名称，如 PE、ROE、营收增速")
    value: str = Field(..., description="指标值，包含单位，如 28.5x、15.2%")
    status: str = Field("neutral", description="优劣判断")

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v):
        """确保值为字符串类型（LLM 可能返回数字）。"""
        return str(v) if not isinstance(v, str) else v

    @field_validator("status", mode="before")
    @classmethod
    def _coerce_status(cls, v):
        """标准化优劣判断为小写。"""
        if isinstance(v, str) and v.lower() in ("good", "neutral", "bad"):
            return v.lower()
        return "neutral"


class StructuredReport(BaseModel):
    """LLM 提取的结构化报告模型。
    
    字段：
    - decision: 交易决策关键词（BUY/SELL/HOLD/增持/减持/持有）
    - confidence: 整体置信度（0-100）
    - target_price: 目标价（数字，无单位）
    - stop_loss_price: 止损价（数字，无单位）
    - risks: 主要风险列表（最多5条）
    - key_metrics: 关键指标列表（最多6条）
    """
    decision: str = Field("HOLD", description="交易决策关键词：BUY/SELL/HOLD/增持/减持/持有")
    confidence: Optional[int] = Field(None, description="整体置信度 0-100")
    target_price: Optional[float] = Field(None, description="目标价（数字，无单位）")
    stop_loss_price: Optional[float] = Field(None, description="止损价（数字，无单位）")
    risks: List[RiskItemSchema] = Field(default_factory=list, description="主要风险，最多5条")
    key_metrics: List[KeyMetricSchema] = Field(default_factory=list, description="关键指标，最多6条")

    @field_validator("target_price", "stop_loss_price", mode="before")
    @classmethod
    def _coerce_price(cls, v):
        """处理价格字段（LLM 可能返回数组而非单个数字）。"""
        if isinstance(v, list):
            return v[0] if v else None
        return v


def extract_structured_data(
    final_trade_decision: str,
    fundamentals_report: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> Optional[StructuredReport]:
    """使用 LLM 结构化输出从报告文本中提取关键数据。
    
    流程：
    1. 构建提取 prompt
    2. 调用 LLM 进行结构化提取
    3. 解析 JSON 响应
    4. 验证并返回结构化数据
    
    Args:
        final_trade_decision: 最终交易决策文本
        fundamentals_report: 基本面报告摘要
        config: LLM 配置（可选）
    
    Returns:
        结构化报告对象，提取失败返回 None
    """
    if not final_trade_decision:
        return None
    if config is None:
        from tradingagents.default_config import DEFAULT_CONFIG
        config = DEFAULT_CONFIG

    try:
        from langchain_core.messages import HumanMessage
        from tradingagents.llm_clients import create_llm_client

        # 创建 LLM 客户端
        client = create_llm_client(
            provider=config.get("llm_provider", "openai"),
            model=config.get("quick_think_llm", "gpt-4o-mini"),
            base_url=config.get("backend_url"),
            api_key=config.get("api_key"),
        )
        llm = client.get_llm()

        # 构建提取 prompt
        prompt = (
            "请从以下投资分析报告中提取结构化信息，并以 JSON 格式返回。\n\n"
            f"【最终交易决策】\n{final_trade_decision[:3000]}\n\n"
            f"【基本面报告摘要】\n{fundamentals_report[:1000]}\n\n"
            "提取要求（请确保输出为有效的 JSON 对象，不要包裹在 markdown 代码块中）：\n"
            "1. decision：决策方向关键词（BUY/SELL/HOLD 或 增持/减持/持有）\n"
            "2. confidence：整体置信度（0-100整数），若文中未明确给出则根据语气判断\n"
            "3. target_price / stop_loss_price：纯数字，若未提及则为 null\n"
            "4. risks：最多5条主要风险，每条包含名称（15字内）、等级（high/medium/low）、一句话说明\n"
            "5. key_metrics：最多6条关键财务/估值指标，每条包含名称、值（含单位）、优劣（good/neutral/bad）"
        )

        # 调用 LLM
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content if hasattr(response, "content") else str(response)
        
        # 解析 JSON 响应
        parsed = json_repair.loads(raw)
        result = StructuredReport(**parsed)
        
        # 验证置信度范围
        if result.confidence is not None and not (0 <= result.confidence <= 100):
            result.confidence = None
        
        return result
    except Exception as e:
        logger.warning(f"LLM 结构化提取失败: {e}")
        if 'raw' in locals():
            logger.warning(f"LLM 原始输出:\n{raw}")
        return None


# ─── 正则表达式提取（LLM 不可用时的回退方案）────────────────────────────────

def _extract_confidence_regex(text: Optional[str]) -> Optional[int]:
    """使用正则表达式从文本中提取置信度。
    
    支持格式：
    - 置信度：75%
    - confidence: 75%
    """
    if not text:
        return None
    for pattern in (r'置信度[:：]\s*(\d+)%', r'confidence[:：]\s*(\d+)%'):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            v = int(m.group(1))
            return v if 0 <= v <= 100 else None
    return None


def _extract_price_regex(text: Optional[str], price_type: str = "target") -> Optional[float]:
    """使用正则表达式从文本中提取价格。
    
    Args:
        text: 文本内容
        price_type: 价格类型（target/stop_loss）
    
    Returns:
        价格数值，提取失败返回 None
    """
    if not text:
        return None
    if price_type == "target":
        patterns = [
            r'目标价[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'目标价格[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'target[:：]\s*[¥$]?\s*(\d+\.?\d*)',
        ]
    else:
        patterns = [
            r'止损价[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'止损价格[:：]\s*[¥$]?\s*(\d+\.?\d*)',
            r'stop[-\s_]?loss[:：]\s*[¥$]?\s*(\d+\.?\d*)',
        ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _extract_verdict(text: Optional[str]) -> Optional[Dict[str, str]]:
    """从文本中提取 VERDICT 标记的结构化决策。
    
    格式：<!-- VERDICT: {"direction": "看多", "reason": "..."} -->
    """
    if not text:
        return None
    match = re.search(r"<!--\s*VERDICT:\s*(\{.*?\})\s*-->", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        # 清理潜在的换行符或不可见字符
        raw_json = match.group(1).strip().replace('\n', ' ').replace('\r', ' ')
        payload = json.loads(raw_json)
    except Exception:
        return None
    direction = str(payload.get("direction") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if not direction:
        return None
    return {"direction": direction, "reason": reason}


def resolve_report_fields(
    result_data: Optional[Dict[str, Any]] = None,
    confidence_override: Optional[int] = None,
    target_price_override: Optional[float] = None,
    stop_loss_override: Optional[float] = None,
) -> Dict[str, Any]:
    """解析并合并报告的所有字段。
    
    流程：
    1. 从 result_data 中提取所有报告字段
    2. 提取 VERDICT 标记中的方向
    3. 使用 LLM 提取的结果或正则表达式提取置信度和价格
    4. 返回合并后的字段字典
    
    Args:
        result_data: 完整分析结果数据
        confidence_override: LLM 提取的置信度（优先级最高）
        target_price_override: LLM 提取的目标价（优先级最高）
        stop_loss_override: LLM 提取的止损价（优先级最高）
    
    Returns:
        包含所有解析字段的字典
    """
    # 初始化所有报告字段
    market_report = sentiment_report = news_report = None
    fundamentals_report = macro_report = smart_money_report = volume_price_report = game_theory_report = None
    investment_plan = trader_investment_plan = None
    final_trade_decision = None

    # 从 result_data 中提取报告字段
    if result_data:
        market_report = result_data.get("market_report")
        sentiment_report = result_data.get("sentiment_report")
        news_report = result_data.get("news_report")
        fundamentals_report = result_data.get("fundamentals_report")
        macro_report = result_data.get("macro_report")
        smart_money_report = result_data.get("smart_money_report")
        volume_price_report = result_data.get("volume_price_report")
        game_theory_report = result_data.get("game_theory_report")
        investment_plan = result_data.get("investment_plan")
        trader_investment_plan = result_data.get("trader_investment_plan")
        final_trade_decision = result_data.get("final_trade_decision")

    # 提取 VERDICT 标记中的方向
    verdict = _extract_verdict(final_trade_decision)
    direction = verdict["direction"] if verdict else None

    # 提取置信度（优先使用 LLM 提取的结果）
    confidence = confidence_override if confidence_override is not None else _extract_confidence_regex(final_trade_decision)

    # 提取目标价（优先使用 LLM 提取的结果，回退到正则表达式）
    target_price = target_price_override if target_price_override is not None else _extract_price_regex(final_trade_decision, "target")
    if target_price is None:
        target_price = _extract_price_regex(trader_investment_plan, "target")

    # 提取止损价（优先使用 LLM 提取的结果，回退到正则表达式）
    stop_loss_price = stop_loss_override if stop_loss_override is not None else _extract_price_regex(final_trade_decision, "stop_loss")
    if stop_loss_price is None:
        stop_loss_price = _extract_price_regex(trader_investment_plan, "stop_loss")

    return {
        "market_report": market_report,
        "sentiment_report": sentiment_report,
        "news_report": news_report,
        "fundamentals_report": fundamentals_report,
        "macro_report": macro_report,
        "smart_money_report": smart_money_report,
        "volume_price_report": volume_price_report,
        "game_theory_report": game_theory_report,
        "investment_plan": investment_plan,
        "trader_investment_plan": trader_investment_plan,
        "final_trade_decision": final_trade_decision,
        "direction": direction,
        "confidence": confidence,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
    }


# ─── CRUD 操作 ────────────────────────────────────────────────────────────────

def init_report(
    db: Session,
    report_id: str,
    symbol: str,
    trade_date: str,
    user_id: Optional[str] = None,
) -> ReportDB:
    """初始化待处理的研报记录（任务提交时调用）。
    
    Args:
        db: 数据库会话
        report_id: 研报 ID（通常等于 job_id）
        symbol: 股票代码
        trade_date: 交易日期
        user_id: 用户 ID（可选）
    
    Returns:
        创建的研报对象
    """
    now = datetime.now(timezone.utc)
    db_report = ReportDB(
        id=report_id,
        user_id=user_id,
        symbol=symbol,
        trade_date=trade_date,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(db_report)
    db.commit()
    db.refresh(db_report)
    return db_report


def update_report_partial(
    db: Session,
    report_id: str,
    status: Optional[str] = None,
    **fields: Any
) -> Optional[ReportDB]:
    """更新研报的部分字段（如增量更新分析师报告）。
    
    Args:
        db: 数据库会话
        report_id: 研报 ID
        status: 新状态（可选）
        **fields: 要更新的字段键值对
    
    Returns:
        更新后的研报对象，不存在返回 None
    """
    db_report = db.query(ReportDB).filter(ReportDB.id == report_id).first()
    if not db_report:
        return None
    
    if status:
        db_report.status = status
    
    for key, value in fields.items():
        if hasattr(db_report, key):
            setattr(db_report, key, value)
    
    db_report.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(db_report)
    return db_report


def finalize_orphan_report(
    db: Session,
    report: ReportDB,
    *,
    error_message: str = STALE_REPORT_ERROR_MESSAGE,
) -> ReportDB:
    """标记孤立的待处理/运行中研报为失败。
    
    孤立研报：任务中断后遗留的未完成研报。
    
    Args:
        db: 数据库会话
        report: 研报对象
        error_message: 错误消息
    
    Returns:
        更新后的研报对象
    """
    if str(report.status or "") not in ACTIVE_REPORT_STATUSES:
        return report

    report.status = "failed"
    report.error = error_message
    report.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    return report


def recover_stale_active_reports(
    db: Session,
    *,
    active_job_ids: Optional[Iterable[str]] = None,
    error_message: str = STALE_REPORT_ERROR_MESSAGE,
) -> Dict[str, int]:
    """恢复中断遗留的活跃研报。
    
    流程：
    1. 查询所有待处理/运行中的研报
    2. 排除当前活跃的任务 ID
    3. 将剩余研报标记为失败
    
    Args:
        db: 数据库会话
        active_job_ids: 当前活跃的任务 ID 列表
        error_message: 错误消息
    
    Returns:
        统计信息字典
    """
    active_job_id_set = {str(job_id) for job_id in (active_job_ids or []) if str(job_id).strip()}
    rows = (
        db.query(ReportDB)
        .filter(ReportDB.status.in_(ACTIVE_REPORT_STATUSES))
        .all()
    )
    if not rows:
        return {"total": 0, "failed": 0}

    failed = 0
    changed = False
    now = datetime.now(timezone.utc)
    for row in rows:
        if str(row.id) in active_job_id_set:
            continue
        row.status = "failed"
        row.error = error_message
        row.updated_at = now
        changed = True
        failed += 1

    if changed:
        db.commit()

    return {
        "total": failed,
        "failed": failed,
    }


def mark_report_failed(
    db: Session,
    report_id: str,
    error_message: str
) -> Optional[ReportDB]:
    """标记研报为失败。
    
    Args:
        db: 数据库会话
        report_id: 研报 ID
        error_message: 错误消息
    
    Returns:
        更新后的研报对象
    """
    return update_report_partial(db, report_id, status="failed", error=error_message)


def create_report(
    db: Session,
    symbol: str,
    trade_date: str,
    decision: Optional[str] = None,
    result_data: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    risk_items: Optional[List[dict]] = None,
    key_metrics: Optional[List[dict]] = None,
    analyst_traces: Optional[List[dict]] = None,
    confidence_override: Optional[int] = None,
    target_price_override: Optional[float] = None,
    stop_loss_override: Optional[float] = None,
    report_id: Optional[str] = None,
) -> ReportDB:
    """创建或完成研报。
    
    流程：
    1. 解析并合并报告字段
    2. 检查是否更新已有记录（通过 init_report 初始化的）
    3. 更新或创建研报记录
    
    Args:
        db: 数据库会话
        symbol: 股票代码
        trade_date: 交易日期
        decision: 交易决策
        result_data: 完整分析结果
        user_id: 用户 ID
        risk_items: 风险项列表
        key_metrics: 关键指标列表
        analyst_traces: 分析师轨迹
        confidence_override: LLM 提取的置信度
        target_price_override: LLM 提取的目标价
        stop_loss_override: LLM 提取的止损价
        report_id: 研报 ID（可选，用于更新已有记录）
    
    Returns:
        创建或更新的研报对象
    """
    # 解析并合并报告字段
    resolved = resolve_report_fields(
        result_data=result_data,
        confidence_override=confidence_override,
        target_price_override=target_price_override,
        stop_loss_override=stop_loss_override,
    )

    now = datetime.now(timezone.utc)
    
    # 检查是否更新已有记录
    db_report = None
    if report_id:
        db_report = db.query(ReportDB).filter(ReportDB.id == report_id).first()

    if db_report:
        # 更新已有记录
        db_report.status = "completed"
        db_report.decision = decision
        db_report.direction = resolved["direction"]
        db_report.confidence = resolved["confidence"]
        db_report.target_price = resolved["target_price"]
        db_report.stop_loss_price = resolved["stop_loss_price"]
        db_report.result_data = result_data
        db_report.risk_items = risk_items
        db_report.key_metrics = key_metrics
        db_report.analyst_traces = analyst_traces
        db_report.market_report = resolved["market_report"]
        db_report.sentiment_report = resolved["sentiment_report"]
        db_report.news_report = resolved["news_report"]
        db_report.fundamentals_report = resolved["fundamentals_report"]
        db_report.macro_report = resolved["macro_report"]
        db_report.smart_money_report = resolved["smart_money_report"]
        db_report.volume_price_report = resolved["volume_price_report"]
        db_report.game_theory_report = resolved["game_theory_report"]
        db_report.investment_plan = resolved["investment_plan"]
        db_report.trader_investment_plan = resolved["trader_investment_plan"]
        db_report.final_trade_decision = resolved["final_trade_decision"]
        db_report.updated_at = now
    else:
        # 创建新记录
        db_report = ReportDB(
            id=report_id or str(uuid4()),
            user_id=user_id,
            symbol=symbol,
            trade_date=trade_date,
            status="completed",
            decision=decision,
            direction=resolved["direction"],
            confidence=resolved["confidence"],
            target_price=resolved["target_price"],
            stop_loss_price=resolved["stop_loss_price"],
            result_data=result_data,
            risk_items=risk_items,
            key_metrics=key_metrics,
            analyst_traces=analyst_traces,
            market_report=resolved["market_report"],
            sentiment_report=resolved["sentiment_report"],
            news_report=resolved["news_report"],
            fundamentals_report=resolved["fundamentals_report"],
            macro_report=resolved["macro_report"],
            smart_money_report=resolved["smart_money_report"],
            volume_price_report=resolved["volume_price_report"],
            game_theory_report=resolved["game_theory_report"],
            investment_plan=resolved["investment_plan"],
            trader_investment_plan=resolved["trader_investment_plan"],
            final_trade_decision=resolved["final_trade_decision"],
            created_at=now,
            updated_at=now,
        )
        db.add(db_report)

    db.commit()
    db.refresh(db_report)
    return db_report


def get_report(db: Session, report_id: str, user_id: Optional[str] = None) -> Optional[ReportDB]:
    """获取单个研报。
    
    Args:
        db: 数据库会话
        report_id: 研报 ID
        user_id: 用户 ID（可选，用于权限校验）
    
    Returns:
        研报对象，不存在返回 None
    """
    query = db.query(ReportDB).filter(ReportDB.id == report_id)
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)
    return query.first()


def get_reports_by_user(
    db: Session,
    user_id: Optional[str] = None,
    symbol: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[ReportDB]:
    """获取用户的研报列表。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID（可选）
        symbol: 股票代码（可选，用于筛选）
        skip: 跳过记录数
        limit: 返回记录数
    
    Returns:
        研报列表
    """
    query = db.query(ReportDB).options(load_only(*REPORT_SUMMARY_COLUMNS))
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)
    if symbol:
        query = query.filter(ReportDB.symbol == symbol)
    return query.order_by(ReportDB.created_at.desc()).offset(skip).limit(limit).all()


def get_latest_reports_by_symbols(
    db: Session,
    symbols: List[str],
    user_id: Optional[str] = None,
) -> List[ReportDB]:
    """批量获取多个标的的最新研报。
    
    Args:
        db: 数据库会话
        symbols: 股票代码列表
        user_id: 用户 ID（可选）
    
    Returns:
        每个标的的最新研报列表
    """
    normalized_symbols = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if not normalized_symbols:
        return []

    query = db.query(ReportDB).options(load_only(*REPORT_SUMMARY_COLUMNS))
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)

    rows = (
        query.filter(ReportDB.symbol.in_(normalized_symbols))
        .order_by(ReportDB.symbol.asc(), ReportDB.created_at.desc())
        .all()
    )

    # 每个标的只保留最新的一条
    latest_by_symbol: dict[str, ReportDB] = {}
    for row in rows:
        symbol = str(row.symbol or "").upper()
        if symbol and symbol not in latest_by_symbol:
            latest_by_symbol[symbol] = row

    return [latest_by_symbol[symbol] for symbol in normalized_symbols if symbol in latest_by_symbol]


def count_reports(
    db: Session,
    user_id: Optional[str] = None,
    symbol: Optional[str] = None,
) -> int:
    """统计研报数量。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID（可选）
        symbol: 股票代码（可选）
    
    Returns:
        研报数量
    """
    query = db.query(func.count(ReportDB.id))
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)
    if symbol:
        query = query.filter(ReportDB.symbol == symbol)
    return query.scalar() or 0


def delete_report(db: Session, report_id: str, user_id: Optional[str] = None) -> bool:
    """删除单个研报。
    
    Args:
        db: 数据库会话
        report_id: 研报 ID
        user_id: 用户 ID（可选，用于权限校验）
    
    Returns:
        是否删除成功
    """
    query = db.query(ReportDB).filter(ReportDB.id == report_id)
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)
    report = query.first()
    if report:
        db.delete(report)
        db.commit()
        return True
    return False


def batch_delete_reports(db: Session, report_ids: Iterable[str], user_id: Optional[str] = None) -> dict:
    """批量删除研报。
    
    Args:
        db: 数据库会话
        report_ids: 研报 ID 列表
        user_id: 用户 ID（可选，用于权限校验）
    
    Returns:
        删除结果字典，包含 deleted_ids 和 missing_ids
    """
    # 标准化 ID 列表
    normalized_ids: list[str] = []
    seen: set[str] = set()
    for raw_report_id in report_ids:
        report_id = str(raw_report_id or "").strip()
        if not report_id or report_id in seen:
            continue
        seen.add(report_id)
        normalized_ids.append(report_id)

    if not normalized_ids:
        raise ValueError("请至少选择 1 份报告")

    # 查询要删除的研报
    query = db.query(ReportDB).filter(ReportDB.id.in_(normalized_ids))
    if user_id:
        query = query.filter(ReportDB.user_id == user_id)

    rows = query.all()
    row_by_id = {str(row.id): row for row in rows}
    deleted_ids: list[str] = []
    missing_ids: list[str] = []

    # 执行删除
    for report_id in normalized_ids:
        row = row_by_id.get(report_id)
        if row is None:
            missing_ids.append(report_id)
            continue
        db.delete(row)
        deleted_ids.append(report_id)

    if deleted_ids:
        db.commit()

    return {
        "deleted_ids": deleted_ids,
        "missing_ids": missing_ids,
    }
