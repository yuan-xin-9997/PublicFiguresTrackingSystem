## Context

推送管理页面当前承载三套并行机制：

1. **每日时间线邮件**（`daily_digest_*` 表，`/notifications/digests/*` 路由，`daily_digest.py`）：按人物 + 事件类型 + 窗口模式定时汇总事件成邮件。
2. **推送规则**（`notification_rules` 表，`/notifications/rules/*` 路由）：含 `immediate`（任务完成后即时推送）与 `scheduled_incremental`（定时增量汇总）两种模式，由 `notifications.py` 的 `enqueue_task_run` 在任务完成时触发即时推送。
3. **定时增量运行**（`scheduled_notification_runs/batches/items` 表，`/notifications/incremental/*` 路由，`scheduled_incremental.py`）：按入库高水位游标切分增量窗口。

三者概念重叠、入口分散。`digest_candidates` 查询已通过 `event_evidence → raw_documents → information_sources` 关联计算 `source_names`，但未把"信息源"作为可配置过滤维度。本设计将三者合并为单一"动态推送"，并新增信息源维度。

约束：FastAPI + Vue + SQLite；禁止硬编码环境信息；北京时间展示；增量部署不得覆盖服务器 `app.json`；写操作需权限与审计；SMTP 凭证脱敏。

## Goals / Non-Goals

**Goals:**
- 推送管理页面只保留一种推送机制"动态推送"（由每日时间线邮件演进），可按人物、事件类型、信息源维度配置。
- 完整移除推送规则与定时增量功能（前端 + 后端 + 规格），不留无入口的遗留后端。
- 信息源维度：多选、空选 = 全部信息源，后端按事件证据所属信息源过滤。
- 保留邮件通道配置、动态推送运行记录、投递记录（历史只读 + 失败重试）。

**Non-Goals:**
- 不重命名后端 `digests` 接口路径与 `daily_digest_*` 表名，仅更换 UI 文案为"动态推送"。
- 不改变动态推送既有窗口模式、发送时间、补跑、预览行为，仅追加信息源过滤。
- 不删除历史投递记录数据，`投递记录`模块保留为只读历史视图（含失败批次重试）。
- 不调整 SMTP 通道配置项与凭证保护机制。

## Decisions

### 决策 1：整体下线推送规则与定时增量，仅保留动态推送
移除 `notification_rules`、`notification_rule_tasks`、`notification_rule_persons`、`scheduled_notification_runs/batches/items` 表及其路由、`scheduled_incremental.py`、`enqueue_task_run` 触发路径、`NotificationWorker.process_incremental_once`。保留 `daily_digest_*` 作为动态推送的承载。

**理由**：用户选择"前端+后端+规格全删"。保留无入口的后端会形成遗留代码与隐性触发（任务完成仍会触发即时推送），违背简化目标。
**备选**：仅删前端（被否，留死代码且即时推送仍隐式触发）；后端停用保留代码（被否，仍需维护）。

### 决策 2：保留 `task_run_events`、`email_delivery_batches/items` 与即时投递消费侧
`task_run_events` 被 `services.py` 事件抽取流程使用，不删除。`email_delivery_batches/items` 承载历史即时推送投递记录，`投递记录`模块与失败批次重试仍需读取/重试它们，故保留表与 `/notifications/deliveries*` 路由与 `NotificationWorker.process_immediate_once`（仅消费历史重试批次，不再有新批次产生）。

**理由**：保护审计历史与失败重试能力，避免删表丢数据。
**备选**：连同投递记录一并删除（被否，丢失审计与重试）。

### 决策 3：信息源维度用链接表 + EXISTS 子查询过滤
新增 `daily_digest_rule_sources(rule_id, source_id)` 链接表（与 `daily_digest_rule_persons` 一致）。`digest_candidates` 增加可选 `source_ids` 参数：非空时追加 `AND EXISTS (SELECT 1 FROM event_evidence ev JOIN raw_documents d ON d.id=ev.document_id WHERE ev.event_id=e.id AND d.source_id IN (...))`；空时不追加（= 全部信息源）。`/notifications/digests/options` 新增 `information_sources` 字段返回可用信息源列表（`id,name,display_type`），与既有 `sources`（配置来源溯源 dict）区分命名。

**理由**：复用 `digest_candidates` 既有的 `event_evidence → raw_documents → information_sources` 关联；链接表与人物维度一致，便于校验与级联删除。
**备选**：`source_ids_json` 列（被否，与人物链接表风格不一致且校验麻烦）。

