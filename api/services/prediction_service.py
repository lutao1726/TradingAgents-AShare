"""
预测准确性追踪服务模块。

核心功能：
1. 预测快照回填：T+1/T+5/T+20 实际价格回填
2. 收益计算：基于预测日收盘价计算各周期收益率
3. 准确率统计：方向准确率、置信度校准度等
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests
from sqlalchemy import or_

from api.database import PredictionSnapshotDB, get_db_ctx

logger = logging.getLogger(__name__)


def _fetch_close(symbol: str, trade_date: datetime) -> Optional[float]:
    """获取指定日期的收盘价。

    优先使用 AKShare，失败时 fallback 到 BaoStock。

    Args:
        symbol: 股票代码（如 600519.SH）
        trade_date: 目标日期

    Returns:
        收盘价，获取失败返回 None
    """
    date_str_ak = trade_date.strftime("%Y%m%d")
    date_str_bs = trade_date.strftime("%Y-%m-%d")
    std_code = symbol.split(".")[0]

    # 尝试 AKShare
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(symbol=std_code, period="daily",
                                 start_date=date_str_ak, end_date=date_str_ak, adjust="")
        if df is not None and not df.empty:
            return float(df.iloc[0]["收盘"])
    except Exception as exc:
        logger.debug("[Prediction] AKShare fetch failed for %s %s: %s", symbol, date_str_ak, exc)

    # Fallback: BaoStock
    try:
        import baostock as bs

        code = std_code
        if code.startswith(("5", "6", "9")):
            bs_code = f"sh.{code}"
        else:
            bs_code = f"sz.{code}"

        lg = bs.login()
        if getattr(lg, "error_code", "1") == "0":
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,close",
                    start_date=date_str_bs,
                    end_date=date_str_bs,
                    frequency="d",
                    adjustflag="2",
                )
                if getattr(rs, "error_code", "1") == "0":
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                    if rows:
                        df = pd.DataFrame(rows, columns=rs.fields)
                        if "close" in df.columns and not df.empty:
                            val = df.iloc[0]["close"]
                            if val and str(val).strip() and str(val).strip() != '-':
                                return float(val)
            finally:
                bs.logout()
    except Exception as exc:
        logger.debug("[Prediction] BaoStock fetch failed for %s %s: %s", symbol, date_str_bs, exc)

    return None


def backfill_prediction(db, snapshot_id: str) -> dict:
    """对单条预测回填 T+1/T+5/T+20 价格。

    Args:
        db: 数据库会话
        snapshot_id: 预测快照 ID

    Returns:
        回填结果字典
    """
    snapshot = db.query(PredictionSnapshotDB).filter_by(id=snapshot_id).first()
    if not snapshot:
        return {"status": "skipped", "reason": "not_found"}

    trade_date = datetime.strptime(snapshot.trade_date, "%Y-%m-%d")
    today = datetime.now().date()

    # 获取预测日收盘价作为基准
    base_close = _fetch_close(snapshot.symbol, trade_date)
    if base_close is None:
        return {"status": "skipped", "reason": "base_price_not_found"}

    results = {}
    has_update = False

    for label, offset_days in [("t1", 1), ("t5", 5), ("t20", 20)]:
        # 如果已经有数据，跳过
        if getattr(snapshot, f"return_{label}") is not None:
            continue

        target_date = trade_date + timedelta(days=offset_days)
        # 只回填已经过去的日期
        if target_date.date() > today:
            continue

        close = _fetch_close(snapshot.symbol, target_date)
        if close is not None:
            return_pct = (close - base_close) / base_close * 100
            setattr(snapshot, f"actual_close_{label}", close)
            setattr(snapshot, f"return_{label}", return_pct)
            results[label] = {"close": close, "return_pct": return_pct}
            has_update = True

    # 方向准确率（基于 T+1）
    if snapshot.return_t1 is not None and snapshot.direction_correct is None:
        predicted_up = snapshot.direction in ("BUY", "看多", "偏多", "买入")
        actual_up = snapshot.return_t1 > 0
        snapshot.direction_correct = (predicted_up == actual_up)
        has_update = True

    if has_update:
        snapshot.backfilled_at = datetime.utcnow()
        db.commit()

    return {"status": "ok", "base_close": base_close, "results": results}


def backfill_pending(limit: int = 100) -> dict:
    """批量回填所有未完全回填的预测。

    Args:
        limit: 最多处理条数

    Returns:
        回填统计
    """
    stats = {"total": 0, "ok": 0, "skipped": 0, "failed": 0}

    with get_db_ctx() as db:
        pending = db.query(PredictionSnapshotDB).filter(
            or_(
                PredictionSnapshotDB.return_t1.is_(None),
                PredictionSnapshotDB.return_t5.is_(None),
                PredictionSnapshotDB.return_t20.is_(None),
            )
        ).order_by(PredictionSnapshotDB.trade_date.asc()).limit(limit).all()

        stats["total"] = len(pending)
        for snap in pending:
            try:
                result = backfill_prediction(db, snap.id)
                if result.get("status") == "ok":
                    stats["ok"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as exc:
                logger.error("[Prediction] Backfill failed for %s: %s", snap.id, exc)
                stats["failed"] += 1

    return stats


def compute_accuracy(user_id: Optional[str] = None, symbol: Optional[str] = None) -> dict:
    """计算预测准确率统计。

    Args:
        user_id: 用户 ID（None 表示全部用户）
        symbol: 股票代码（None 表示全部标的）

    Returns:
        准确率统计字典
    """
    with get_db_ctx() as db:
        query = db.query(PredictionSnapshotDB).filter(
            PredictionSnapshotDB.backfilled_at.isnot(None),
            PredictionSnapshotDB.direction_correct.isnot(None),
        )

        if user_id:
            query = query.filter(PredictionSnapshotDB.user_id == user_id)
        if symbol:
            query = query.filter(PredictionSnapshotDB.symbol == symbol)

        snapshots = query.all()

        if not snapshots:
            return {
                "total": 0,
                "correct": 0,
                "accuracy": 0.0,
                "t1_avg_return": None,
                "t5_avg_return": None,
                "t20_avg_return": None,
                "confidence_calibration": {},
            }

        total = len(snapshots)
        correct = sum(1 for s in snapshots if s.direction_correct)
        accuracy = correct / total if total > 0 else 0.0

        # 按周期统计
        t1_returns = [s.return_t1 for s in snapshots if s.return_t1 is not None]
        t5_returns = [s.return_t5 for s in snapshots if s.return_t5 is not None]
        t20_returns = [s.return_t20 for s in snapshots if s.return_t20 is not None]

        # 置信度校准度
        confidence_buckets = {}
        for s in snapshots:
            if s.confidence is None:
                continue
            bucket = (s.confidence // 10) * 10  # 0-10, 10-20, ...
            bucket_key = str(bucket)
            if bucket_key not in confidence_buckets:
                confidence_buckets[bucket_key] = {"total": 0, "correct": 0}
            confidence_buckets[bucket_key]["total"] += 1
            if s.direction_correct:
                confidence_buckets[bucket_key]["correct"] += 1

        return {
            "total": total,
            "correct": correct,
            "accuracy": round(accuracy, 4),
            "t1_avg_return": round(sum(t1_returns) / len(t1_returns), 4) if t1_returns else None,
            "t5_avg_return": round(sum(t5_returns) / len(t5_returns), 4) if t5_returns else None,
            "t20_avg_return": round(sum(t20_returns) / len(t20_returns), 4) if t20_returns else None,
            "confidence_calibration": {
                k: {
                    "total": v["total"],
                    "accuracy": round(v["correct"] / v["total"], 4) if v["total"] > 0 else 0.0
                }
                for k, v in confidence_buckets.items()
            },
        }


def get_prediction_history(user_id: Optional[str] = None, symbol: Optional[str] = None,
                           limit: int = 50, offset: int = 0) -> list[dict]:
    """获取预测历史记录。

    Args:
        user_id: 用户 ID
        symbol: 股票代码
        limit: 返回条数限制
        offset: 分页偏移

    Returns:
        预测快照字典列表
    """
    with get_db_ctx() as db:
        query = db.query(PredictionSnapshotDB)

        if user_id:
            query = query.filter(PredictionSnapshotDB.user_id == user_id)
        if symbol:
            query = query.filter(PredictionSnapshotDB.symbol == symbol)

        snapshots = query.order_by(PredictionSnapshotDB.created_at.desc()).limit(limit).offset(offset).all()
        return [s.to_dict() for s in snapshots]


def save_prediction_snapshot(db, job_id: str, user_id: Optional[str], symbol: str,
                             trade_date: str, result: dict) -> Optional[PredictionSnapshotDB]:
    """保存预测快照。

    Args:
        db: 数据库会话
        job_id: 任务 ID
        user_id: 用户 ID
        symbol: 股票代码
        trade_date: 交易日期
        result: 分析结果字典

    Returns:
        预测快照对象
    """
    # 提取 risk_verdict
    risk_verdict = None
    risk_feedback = result.get("risk_feedback_state")
    if isinstance(risk_feedback, dict):
        risk_verdict = risk_feedback.get("latest_risk_verdict")
    if risk_verdict is None and isinstance(risk_feedback, str):
        risk_verdict = risk_feedback

    snapshot = PredictionSnapshotDB(
        id=job_id,
        user_id=user_id,
        report_id=job_id,
        symbol=symbol,
        trade_date=trade_date,
        direction=result.get("decision", "UNKNOWN"),
        confidence=result.get("confidence"),
        target_price=result.get("target_price"),
        stop_loss_price=result.get("stop_loss_price"),
        analyst_traces=result.get("analyst_traces"),
        risk_verdict=risk_verdict,
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot
