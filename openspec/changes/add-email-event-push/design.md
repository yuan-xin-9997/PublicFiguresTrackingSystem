## Context

采集任务目前由 `run_collection_task` 抓取材料、写入 `raw_documents`、分析并插入或合并 `timeline_events`，最后更新 `task_runs`。事件与采集任务运行之间没有直接关系，任务完成后也没有可靠的异步外发机制。系统配置来自代码默认值、`app.json` 和环境变量，配置页面目前只读；SQLite 是单机部署的唯一数据库。

本变更跨越配置、数据库、任务执行、后台调度、SMTP 外部服务、FastAPI API、Vue 页面、权限、审计和运维文档。邮件服务故障不能回滚或污染已经成功的采集结果，重启和任务重跑也不能导致重复邮件。

## Goals / Non-Goals

**Goals:**

- 管理员可在页面配置邮件通道和推送规则，也可仅使用 `app.json`；页面非空字段按字段覆盖文件配置。
- 推送规则可选择一个或多个现有采集任务、零个或多个指定人物和一个或多个事件类型；人物为空时匹配全部人物。
- 只为规则生效后的任务运行所新建事件创建投递，不回溯历史，不推送去重合并到旧事件的材料。
- 通过 SQLite 持久化 outbox、唯一约束和独立 worker 实现可恢复、可重试、可观测的邮件投递。
- SMTP 凭证不硬编码、不明文返回、不写日志；修改配置、规则和测试发送均受管理员权限与审计保护。

**Non-Goals:**

- 不实现短信、Webhook、企业即时通信或移动端推送。
- 不提供历史事件补发、定时摘要或由登录用户自助维护的个人订阅；人物范围仍由管理员在全局推送规则中维护。
- 不改变事件抽取、去重、审核和现有采集调度语义。
- 不迁移到 PostgreSQL、消息队列或多进程 worker。

## Decisions

### 1. 使用 SQLite transactional outbox，并显式记录任务运行新建事件

新增 `task_run_events(run_id, event_id, created_at)`。`analyze_document` 在事件插入成功时返回新事件 ID，任务服务在同一业务路径写入该关联；命中既有 `dedup_key` 的事件不写入关联。任务结束后，在一个短事务中根据已启用规则计算匹配事件和收件人，并创建 outbox 批次及明细。

新增：

- `notification_settings`：单例页面覆盖配置、加密后的 SMTP 密码、更新时间和操作者。
- `notification_rules`：名称、启停、事件类型 JSON、创建/更新时间。
- `notification_rule_tasks`：规则与 `collection_tasks` 的多对多关系。
- `notification_rule_persons`：规则与 `persons` 的可选多对多关系；某规则没有关联行时表示全部人物。
- `email_delivery_batches`：任务运行、收件人、分片序号、状态、尝试次数、下次尝试、错误摘要、发送时间；`UNIQUE(task_run_id, recipient, part_number)`。
- `email_delivery_items`：批次内事件并冗余收件人以建立全局幂等键；`UNIQUE(task_run_id, event_id, recipient)`。

同一收件人被多条重叠规则命中时，对规则结果取并集，因此一个任务运行中的同一事件只出现一次。人物范围同样在 outbox 创建时求值：人物关联为空的规则匹配全部人物，存在关联时只匹配 `timeline_events.person_id` 在集合中的事件。选择关联表而不是在 `timeline_events` 增加单一 `task_run_id`，是因为未来一个事件可能从多个运行获得证据，而本功能只需要保存“首次由本运行新建”的事实。

备选方案是在任务结束时间和事件 `created_at` 之间做时间范围查询；该方案会受并发、时钟边界和其他写入影响，无法可靠证明事件来自哪个任务，因此不采用。

### 2. 使用独立、持久化的轻量邮件 worker

