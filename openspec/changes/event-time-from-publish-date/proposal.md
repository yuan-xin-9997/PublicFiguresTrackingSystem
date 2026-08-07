## Why

事件抽取当前用事件正文中出现的完整日期（`DATE_PATTERNS[0]`）作为事件发生时间 `start_at`，仅当正文没有完整日期时才回退到文章发布时间 `published_at`。但正文里的日期常常并不是事件的发生时间——例如「李强签署国务院令 公布修订后的《集成电路布图设计保护条例》」正文中出现的条例原发布日期或施行日期被误当成签署事件的时间，而文章真正的发布时间（2026-08-03）反而被忽略。正文日期来源不可控（引用的历史日期、施行日期、转载电头、背景时间等），而发布时间是来源元数据，更可靠。数据库里已有 migration v2/v3 在被动修补部分被正文日期污染的 `start_at`，但抽取器本身仍优先取正文日期，问题在源头持续产生。需要把事件发生时间统一改为取文章发布时间，并修复已被正文日期污染的存量数据。

## What Changes

- 事件发生时间 `start_at` 一律取文章发布时间 `published_at`，不再从事件正文抽取日期；正文日期不再覆盖发布时间。适用于本地规则、外部模型、外部模型失败后的本地回退三条抽取路径，以及行程、言论、其他三类事件。
- 明确未来时态的计划性事件（如「将于 X 访问 Y」）也取发布时间；其 `confirmation_status` 由"预计"变为"已完成"。保留"据称/可能/预计/传闻/或将"等不确定性关键词触发的 `rumored`/`expected` 判定（这类状态反映不确定性，与发生时间来源无关）。
- 移除抽取器中用于设定 `start_at` 的正文日期解析（`_iso_date` 的正文匹配分支）；保留 `DATE_PATTERNS` 在 `_content_units` 中用于切分扁平列表页的分段用途（与事件时间无关，不变）。
- 外部模型提示词不再要求模型返回 `start_at`，由系统按发布时间统一填写并覆盖模型返回值。
- `time_precision`：有发布时间记为 `day`，无发布时间记为 `unknown`。
- **BREAKING**（数据语义）：新增数据库迁移，把未锁定（`human_locked=0`）事件中被正文日期覆盖的 `start_at` 改回 `COALESCE(d.published_at, d.collected_at)`，同步更新 `time_precision` 并重算 `dedup_key` 以保持去重一致；跳过人工锁定事件，写审计日志，幂等可重复执行。
- 同步更新需求规格说明书、设计说明书、README.md、Jenkinsfile（如涉及）、单元测试。

## Capabilities

### New Capabilities
- `event-time-sourcing`: 事件发生时间以文章发布时间为唯一来源，禁止从事件正文抽取时间；覆盖三条抽取路径、三类事件类型、未来计划性事件处理、存量数据修复与去重一致性。

### Modified Capabilities
<!-- 无。event-subject-attribution 规定的是主体归属校验与北京时间展示兼容，不涉及 start_at 来源；本变更不改变其要求。 -->

## Impact

- 受影响代码：`src/app/backend/extractor.py`（`_iso_date`、`_local_extract_with_stats`、`_external_extract_with_stats`、外部模型 prompt、`time_precision`/`confirmation_status` 计算）、`src/app/backend/database.py`（新增迁移版本）、`src/app/backend/services.py`（确认 `start_at` 落库逻辑与新规则一致）。
- 受影响测试：`src/tests/test_extractor.py` 中断言"正文日期作为 start_at"的用例（如 `test_full_chinese_date_is_saved_as_beijing_calendar_day`、`test_month_day_uses_article_timestamp_instead_of_runtime_year` 的 `time_precision` 断言、外部模型用例）需更新为发布时间。
- 数据库：新增迁移版本，修改未锁定事件的 `start_at`、`time_precision`、`dedup_key`；`human_locked=1` 事件不受影响。
- 配置与安全：不新增配置项；不涉及外部服务、认证、权限或网页抓取配置变更；沿用现有北京时间展示规则。
- 非目标：不改变事件主体归属校验、正文清洗、地点抽取、推送管理或时间线筛选 UI 行为。
