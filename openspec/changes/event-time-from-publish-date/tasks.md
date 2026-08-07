# Implementation Tasks

## 1. 抽取器：事件时间改取发布时间

- [x] 1.1 将 `extractor.py` 的 `_iso_date(text, fallback)` 重构为 `_publish_time(published_at)`：仅对发布时间归一化（北京时间）返回 ISO 字符串或 `None`，删除 `DATE_PATTERNS[0]`/`DATE_PATTERNS[1]` 对正文 `text` 的匹配分支
- [x] 1.2 `_local_extract_with_stats` 中 `start_at` 改为 `_publish_time(document.get("published_at"))`；移除 `has_explicit_full_date` 变量及其在 `time_precision` 上的使用
- [x] 1.3 `time_precision` 改为：有 `start_at` 记 `day`，无则 `unknown`；不再用 `exact` 表达来自发布时间的发生时间
- [x] 1.4 确认 `_content_units` 切分扁平列表页用的是内联日期正则而非 `DATE_PATTERNS` 常量；`DATE_PATTERNS` 与 `BEIJING_TIMEZONE` 已移除
- [x] 1.5 复核 `confirmation_status`：基线 `completed`（发布时间在过去成立），保留「据称/可能/预计/传闻/或将」关键词的 `rumored`/`expected` 判定；外部路径同样强制该规则

## 2. 外部模型路径统一

- [x] 2.1 修改 `_external_extract_with_stats` 的 prompt：`output` 字段说明不再要求模型返回 `start_at`，注明发生时间由系统按发布时间填写
- [x] 2.2 对每个通过校验的外部候选强制 `item["start_at"] = _publish_time(document.get("published_at"))`，覆盖模型返回值；删除原"`event_type=='other' and not start_at` 才回退发布时间"的特殊分支
- [x] 2.3 同步 `item["time_precision"]` 为 `day`/`unknown` 新口径（直接赋值而非 setdefault）

## 3. 落库逻辑一致性

- [x] 3.1 调整 `services.py` 事件更新处 `start_at=CASE WHEN ? IS NOT NULL THEN ? ...` 始终采信抽取器的发布时间（移除原"较早优先"守卫，避免保留旧的正文日期）
- [x] 3.2 `_publish_time` 与 `services.normalize_datetime` 采用一致的 fromisoformat→UTC 归一化，前者增加 RFC 2822（RSS）回退，两者在 ISO 输入下输出一致

## 4. 存量数据迁移

- [x] 4.1 在 `database.py` 迁移流程新增 `schema_version = 10`，由闸门保证只执行一次
- [x] 4.2 提取非锁定事件的证据文档 `COALESCE(published_at, collected_at)`，在 Python 端经 `_publish_time` 归一化为 UTC ISO；锁定事件不入修复集
- [x] 4.3 复用 `extractor.event_dedup_key`（文本取首条 evidence，回退 title）以新 `start_at` 重算受影响非锁定事件的键
- [x] 4.4 冲突合并：受保护键（锁定 + 非修复集）> 删除整组；同组多事件 > 保留 `id` 最小；锁定事件永不删除；用临时 `repair-migration-<id>` 键避免中间 UNIQUE 冲突
- [x] 4.5 写入 `audit_logs` 摘要（已修复/跳过锁定/删除重复计数）；闸门 + 确定性的 start_at 与 dedup_key 共同保证幂等

## 5. 测试

- [x] 5.1 更新 `tests/test_extractor.py::test_full_chinese_date_is_saved_as_beijing_calendar_day`：`start_at` 改为发布时间，`time_precision` 改为 `day`
- [x] 5.2 更新 `test_month_day_uses_article_timestamp_instead_of_runtime_year` 的 `time_precision` 断言（`exact` -> `day`），`start_at` 仍为发布时间
- [x] 5.3 新增用例：正文含历史/施行完整日期（模拟「李强签署国务院令」条例案例）时 `start_at` 取发布时间，不取正文日期
- [x] 5.4 新增用例：外部模型返回正文日期 `start_at` 被发布时间覆盖
- [x] 5.5 新增用例：未来时态事件（「将于X访问Y」）`start_at` 取发布时间、`confirmation_status` 为 `completed`；含「预计/或将」仍标 `rumored`/`expected`
- [x] 5.6 新增用例：迁移修复未锁定事件 `start_at`、跳过 `human_locked`、重算 `dedup_key`、冲突合并、幂等
- [x] 5.7 运行全量测试（`pytest`）通过（72 passed）

## 6. 文档与交付

- [ ] 6.1 更新需求规格说明书、设计说明书中事件时间相关章节
- [ ] 6.2 更新 `README.md`：补充事件发生时间取文章发布时间的口径说明
- [ ] 6.3 检查 `JenkinsConfig/Jenkinsfile` 是否需调整（部署前备份 `data/app.sqlite3` 的提示等）
- [ ] 6.4 提交 Github 后手动触发 Jenkins 手工构建，提示用户访问构建后服务验证（重点核验「李强签署国务院令」类事件时间已改为发布时间）