应用 lifespan 始终启动 `NotificationWorker`；worker 按 `notifications.email.worker_poll_seconds` 轮询到期批次。仅当有效配置启用且存在待处理批次时连接 SMTP。每次发送在数据库事务外完成，成功后以短事务把批次和明细标记为 `sent`，失败后记录经过清洗且截断的错误摘要，并按配置的指数退避更新 `next_attempt_at`。达到最大次数后标记 `failed`，管理员可从页面手工重试。

邮件发送失败不会改变 `task_runs.status` 或已入库事件；任务日志只追加“已排队/推送失败”等关联信息。worker 重启后继续处理 `pending`/`retrying` 批次。

备选方案是在采集请求内同步发信；它会拉长任务事务和 API 响应，并把外部邮件故障耦合到采集状态，因此不采用。引入 Redis/Celery 对当前单机 SQLite 规模过重，也不采用。

### 3. 页面配置逐字段覆盖 `app.json`

`DEFAULT_CONFIG` 和 `src/config/app.json` 增加：

```json
{
  "notifications": {
    "email": {
      "enabled": false,
      "smtp_host": "",
      "smtp_port": 587,
      "security": "starttls",
      "username": "",
      "password_env": "PFTS_SMTP_PASSWORD",
      "credential_key_env": "PFTS_NOTIFICATION_CREDENTIAL_KEY",
      "from_address": "",
      "from_name": "",
      "to_addresses": [],
      "subject_prefix": "[PFTS]",
      "max_events_per_message": 25,
      "worker_poll_seconds": 15,
      "max_attempts": 5,
      "retry_base_seconds": 60,
      "timeout_seconds": 15
    }
  }
}
```

有效配置解析顺序为：代码默认值 < `app.json` < `PFTS_NOTIFICATIONS__...` 环境覆盖 < 数据库页面非空覆盖。页面清空某字段表示删除覆盖并回退文件/环境配置，而不是写入空值遮蔽。API 返回每个字段的来源与脱敏值，便于解释最终配置。

`to_addresses` 在页面和文件中规范化、去重并校验格式。SMTP 主机、端口、用户名、发件人和收件人均不得硬编码。

备选方案是页面保存后回写 `app.json`；生产部署会保留服务器配置文件，且应用进程不应修改代码/配置目录，因此采用数据库覆盖层。

### 4. 页面 SMTP 密码使用外部主密钥加密

页面提交的新 SMTP 密码使用 `cryptography` 的 Fernet 对称加密后写入 SQLite，主密钥从 `credential_key_env` 指定的环境变量读取，绝不进入数据库或 API。未配置有效主密钥时，后端拒绝保存页面密码，但仍允许通过 `password_env` 使用环境变量密码。读取配置时，页面加密密码优先于 `password_env`；页面未保存密码时回退文件定义的密码环境变量。

所有读取 API只返回 `password_configured` 和来源。保存表单中的空密码表示保持原值，显式“清除页面密码”操作才删除密文。SMTP 异常只保留类型、阶段和安全的服务端响应摘要，不记录认证值、完整邮件正文或收件人列表。

备选方案是把密码明文存入 SQLite；这与配置脱敏和备份安全要求冲突，不采用。只允许环境变量虽然最安全，但不能满足页面配置邮箱信息的需求。

### 5. 邮件以“任务运行 + 收件人”聚合

一个批次发送一封 UTF-8 multipart 邮件，主题包含可配置前缀、任务名和新增事件数。正文同时提供纯文本和 HTML，逐条包含事件类型、人物、标题、摘要、北京时间、地点、确认/审核状态、具体来源和基于 `server.base_url` 生成的站内详情链接。无外部基础 URL 时不生成不可访问的绝对链接。

只发送本次运行新建、类型命中规则且人物满足规则可选范围的事件。规则在任务结束创建 outbox 时求值并固化事件列表，之后修改任务、人物或事件类型范围不会改变已排队批次。邮件内容在实际发送时从事件表读取最新可见字段；已被驳回或删除的事件在发送前跳过并记录原因，空批次标记为 `skipped`。

备选方案是一事件一邮件；在单次采集产生多个事件时噪声和 SMTP 压力更大，因此采用聚合邮件。

