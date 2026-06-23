import { useEffect, useMemo, useState } from 'react'
import { TrendingUp, TrendingDown, Minus, RefreshCw, ChevronLeft, ChevronRight, Search } from 'lucide-react'
import { api } from '@/services/api'
import type { PredictionSnapshotResponse, PredictionAccuracyResponse } from '@/types'

type PredictionRow = PredictionSnapshotResponse

function formatNumber(value: number | null | undefined, decimals = 2): string {
    if (value === null || value === undefined) return '-'
    return value.toFixed(decimals)
}

function getDirectionLabel(direction: string): { label: string; color: string; icon: typeof TrendingUp } {
    const d = direction.toUpperCase()
    if (d.includes('BUY') || d.includes('多')) return { label: '看多', color: 'text-red-600 dark:text-red-400', icon: TrendingUp }
    if (d.includes('SELL') || d.includes('空')) return { label: '看空', color: 'text-green-600 dark:text-green-400', icon: TrendingDown }
    return { label: '中性', color: 'text-slate-600 dark:text-slate-400', icon: Minus }
}

function getAccuracyColor(correct: boolean | null | undefined): string {
    if (correct === true) return 'text-emerald-600 dark:text-emerald-400'
    if (correct === false) return 'text-rose-600 dark:text-rose-400'
    return 'text-slate-400'
}

