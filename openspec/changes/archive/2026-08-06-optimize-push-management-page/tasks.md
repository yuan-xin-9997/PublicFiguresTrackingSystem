## 1. 数据迁移

- [x] 1.1 在 `src/app/backend/database.py` 的 `ensure_schema` 中新增 `daily_digest_rule_sources(rule_id, source_id)` 链接表（`PRIMARY KEY(rule_id, source_id)`，外键 `ON DELETE CASCADE`）及按 `source_id` 的索引
- [x] 1.2 在 `ensure_schema` 中按外键顺序 `DROP TABLE IF EXISTS`：`scheduled_notification_items`、`scheduled_notification_batches`、`scheduled_notification_runs`、`notification_rule_persons`、`notification_rule_tasks`、`notification_rules`；保留 `email_delivery_batches/items`、`task_run_events`、`daily_digest_*`、`notification_settings`
- [x] 1.3 递增 `schema_version`，确保迁移幂等（`CREATE/DROP IF EXISTS`），并在自测环境验证新库与既有库升级均通过

## 2. 后端

- [x] 2.1 删除 `src/app/backend/scheduled_incremental.py` 文件
- [x] 2.2 在 `src/app/backend/notifications.py` 移除 `scheduled_incremental` 导入、`process_incremental_once` 方法及其在 `process_once` 调度列表中的引用；移除增量批次处理分支，保留 `process_immediate_once`（历史重试）与 `process_digest_once`
- [x] 2.3 在 `src/app/backend/notifications.py` 移除 `list_rules`、`save_rule`、`delete_rule`、`enqueue_task_run` 等推送规则函数及其引用
- [x] 2.4 在 `src/app/backend/services.py` 移除 `enqueue_task_run` 导入与 `services.py:321` 附近的调用及对返回计数的使用，确保任务完成流程不受影响
- [x] 2.5 在 `src/app/backend/main.py` 移除路由：`/notifications/rules`（GET/POST）、`/notifications/rules/{rule_id}`（PUT/DELETE）、`/notifications/rules/{rule_id}/preview`、`/notifications/rules/{rule_id}/run-now`、`/notifications/incremental/config`、`/notifications/incremental/runs`、`/notifications/incremental/runs/{run_id}`、`/notifications/incremental/batches/{batch_id}/retry`；保留 `/notifications/email/*`、`/notifications/digests/*`、`/notifications/deliveries*`
- [x] 2.6 在 `src/app/backend/daily_digest.py` 的 `digest_candidates` 增加可选 `source_ids` 参数，非空时追加 `AND EXISTS (SELECT 1 FROM event_evidence ev JOIN raw_documents d ON d.id=ev.document_id WHERE ev.event_id=e.id AND d.source_id IN (...))`，空时不追加
- [x] 2.7 在 `daily_digest.py` 的 `save_digest_rule`/`_normalize_rule_values`/`_hydrate_rule`/`_rule_lists` 支持 `source_ids`：校验信息源存在性、持久化到 `daily_digest_rule_sources`、水合返回 `source_ids`
- [x] 2.8 在 `daily_digest.py` 的 `preview_digest`、`create_digest_run` 调用 `digest_candidates` 时传入规则的 `source_ids`
- [x] 2.9 在 `main.py` 的 `/notifications/digests/options` 响应新增 `information_sources` 字段（`SELECT id,name,type FROM information_sources WHERE deleted_at IS NULL ORDER BY name`），与既有 `sources` 区分；在 digest 规则 POST/PUT 接收并透传 `source_ids`

## 3. 前端

