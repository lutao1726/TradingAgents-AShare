"""
定时分析服务模块：处理定时分析任务的 CRUD 操作。

核心功能：
1. 定时任务 CRUD：创建、查询、更新、删除定时分析任务
2. 批量操作：批量更新、批量删除定时任务
3. 任务执行状态管理：标记成功/失败，自动禁用连续失败的任务
4. 待执行任务查询：查询当天需要执行的任务

数据模型：
- ScheduledAnalysisDB：定时分析任务数据库模型

业务规则：
- 每个用户最多 10 个定时任务
- 每个用户对同一标的只能设置一个定时任务
- 触发时间仅允许 20:00~次日 08:00（避免影响白天使用）
- 连续失败 3 次自动禁用任务
"""

from typing import Iterable, List, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import ScheduledAnalysisDB

# 每个用户的定时任务数量上限
MAX_SCHEDULED_ITEMS = 10

# 有效的分析周期
VALID_HORIZONS = {"short", "medium"}


def _validate_trigger_time(t: str) -> str:
    """验证触发时间格式（HH:MM）。
    
    允许的时间范围：
    - 20:00~23:59（晚上）
    - 00:00~08:00（凌晨）
    
    Args:
        t: 时间字符串（HH:MM）
    
    Returns:
        格式化后的时间字符串
    
    Raises:
        ValueError: 时间格式错误或不在允许范围内
    """
    parts = t.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式错误，请使用 HH:MM")
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("时间格式错误，请使用 HH:MM")
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("时间格式错误，请使用 HH:MM")
    
    time_val = hh * 60 + mm
    # 允许范围：20:00 (1200) ~ 23:59 (1439) 或 00:00 (0) ~ 08:00 (480)
    if 8 * 60 < time_val < 20 * 60:
        raise ValueError("定时时间仅允许 20:00~次日 08:00（避免影响白天使用）")
    
    return f"{hh:02d}:{mm:02d}"


def list_scheduled(db: Session, user_id: str) -> List[dict]:
    """获取用户的定时分析任务列表。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
    
    Returns:
        定时任务字典列表
    """
    items = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.user_id == user_id)
        .order_by(ScheduledAnalysisDB.created_at)
        .all()
    )
    return [_to_dict(item) for item in items]


def get_scheduled(db: Session, user_id: str, item_id: str) -> Optional[dict]:
    """获取单个定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_id: 任务 ID
    
    Returns:
        定时任务字典，不存在返回 None
    """
    item = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.user_id == user_id, ScheduledAnalysisDB.id == item_id)
        .first()
    )
    if not item:
        return None
    return _to_dict(item)


def get_scheduled_batch(db: Session, user_id: str, item_ids: Iterable[str]) -> List[dict]:
    """批量获取定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_ids: 任务 ID 列表
    
    Returns:
        定时任务字典列表（按请求顺序返回）
    
    Raises:
        ValueError: 任务 ID 列表为空或部分任务不存在
    """
    normalized_ids = _normalize_item_ids(item_ids)
    if not normalized_ids:
        raise ValueError("请至少选择一个定时任务")

    items = (
        db.query(ScheduledAnalysisDB)
        .filter(
            ScheduledAnalysisDB.user_id == user_id,
            ScheduledAnalysisDB.id.in_(normalized_ids),
        )
        .all()
    )
    item_map = {item.id: item for item in items}
    
    # 检查是否有不存在的任务
    missing_ids = [item_id for item_id in normalized_ids if item_id not in item_map]
    if missing_ids:
        raise ValueError("部分定时任务不存在或已失效，请刷新后重试")

    # 按请求顺序返回
    return [_to_dict(item_map[item_id]) for item_id in normalized_ids]


def _normalize_item_ids(item_ids: Iterable[str]) -> list[str]:
    """标准化任务 ID 列表（去重、去空）。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in item_ids:
        item_id = (raw_id or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        normalized.append(item_id)
    return normalized


def _validate_horizon(horizon: str) -> str:
    """验证分析周期。
    
    Args:
        horizon: 分析周期（short/medium）
    
    Returns:
        验证后的分析周期
    
    Raises:
        ValueError: 无效的分析周期
    """
    if horizon not in VALID_HORIZONS:
        raise ValueError("horizon 必须为 short 或 medium")
    return horizon


def _apply_scheduled_updates(item: ScheduledAnalysisDB, **kwargs) -> None:
    """应用定时任务更新。
    
    Args:
        item: 定时任务对象
        **kwargs: 要更新的字段
    """
    if "is_active" in kwargs:
        item.is_active = kwargs["is_active"]
        if kwargs["is_active"]:
            item.consecutive_failures = 0  # 重新启用时重置连续失败次数
    if "horizon" in kwargs:
        item.horizon = _validate_horizon(kwargs["horizon"])
    if "trigger_time" in kwargs:
        item.trigger_time = _validate_trigger_time(kwargs["trigger_time"])


def create_scheduled(
    db: Session,
    user_id: str,
    symbol: str,
    horizon: str = "short",
    trigger_time: str = "20:00",
) -> dict:
    """创建定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbol: 股票代码
        horizon: 分析周期（short/medium）
        trigger_time: 触发时间（HH:MM）
    
    Returns:
        创建的定时任务字典
    
    Raises:
        ValueError: 定时任务数量已达上限或标的已有定时任务
    """
    # 检查数量上限
    count = db.query(ScheduledAnalysisDB).filter(
        ScheduledAnalysisDB.user_id == user_id
    ).count()
    if count >= MAX_SCHEDULED_ITEMS:
        raise ValueError(f"定时分析数量已达上限 ({MAX_SCHEDULED_ITEMS})")

    # 检查是否已有相同标的的任务
    existing = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.user_id == user_id, ScheduledAnalysisDB.symbol == symbol)
        .first()
    )
    if existing:
        raise ValueError(f"{symbol} 已有定时分析任务")

    horizon = _validate_horizon(horizon)
    trigger_time = _validate_trigger_time(trigger_time)

    item = ScheduledAnalysisDB(
        id=uuid4().hex,
        user_id=user_id,
        symbol=symbol,
        horizon=horizon,
        trigger_time=trigger_time,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _to_dict(item)


def ensure_scheduled_for_symbols(
    db: Session,
    user_id: str,
    symbols: Iterable[str],
    horizon: str = "short",
    trigger_time: str = "20:00",
) -> dict:
    """确保给定的标的存在于定时任务中（不重复创建）。
    
    用于持仓导入时自动创建定时任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        symbols: 股票代码列表
        horizon: 分析周期（short/medium）
        trigger_time: 触发时间（HH:MM）
    
    Returns:
        操作结果字典，包含 created、existing、skipped_limit
    """
    horizon = _validate_horizon(horizon)
    trigger_time = _validate_trigger_time(trigger_time)

    # 查询已有的定时任务
    existing_items = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.user_id == user_id)
        .order_by(ScheduledAnalysisDB.created_at)
        .all()
    )
    existing_symbols = {item.symbol for item in existing_items}
    remaining_slots = max(0, MAX_SCHEDULED_ITEMS - len(existing_items))

    created: list[str] = []
    existing: list[str] = []
    skipped_limit: list[str] = []
    seen: set[str] = set()

    for raw_symbol in symbols:
        symbol = (raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        if symbol in existing_symbols:
            existing.append(symbol)  # 已存在
            continue

        if remaining_slots <= 0:
            skipped_limit.append(symbol)  # 达到上限
            continue

        # 创建新任务
        db.add(
            ScheduledAnalysisDB(
                id=uuid4().hex,
                user_id=user_id,
                symbol=symbol,
                horizon=horizon,
                trigger_time=trigger_time,
            )
        )
        existing_symbols.add(symbol)
        created.append(symbol)
        remaining_slots -= 1

    if created:
        db.flush()  # 刷新以获取新创建的记录

    return {
        "created": created,
        "existing": existing,
        "skipped_limit": skipped_limit,
    }


def update_scheduled(db: Session, user_id: str, item_id: str, **kwargs) -> Optional[dict]:
    """更新定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_id: 任务 ID
        **kwargs: 要更新的字段（is_active、horizon、trigger_time）
    
    Returns:
        更新后的定时任务字典，不存在返回 None
    """
    item = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.id == item_id, ScheduledAnalysisDB.user_id == user_id)
        .first()
    )
    if not item:
        return None

    _apply_scheduled_updates(item, **kwargs)

    db.commit()
    db.refresh(item)
    return _to_dict(item)


def batch_update_scheduled(
    db: Session,
    user_id: str,
    item_ids: Iterable[str],
    **kwargs,
) -> List[dict]:
    """批量更新定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_ids: 任务 ID 列表
        **kwargs: 要更新的字段
    
    Returns:
        更新后的定时任务字典列表
    
    Raises:
        ValueError: 任务 ID 列表为空、无更新字段、或部分任务不存在
    """
    normalized_ids = _normalize_item_ids(item_ids)
    if not normalized_ids:
        raise ValueError("请至少选择一个定时任务")
    if not kwargs:
        raise ValueError("至少提供一个更新字段")

    # 查询要更新的任务
    items = (
        db.query(ScheduledAnalysisDB)
        .filter(
            ScheduledAnalysisDB.user_id == user_id,
            ScheduledAnalysisDB.id.in_(normalized_ids),
        )
        .all()
    )
    item_map = {item.id: item for item in items}
    
    # 检查是否有不存在的任务
    missing_ids = [item_id for item_id in normalized_ids if item_id not in item_map]
    if missing_ids:
        raise ValueError("部分定时任务不存在或已失效，请刷新后重试")

    # 应用更新
    for item_id in normalized_ids:
        _apply_scheduled_updates(item_map[item_id], **kwargs)

    db.commit()
    for item in items:
        db.refresh(item)
    
    # 按请求顺序返回
    return [_to_dict(item_map[item_id]) for item_id in normalized_ids]


def delete_scheduled(db: Session, user_id: str, item_id: str) -> bool:
    """删除定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_id: 任务 ID
    
    Returns:
        是否删除成功
    """
    item = (
        db.query(ScheduledAnalysisDB)
        .filter(ScheduledAnalysisDB.id == item_id, ScheduledAnalysisDB.user_id == user_id)
        .first()
    )
    if not item:
        return False
    db.delete(item)
    db.commit()
    return True


def batch_delete_scheduled(db: Session, user_id: str, item_ids: Iterable[str]) -> dict:
    """批量删除定时分析任务。
    
    Args:
        db: 数据库会话
        user_id: 用户 ID
        item_ids: 任务 ID 列表
    
    Returns:
        删除结果字典，包含 deleted_ids 和 missing_ids
    
    Raises:
        ValueError: 任务 ID 列表为空
    """
    normalized_ids = _normalize_item_ids(item_ids)
    if not normalized_ids:
        raise ValueError("请至少选择一个定时任务")

    # 查询要删除的任务
    items = (
        db.query(ScheduledAnalysisDB)
        .filter(
            ScheduledAnalysisDB.user_id == user_id,
            ScheduledAnalysisDB.id.in_(normalized_ids),
        )
        .all()
    )
    item_map = {item.id: item for item in items}
    deleted_ids: list[str] = []
    missing_ids: list[str] = []

    # 执行删除
    for item_id in normalized_ids:
        item = item_map.get(item_id)
        if item is None:
            missing_ids.append(item_id)
            continue
        db.delete(item)
        deleted_ids.append(item_id)

    if deleted_ids:
        db.commit()

    return {
        "deleted_ids": deleted_ids,
        "missing_ids": missing_ids,
    }


def get_pending_tasks(db: Session, today: str, current_hhmm: str) -> List[ScheduledAnalysisDB]:
    """获取当天待执行的定时任务。
    
    条件：
    1. 任务处于激活状态
    2. 今天尚未执行过
    3. 触发时间已过
    
    Args:
        db: 数据库会话
        today: 今天日期（YYYY-MM-DD）
        current_hhmm: 当前时间（HH:MM）
    
    Returns:
        待执行的任务列表
    """
    all_active = (
        db.query(ScheduledAnalysisDB)
        .filter(
            ScheduledAnalysisDB.is_active == True,
            (ScheduledAnalysisDB.last_run_date != today) | (ScheduledAnalysisDB.last_run_date == None),
        )
        .all()
    )
    # 过滤触发时间已过的任务
    return [t for t in all_active if (t.trigger_time or "20:00") <= current_hhmm]


def mark_run_success(db: Session, item_id: str, trade_date: str, report_id: str):
    """标记定时任务执行成功。
    
    Args:
        db: 数据库会话
        item_id: 任务 ID
        trade_date: 交易日期
        report_id: 研报 ID
    """
    item = db.query(ScheduledAnalysisDB).filter(ScheduledAnalysisDB.id == item_id).first()
    if item:
        item.last_run_date = trade_date
        item.last_run_status = "success"
        item.last_report_id = report_id
        item.consecutive_failures = 0
        db.commit()


def mark_run_failed(db: Session, item_id: str, trade_date: str):
    """标记定时任务执行失败。
    
    连续失败 3 次自动禁用任务。
    
    Args:
        db: 数据库会话
        item_id: 任务 ID
        trade_date: 交易日期
    """
    item = db.query(ScheduledAnalysisDB).filter(ScheduledAnalysisDB.id == item_id).first()
    if item:
        item.last_run_date = trade_date
        item.last_run_status = "failed"
        item.consecutive_failures = (item.consecutive_failures or 0) + 1
        
        # 连续失败 3 次自动禁用
        if item.consecutive_failures >= 3:
            item.is_active = False
        
        db.commit()


def record_manual_test_result(
    db: Session,
    item_id: str,
    status: str,
    report_id: Optional[str] = None,
) -> None:
    """记录手动触发测试结果（不影响当天的定时调度）。
    
    Args:
        db: 数据库会话
        item_id: 任务 ID
        status: 状态（success/failed）
        report_id: 研报 ID（可选）
    """
    item = db.query(ScheduledAnalysisDB).filter(ScheduledAnalysisDB.id == item_id).first()
    if not item:
        return
    
    item.last_run_status = status
    if report_id:
        item.last_report_id = report_id
    if status == "success":
        item.consecutive_failures = 0  # 成功时重置连续失败次数
    
    db.commit()


def _to_dict(item: ScheduledAnalysisDB) -> dict:
    """将定时任务对象转换为字典。"""
    return {
        "id": item.id,
        "symbol": item.symbol,
        "horizon": item.horizon or "short",
        "trigger_time": item.trigger_time or "15:30",
        "is_active": item.is_active,
        "last_run_date": item.last_run_date,
        "last_run_status": item.last_run_status,
        "last_report_id": item.last_report_id,
        "consecutive_failures": item.consecutive_failures,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
