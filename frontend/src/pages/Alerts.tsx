import { useEffect, useState } from 'react'
import { Plus, Trash2, Bell, BellOff, ToggleLeft, ToggleRight, AlertTriangle } from 'lucide-react'
import { api } from '@/services/api'
import type { Alert, AlertCreateRequest, AlertUpdateRequest, AlertTriggerRequest } from '@/types'

const TRIGGER_TYPE_LABELS: Record<string, string> = {
    price_above: '价格突破',
    price_below: '价格跌破',
    daily_change_pct: '单日涨跌幅',
    unrealized_pnl_pct: '持仓盈亏',
}

export default function Alerts() {
    const [alerts, setAlerts] = useState<Alert[]>([])
    const [loading, setLoading] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [showCreate, setShowCreate] = useState(false)
    const [creating, setCreating] = useState(false)

    // 创建表单状态
    const [symbol, setSymbol] = useState('')
    const [name, setName] = useState('')
    const [triggers, setTriggers] = useState<AlertTriggerRequest[]>([
        { trigger_type: 'price_above', threshold: 0, enabled: true },
    ])

    const fetchAlerts = async () => {
        setLoading(true)
        setError(null)
        try {
            const response = await api.getAlerts()
            setAlerts(response.alerts || [])
        } catch (err) {
            setError(err instanceof Error ? err.message : '加载预警列表失败')
        } finally {
            setLoading(false)
        }
    }

    const handleCreate = async () => {
        if (!symbol.trim()) return
        setCreating(true)
        try {
            const request: AlertCreateRequest = {
                symbol: symbol.trim(),
                name: name.trim() || undefined,
                triggers: triggers.filter(t => t.enabled),
            }
            await api.createAlert(request)
            setShowCreate(false)
            setSymbol('')
            setName('')
            setTriggers([{ trigger_type: 'price_above', threshold: 0, enabled: true }])
            fetchAlerts()
        } catch (err) {
            setError(err instanceof Error ? err.message : '创建预警失败')
        } finally {
            setCreating(false)
        }
    }

    const handleToggle = async (alert: Alert) => {
        try {
            const request: AlertUpdateRequest = { is_active: !alert.is_active }
            await api.updateAlert(alert.id, request)
            fetchAlerts()
        } catch (err) {
            setError(err instanceof Error ? err.message : '更新预警失败')
        }
    }

    const handleDelete = async (alertId: string) => {
        try {
            await api.deleteAlert(alertId)
            fetchAlerts()
        } catch (err) {
            setError(err instanceof Error ? err.message : '删除预警失败')
        }
    }

    const addTrigger = () => {
        setTriggers([...triggers, { trigger_type: 'price_below', threshold: 0, enabled: true }])
    }

    const updateTrigger = (index: number, field: keyof AlertTriggerRequest, value: string | number | boolean) => {
        const updated = [...triggers]
        updated[index] = { ...updated[index], [field]: value }
        setTriggers(updated)
    }

    const removeTrigger = (index: number) => {
        setTriggers(triggers.filter((_, i) => i !== index))
    }

    useEffect(() => {
        fetchAlerts()
    }, [])

    return (
        <div className="space-y-6">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold text-slate-900 dark:text-slate-100">持仓预警</h1>
                    <p className="text-sm text-slate-500 mt-1">当持仓标的触发预设条件时，通过企业微信或钉钉推送通知</p>
                </div>
                <button
                    onClick={() => setShowCreate(!showCreate)}
                    className="btn-primary inline-flex items-center gap-2 text-sm"
                >
                    <Plus className="w-4 h-4" />
                    新建预警
                </button>
            </div>

            {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700 dark:border-rose-900/60 dark:bg-rose-950/30 dark:text-rose-300">
                    {error}
                </div>
            )}

            {/* 创建表单 */}
            {showCreate && (
                <div className="card p-6 space-y-4">
                    <h3 className="text-lg font-medium text-slate-900 dark:text-slate-100">新建预警</h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                                股票代码 <span className="text-rose-500">*</span>
                            </label>
                            <input
                                type="text"
                                value={symbol}
                                onChange={(e) => setSymbol(e.target.value)}
                                className="input w-full"
                                placeholder="如：600519.SH"
                            />
                        </div>
                        <div>
                            <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                                预警名称（可选）
                            </label>
                            <input
                                type="text"
                                value={name}
                                onChange={(e) => setName(e.target.value)}
                                className="input w-full"
                                placeholder="如：茅台止盈止损"
                            />
                        </div>
                    </div>

                    {/* 触发条件 */}
                    <div>
                        <label className="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-2">
                            触发条件
                        </label>
                        <div className="space-y-2">
                            {triggers.map((trigger, index) => (
                                <div key={index} className="flex items-center gap-2">
                                    <select
                                        value={trigger.trigger_type}
                                        onChange={(e) => updateTrigger(index, 'trigger_type', e.target.value)}
                                        className="input w-40"
                                    >
                                        <option value="price_above">价格突破</option>
                                        <option value="price_below">价格跌破</option>
                                        <option value="daily_change_pct">单日涨跌幅</option>
                                        <option value="unrealized_pnl_pct">持仓盈亏</option>
                                    </select>
                                    <input
                                        type="number"
                                        value={trigger.threshold}
                                        onChange={(e) => updateTrigger(index, 'threshold', parseFloat(e.target.value) || 0)}
                                        className="input w-24"
                                        placeholder="阈值"
                                        step="0.01"
                                    />
                                    <span className="text-sm text-slate-500">
                                        {trigger.trigger_type === 'daily_change_pct' || trigger.trigger_type === 'unrealized_pnl_pct' ? '%' : '元'}
                                    </span>
                                    <button
                                        onClick={() => removeTrigger(index)}
                                        className="text-slate-400 hover:text-rose-500"
                                    >
                                        <Trash2 className="w-4 h-4" />
                                    </button>
                                </div>
                            ))}
                        </div>
                        <button
                            onClick={addTrigger}
                            className="mt-2 text-sm text-blue-600 hover:text-blue-700 dark:text-blue-400"
                        >
                            + 添加条件
                        </button>
                    </div>

                    <div className="flex items-center gap-3 pt-2">
                        <button
                            onClick={handleCreate}
                            disabled={creating || !symbol.trim()}
                            className="btn-primary text-sm"
                        >
                            {creating ? '创建中...' : '创建预警'}
                        </button>
                        <button
                            onClick={() => setShowCreate(false)}
                            className="btn-secondary text-sm"
                        >
                            取消
                        </button>
                    </div>
                </div>
            )}

            {/* 预警列表 */}
            <div className="card overflow-hidden">
                {loading ? (
                    <div className="p-8 text-center text-slate-400">加载中...</div>
                ) : alerts.length === 0 ? (
                    <div className="p-8 text-center text-slate-400">
                        <AlertTriangle className="w-12 h-12 mx-auto mb-2 opacity-30" />
                        <p>暂无预警</p>
                        <p className="text-sm mt-1">点击右上角"新建预警"添加持仓预警</p>
                    </div>
                ) : (
                    <div className="divide-y divide-slate-200 dark:divide-slate-700">
                        {alerts.map((alert) => (
                            <div key={alert.id} className="p-4 hover:bg-slate-50 dark:hover:bg-slate-800/50">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-3">
                                        <div className={`p-2 rounded-lg ${alert.is_active ? 'bg-blue-50 dark:bg-blue-950/30' : 'bg-slate-100 dark:bg-slate-800'}`}>
                                            {alert.is_active ? (
                                                <Bell className="w-5 h-5 text-blue-600 dark:text-blue-400" />
                                            ) : (
                                                <BellOff className="w-5 h-5 text-slate-400" />
                                            )}
                                        </div>
                                        <div>
                                            <div className="font-medium text-slate-900 dark:text-slate-100">
                                                {alert.name || alert.symbol}
                                            </div>
                                            <div className="text-sm text-slate-500">
                                                {alert.symbol}
                                            </div>
                                        </div>
                                    </div>
                                    <div className="flex items-center gap-2">
                                        <button
                                            onClick={() => handleToggle(alert)}
                                            className="text-slate-400 hover:text-slate-600"
                                        >
                                            {alert.is_active ? (
                                                <ToggleRight className="w-5 h-5 text-blue-600" />
                                            ) : (
                                                <ToggleLeft className="w-5 h-5" />
                                            )}
                                        </button>
                                        <button
                                            onClick={() => handleDelete(alert.id)}
                                            className="text-slate-400 hover:text-rose-500"
                                        >
                                            <Trash2 className="w-4 h-4" />
                                        </button>
                                    </div>
                                </div>
                                {alert.triggers.length > 0 && (
                                    <div className="mt-3 flex flex-wrap gap-2">
                                        {alert.triggers.map((trigger) => (
                                            <span
                                                key={trigger.id}
                                                className="inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300"
                                            >
                                                {TRIGGER_TYPE_LABELS[trigger.trigger_type] || trigger.trigger_type}
                                                {trigger.threshold}
                                                {trigger.trigger_type.includes('pct') ? '%' : '元'}
                                            </span>
                                        ))}
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </div>
    )
}