- [x] 3.1 在 `src/app/frontend/src/main.js` 删除推送规则表单区块、"推送规则"列表区块、"定时增量运行"区块及其模板
- [x] 3.2 在 `main.js` 删除相关 state（`ruleForm`、`editingRuleId`、`rulePersonSearch`、`visibleRulePersons`、`incrementalRunFilters`、`incrementalPreview`、`selectedIncrementalRun` 等）与方法（`saveNotificationRule`、`editRule`、`resetRuleForm`、`selectAllRuleTasks`、`clearRuleTasks`、`selectAllRulePersons`、`clearRulePersons`、`addRuleSendTime`、`removeRuleSendTime`、`toggleRule`、`removeRule`、`previewIncrementalRule`、`runIncrementalRule`、`openIncrementalRun`、`retryIncrementalBatch`），并从 `return` 块清理
- [x] 3.3 在 `main.js` 为 `digestForm` 增加 `source_ids`（数组），新增信息源多选 picker（全选/清空/搜索，交互与人物 picker 一致），空选 = 全部信息源
- [x] 3.4 在 `digestPayload`/`editDigestRule`/`resetDigestForm` 中处理 `source_ids`（提交时映射为 Number 数组）
- [x] 3.5 将"新增/编辑每日时间线邮件"、"每日时间线邮件"列表、"日报运行记录"、仪表盘"最近日报运行"及相关 flash 消息文案统一改为"动态推送"
- [x] 3.6 在动态推送规则列表表格增加信息源列（空选显示"全部信息源"）

## 4. 配置

- [x] 4.1 在 `src/app/backend/config.py` 移除 `notifications.scheduled_incremental` 默认配置块，确保 `notifications.daily_digest` 与 `notifications.email` 不受影响
- [x] 4.2 检查 `src/config/app.json` 模板/示例，移除 `scheduled_incremental` 段；确认增量部署不覆盖服务器既有 `app.json`，残留 `scheduled_incremental` 段被安全忽略
- [x] 4.3 确认系统配置页面仍能展示动态推送与邮件通道生效值，SMTP 密钥继续脱敏

## 5. 测试

- [x] 5.1 更新 `src/app/frontend/src/notifications-ui.test.js`：移除推送规则/定时增量运行相关断言，新增推送管理页面不呈现已下线模块的断言
- [x] 5.2 更新 `src/app/frontend/src/daily-digest-ui.test.js`：新增动态推送信息源 picker 渲染、空选=全部、多选提交、文案为"动态推送"的断言
- [x] 5.3 新增/更新后端测试：`digest_candidates` 信息源过滤（所选信息源、空选全部、多源去重）、`save_digest_rule` 信息源校验与持久化、digest options 返回 `information_sources`
- [x] 5.4 新增后端测试：移除的路由（`/notifications/rules*`、`/notifications/incremental/*`）返回 404；保留的 `/notifications/digests/*`、`/notifications/deliveries*` 正常
- [x] 5.5 运行全部单元测试与冒烟测试，确保通过；验证 `NotificationWorker` 仅含 immediate（历史重试）与 digest 处理

## 6. 文档

- [x] 6.1 更新 `src/README.md`：推送管理页面介绍（动态推送维度含信息源、移除推送规则与定时增量）、配置文件说明、页面介绍
- [x] 6.2 更新需求规格说明书：新增动态推送需求、移除推送规则与定时增量需求
- [x] 6.3 更新设计说明书：推送管理架构调整为单一动态推送、信息源维度过滤设计、移除清单
- [x] 6.4 检查 `openspec` 规格一致性：`dynamic-push` 与 `scheduled-incremental-event-push` delta 与文档描述吻合

## 7. 部署 / Jenkins

- [x] 7.1 检查 `src/JenkinsConfig/Jenkinsfile`：确认构建、测试、部署步骤兼容后端文件删除与数据库迁移，不覆盖服务器 `app.json` 与 `data` 目录
- [x] 7.2 验证 Linux 运维脚本 `start.sh`、`stop.sh`、`status.sh` 与 systemd 启停、状态检查正常（如适用验证 Windows `start.ps1`/`stop.ps1`/`status.ps1`）
- [x] 7.3 自测通过后提交 Github，手动触发 Jenkins 手工构建，提示用户访问构建后服务验证：动态推送 CRUD（含信息源维度）、推送管理页面无推送规则/定时增量运行模块、历史投递记录可读