### 决策 4：UI 文案统一改为"动态推送"，后端标识不变
前端 `main.js` 将"新增/编辑每日时间线邮件"、"每日时间线邮件"列表、"日报运行记录"、仪表盘"最近日报运行"、相关 flash 消息改为"动态推送"。后端 `digests` 路径、`daily_digest_*` 表名、函数名保持不变。

**理由**：降低迁移风险与改动面；用户需求仅为界面名称变更。
**备选**：后端一并重命名为 `dynamic_push`（被否，风险高、收益低）。

### 决策 5：配置移除 `scheduled_incremental` 块
`config.py` 默认配置与 `app.json` schema 中移除 `notifications.scheduled_incremental` 段（默认发送时刻、轮询周期等）。动态推送沿用既有 `notifications.daily_digest` 与 `notifications.email` 配置。增量部署不覆盖服务器现有 `app.json`，缺失字段用代码默认值。

### 决策 6：规格新增 `dynamic-push`，移除 `scheduled-incremental-event-push`
新建 `specs/dynamic-push/spec.md` 承接动态推送能力（含信息源维度及复用的 SMTP 重试、权限审计、配置部署要求）。`scheduled-incremental-event-push` 以 REMOVED delta 移除全部 9 条需求；共享的 SMTP/权限/配置行为迁移至 `dynamic-push` 规格覆盖。

## Risks / Trade-offs

- **[风险] 删表丢历史定时增量运行记录** -> 缓解：`scheduled_notification_*` 表数据视为可弃（功能下线）；若需保留审计，可在迁移前导出。投递历史（`email_delivery_batches/items`）保留。
- **[风险] 信息源过滤使部分事件不再进入动态推送，用户感知推送"变少"** -> 缓解：空选默认 = 全部信息源，行为与现状一致；仅在用户主动选择信息源时收窄，预览可提前看到候选数。
- **[风险] `enqueue_task_run` 移除影响任务完成流程** -> 缓解：`services.py:321` 调用点同步移除并清理返回值使用；任务运行本身不受影响，仅不再产生即时推送。
- **[风险] 前端删除大量状态/方法遗留悬空引用** -> 缓解：`main.js` return 块与模板同步清理；`notifications-ui.test.js` 更新；冒烟测试覆盖推送管理页渲染与动态推送 CRUD。
- **[风险] 迁移在已有数据的环境执行失败** -> 缓解：迁移幂等（`CREATE TABLE IF NOT EXISTS`、`DROP TABLE IF EXISTS`），`schema_version` 控制；回滚靠部署前 SQLite 备份。

## Migration Plan

1. **数据库迁移**（`database.py` ensure_schema 内，`schema_version` 递增）：
   - 新增 `daily_digest_rule_sources` 表。
   - `DROP TABLE IF EXISTS` `scheduled_notification_items`、`scheduled_notification_batches`、`scheduled_notification_runs`、`notification_rule_persons`、`notification_rule_tasks`、`notification_rules`（顺序遵守外键）。
   - 保留 `email_delivery_batches/items`、`task_run_events`、`daily_digest_*`、`notification_settings`。
2. **后端**：删除 `scheduled_incremental.py`；`notifications.py` 移除其导入、`process_incremental_once`、增量批次处理；`services.py` 移除 `enqueue_task_run` 调用与导入；`main.py` 移除 `/notifications/rules*`、`/notifications/incremental/*` 路由；`digest_candidates` 与 digest 规则保存/水合支持 `source_ids`；digest options 返回 `information_sources`；`config.py` 移除 `scheduled_incremental` 默认块。
3. **前端**：`main.js` 删除推送规则/定时增量运行区块、相关 state/方法/return 项；动态推送表单增加信息源 picker（`digestForm.source_ids`，全选/清空/搜索）；文案改"动态推送"。
4. **测试**：更新 `notifications-ui.test.js`、`daily-digest-ui.test.js`、后端 digest/notifications 测试；移除推送规则/增量用例；新增信息源维度用例。
5. **文档/部署**：更新 README、需求规格说明书、设计说明书、Jenkinsfile；提交 Github → 手动触发 Jenkins → 用户验证。
6. **回滚**：部署前备份 `data/app.sqlite3`；失败时还原备份 + 旧代码镜像。删表不可逆，故备份为强制前置。

## Open Questions

- 是否需要对下线的 `scheduled_notification_*` 历史运行记录做导出存档？（当前设计直接丢弃，因功能下线；如需审计留痕需在迁移前导出。）
- `投递记录`模块长期保留是否需要在 UI 标注"仅历史"？（建议加副标题说明，待实现时确认。）