### 6. API、页面和权限

新增管理员写接口：

- `GET/PUT /api/v1/notifications/email/config`
- `POST /api/v1/notifications/email/test`
- `GET/POST/PUT/DELETE /api/v1/notifications/rules`
- `POST /api/v1/notifications/deliveries/{id}/retry`

新增只读接口：

- `GET /api/v1/notifications/options`（可选采集任务、人物、事件类型）
- `GET /api/v1/notifications/deliveries`、`GET /api/v1/notifications/deliveries/{id}`

权限系统增加 `notifications` 页面键。管理员拥有全部读写权限；被授予该页面的普通用户可查看脱敏通道状态、规则和投递结果，但所有写入、测试发送和重试接口仍要求 admin。每次配置/规则变更、测试发送和手工重试写入审计日志。

Vue 增加“推送管理”导航和四个区域：生效邮件配置、规则编辑、测试发送、投递记录。规则编辑器以可搜索/勾选列表维护任务与人物范围，人物不勾选时明确显示“全部人物”，并在规则摘要中显示人物范围。任务中心的任务运行日志继续显示本次排队数量和最近失败摘要，并提供跳转。

## Risks / Trade-offs

- [页面凭证依赖外部主密钥，密钥丢失后无法解密] → 启动/配置页显示明确健康状态；更换密钥前重新录入密码；备份文档同时提醒安全保存环境密钥但不得把密钥打入普通备份。
- [SMTP 服务限制频率、拒绝 HTML 或暂时不可用] → 每运行每收件人聚合、multipart 回退、超时、指数退避和最大重试；错误不影响采集结果。
- [进程在 SMTP 接收邮件后、写入 sent 状态前崩溃，可能重复投递] → 使用稳定的 `Message-ID`（由 batch ID 派生）并在重试中复用；SQLite outbox 提供至少一次投递，无法对普通 SMTP 保证严格 exactly-once，此边界在运维文档中说明。
- [页面覆盖和环境配置来源混淆] → 有效配置 API逐字段返回来源；清空覆盖恢复继承；测试覆盖优先级与重启行为。
- [大量事件导致邮件过大] → 配置单封最大事件数，超出时按稳定事件 ID 分片并使每个分片具有独立批次键；默认值在实现时取安全的小批量。
- [SQLite worker 与采集并发写产生竞争] → 网络 I/O 全部位于事务外，领取批次和状态更新使用短事务及现有 busy timeout，一次只领取有限批次。
- [把人物选择改为必填会使既有规则升级后失效] → 使用“空关联即全部人物”的兼容语义；迁移只建表不回填，既有规则继续保持原行为。
- [人物被软删除后仍可能出现在旧规则中] → 选项接口只提供当前可用人物，匹配时要求人物仍可用；规则读取保留已关联 ID 以便管理员识别和清理，历史投递不受影响。

## Migration Plan

1. 在向后兼容的 schema 迁移中创建通知配置、规则、规则任务/人物关联、任务运行事件关联、投递批次和明细表及索引；默认不启用邮件。
2. 增加默认配置和 `app.json` 示例。现有部署未配置时行为保持不变，不产生邮件。
3. 部署后先配置环境主密钥/SMTP 密码环境变量，再由管理员在页面保存通道参数并发送测试邮件。
4. 创建并启用推送规则；可不选择人物以匹配全部人物，也可选择一个或多个人物缩小范围。规则只作用于此后完成的采集任务运行，不扫描历史事件。
5. 回滚应用前停止 worker；旧版本会忽略新增表。若需要重新部署新版，未发送 outbox 保留并可继续处理。
6. 若必须停止推送，先在页面或配置中关闭 `enabled`；保留投递记录用于审计，不级联删除事件或任务数据。

## Open Questions

无阻塞问题。实现默认采用全局收件人列表、管理员规则中的可选人物范围、任务运行聚合邮件和仅未来新增事件语义；后续如需按登录用户自助订阅或历史补发，另行提出变更。
