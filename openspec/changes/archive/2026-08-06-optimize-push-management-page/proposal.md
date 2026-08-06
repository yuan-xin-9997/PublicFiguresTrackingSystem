## Why

推送管理页面当前并存三套重叠的推送机制——"每日时间线邮件"、"推送规则（即时推送）"、"定时增量汇总"，概念重叠、入口分散，维护与理解成本高。将"每日时间线邮件"演进为单一的"动态推送"并增加信息源维度，同时下线未被持续使用的推送规则与定时增量功能，使推送管理页面聚焦于一种可按人物、事件类型、信息源灵活配置的动态推送能力，降低运维与认知负担。

## What Changes

- 将"每日时间线邮件"（新增/编辑表单、规则列表）及相关文案、按钮、提示、flash 消息统一更名为"动态推送"；"日报运行记录"随之更名为"动态推送运行记录"，仪表盘"最近日报运行"更名为"最近动态推送运行"。
- "动态推送"规则新增**信息源维度**多选过滤：表单增加信息源多选 picker（支持全选/清空/搜索，交互与人物选择一致）；**空选 = 全部信息源**；后端汇总时仅纳入事件证据所属信息源命中所选范围的事件。
- **BREAKING** 移除"新增推送规则 / 编辑推送规则"表单与"推送规则"列表模块，下线后端事件推送规则的 CRUD、即时推送触发、定时增量汇总、预览、立即汇总、批次重试等接口与调度逻辑。
- **BREAKING** 移除"定时增量运行"模块及其后端运行/批次/详情/重试接口。
- **BREAKING** 归档并移除 `scheduled-incremental-event-push` 规格，该能力整体下线。
- 保留：邮件通道（SMTP）配置、动态推送运行记录、投递记录（作为历史只读视图，不再产生即时推送新记录）。

## Non-Goals

- 不改变后端 `digests` 系列接口路径与 `digest_rules` 表名等内部标识，仅更换界面展示名称"动态推送"，以降低迁移风险（如需后端重命名另行立项）。
- 不改变动态推送既有的窗口模式（昨天自然日 / 滚动 N 小时）、发送时间、收件人、补跑、预览等已有行为，仅在汇总查询中追加信息源过滤维度。
- 不删除历史投递记录数据，`投递记录`模块保留为只读历史视图。
- 不调整邮件通道（SMTP）配置项与凭证保护机制。

## Capabilities

### New Capabilities

- `dynamic-push`: 动态推送能力——可按人物、事件类型、信息源维度配置的定时时间线邮件汇总推送，含窗口模式、预览、补跑、运行记录、SMTP 持久化重试、权限审计与配置安全约束。本规格同时承接原 `scheduled-incremental-event-push` 中仍复用的 SMTP 重试、权限审计、配置部署等共享行为。

### Modified Capabilities

- `scheduled-incremental-event-push`: 整体下线，移除全部需求（推送规则即时/定时增量模式、入库高水位切分、游标生命周期、运行事务幂等、北京时间调度恢复、筛选排序与空窗口、持久化重试、管理界面与权限审计、配置部署兼容）。该能力对应的代码与规格一并移除。

## Impact

- **前端** `src/app/frontend/src/main.js`：删除推送规则表单/列表、定时增量运行区块及相关状态（`ruleForm`、`editingRuleId`、`rulePersonSearch`、`incrementalRunFilters`、`incrementalPreview`、`selectedIncrementalRun` 等）与方法（`saveNotificationRule`、`editRule`、`resetRuleForm`、`toggleRule`、`removeRule`、`previewIncrementalRule`、`runIncrementalRule`、`openIncrementalRun`、`retryIncrementalBatch` 及发送时刻辅助方法）；将"每日时间线邮件/日报"文案改为"动态推送"；为动态推送表单增加信息源多选 picker 及 `digestForm.source_ids`。
- **后端** `src/app/backend/notifications.py`：移除 `/notifications/rules*`、`/notifications/incremental/*` 路由及推送规则/定时增量模型与调度入口；保留 SMTP 通道、`/notifications/digests/*`、`/notifications/deliveries*`（历史只读）。
- **后端** `src/app/backend/scheduled_incremental.py`：整体移除（定时增量调度、游标推进、运行/批次管理）。
- **后端** `src/app/backend/daily_digest.py`、`database.py`：`digest_rules` 增加 `source_ids`（JSON 数组）字段；汇总查询按信息源过滤事件（通过事件→证据→文档→信息源关联）；`/notifications/digests/options` 返回可用信息源列表。
- **数据库迁移**（SQLite）：新增 `digest_rules.source_ids`；移除推送规则与定时增量相关表（如 `push_rules`、`scheduled_incremental_runs`、`scheduled_incremental_batches`、`scheduled_incremental_run_events` 等，以实际模型为准）；保留 `deliveries` 等历史投递表可读。迁移须幂等且不覆盖既有数据。
- **配置** `src/config/app.json`：移除定时增量相关默认配置项（默认发送时刻、轮询周期等）；动态推送信息源维度不引入新的硬编码环境信息。增量部署不得覆盖服务器已有 `app.json`，缺失新增字段时使用代码默认值。
- **规格文档**：新增 `specs/dynamic-push/spec.md`；归档 `scheduled-incremental-event-push`。
- **测试**：更新 `src/app/frontend/src/notifications-ui.test.js`、`daily-digest-ui.test.js` 及后端测试，移除推送规则/定时增量用例，新增信息源维度过滤用例（空选=全部、多选过滤、跨信息源去重）。
- **文档与部署**：同步更新 README.md、需求规格说明书、设计说明书、Jenkinsfile；提交 Github 后手动触发 Jenkins 构建并请用户验证。
- **配置项与安全约束**：涉及数据库（SQLite 迁移）、外部 SMTP 服务（保持现有凭证保护与脱敏）、认证与权限（`notifications` 页面权限可见脱敏数据，写操作仅管理员，所有写操作记录不含凭证与正文的审计摘要）；信息源维度仅使用系统既有信息源数据，不涉及外部抓取或新凭证。本方案不偏离 FastAPI、Vue、SQLite 及既有目录结构，无需审批。
