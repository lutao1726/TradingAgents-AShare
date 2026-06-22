"""
回测服务模块：运行历史分析并比较决策与后续价格表现。

核心功能：
1. 历史分析：在指定日期范围运行分析任务
2. 决策分类：将分析结果分类为 BUY/SELL/HOLD
3. 收益计算：计算每个决策的后续收益
4. 统计分析：计算胜率、平均收益等统计数据

设计特点：
- 完全非侵入式：复用现有 TradingAgentsGraph.propagate()，不修改任何现有代码
- 内存存储：结果以 JSON 存储在内存中，无需额外数据库表
- 后台执行：回测在后台线程中运行，不阻塞 API

使用场景：
- 验证分析策略的历史表现
- 优化分析师组合
- 评估不同持仓周期的收益
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

# ──────────────────────────────────────────────────────────────────────────────
# 内存存储（无额外数据库表 — 结果以 JSON 存储在 job 中）
# ──────────────────────────────────────────────────────────────────────────────
_backtest_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _utcnow_iso() -> str:
    """获取当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _set(job_id: str, **kwargs: Any) -> None:
    """更新回测任务的状态。"""
    with _lock:
        if job_id not in _backtest_jobs:
            _backtest_jobs[job_id] = {}
        _backtest_jobs[job_id].update(kwargs)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """获取回测任务的状态。"""
    return _backtest_jobs.get(job_id)


def list_jobs() -> List[Dict[str, Any]]:
    """获取所有回测任务列表（按创建时间降序）。"""
    with _lock:
        return sorted(_backtest_jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)


def delete_job(job_id: str) -> bool:
    """删除回测任务。"""
    with _lock:
        if job_id in _backtest_jobs:
            del _backtest_jobs[job_id]
            return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# 交易日工具函数（轻量级 — 无交易所依赖）
# ──────────────────────────────────────────────────────────────────────────────

def _get_trading_dates(start: str, end: str, interval_days: int) -> List[str]:
    """获取指定范围内的交易日列表（仅工作日）。
    
    Args:
        start: 开始日期（YYYY-MM-DD）
        end: 结束日期（YYYY-MM-DD）
        interval_days: 采样间隔天数
    
    Returns:
        交易日列表
    """
    fmt = "%Y-%m-%d"
    cur = datetime.strptime(start, fmt)
    end_dt = datetime.strptime(end, fmt)
    dates = []
    while cur <= end_dt:
        if cur.weekday() < 5:  # 仅周一至周五
            dates.append(cur.strftime(fmt))
        cur += timedelta(days=interval_days)
    return dates


def _get_price_after(symbol: str, base_date: str, hold_days: int) -> Optional[float]:
    """获取基准日期后 hold_days 个交易日的收盘价。
    
    Args:
        symbol: 股票代码
        base_date: 基准日期（YYYY-MM-DD）
        hold_days: 持仓天数
    
    Returns:
        收盘价，获取失败返回 None
    """
    try:
        import akshare as ak
        from tradingagents.dataflows.interface import route_to_vendor
        import pandas as pd

        fmt = "%Y-%m-%d"
        start_dt = datetime.strptime(base_date, fmt)
        # 从基准日期 +1 天开始获取数据，扩展窗口以覆盖 hold_days
        fetch_start = (start_dt + timedelta(days=1)).strftime(fmt)
        fetch_end = (start_dt + timedelta(days=hold_days + 30)).strftime(fmt)

        csv_data = route_to_vendor("get_stock_data", symbol, fetch_start, fetch_end)
        if not csv_data:
            return None

        df = pd.read_csv(pd.io.common.StringIO(csv_data))
        # 查找收盘价列
        close_cols = [c for c in df.columns if "close" in c.lower() or "收盘" in c]
        date_cols = [c for c in df.columns if "date" in c.lower() or "日期" in c or "time" in c.lower()]
        if not close_cols or not date_cols:
            return None

        df = df.sort_values(date_cols[0]).reset_index(drop=True)
        if len(df) < hold_days:
            hold_days = len(df) - 1
        if hold_days < 1:
            return None
        return float(df[close_cols[0]].iloc[hold_days - 1])
    except Exception:
        return None


def _get_price_on(symbol: str, date: str) -> Optional[float]:
    """获取指定日期或之前的收盘价。
    
    Args:
        symbol: 股票代码
        date: 日期（YYYY-MM-DD）
    
    Returns:
        收盘价，获取失败返回 None
    """
    try:
        from tradingagents.dataflows.interface import route_to_vendor
        import pandas as pd

        fmt = "%Y-%m-%d"
        start = (datetime.strptime(date, fmt) - timedelta(days=5)).strftime(fmt)
        csv_data = route_to_vendor("get_stock_data", symbol, start, date)
        if not csv_data:
            return None
        df = pd.read_csv(pd.io.common.StringIO(csv_data))
        close_cols = [c for c in df.columns if "close" in c.lower() or "收盘" in c]
        date_cols = [c for c in df.columns if "date" in c.lower() or "日期" in c or "time" in c.lower()]
        if not close_cols or not date_cols:
            return None
        df = df.sort_values(date_cols[0]).reset_index(drop=True)
        return float(df[close_cols[0]].iloc[-1])
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 核心回测运行器
# ──────────────────────────────────────────────────────────────────────────────