export default function Predictions() {
    const PAGE_SIZE = 20

    const [predictions, setPredictions] = useState<PredictionRow[]>([])
    const [total, setTotal] = useState(0)
    const [page, setPage] = useState(0)
    const [loading, setLoading] = useState(false)
    const [accuracy, setAccuracy] = useState<PredictionAccuracyResponse | null>(null)
    const [accuracyLoading, setAccuracyLoading] = useState(false)
    const [searchSymbol, setSearchSymbol] = useState('')
    const [backfilling, setBackfilling] = useState(false)
    const [backfillResult, setBackfillResult] = useState<string | null>(null)
    const [error, setError] = useState<string | null>(null)

    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

    const fetchPredictions = async (targetPage: number, symbol?: string) => {
        setLoading(true)
        setError(null)
        try {
            const params: Record<string, unknown> = {
                limit: PAGE_SIZE,
                offset: targetPage * PAGE_SIZE,
            }
            if (symbol) params.symbol = symbol
            const response = await api.getPredictions(params)
            setPredictions(response.predictions || [])
            setTotal(response.total || 0)
            setPage(targetPage)
        } catch (err) {
            setError(err instanceof Error ? err.message : '加载预测记录失败')
        } finally {
            setLoading(false)
        }
    }

    const fetchAccuracy = async (symbol?: string) => {
        setAccuracyLoading(true)
        try {
            const params: Record<string, unknown> = {}
            if (symbol) params.symbol = symbol
            const response = await api.getPredictionAccuracy(params)
            setAccuracy(response)
        } catch {
            // accuracy fetch failure is non-critical
        } finally {
            setAccuracyLoading(false)
        }
    }

    const handleSearch = () => {
        setPage(0)
        fetchPredictions(0, searchSymbol.trim() || undefined)
        fetchAccuracy(searchSymbol.trim() || undefined)
    }

    const handleBackfill = async () => {
        setBackfilling(true)
        setBackfillResult(null)
        try {
            await api.triggerBackfill({ limit: 200 })
            setBackfillResult('回填任务已提交，请稍后刷新查看结果')
            // 3 秒后自动刷新
            setTimeout(() => {
                fetchPredictions(page, searchSymbol.trim() || undefined)
                fetchAccuracy(searchSymbol.trim() || undefined)
            }, 3000)
        } catch (err) {
            setBackfillResult(err instanceof Error ? err.message : '回填任务提交失败')
        } finally {
            setBackfilling(false)
        }
    }

    useEffect(() => {
        fetchPredictions(0)
        fetchAccuracy()
    }, [])

    const accuracyStats = useMemo(() => {
        if (!accuracy) return null
        return [
            { label: '总预测数', value: accuracy.total.toString(), color: 'text-slate-700 dark:text-slate-200' },
            { label: '正确数', value: accuracy.correct.toString(), color: 'text-emerald-600 dark:text-emerald-400' },
            { label: '方向准确率', value: `${(accuracy.accuracy * 100).toFixed(1)}%`, color: accuracy.accuracy >= 0.5 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400' },
            { label: 'T+1 平均收益', value: accuracy.t1_avg_return !== null ? `${formatNumber(accuracy.t1_avg_return)}%` : '-', color: (accuracy.t1_avg_return || 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400' },
            { label: 'T+5 平均收益', value: accuracy.t5_avg_return !== null ? `${formatNumber(accuracy.t5_avg_return)}%` : '-', color: (accuracy.t5_avg_return || 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400' },
            { label: 'T+20 平均收益', value: accuracy.t20_avg_return !== null ? `${formatNumber(accuracy.t20_avg_return)}%` : '-', color: (accuracy.t20_avg_return || 0) >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400' },
        ]
    }, [accuracy])

    return (
        <div className="space-y-6">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">预测追踪</h1>
                    <p className="text-sm text-slate-500 mt-1">追踪每次分析预测的准确性，持续优化决策质量</p>
                </div>
                <button
                    onClick={handleBackfill}
                    disabled={backfilling}
                    className="btn-secondary inline-flex items-center gap-2 text-sm"
                >
                    {backfilling ? <RefreshCw className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                    {backfilling ? '回填中...' : '手动回填'}
                </button>
            </div>

            {/* 准确率统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
                {accuracyStats?.map((stat) => (
                    <div key={stat.label} className="card p-4">
                        <div className="text-xs text-slate-500 mb-1">{stat.label}</div>
                        <div className={`text-xl font-bold ${stat.color}`}>
                            {accuracyLoading ? '-' : stat.value}
                        </div>
                    </div>
                ))}
            </div>

            {backfillResult && (
                <div className={`rounded-lg px-4 py-3 text-sm ${backfillResult.includes('失败') ? 'bg-rose-50 text-rose-700 dark:bg-rose-950/30 dark:text-rose-300' : 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300'}`}>
                    {backfillResult}
                </div>
            )}

            {/* 搜索栏 */}
            <div className="flex items-center gap-3">
                <div className="relative flex-1 max-w-md">
                    <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                    <input
                        type="text"
                        value={searchSymbol}
                        onChange={(e) => setSearchSymbol(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
                        className="input w-full pl-10"
                        placeholder="按股票代码筛选..."
                    />
                </div>
                <button onClick={handleSearch} className="btn-primary text-sm">
                    搜索
                </button>
            </div>

            {/* 预测列表 */}
            <div className="card overflow-hidden">
                {error && (
                    <div className="p-4 text-sm text-rose-600 bg-rose-50 dark:bg-rose-950/30">
                        {error}
                    </div>
                )}

                <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                        <thead>
                            <tr className="border-b border-slate-200 dark:border-slate-700">
                                <th className="text-left py-3 px-4 font-medium text-slate-500">标的</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">预测日期</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">方向</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">置信度</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">目标价</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">止损价</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">T+1 收益</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">T+5 收益</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">T+20 收益</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">方向正确</th>
                                <th className="text-left py-3 px-4 font-medium text-slate-500">风控裁决</th>
                            </tr>
                        </thead>
                        <tbody>
                            {loading ? (
                                <tr>
                                    <td colSpan={11} className="text-center py-12 text-slate-400">
                                        加载中...
                                    </td>
                                </tr>
                            ) : predictions.length === 0 ? (
                                <tr>
                                    <td colSpan={11} className="text-center py-12 text-slate-400">
                                        暂无预测记录
                                    </td>
                                </tr>
                            ) : (
                                predictions.map((pred) => {
                                    const direction = getDirectionLabel(pred.direction)
                                    const DirIcon = direction.icon
                                    return (
                                        <tr key={pred.id} className="border-b border-slate-100 dark:border-slate-800 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                                            <td className="py-3 px-4 font-mono">{pred.symbol}</td>
                                            <td className="py-3 px-4">{pred.trade_date}</td>
                                            <td className="py-3 px-4">
                                                <div className={`flex items-center gap-1.5 ${direction.color}`}>
                                                    <DirIcon className="w-4 h-4" />
                                                    <span className="font-medium">{direction.label}</span>
                                                </div>
                                            </td>
                                            <td className="py-3 px-4">{pred.confidence !== null ? `${pred.confidence}%` : '-'}</td>
                                            <td className="py-3 px-4">{formatNumber(pred.target_price)}</td>
                                            <td className="py-3 px-4">{formatNumber(pred.stop_loss_price)}</td>
                                            <td className={`py-3 px-4 ${pred.return_t1 !== null && pred.return_t1 !== undefined ? (pred.return_t1 >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400') : 'text-slate-400'}`}>
                                                {pred.return_t1 !== null && pred.return_t1 !== undefined ? `${formatNumber(pred.return_t1)}%` : '-'}
                                            </td>
                                            <td className={`py-3 px-4 ${pred.return_t5 !== null && pred.return_t5 !== undefined ? (pred.return_t5 >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400') : 'text-slate-400'}`}>
                                                {pred.return_t5 !== null && pred.return_t5 !== undefined ? `${formatNumber(pred.return_t5)}%` : '-'}
                                            </td>
                                            <td className={`py-3 px-4 ${pred.return_t20 !== null && pred.return_t20 !== undefined ? (pred.return_t20 >= 0 ? 'text-emerald-600 dark:text-emerald-400' : 'text-rose-600 dark:text-rose-400') : 'text-slate-400'}`}>
                                                {pred.return_t20 !== null && pred.return_t20 !== undefined ? `${formatNumber(pred.return_t20)}%` : '-'}
                                            </td>
                                            <td className={`py-3 px-4 ${getAccuracyColor(pred.direction_correct)}`}>
                                                {pred.direction_correct === true ? '✓' : pred.direction_correct === false ? '✗' : '-'}
                                            </td>
                                            <td className="py-3 px-4">
                                                {pred.risk_verdict ? (
                                                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium ${
                                                        pred.risk_verdict === 'pass' ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/30 dark:text-emerald-300' :
                                                        pred.risk_verdict === 'reject' ? 'bg-rose-100 text-rose-700 dark:bg-rose-950/30 dark:text-rose-300' :
                                                        'bg-amber-100 text-amber-700 dark:bg-amber-950/30 dark:text-amber-300'
                                                    }`}>
                                                        {pred.risk_verdict === 'pass' ? '通过' : pred.risk_verdict === 'reject' ? '拒绝' : '修订'}
                                                    </span>
                                                ) : '-'}
                                            </td>
                                        </tr>
                                    )
                                })
                            )}
                        </tbody>
                    </table>
                </div>

                {/* 分页 */}
                {totalPages > 1 && (
                    <div className="flex items-center justify-between px-4 py-3 border-t border-slate-200 dark:border-slate-700">
                        <div className="text-sm text-slate-500">
                            共 {total} 条记录，第 {page + 1} / {totalPages} 页
                        </div>
                        <div className="flex items-center gap-2">
                            <button
                                onClick={() => fetchPredictions(page - 1, searchSymbol.trim() || undefined)}
                                disabled={page === 0 || loading}
                                className="btn-secondary text-sm disabled:opacity-50"
                            >
                                <ChevronLeft className="w-4 h-4" />
                                上一页
                            </button>
                            <button
                                onClick={() => fetchPredictions(page + 1, searchSymbol.trim() || undefined)}
                                disabled={page >= totalPages - 1 || loading}
                                className="btn-secondary text-sm disabled:opacity-50"
                            >
                                下一页
                                <ChevronRight className="w-4 h-4" />
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}
