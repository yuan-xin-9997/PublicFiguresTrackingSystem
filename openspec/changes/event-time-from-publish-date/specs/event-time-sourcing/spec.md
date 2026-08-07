## ADDED Requirements

### Requirement: 事件发生时间取文章发布时间

系统 SHALL 将每个时间线事件的 `start_at` 设为其来源文章的发布时间（`published_at`），MUST NOT 从事件正文、证据片段或标题中抽取日期作为 `start_at`。该规则适用于行程、言论、其他三类事件。

#### Scenario: 正文含完整日期但与发布时间不一致

- **WHEN** 一篇文章发布于 2026-08-03，正文证据为「李强签署国务院令 公布修订后的《集成电路布图设计保护条例》」且正文另含历史或施行日期（如「2001年10月1日」）
- **THEN** 系统将该事件的 `start_at` 设为发布时间 2026-08-03，不使用正文中的历史日期

#### Scenario: 正文仅含月日

- **WHEN** 证据正文仅含「12月3日」而无年份，文章发布时间为 2023-12-04
- **THEN** 系统将 `start_at` 设为发布时间 2023-12-04，不使用「12月3日」

#### Scenario: 正文无任何日期

- **WHEN** 证据正文不含任何日期，文章发布时间为 2026-07-16
- **THEN** 系统将 `start_at` 设为发布时间 2026-07-16

#### Scenario: 文章无发布时间

- **WHEN** 来源文章没有 `published_at`
- **THEN** 系统将 `start_at` 留空，`time_precision` 记为 `unknown`

### Requirement: 三条抽取路径统一使用发布时间

系统 MUST 在本地规则抽取、外部模型抽取以及外部模型失败后的本地回退抽取这三条路径中，均以文章发布时间填写 `start_at`；外部模型返回的 `start_at` MUST 被系统按发布时间覆盖，不得直接采信。

#### Scenario: 本地规则路径

- **WHEN** 本地规则抽取从证据片段生成事件
- **THEN** 该事件 `start_at` 等于文章发布时间

#### Scenario: 外部模型返回正文日期

- **WHEN** 外部模型对发布于 2026-08-03 的文章返回 `start_at` 为正文中的「2025-06-01」
- **THEN** 系统将其覆盖为发布时间 2026-08-03 后再入库

#### Scenario: 外部模型失败回退本地

- **WHEN** 外部模型不可用且系统回退到本地抽取
- **THEN** 回退生成的事件 `start_at` 同样取文章发布时间

### Requirement: 未来计划性事件取发布时间

系统 SHALL 对明确表述为未来的计划性事件（如含「将于」「计划」「拟」等未来时态的行程）也以文章发布时间作为 `start_at`；其 `confirmation_status` 按发布时间判定为 `completed`。系统 MUST 继续依据「据称」「可能」「预计」「传闻」「或将」等不确定性关键词将 `confirmation_status` 标记为 `rumored` 或 `expected`，该判定独立于发生时间来源。

#### Scenario: 未来时态行程

- **WHEN** 发布于 2026-08-05 的文章含「李强将于8月20日访问俄罗斯」
- **THEN** 该行程事件 `start_at` 为 2026-08-05，`confirmation_status` 为 `completed`

#### Scenario: 含不确定性关键词

- **WHEN** 证据含「李强或将出席」等不确定性表述
- **THEN** 系统仍按不确定性关键词将 `confirmation_status` 标为 `rumored` 或 `expected`，`start_at` 仍取发布时间

### Requirement: 时间精度随发布时间有无判定

系统 SHALL 在 `start_at` 取自发布时间且发布时间存在时将 `time_precision` 记为 `day`；当文章无发布时间、`start_at` 为空时记为 `unknown`。系统 MUST NOT 使用 `exact` 表示来自发布时间的发生时间。

#### Scenario: 有发布时间

- **WHEN** 事件 `start_at` 取自文章发布时间
- **THEN** `time_precision` 为 `day`

#### Scenario: 无发布时间

- **WHEN** 文章无发布时间导致 `start_at` 为空
- **THEN** `time_precision` 为 `unknown`

### Requirement: 存量事件按发布时间修复

系统 SHALL 提供一次性数据库迁移，将未锁定（`human_locked=0`）事件中被正文日期覆盖的 `start_at` 改回其证据文章的 `COALESCE(published_at, collected_at)`，并将 `time_precision` 设为 `day`；MUST 跳过 `human_locked=1` 的事件；MUST 同步重算受影响事件的 `dedup_key`，使其与新的 `start_at` 及现行抽取逻辑一致，避免重分析产生重复事件。

#### Scenario: 未锁定事件改回发布时间

- **WHEN** 迁移运行且某未锁定事件的 `start_at` 来自正文日期、其证据文章发布时间为 2026-08-03
- **THEN** 该事件 `start_at` 更新为 2026-08-03，`time_precision` 为 `day`

#### Scenario: 人工锁定事件跳过

- **WHEN** 迁移范围包含 `human_locked=1` 的事件
- **THEN** 系统不修改该事件的 `start_at` 或 `dedup_key`

#### Scenario: 重算去重键避免重复

- **WHEN** 迁移更新某事件 `start_at` 后
- **THEN** 该事件 `dedup_key` 用新的 `start_at` 重算，后续对同一文章重分析不会因键不一致而生成重复事件

#### Scenario: 重算键冲突合并

- **WHEN** 重算后多个未锁定事件落到同一 `dedup_key`
- **THEN** 系统保留其一并删除冗余的未锁定重复事件，人工锁定事件不被删除

#### Scenario: 迁移幂等可重复

- **WHEN** 迁移对已修复的数据库再次执行
- **THEN** 系统不产生额外修改、不创建重复事件、不重复删除

### Requirement: 修复过程可审计

系统 MUST 在迁移执行时记录修复的事件数量、跳过的人工锁定数量及删除的重复数量，并将摘要写入审计日志；管理员可在任务日志或维护结果中查看。

#### Scenario: 查看修复摘要

- **WHEN** 一次性迁移执行完成
- **THEN** 审计日志或任务日志包含已修复、已跳过、已删除重复事件的计数
