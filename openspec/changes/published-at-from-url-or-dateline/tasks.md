# tasks

## 1. 提取逻辑

- [x] 1.1 新增 `_URL_DATE_PATTERNS`（支持 `/20260803/`、`/2026/0803/`、`/2026/08/03/`）和 `_FUTURE_DATE_SUFFIX_MARKERS`/`_FUTURE_DATE_PREFIX_MARKERS` 常量
- [x] 1.2 新增 `_is_future_context_date(text, start, end)` 辅助
- [x] 1.3 重写 `infer_published_at`：URL 发布日期路径优先（含月日范围校验），正文回退时跳过施行/未来语境日期

## 2. 存量迁移

- [x] 2.1 新增 `migrate_published_at_to_url_date(connection)`：扫描 `raw_documents`，用 `infer_published_at(url, "")` 取 URL 日期，与 `published_at` 北京日历日比较，且不晚于 `collected_at` 日历日
- [x] 2.2 重置被污染文档 `published_at`，重算受影响 `human_locked=0` 事件 `start_at`/`dedup_key`（复用 V10 冲突解决：protected_keys + 临时 key + min-id 存活 + 整组删除）
- [x] 2.3 跳过 `human_locked=1`，写审计日志（`migrate_published_at_to_url`），幂等
- [x] 2.4 `initialize` 增加 schema_version=11 闸门

## 3. 测试

- [x] 3.1 `test_collectors.py`：URL 优先（含 news.cn 八位连写 + 三种格式）、正文回退跳过施行/将于语境、正文非施行日期回退
- [x] 3.2 `test_extractor.py`：V11 迁移重置 `published_at` + 重算事件 `start_at`/`dedup_key` + 跳过锁定 + 幂等
- [x] 3.3 `test_notifications.py`：schema_version 期望 10 -> 11
- [x] 3.4 全量 `pytest` 通过（74 passed）

## 4. 文档与交付

- [x] 4.1 更新需求规格说明书 `FR-AI-002A` / 设计说明书 §8.2 关于 `published_at` 提取口径
- [x] 4.2 更新 README.md
- [ ] 4.3 提交 Github 后手动触发 Jenkins，V11 迁移在启动时重置被污染文档；验证事件 1848 `start_at` 变为 `2026-08-02T16:00:00+00:00`