def _run_single_analysis(symbol: str, trade_date: str, selected_analysts: List[str], config: Dict[str, Any]) -> Dict[str, Any]:
    """运行单次分析（无 SSE）。
    
    Args:
        symbol: 股票代码
        trade_date: 交易日期
        selected_analysts: 选择的分析师列表
        config: 配置字典
    
    Returns:
        分析结果字典
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.dataflows.config import set_config

    set_config(config)
    graph = TradingAgentsGraph(
        selected_analysts=selected_analysts,
        debug=False,
        config=config,
    )
    final_state, _ = graph.propagate(symbol, trade_date)
    decision_raw = final_state.get("final_trade_decision", "")
    decision = graph.process_signal(decision_raw)
    return {
        "final_trade_decision": decision_raw,
        "decision": decision,
    }


def _classify_decision(decision: str) -> str:
    """将决策分类为 BUY / SELL / HOLD。
    
    Args:
        decision: 决策文本
    
    Returns:
        分类后的决策（BUY/SELL/HOLD）
    """
    d = decision.upper()
    if any(k in d for k in ["BUY", "增持", "买入", "BULLISH"]):
        return "BUY"
    if any(k in d for k in ["SELL", "减持", "卖出", "BEARISH"]):
        return "SELL"
    return "HOLD"


def _compute_stats(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """计算回测统计数据。
    
    Args:
        records: 回测记录列表
    
    Returns:
        统计数据字典，包含：
        - total_signals: 交易信号总数
        - win_rate: 胜率（%）
        - avg_return_pct: 平均收益率（%）
        - best_return_pct: 最高收益率（%）
        - worst_return_pct: 最低收益率（%）
    """
    trades = [r for r in records if r.get("action") in ("BUY", "SELL") and r.get("return_pct") is not None]
    if not trades:
        return {"total_signals": 0, "win_rate": None, "avg_return_pct": None, "best_return_pct": None, "worst_return_pct": None}

    wins = 0
    returns = []
    for t in trades:
        ret = t["return_pct"]
        returns.append(ret)
        # 盈利条件：BUY 时正收益，SELL 时负收益
        if t["action"] == "BUY" and ret > 0:
            wins += 1
        elif t["action"] == "SELL" and ret < 0:
            wins += 1

    return {
        "total_signals": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_return_pct": round(sum(returns) / len(returns), 2),
        "best_return_pct": round(max(returns), 2),
        "worst_return_pct": round(min(returns), 2),
    }


def _run_backtest(job_id: str, symbol: str, start_date: str, end_date: str,
                  selected_analysts: List[str], hold_days: int, sample_interval: int,
                  config: Dict[str, Any]) -> None:
    """后台线程：运行回测并存储结果。
    
    流程：
    1. 获取交易日列表
    2. 遍历每个交易日
    3. 运行分析任务
    4. 分类决策
    5. 计算收益
    6. 存储结果
    7. 计算统计数据
    """
    _set(job_id, status="running", started_at=_utcnow_iso())

    dates = _get_trading_dates(start_date, end_date, sample_interval)
    total = len(dates)
    _set(job_id, total_dates=total, completed_dates=0, records=[], error=None)

    records: List[Dict[str, Any]] = []

    for i, trade_date in enumerate(dates):
        record: Dict[str, Any] = {"date": trade_date, "action": "HOLD", "return_pct": None, "error": None}
        try:
            # 运行分析
            analysis = _run_single_analysis(symbol, trade_date, selected_analysts, config)
            action = _classify_decision(analysis["decision"])
            record["action"] = action
            record["decision_summary"] = analysis["final_trade_decision"][:200] if analysis.get("final_trade_decision") else ""

            # 计算收益（仅 BUY/SELL）
            if action in ("BUY", "SELL"):
                entry_price = _get_price_on(symbol, trade_date)
                exit_price = _get_price_after(symbol, trade_date, hold_days)
                if entry_price and exit_price and entry_price > 0:
                    raw_return = (exit_price - entry_price) / entry_price * 100
                    record["entry_price"] = round(entry_price, 2)
                    record["exit_price"] = round(exit_price, 2)
                    record["return_pct"] = round(raw_return if action == "BUY" else -raw_return, 2)
        except Exception as exc:
            record["error"] = str(exc)[:200]

        records.append(record)
        _set(job_id, completed_dates=i + 1, records=list(records))

    # 计算统计数据
    stats = _compute_stats(records)
    _set(job_id,
         status="completed",
         finished_at=_utcnow_iso(),
         records=records,
         stats=stats)


def submit(
    symbol: str,
    start_date: str,
    end_date: str,
    selected_analysts: List[str],
    hold_days: int,
    sample_interval: int,
    config: Dict[str, Any],
) -> str:
    """提交回测任务。
    
    Args:
        symbol: 股票代码
        start_date: 开始日期（YYYY-MM-DD）
        end_date: 结束日期（YYYY-MM-DD）
        selected_analysts: 选择的分析师列表
        hold_days: 持仓天数
        sample_interval: 采样间隔天数
        config: 配置字典
    
    Returns:
        任务 ID
    """
    job_id = uuid4().hex
    _set(job_id,
         job_id=job_id,
         symbol=symbol,
         start_date=start_date,
         end_date=end_date,
         selected_analysts=selected_analysts,
         hold_days=hold_days,
         sample_interval=sample_interval,
         status="pending",
         created_at=_utcnow_iso(),
         total_dates=0,
         completed_dates=0,
         records=[],
         stats=None,
         error=None)

    # 在后台线程中运行回测
    thread = threading.Thread(
        target=_run_backtest,
        args=(job_id, symbol, start_date, end_date, selected_analysts, hold_days, sample_interval, config),
        daemon=True,
    )
    thread.start()
    return job_id
