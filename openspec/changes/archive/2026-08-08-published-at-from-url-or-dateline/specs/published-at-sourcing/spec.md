## ADDED Requirements

### Requirement: 发布时间优先取 URL 发布日期路径

系统 SHALL 在推断文档 `published_at` 时，优先使用 URL 中的发布日期路径（`/20260803/`、`/2026/0803/`、`/2026/08/03/` 三种格式）。当 URL 含合规日期路径时，MUST 采用其为 `published_at`，MUST NOT 扫描正文日期覆盖之。

#### Scenario: URL 含八位连写日期路径

- **WHEN** 文档 URL 为 `https://www.news.cn/politics/leaders/20260803/c.html`，正文含「自2026年10月15日起施行」
- **THEN** `published_at` 设为 `2026-08-03T00:00:00+08:00`，不采用正文里的 10-15

#### Scenario: URL 含年/月/日或年/月日路径

- **WHEN** URL 为 `https://example.com/2026/08/03/post` 或 `https://example.com/2026/0803/post`
- **THEN** `published_at` 设为 `2026-08-03T00:00:00+08:00`

#### Scenario: webfetch 未返回发布时间

- **WHEN** webfetch `generic.article` 未返回 `date`，URL 含发布日期路径
- **THEN** 系统回退到 `infer_published_at` 并按 URL 路径确定 `published_at`

### Requirement: 正文日期仅作回退并排除施行语境

系统 SHALL 仅在 URL 不含发布日期路径时，才回退到正文前 5000 字的完整年月日。回退时 MUST 跳过处于施行/生效/未来语境的日期：日期后 10 字符内含「起施行/起生效/起执行/起实施/起公布/正式施行/正式实施/之日起」，或日期前 8 字符内含「将于/预计/拟于/计划于」。

#### Scenario: 正文仅含施行日期且 URL 无日期路径

- **WHEN** URL 无日期路径，正文为「自2026年10月15日起施行。」
- **THEN** `published_at` 留空（不把施行日期误当发布日期）

#### Scenario: 正文含将于前缀的未来日期

- **WHEN** URL 无日期路径，正文为「李强将于2026年10月15日出席。」
- **THEN** `published_at` 留空

#### Scenario: 正文含非施行的发布日期

- **WHEN** URL 无日期路径，正文为「新华社北京2026年8月3日电 发稿。」
- **THEN** `published_at` 设为 `2026-08-03T00:00:00+08:00`

### Requirement: 存量被污染文档的迁移修复

系统 SHALL 提供 schema_version=11 迁移，重置 `published_at` 被 正文施行/生效日期污染的存量文档：当文档 URL 含发布日期路径、其北京日历日与当前 `published_at` 北京日历日不一致、且不晚于 `collected_at` 北京日历日时，用 URL 日期重置 `published_at`。迁移 SHALL 同步重算受影响 `human_locked=0` 事件的 `start_at` 与 `dedup_key`，MUST NOT 修改或删除 `human_locked=1` 事件。迁移 MUST 幂等，重复执行不产生进一步变更，并写审计日志。

#### Scenario: 迁移重置被污染文档并重算事件

- **WHEN** 一篇文档 `published_at=2026-10-15`（污染）、URL 含 `/20260803/`、关联一条 `human_locked=0` 事件 `start_at=2026-10-15`
- **THEN** 迁移后文档 `published_at=2026-08-03T00:00:00+08:00`，事件 `start_at=2026-08-02T16:00:00+00:00`、`dedup_key` 重算、`time_precision=day`

#### Scenario: 迁移跳过人工锁定事件

- **WHEN** 一条 `human_locked=1` 事件 `start_at=2010-01-01`，其文档被重置 `published_at`
- **THEN** 该锁定事件的 `start_at`、`dedup_key` 不变

#### Scenario: 迁移幂等

- **WHEN** 迁移在已修复的数据库上再次执行
- **THEN** `repaired_documents=0`、`repaired_events=0`，数据无变化

#### Scenario: URL 日期晚于采集日期时不重置

- **WHEN** 文档 URL 日期路径的北京日历日晚于 `collected_at` 北京日历日
- **THEN** 该文档 `published_at` 不被重置（URL 日期可能并非发布日期）
