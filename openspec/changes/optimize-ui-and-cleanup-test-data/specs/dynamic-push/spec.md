## MODIFIED Requirements

### Requirement: 动态推送管理界面、权限与审计完整

推送管理页面和 API SHALL 提供动态推送规则 CRUD、窗口预览、补跑、运行列表/详情与批次重试。拥有 `notifications` 页面权限的用户可查看脱敏数据；只有管理员可执行写操作。所有写操作 MUST 记录不含凭证和正文的审计摘要。页面 SHALL 仅保留邮件通道、动态推送与运行记录，不再呈现投递记录模块。

#### Scenario: 普通授权用户只读访问

- **WHEN** 拥有 `notifications` 页面权限的普通用户查看动态推送规则和运行详情
- **THEN** 系统返回北京时间窗口、候选/批次数及清洗后的错误，但拒绝创建、修改、补跑或重试请求

#### Scenario: 管理员写操作留审计

- **WHEN** 管理员创建、修改、删除动态推送规则或重试批次
- **THEN** 系统记录审计摘要且摘要不含 SMTP 密码、凭证或邮件正文

#### Scenario: 页面不再呈现已下线模块

- **WHEN** 用户打开推送管理页面
- **THEN** 页面不呈现"新增推送规则"、"推送规则"、"定时增量运行"与"投递记录"模块

## REMOVED Requirements

### Requirement: 投递记录（历史）保留

**Reason**: 旧通知系统的投递记录基于 `email_delivery_batches` / `email_delivery_items` 表，数据库迁移 v9 已废弃旧推送规则，该表不再有写入。当前部署中记录数为 0，动态推送系统使用独立的 `daily_digest_*` 表追踪投递。保留该模块会导致页面展示无意义的空列表。

**Migration**: 投递记录 API 端点（`GET /api/v1/notifications/deliveries`、`GET /api/v1/notifications/deliveries/{id}`、`POST /api/v1/notifications/deliveries/{batch_id}/retry`）将被移除。`email_delivery_batches` 和 `email_delivery_items` 表保留在数据库中不删除。前端投递记录面板模板、状态变量（`deliveryFilters`、`deliveryTotal`、`selectedDelivery`）和函数（`openDelivery`、`retryDelivery`）将被移除。任务运行查询中的 `notification_batches`、`notification_items`、`notification_error` 统计将被移除。