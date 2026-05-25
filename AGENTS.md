# Agents 架构文档

本文档详细描述 TradingAgents-AShare 多智能体系统中所有 Agent 的角色定义、职责边界、数据来源、LLM 策略、协作流程以及前端架构。

---

## 目录

- [系统总览](#系统总览)
- [技术栈](#技术栈)
- [LLM 策略](#llm-策略)
- [记忆系统](#记忆系统)
- [Agent 一览](#agent-一览)
- [第一层：分析师团队（Analyst Team）](#第一层分析师团队analyst-team)
- [第二层：研究员团队（Research Team）](#第二层研究员团队research-team)
- [第三层：决策层（Decision Layer）](#第三层决策层decision-layer)
- [第四层：风控博弈层（Risk Layer）](#第四层风控博弈层risk-layer)
- [协作流程图](#协作流程图)
- [状态流转](#状态流转)
- [前端架构](#前端架构)
- [API 集成](#api-集成)
- [相关源码索引](#相关源码索引)

---

## 系统总览

TradingAgents-AShare 模拟真实投研机构的部门协作，将投资决策拆解为 **四个层级、十五名智能体** 的流水线：

```
用户意图 → 数据采集 → 分析师并行分析 → 多空辩论 → 研究总监裁决
→ 交易员生成方案 → 三方风控辩论 → 风控裁决 → 终态信号输出
```

每一名 Agent 都是一个 LangGraph 节点，通过共享的 `AgentState` 进行数据传递，由 `StateGraph` 编排执行顺序与条件跳转。

### 功能特性

- **辩论对战可视化**：点击 Agent 卡片即可打开辩论 Drawer，实时观看多空对抗与风控三方辩论
- **意图驱动的自然语言交互**：直接输入"调研茅台短线"即可自动识别标的、解析投资周期
- **自选股与定时分析**：数据库持久化自选列表，支持批量加入股票、自定义周期与触发时间
- **持仓追踪与跟踪看板**：支持导入持仓数据，实时查看价格、当日区间、持仓盈亏
  - **全量保存**：替换所有持仓
  - **追加新标的**：仅添加新标的，保留原有持仓
  - **删除单个持仓**：按标的代码删除
  - **清空持仓**：一键清除所有持仓
  - **截图识别**：上传券商持仓截图自动解析
- **结构化研报管理**：分析结果结构化存储，支持按标的、日期检索历史研报
- **多模型厂商支持**：OpenAI、Anthropic、Google Gemini、DeepSeek、Moonshot、智谱、硅基流动等
- **股票代码自动识别**：支持沪深北交所股票、ETF 基金、可转债等全品种代码自动映射交易所

---

## 技术栈

### 后端

| 组件 | 技术 | 说明 |
|------|------|------|
| **框架** | FastAPI | 高性能异步 Web 框架 |
| **Agent 编排** | LangGraph | 基于 LangChain 的有向图状态机 |
| **数据库** | SQLite (SQLAlchemy ORM) | 轻量级关系型数据库 |
| **LLM 客户端** | LangChain OpenAI/Anthropic/Google | 多模型厂商统一接口 |
| **数据源** | AKShare、BaoStock、yfinance | A 股行情、财务、新闻数据 |
| **任务调度** | APScheduler | 定时分析任务调度 |
| **记忆系统** | BM25 (rank_bm25) | 基于词法相似度的经验检索 |

### 前端

| 组件 | 技术 | 说明 |
|------|------|------|
| **框架** | React 18 + TypeScript | 类型安全的组件化开发 |
| **构建工具** | Vite | 快速开发与热更新 |
| **路由** | React Router v7 | 单页应用路由管理 |
| **状态管理** | Zustand | 轻量级状态管理库 |
| **样式** | Tailwind CSS v4 | 原子化 CSS 框架 |
| **图表** | Lightweight Charts、Recharts | K 线图与数据可视化 |
| **流程图** | @xyflow/react | Agent 协作流程可视化 |
| **Markdown** | react-markdown + remark-gfm | 研报内容渲染 |
| **图标** | Lucide React | 统一图标库 |
| **SSE** | EventSource | Token 级流式输出 |

---

## LLM 策略

系统采用 **双模型策略**，根据任务复杂度分配不同级别的 LLM：

| 模型层级 | 配置键 | 典型模型 | 使用场景 |
|---------|--------|---------|---------|
| **深度思考** | `deep_think_llm` | GPT-4o | 研究总监裁决、风控裁决 |
| **快速思考** | `quick_think_llm` | GPT-4o-mini | 分析师报告、研究员辩论、交易员方案、风控辩论 |

所有 Agent 均支持 Token 级流式输出，通过 `current_tracker_var` 上下文变量将实时 token 推送至前端。

---

## 记忆系统

系统维护五组独立的 BM25 记忆实例，用于跨会话的经验积累：

| 记忆实例 | 关联 Agent | 用途 |
|---------|-----------|------|
| `bull_memory` | Bull Researcher | 存储多头分析情境与建议 |
| `bear_memory` | Bear Researcher | 存储空头分析情境与建议 |
| `trader_memory` | Trader | 存储交易决策情境与建议 |
| `invest_judge_memory` | Research Manager | 存储投资裁决情境与建议 |
| `risk_manager_memory` | Risk Manager | 存储风控裁决情境与建议 |

记忆检索使用 BM25 算法，无需外部 API 调用，离线可用。每次分析完成后，`Reflector` 会根据实际收益对各 Agent 的决策进行反思，并写入对应记忆。

---

## Agent 一览

| # | Agent 名称 | 中文名 | 层级 | LLM | 分析周期 |
|---|-----------|--------|------|-----|---------|
| 1 | Fundamentals Analyst | 基本面分析师 | 分析师 | Quick | 中线 |
| 2 | News Analyst | 新闻分析师 | 分析师 | Quick | 短线 |
| 3 | Social Media Analyst | 舆情分析师 | 分析师 | Quick | 短线 |
| 4 | Market Analyst | 技术分析师 | 分析师 | Quick | 短线 |
| 5 | Macro Analyst | 宏观分析师 | 分析师 | Quick | 中线 |
| 6 | Smart Money Analyst | 主力资金分析师 | 分析师 | Quick | 短线 |
| 7 | Volume Price Analyst | 量价分析师 | 分析师 | Quick | 短线 |
| 8 | Bull Researcher | 多头研究员 | 研究员 | Quick | — |
| 9 | Bear Researcher | 空头研究员 | 研究员 | Quick | — |
| 10 | Research Manager | 研究总监 | 管理层 | Deep | — |
| 11 | Trader | 交易员 | 决策层 | Quick | — |
| 12 | Aggressive Analyst | 激进风控 | 风控层 | Quick | — |
| 13 | Conservative Analyst | 保守风控 | 风控层 | Quick | — |
| 14 | Neutral Analyst | 中性风控 | 风控层 | Quick | — |
| 15 | Risk Judge / Portfolio Manager | 风控裁决 / 组合经理 | 风控层 | Deep | — |

---

## 第一层：分析师团队（Analyst Team）

七名分析师从 START 节点 **并行启动**，各自独立完成数据采集与初步研判，输出结构化报告至共享状态。每名分析师可配置独立的数据窗口和分析周期（短线/中线），并支持用户意图中的 `focus_areas` 和 `specific_questions` 定向引导。

### 1. Fundamentals Analyst — 基本面分析师

- **源码**：`tradingagents/agents/analysts/fundamentals_analyst.py`
- **分析周期**：中线（固定）
- **数据来源**：`get_fundamentals`、`get_balance_sheet`、`get_cashflow`、`get_income_statement`
- **输出字段**：`fundamentals_report`
- **职责**：分析公司财务报表（资产负债表、利润表、现金流量表），评估盈利能力、偿债能力、成长性等基本面指标，给出中长期投资价值判断。

### 2. News Analyst — 新闻分析师

- **源码**：`tradingagents/agents/analysts/news_analyst.py`
- **分析周期**：短线（固定）
- **数据窗口**：14 天
- **数据来源**：`get_news`（个股新闻）、`get_global_news`（全球新闻）
- **输出字段**：`news_report`
- **职责**：追踪近期公司相关新闻与全球重大事件，评估新闻事件对股价的短期影响，识别利好/利空信号。

### 3. Social Media Analyst — 舆情分析师

- **源码**：`tradingagents/agents/analysts/social_media_analyst.py`
- **分析周期**：短线（固定）
- **数据窗口**：7 天
- **数据来源**：`get_news`、`get_zt_pool`（涨停池）、`get_hot_stocks_xq`（雪球热门）
- **输出字段**：`sentiment_report`
- **职责**：分析市场情绪与散户舆情，通过涨停池数据和社交平台热门股信息，判断市场情绪温度与资金偏好。

### 4. Market Analyst — 技术分析师

- **源码**：`tradingagents/agents/analysts/market_analyst.py`
- **分析周期**：短线（固定）
- **数据来源**：`get_stock_data`（K 线数据）、`get_indicators`（技术指标）
- **技术指标**：MA50、MA200、EMA10、RSI、MACD、布林带、ATR、VWMA 等
- **输出字段**：`market_report`
- **职责**：基于 K 线形态与技术指标进行技术面分析，识别趋势、支撑/阻力位、超买超卖信号。

### 5. Macro Analyst — 宏观分析师

- **源码**：`tradingagents/agents/analysts/macro_analyst.py`
- **分析周期**：中线（固定）
- **数据来源**：`get_board_fund_flow`（板块资金流向）、`get_news`
- **输出字段**：`macro_report`
- **职责**：分析宏观经济环境、行业板块资金流向、政策面变化，评估标的所处的宏观与板块背景。

### 6. Smart Money Analyst — 主力资金分析师

- **源码**：`tradingagents/agents/analysts/smart_money_analyst.py`
- **分析周期**：短线（固定）
- **数据来源**：`get_individual_fund_flow`（个股资金流向）、`get_lhb_detail`（龙虎榜）、`get_indicators`（成交量 VWMA）
- **输出字段**：`smart_money_report`
- **职责**：追踪主力资金（机构、游资）的进出行为，通过龙虎榜和资金流向数据判断主力意图。

### 7. Volume Price Analyst — 量价分析师

- **源码**：`tradingagents/agents/analysts/volume_price_analyst.py`
- **分析周期**：短线（固定）
- **数据来源**：`DataCollector` 预计算的 `vpa_indicators`（量价指标）、`stock_data`（K 线）
- **输出字段**：`volume_price_report`
- **职责**：基于量价关系分析市场的供需力量，识别放量突破、缩量回调等量价配合信号。

---

## 第二层：研究员团队（Research Team）

分析师报告完成后，系统进入 **多空辩论** 阶段。Bull Researcher 与 Bear Researcher 交替发言，围绕 Claim（论点）展开结构化红蓝对抗辩论。

### 8. Bull Researcher — 多头研究员

- **源码**：`tradingagents/agents/researchers/bull_researcher.py`
- **立场**：看多（Bullish）
- **记忆**：`bull_memory`
- **输入**：全部七份分析师报告、辩论历史、历史记忆、Claim 列表
- **职责**：构建多头投资论点，基于分析师报告提出看多理由，回应空头质疑，维护和推进多头 Claim。

### 9. Bear Researcher — 空头研究员

- **源码**：`tradingagents/agents/researchers/bear_researcher.py`
- **立场**：看空（Bearish）
- **记忆**：`bear_memory`
- **输入**：同 Bull Researcher
- **职责**：构建空头投资论点，质疑多头 Claim，提出风险因素和看空理由，推动辩论深入。

**辩论机制**：
- 辩论轮次由 `max_debate_rounds` 配置控制（默认 2 轮，即 Bull-Bear 各发言 2 次）
- 每轮发言通过 Claim 系统追踪论点的提出、质疑与解决
- 辩论历史累积在 `investment_debate_state` 中

---

## 第三层：决策层（Decision Layer）

### 10. Research Manager — 研究总监

- **源码**：`tradingagents/agents/managers/research_manager.py`
- **LLM**：Deep Thinking（深度思考模型）
- **记忆**：`invest_judge_memory`
- **输入**：辩论历史、全部分析师报告、Claim 状态、历史记忆
- **输出字段**：`investment_plan`
- **职责**：综合评判多空辩论结果，对未解决的 Claim 做出裁决，形成结构化的投资计划，明确方向（看多/看空/中性）、核心逻辑与关键假设。

### 11. Trader — 交易员

- **源码**：`tradingagents/agents/trader/trader.py`
- **记忆**：`trader_memory`
- **输入**：投资计划、全部分析师报告、标的/市场/用户上下文、风控反馈、历史记忆
- **输出字段**：`trader_investment_plan`
- **职责**：将研究总监的投资计划转化为可执行的交易方案，包括具体的方向（买入/卖出/持有）、目标价、止损价、仓位建议与执行前提条件。

---

## 第四层：风控博弈层（Risk Layer）

交易员方案生成后，进入三方风控辩论阶段。三名风控分析师从不同风险偏好角度审查交易方案，最终由风控裁决做出终审。

### 12. Aggressive Analyst — 激进风控

- **源码**：`tradingagents/agents/risk_mgmt/aggressive_debator.py`
- **立场**：激进（Aggressive）
- **职责**：从进攻型视角审查方案，关注错失机会的风险，倾向于支持执行，质疑过度保守的风控假设。

### 13. Conservative Analyst — 保守风控

- **源码**：`tradingagents/agents/risk_mgmt/conservative_debator.py`
- **立场**：保守（Conservative）
- **职责**：从防守型视角审查方案，关注下行风险和资金安全，质疑过度乐观的假设，提出止损和仓位限制建议。

### 14. Neutral Analyst — 中性风控

- **源码**：`tradingagents/agents/risk_mgmt/neutral_debator.py`
- **立场**：中性（Neutral）
- **职责**：从平衡视角审查方案，识别激进与保守双方的逻辑漏洞，提出折中建议，关注执行层面的矛盾。

**风控辩论机制**：
- 辩论轮次由 `max_risk_discuss_rounds` 配置控制（默认 1 轮，即三方各发言 1 次）
- 发言顺序：Aggressive → Conservative → Neutral → 循环
- 辩论历史累积在 `risk_debate_state` 中

### 15. Risk Judge / Portfolio Manager — 风控裁决 / 组合经理

- **源码**：`tradingagents/agents/managers/risk_manager.py`
- **LLM**：Deep Thinking（深度思考模型）
- **记忆**：`risk_manager_memory`
- **输入**：交易员方案、风控辩论历史、全部分析师报告、标的/市场/用户上下文、Claim 状态
- **职责**：对三方风控辩论进行终审裁决，输出结构化风控结论：
  - **裁决结果**（`verdict`）：`pass`（通过）/ `revise`（修订）/ `reject`（拒绝）
  - **硬约束**（`hard_constraints`）：不可突破的风控红线
  - **软约束**（`soft_constraints`）：建议性风控条件
  - **执行前提**（`execution_preconditions`）：执行前必须满足的条件
  - **脱险触发**（`de_risk_triggers`）：需要立即减仓/清仓的触发条件

**修订机制**：若裁决结果为 `revise`，交易员会被要求重新修订方案（最多重试 `max_retries` 次），修订后重新进入风控辩论流程。

---

## 协作流程图

```
                        ┌──────────────────────────────────────────────────┐
                        │                 START（并行启动）                  │
                        └───┬───┬───┬───┬───┬───┬───┬──────────────────────┘
                            │   │   │   │   │   │   │
                    ┌───────┘   │   │   │   │   │   └───────┐
                    ▼           ▼   ▼   ▼   ▼   ▼           ▼
               ┌─────────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐
               │Fundamen-│ │News  │ │Social│ │Market│ │Macro │ │Smart │ │Volume│
               │tals     │ │Analyst│ │Media │ │Tech  │ │Analyst│ │Money │ │Price │
               │Analyst  │ │      │ │Analyst│ │Analyst│ │      │ │Analyst│ │Analyst│
               └────┬────┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘
                    │         │        │        │        │        │        │
                    └─────────┴────┬───┴────────┴────────┴────────┴────────┘
                                   ▼
                        ┌─────────────────────┐
                        │   Bull Researcher   │◄──── Claim 驱动辩论
                        │   (多头研究员)        │
                        └──────────┬──────────┘
                                   │
                          ┌────────┴────────┐
                          ▼                 ▼
                ┌──────────────┐   (达到轮次上限)
                │Bear Researcher│         │
                │ (空头研究员)   │         │
                └──────┬───────┘         │
                       │                 │
                       └────────┬────────┘
                                ▼
                        ┌───────────────────┐
                        │ Research Manager  │  ◄── Deep Thinking
                        │   (研究总监)       │
                        └────────┬──────────┘
                                 ▼
                        ┌───────────────────┐
                        │     Trader        │
                        │    (交易员)        │
                        └────────┬──────────┘
                                 ▼
                        ┌───────────────────┐
                        │ Aggressive Analyst│
                        │   (激进风控)       │
                        └────────┬──────────┘
                                 ▼
                        ┌───────────────────┐
                        │Conservative Anal. │
                        │   (保守风控)       │
                        └────────┬──────────┘
                                 ▼
                        ┌───────────────────┐
                        │ Neutral Analyst   │
                        │   (中性风控)       │
                        └────────┬──────────┘
                                 ▼
                        ┌───────────────────┐
                        │   Risk Judge      │  ◄── Deep Thinking
                        │ (风控裁决/组合经理) │
                        └────────┬──────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
                [pass]       [revise]      [reject]
                  │            │              │
                  ▼            ▼              ▼
                 END     Trader(修订)        END
                           │
                     (重新进入风控辩论)
```

---

## 状态流转

所有 Agent 通过 `AgentState` 共享状态，关键状态字段如下：

| 状态字段 | 类型 | 说明 |
|---------|------|------|
| `company_of_interest` | `str` | 目标标的代码 |
| `trade_date` | `str` | 交易日期 |
| `user_intent` | `UserIntent` | 解析后的用户意图（标的、周期、关注领域、具体问题） |
| `instrument_context` | `InstrumentContext` | 标的上下文（市场、交易所、币种等） |
| `market_context` | `MarketContext` | 市场上下文（时区、开盘状态、分析模式） |
| `user_context` | `UserContext` | 用户上下文（风险偏好、持仓、资金） |
| `market_report` | `str` | 技术分析师报告 |
| `fundamentals_report` | `str` | 基本面分析师报告 |
| `sentiment_report` | `str` | 舆情分析师报告 |
| `news_report` | `str` | 新闻分析师报告 |
| `macro_report` | `str` | 宏观分析师报告 |
| `smart_money_report` | `str` | 主力资金分析师报告 |
| `volume_price_report` | `str` | 量价分析师报告 |
| `investment_debate_state` | `InvestDebateState` | 投资辩论状态（多空历史、Claim、轮次） |
| `investment_plan` | `str` | 研究总监的投资计划 |
| `trader_investment_plan` | `str` | 交易员的交易方案 |
| `risk_debate_state` | `RiskDebateState` | 风控辩论状态（三方历史、Claim、轮次） |
| `risk_feedback_state` | `RiskFeedbackState` | 风控反馈状态（裁决、约束、重试次数） |
| `analyst_traces` | `List[TraceItem]` | 分析师的研判轨迹（结论、置信度） |

### Claim 系统

辩论过程中的核心追踪机制，每条 Claim 包含：

- **唯一 ID**：如 `INV-1`、`RISK-3`
- **内容**：论点描述
- **立场**：`bullish` / `bearish` / `aggressive` / `conservative` / `neutral`
- **状态**：`open`（待回应）→ `resolved`（已解决）/ `unresolved`（仍有争议）
- **聚焦列表**：`focus_claim_ids` 标记下一轮必须回应的 Claim

---

## 前端架构

### 页面路由

| 路径 | 页面组件 | 说明 |
|------|---------|------|
| `/` | Dashboard | 控制台首页，展示系统概览 |
| `/tracking-board` | TrackingBoard | 跟踪看板，实时持仓监控 |
| `/analysis` | Analysis | 智能分析，发起新的分析任务 |
| `/reports` | Reports | 研报管理，查看历史分析报告 |
| `/portfolio` | Portfolio | 自选 & 定时分析管理 |
| `/settings` | Settings | 系统设置，配置 LLM、邮箱等 |
| `/feedback` | Feedback | 用户反馈 |
| `/login` | Login | 登录页面 |

### 核心组件

| 组件 | 路径 | 说明 |
|------|------|------|
| `Layout` | `components/Layout.tsx` | 主布局框架，包含侧边栏和头部 |
| `Sidebar` | `components/Sidebar.tsx` | 侧边导航栏 |
| `Header` | `components/Header.tsx` | 顶部导航栏 |
| `DebateDrawer` | `components/DebateDrawer.tsx` | 辩论对战可视化抽屉 |
| `DebateTimeline` | `components/DebateTimeline.tsx` | 辩论时间线组件 |
| `DecisionCard` | `components/DecisionCard.tsx` | 决策卡片组件 |
| `RiskRadar` | `components/RiskRadar.tsx` | 风控雷达图 |
| `KeyMetrics` | `components/KeyMetrics.tsx` | 关键指标展示 |
| `KlinePanel` | `components/KlinePanel.tsx` | K 线图面板 |
| `ReportViewer` | `components/ReportViewer.tsx` | 研报查看器 |
| `TrackingBoardPanel` | `components/TrackingBoardPanel.tsx` | 跟踪看板面板 |
| `AgentCollaboration` | `components/AgentCollaboration.tsx` | Agent 协作流程图 |
| `ChatCopilotPanel` | `components/ChatCopilotPanel.tsx` | 聊天副驾驶面板 |
| `TaskProgressBanner` | `components/TaskProgressBanner.tsx` | 任务进度横幅 |

### 状态管理

使用 Zustand 进行全局状态管理：

| Store | 路径 | 说明 |
|-------|------|------|
| `authStore` | `stores/authStore.ts` | 用户认证状态（登录、Token、用户信息） |
| `analysisStore` | `stores/analysisStore.ts` | 分析任务状态（任务进度、SSE 连接） |

### 自定义 Hooks

| Hook | 路径 | 说明 |
|------|------|------|
| `useSSE` | `hooks/useSSE.ts` | Server-Sent Events 流式数据连接 |
| `useTypeWriter` | `hooks/useTypeWriter.ts` | 打字机效果动画 |

### 工具函数

| 工具 | 路径 | 说明 |
|------|------|------|
| `portfolioSync` | `utils/portfolioSync.ts` | 持仓同步工具 |
| `progressFeedback` | `utils/progressFeedback.ts` | 进度反馈处理 |
| `reportText` | `utils/reportText.ts` | 研报文本处理 |

### 类型定义

所有 TypeScript 类型定义集中在 `types/index.ts`，包括：

- `Agent` / `AgentTeam`：Agent 相关类型
- `AnalysisRequest` / `AnalysisResponse`：分析请求/响应类型
- `InstrumentContext` / `MarketContext` / `UserContext`：上下文类型
- `InvestDebateState` / `RiskDebateState`：辩论状态类型
- `WatchlistItem` / `ScheduledAnalysis`：自选/定时任务类型
- `Report`：研报类型
- `TrackingBoardItem` / `TrackingBoardResponse`：跟踪看板类型

### 前端与后端交互

1. **REST API**：常规 CRUD 操作通过标准 HTTP 请求
2. **SSE (Server-Sent Events)**：分析任务进度和 Token 级流式输出
3. **WebSocket**：实时数据推送（如行情更新）

---

## API 集成

系统提供标准 REST API，方便集成到自定义脚本、交易机器人或第三方看板：

| 操作 | 接口 |
|------|------|
| 触发分析 | `POST /v1/analyze` → 返回 `job_id` |
| 状态追踪 | `GET /v1/jobs/{job_id}` |
| 获取结果 | `GET /v1/jobs/{job_id}/result` |
| 历史检索 | `GET /v1/reports` |
| 批量获取最新报告 | `POST /v1/reports/latest-by-symbols` |
| 持仓查询 | `GET /v1/portfolio/imports` |
| 全量保存持仓 | `POST /v1/portfolio/imports` |
| 追加新标的 | `POST /v1/portfolio/imports/append` |
| 删除单个持仓 | `DELETE /v1/portfolio/imports/{symbol}` |
| 清空所有持仓 | `DELETE /v1/portfolio/imports` |
| 截图识别持仓 | `POST /v1/portfolio/parse-image` |
| 跟踪看板摘要/明细 | `GET /v1/dashboard/tracking-board` |
| 自选股管理 | `GET/POST/DELETE /v1/watchlist` |
| 定时任务管理 | `GET/POST/DELETE /v1/scheduled` |
| 批量定时任务操作 | `PATCH /v1/scheduled/batch`、`POST /v1/scheduled/batch/delete`、`POST /v1/scheduled/batch/trigger` |
| 模型配置 | `GET/PATCH /v1/config` |
| 模型 warmup | `POST /v1/config/warmup` |
| 用户认证 | `POST /v1/auth/login`、`POST /v1/auth/register` |
| API Token | `GET/POST /v1/tokens` |

认证：Web 端登录后在"设置 / API Token"生成密钥，通过 `Authorization: Bearer <TOKEN>` 传入。

### 股票代码识别规则

`_normalize_code` 函数自动将 6 位纯数字代码映射为带交易所后缀的标准格式：

| 代码前缀 | 交易所 | 示例 |
|---------|--------|------|
| `6` | 沪市 (SH) | 600519 → 600519.SH |
| `0`、`3` | 深市 (SZ) | 000001 → 000001.SZ |
| `5` | 沪市 (SH) | 510300 → 510300.SH |
| `15`、`16` | 深市 (SZ) | 159529 → 159529.SZ |
| `11`、`13` | 沪市 (SH) | 110035 → 110035.SH |
| `12` | 深市 (SZ) | 127003 → 127003.SZ |
| `4`、`8`、`9` | 北交所 (BJ) | 830799 → 830799.BJ |

```bash
curl -X POST 'https://app.510168.xyz/v1/analyze' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <YOUR_API_TOKEN>' \
  -d '{"symbol": "分析一下600519.SH短期趋势", "trade_date": "2026-03-28"}'
```

---

## 相关源码索引

### 后端

| 模块 | 路径 | 说明 |
|------|------|------|
| API 入口 | `api/main.py` | FastAPI 应用主入口 |
| 数据库模型 | `api/database.py` | SQLAlchemy ORM 模型定义 |
| Agent 入口 | `tradingagents/agents/__init__.py` | Agent 包初始化 |
| 分析师 | `tradingagents/agents/analysts/*.py` | 7 名分析师实现 |
| 研究员 | `tradingagents/agents/researchers/*.py` | 多头/空头研究员 |
| 交易员 | `tradingagents/agents/trader/trader.py` | 交易员 |
| 风控辩论 | `tradingagents/agents/risk_mgmt/*.py` | 三方风控辩手 |
| 管理层 | `tradingagents/agents/managers/*.py` | 研究总监、风控裁决 |
| 图编排 | `tradingagents/graph/trading_graph.py` | 主编排类 |
| 图构建 | `tradingagents/graph/setup.py` | StateGraph 节点与边的定义 |
| 条件逻辑 | `tradingagents/graph/conditional_logic.py` | 辩论轮次、分支判断 |
| 状态传播 | `tradingagents/graph/propagation.py` | 初始状态构建 |
| 意图解析 | `tradingagents/graph/intent_parser.py` | 自然语言意图解析 |
| 信号处理 | `tradingagents/graph/signal_processing.py` | 最终交易信号提取 |
| 反思系统 | `tradingagents/graph/reflection.py` | 决策反思与记忆写入 |
| 状态定义 | `tradingagents/agents/utils/agent_states.py` | AgentState 及子状态 TypedDict |
| 记忆系统 | `tradingagents/agents/utils/memory.py` | BM25 记忆实现 |
| 辩论工具 | `tradingagents/agents/utils/debate_utils.py` | Claim 格式化、辩论状态更新 |
| Prompt 模板 | `tradingagents/prompts/zh.py` / `en.py` | 中英文 Prompt 定义 |
| 默认配置 | `tradingagents/default_config.py` | LLM、辩论轮次、数据源等默认配置 |
| 数据流 | `tradingagents/dataflows/*.py` | 数据采集与处理 |
| LLM 客户端 | `tradingagents/llm_clients/*.py` | 多模型厂商客户端 |
| 服务层 | `api/services/*.py` | 业务逻辑服务 |
| 持仓导入服务 | `api/services/portfolio_import_service.py` | 持仓导入、追加、删除、代码识别 |

### 前端

| 模块 | 路径 | 说明 |
|------|------|------|
| 入口 | `frontend/src/main.tsx` | React 应用入口 |
| 路由 | `frontend/src/App.tsx` | 路由配置与布局 |
| 页面 | `frontend/src/pages/*.tsx` | 页面组件 |
| 组件 | `frontend/src/components/*.tsx` | 通用组件 |
| 状态 | `frontend/src/stores/*.ts` | Zustand 状态管理 |
| 服务 | `frontend/src/services/api.ts` | API 调用封装 |
| 类型 | `frontend/src/types/index.ts` | TypeScript 类型定义 |
| Hooks | `frontend/src/hooks/*.ts` | 自定义 Hooks |
| 工具 | `frontend/src/utils/*.ts` | 工具函数 |
| 样式 | `frontend/src/index.css` | 全局样式 |

---

## 部署与配置

### Docker 一键部署 (推荐)

```bash
docker pull ghcr.io/kylinmountain/tradingagents-ashare:latest

mkdir -p $(pwd)/data
export TA_APP_SECRET_KEY=$(openssl rand -base64 32)

docker run -d -p 8000:8000 \
  --name tradingagents \
  -v $(pwd)/data:/app/data \
  -e DATABASE_URL="sqlite:///./data/tradingagents.db" \
  -e TA_APP_SECRET_KEY="${TA_APP_SECRET_KEY}" \
  ghcr.io/kylinmountain/tradingagents-ashare:latest
```

### 源码安装

```bash
git clone https://github.com/KylinMountain/TradingAgents-AShare.git
cd TradingAgents-AShare

# 后端（Python 3.10+）
uv sync

# 前端（Node.js 18+）
cd frontend
npm install
npm run build
cd ..
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TA_APP_SECRET_KEY` | 应用密钥（加密 API Key、签发 JWT） | 内置默认值（仅开发环境） |
| `DATABASE_URL` | 数据库连接字符串 | `sqlite:///./data/tradingagents.db` |
| `TA_LLM_PROVIDER` | LLM 提供商 | `openai` |
| `TA_LLM_DEEP` | 深度思考模型 | `gpt-4o` |
| `TA_LLM_QUICK` | 快速思考模型 | `gpt-4o-mini` |
| `TA_BASE_URL` | LLM API 基础 URL | `https://api.openai.com/v1` |
| `TA_API_KEY` | LLM API Key | 空（在前端设置页面配置） |
| `TA_MAX_DEBATE` | 最大辩论轮次 | `2` |
| `TA_MAX_RISK` | 最大风控讨论轮次 | `1` |
| `TA_LANGUAGE` | Prompt 语言 | `zh` |

---

## 许可说明

- 本项目基于 [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents) (Apache 2.0) 二次开发
- 新增模块 (`api/`, `frontend/`) 及对核心逻辑的深度修改采用 `PolyForm Noncommercial 1.0.0` 协议
- 详情请参阅根目录下的 [LICENSE](./LICENSE) 文件

---

## 重要声明

- **仅供学习研究**：本项目仅用于学术研究、技术演示及学习交流目的，不构成任何形式的投资建议
- **实盘风险**：证券市场有风险，投资需谨慎。基于系统生成的任何观点、建议或计划，仅代表算法博弈结果，不对实际投资损益负责
- **数据延迟**：分析所依赖的数据源可能存在延迟或偏差，请以交易所实时公告为准
