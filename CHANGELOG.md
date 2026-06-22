# Changelog

All notable changes to this project will be documented in this file.

## [v0.5.1] - 2026-05-26

### Added
- **持仓管理增强**
  - 追加新标的功能：`POST /v1/portfolio/imports/append`，仅添加新标的，保留原有持仓
  - 删除单个持仓功能：`DELETE /v1/portfolio/imports/{symbol}`，简洁版和详细版视图均支持
  - 持仓管理界面增加蓝色"追加新标的"按钮和每行"删除"按钮
  - 删除操作带 loading 状态反馈（旋转动画），防止重复点击
- **股票代码自动识别扩展**
  - 新增 `5xxxxx`（沪市基金/ETF）、`15xxxx`/`16xxxx`（深市基金/ETF）代码识别
  - 新增 `11xxxx`/`13xxxx`（沪市可转债）、`12xxxx`（深市可转债）代码识别
  - 新增 `9xxxxx`（北交所）代码识别
- **文档**
  - 新增 `AGENTS.md` 架构文档，详细描述 15 名 Agent 的角色定义、职责边界、数据来源、LLM 策略、协作流程及前端架构

### Changed
- 追加新标的后自动重新计算所有持仓的 `current_position_pct`，确保占比数据准确
- 删除操作统一使用 `importFeedback` 状态显示成功/失败消息，替代 `alert()` 弹窗
- 前端 `appendPortfolioPositions` 改为发送原始文本，由后端统一解析，减少前后端解析逻辑重复

### Fixed
- 修复追加持仓 API 前端发送格式 `{ positions: [...] }` 与后端期望 `{ text: "..." }` 不匹配导致的 422 错误
- 修复 `_normalize_code` 不识别 `15xxxx` 等基金代码导致追加/保存持仓时标的被静默跳过的问题
- 修复 `PortfolioSyncRequest` 请求模型和 `_parse_positions_text` 解析函数缺失导致追加端点无法工作的问题

## [v0.5.0] - 2026-03-22

### Added
- **Token 级流式输出**：全部 15 个 Agent 支持 astream Token 推送，对话框实时展示 LLM 输出过程。
- **自选股管理**：数据库持久化的自选列表（上限 50），支持股票代码/名称模糊搜索。
- **定时分析**：每个交易日在用户设定时间（20:00~08:00）自动触发分析，连续失败 3 次自动停用。
- **后台调度器**：FastAPI lifespan 内嵌 asyncio 调度协程，交易日判断、防重复触发、串行预采集。
- **多阶段状态指示器**：用户提交后即时反馈：连接中 → 识别标的 → 采集数据 → 多智能体分析。
- **股票搜索 API**：`/v1/market/stock-search` 支持代码前缀和名称模糊匹配，7 天 TTL 缓存。
- **Dependabot**：自动依赖更新（pip、npm、GitHub Actions、Docker）。

### Changed
- **对话框重构**：Agent 消息改为紧凑卡片（图标+标签+实时预览），点击展开完整内容，完成后自动转为报告卡片。
- **图标体系统一**：对话框与协作面板使用一致的 Lucide 图标与配色，覆盖全部 15 个 Agent。
- **结构化提取增强**：使用 json-repair 替代正则剥离；Pydantic 模型容忍 LLM 输出变体（数组→首元素、数字→字符串、缺失字段默认值）。
- **后端异步并行**：分析师节点全部转为 async，数据采集并行执行，意图解析不再阻塞 SSE 流返回。
- **Portfolio 页面**：从"热榜选股"重构为"自选 & 定时分析"，去掉外部热榜数据依赖。
- **日志体系**：uvicorn 日志配置文件统一时间戳格式。
- **Docker 优化**：CMD 使用 tradingagents-api 入口点；git tag 注入 VERSION build-arg。
- **登录页**：12-Agent → 15-Agent，补齐宏观分析、主力资金、博弈裁判。
- **SQLite WAL 模式**：启用 WAL 支持并发读写。

