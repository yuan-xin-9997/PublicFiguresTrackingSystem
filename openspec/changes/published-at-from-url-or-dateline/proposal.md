## Why

`event-time-from-publish-date` 变更把事件 `start_at` 改为取文章 `published_at`，并假设 `published_at` 本身可靠。但验证线上数据时发现「李强签署国务院令 公布修订后的《集成电路布图设计保护条例》」这条事件的 `start_at` 仍是 `2026-10-15`（施行日期），原因是其文档的 `published_at` 字段本身就被污染成了 `2026-10-15`：

- webfetch 的 `generic.article` 没有返回发布时间（`fetch_metadata_json` 无 `date`），PFTS 回退到 `collectors.infer_published_at`。
- `infer_published_at` 旧逻辑**先扫正文前 5000 字找完整年月日，再用 URL 日期路径**。正文 pattern 要求带年份，于是电头「8月3日」（无年份）匹配不到，反而抓到正文里带年份的「2026年10月15日」（条例施行日期）。
- 正确的发布日期 8-3 其实就在 URL 路径 `/20260803/` 里，但因为正文日期优先级更高而没被采用；且旧 URL 正则 `/(20\d{2})/(\d{2})(\d{2})/` 只匹配 `/2026/0803/` 格式，匹配不到 news.cn 的 `/20260803/` 八位连写格式。

结果：V10 迁移「忠实地」把 `start_at` 设成了被污染的 `published_at`（10-15），同类政务稿（含施行、生效、会议日期）普遍存在此问题。需要从源头修正 `published_at` 提取，并清理存量。

## What Changes

- `collectors.infer_published_at` 改为 **URL 发布日期路径优先**：URL 含 `/20260803/`、`/2026/0803/`、`/2026/08/03/` 三种格式之一时，直接采用为 `published_at`，不再扫正文。URL 日期路径是 CMS 发布日标记，比正文日期可靠（正文日期可能是条例施行日、会议日、历史日）。
- URL 无日期路径时，回退到正文前 5000 字的完整年月日，但 **跳过施行/生效/未来语境**的日期（后缀「起施行/起生效/起执行/起实施/起公布/正式施行/正式实施/之日起」，前缀「将于/预计/拟于/计划于」），避免把「自 2026 年 10 月 15 日起施行」当成发布日期。
- 修正 URL 日期正则，支持八位连写 `/20260803/`（原正则匹配不到），并校验月日范围。
- **数据语义**：新增 schema_version=11 迁移 `migrate_published_at_to_url_date`，重置被污染的 `published_at`（URL 日期与 `published_at` 北京日历日不一致，且 URL 日期不晚于 `collected_at` 日历日），并重算受影响 `human_locked=0` 事件的 `start_at`/`dedup_key`；跳过人工锁定事件，冲突解决策略与 V10 一致，写审计日志，幂等。
- 同步更新需求规格说明书、设计说明书、README.md、单元测试。

## Capabilities

### New Capabilities
- `published-at-sourcing`: 文档发布时间 `published_at` 的提取口径--URL 发布日期路径优先，正文完整年月日作为回退并排除施行/生效/未来语境日期；存量被污染文档的迁移修复。

### Modified Capabilities
- 无（`event-time-sourcing` 已由 `event-time-from-publish-date` 变更定义并覆盖 `start_at` 取 `published_at`；本变更有针对性地修复 `published_at` 字段本身的来源）。

## Impact

- **受影响代码**：`src/app/backend/collectors.py`（`infer_published_at` 及新增常量/辅助）、`src/app/backend/database.py`（V11 迁移 + schema_version 闸门）。
- **受影响数据**：线上被污染的 `raw_documents.published_at` 及其关联事件的 `start_at`/`dedup_key`，由 V11 迁移在启动时自动修复；`human_locked` 事件不受影响。
- **下游**：动态推送按 `start_at` 归属窗口，修复后部分事件会从错误窗口移到正确窗口（发布日），符合预期。
- **风险**：URL 日期路径偶有非发布含义（如栏目日期），通过「URL 日期不晚于 `collected_at`」约束降低误改；正文回退跳过施行语境后若文章只剩施行日期，`published_at` 为空，`start_at` 回退到 `collected_at`（V10 的 `COALESCE`），比错误地用施行日期更安全。