### Fixed
- 修复 mini_racer/V8 多线程 crash：启动时预加载交易日历，后续请求全走缓存。
- 修复结构化提取失败：LLM 返回 markdown 代码围栏或非标准 JSON 格式导致 Pydantic 解析错误。
- 修复定时任务重复触发：启动前标记 last_run_date，防止调度循环重复启动。
- 修复 `import re` 缺失导致 report_service 崩溃。
- 修复 `_load_cn_stock_map` 错误导入位置。
- 修复 Agent 卡片状态：job.completed 时标记所有 Agent 为已完成，不再显示"撰写中"。
- 修复意图解析 JSON 在对话框显示的问题。
- 移除协作面板流光动画。

### Removed
- 移除 12 个未使用的依赖（backtrader、chainlit、redis、alembic、rich、typer 等）。
- 移除热榜选股功能（外部数据源不稳定且有合规风险）。
- 移除对话框中冗余的系统消息（job.created、job.running、agent.tool_call）。

## [v0.4.4] - 2026-03-18

### Fixed
- Fixed critical **SQLAlchemy TimeoutError** by unifying database session lifecycle across API endpoints and background tasks.
- Fixed **Resource/Semaphore Leakage** on shutdown by adding executor shutdown to the FastAPI lifespan.
- Improved repository structure by moving `announcements.json` to the `api/` directory and updating search paths.
- Cleaned up redundant `uv.lock.cp313` and `CLAUDE.md` files.
- Resolved **Announcement Schema Validation** errors (500) by aligning `announcements.json` with Pydantic model requirements.
- Made `/v1/announcements/latest` a public endpoint to ensure visibility before login.

## [v0.4.3] - 2026-03-16

### Added
- Added **Task Lifecycle Persistence and Recovery** (#32): Analysis jobs can now survive server restarts.
- Added **Configurable Max Workers** (#33): Job executor concurrency is now tunable via `TA_MAX_WORKERS` env var.
- Added persistent report lifecycle fields, including `status`, `error`, and richer section-level report storage.
- Added structured analyst trace persistence to support future report-side insight displays.
- Added header announcement support backed by `announcements.json` and `/v1/announcements/latest`.

### Changed
- Changed the report flow to initialize records earlier and update report content incrementally during long-running analysis jobs.
- Changed the header announcement entry to load from backend data instead of hard-coded preview text.
- Improved error messaging for failed analysis steps in the UI.

### Fixed
- Fixed report serialization gaps so newly persisted lifecycle and extended section fields can be returned consistently.
- Fixed report finalization and failure recording so completed and failed jobs leave clearer artifacts for follow-up inspection.

## [v0.4.2] - 2026-03-16

### Added
- Added user-context grounding so analysis can incorporate objective, risk preference, investment horizon, and holding constraints.
- Added local Docker one-click deployment script for easier self-hosted setup.

### Changed
- Upgraded the debate workflow to a claim-driven flow for stronger argument organization and downstream judgment.
- Improved multi-horizon analysis wording and parameter handling.

### Fixed
- Fixed structured extraction prompts by explicitly restoring missing JSON keywords that caused 400 errors.
- Removed mistakenly committed runtime artifacts such as `deploy` and `.vite` from version control.

## [v0.4.1] - 2026-03-15

### Added
- Added intent-driven multi-horizon analysis with streaming progress updates.
- Added integrated frontend-backend Docker packaging and multi-architecture CI/CD automation.
- Added restored A-share analysis skills with a hardened CI environment.

### Changed
- Re-applied missing dependency updates including `marshmallow` and `python-socketio`.

### Fixed
- Fixed review issues raised during the v0.4.1 stabilization cycle.
- Improved SKILL metadata and SEO-related presentation.

## [v0.4.0] - 2026-03-13

### Added
- Added monorepo synchronization and the new game-theory agent integration.
- Added report `direction` field and UTC timestamp serialization.
- Added frontend commit message support.
- Added skills support for using TradingAgents through reusable skill workflows.

### Fixed
- Fixed default agent settings.
- Fixed stock symbol normalization at task startup and during K-line data retrieval.

## [v0.3.0] - 2026-03-12

### Changed
- Removed the redundant `frontend_backup/` tree from the main branch to simplify the repository layout.
