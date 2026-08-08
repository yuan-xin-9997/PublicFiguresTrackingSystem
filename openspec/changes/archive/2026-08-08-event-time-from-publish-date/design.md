## Context

公开人物行程追踪系统通过 `extractor.py` 从采集到的文章中抽取时间线事件。当前 `_iso_date(text, fallback)` 在设定事件 `start_at` 时，**优先匹配证据片段正文里的完整日期**（`DATE_PATTERNS[0]`，含年份），只有正文无完整日期时才回退到文章发布时间 `published_at`。这导致正文里出现的引用日期、施行日期、历史日期被误当成事件发生时间（如「李强签署国务院令 公布修订后的《集成电路布图设计保护条例》」误取正文中的条例日期而非发布时间 2026-08-03）。

`database.py` 已有 migration v2/v3 被动修补部分被污染的 `start_at`，但抽取器仍在源头优先取正文日期，问题持续产生。三条抽取路径（本地规则 `_local_extract_with_stats`、外部模型 `_external_extract_with_stats`、外部失败回退本地）的 `start_at` 逻辑不一致：本地路径用 `_iso_date`，外部路径仅在 `other` 类型且无 `start_at` 时回退发布时间，其余采信模型返回值。

约束：FastAPI + Vue + SQLite；不得硬编码环境信息；时间展示沿用北京时间规则；不得为普通用户增加写权限；变更需同步更新文档与测试并通过后交付 Jenkins。

## Goals / Non-Goals

**Goals:**

- 将事件 `start_at` 的唯一来源改为文章发布时间，适用于三条抽取路径与三类事件类型。
- 移除抽取器中用于设定 `start_at` 的正文日期解析，消除源头污染。
- 一次性修复存量中被正文日期覆盖的未锁定事件，并保持 `dedup_key` 一致以避免重分析产生重复。
- 保持不确定性关键词的 `rumored`/`expected` 判定与北京时间展示行为不变。

**Non-Goals:**

- 不改变事件主体归属校验、正文清洗、地点抽取、推送管理与时间线筛选 UI 行为。
- 不引入新的配置项或外部服务依赖。
- 不为普通用户增加维护写权限。
- 不自动回滚已修复的 `start_at`（数据语义变更，回滚靠部署前备份）。

## Decisions

### 决策 1：`start_at` 一律取 `published_at`，移除正文日期解析

将 `_iso_date(text, fallback)` 简化为只对发布时间归一化的辅助函数（如 `_publish_time(published_at)`）：存在则按北京时间归一化为 ISO 字符串返回，否则返回 `None`。删除其中 `DATE_PATTERNS[0]`/`DATE_PATTERNS[1]` 对正文 `text` 的匹配分支。`_local_extract_with_stats` 中 `start_at = _iso_date(segment, document.get("published_at"))` 改为 `start_at = _publish_time(document.get("published_at"))`。

`has_explicit_full_date` 与 `DATE_PATTERNS` 常量在时间路径中不再需要（`time_precision` 改由发布时间有无决定，见决策 4）。`_content_units` 中用于切分扁平列表页的日期正则是内联字面量（`re.split(r"\s+(?=20\d{2}...")`），与 `DATE_PATTERNS` 常量无关，保持不动。

**理由**：发布时间是来源元数据，可靠；正文日期来源不可控。用户已确认含未来计划性事件也一律取发布时间。
**备选**：仅对已发生事件取发布时间、未来时态事件保留正文未来日期。被用户否决（"一律取发布时间"）。

### 决策 2：三条抽取路径统一覆盖

- 本地规则路径：按决策 1 取发布时间。
- 外部模型路径：修改 prompt 的 `output` 字段说明，不再要求模型返回 `start_at`（提示"发生时间由系统按发布时间填写，模型不返回"）；在 `_external_extract_with_stats` 中对每个通过校验的候选强制 `item["start_at"] = _publish_time(document.get("published_at"))`，覆盖模型可能返回的任何正文日期。删除原"`other` 类型且无 `start_at` 才回退"的特殊分支。
- 外部失败回退本地：复用本地路径，天然一致。

**理由**：三条路径行为一致才能避免因切换 provider 产生不同时间；外部模型对时间抽取不可靠，不应采信。

### 决策 3：`confirmation_status` 基线为 `completed`，保留不确定性关键词判定

发布时间恒在过去，故基线 `confirmation = "completed"`（原 `start_at <= now` 判定自然成立）。保留「据称/可能/预计/传闻/或将」关键词将状态改为 `rumored`/`expected` 的逻辑不变。未来日期触发的 `expected` 随之消失（用户已接受）。

**注意**：「预计」既是未来时态词也是不确定性关键词，含「预计」的事件仍会标为 `expected`，反映不确定性而非未来调度，符合预期。

### 决策 4：`time_precision` 简化为 `day` / `unknown`

有发布时间（`start_at` 非空）记 `day`；无发布时间记 `unknown`。不再使用 `exact` 表达来自发布时间的发生时间--发布时间的时分只是发布时刻，不代表事件精确发生时刻，`day` 更诚实。

### 决策 5：存量修复用 Python 级迁移，重算 `dedup_key`

在 `database.py` 初始化迁移流程中新增一个 `schema_version` 版本（紧随现有最高版本），分两步：

1. **SQL 批量更新**：对 `human_locked=0` 且有证据文档的事件，`start_at = (SELECT COALESCE(d.published_at, d.collected_at) FROM event_evidence ee JOIN raw_documents d ... ORDER BY ... LIMIT 1)`，`time_precision='day'`。`human_locked=1` 不在更新范围。
2. **Python 重算 `dedup_key` + 冲突合并**：对每个被更新（及所有非锁定）事件，取其首条 `evidence_text`，复用 `extractor.event_core_text` / `event_dedup_key` 以新 `start_at` 重算键；按新键分组，若多个非锁定事件落到同一键，保留 `id` 最小者，删除其余非锁定冗余事件（锁定事件永不删除，若某键下既有锁定又有非锁定，则非锁定视为冗余删除）。最后向 `audit_logs` 写入一条摘要（已修复数、跳过锁定数、删除重复数）。

幂等性：`schema_version` 闸门保证只跑一次；即便重跑，`start_at` 更新确定性、`dedup_key` 重算确定性、冲突删除只删真重复，不会产生额外副作用。

**理由**：`dedup_key` 依赖 `event_core_text` 的中日韩/字母数字归一化与小写，纯 SQL 难以准确复现，Python 复用现有函数最稳妥，满足"重分析不产生重复"要求。
**备选**：纯 SQL 迁移（与 v2/v3 一致）但无法准确重算 `dedup_key`，会在后续重分析时产生重复事件。否决。
**依赖**：`database.py` 导入 `app.backend.extractor` 的两个纯函数。`extractor` 不依赖 `database`，无循环依赖。若希望 `database.py` 保持低层独立，可在迁移内联这两个小函数的等价实现--推荐直接复用。

## Risks / Trade-offs

- **[丢失未来事件的"预计"状态]** -> 用户已接受；不确定性关键词仍标记 `rumored`/`expected`；未来行程事件仍以发布时间出现在时间线上，不丢失事件本身。
- **[无 `published_at` 的文章事件 `start_at` 为空]** -> 采集器 `infer_published_at` 从电头/URL 推断发布时间，多数文章有值；空值事件 `time_precision=unknown`，仍可被搜索，仅不参与时间排序优先。可接受。
- **[迁移修改存量事件数据，不可自动回滚]** -> 跳过 `human_locked`；幂等可重复；部署前提示备份 `app.sqlite3`；回滚方式为还原备份 + 回退代码。
- **[重算 `dedup_key` 冲突时删除事件]** -> 仅删除非锁定、同键真重复（同人物/类型/正文/新日期），保留 `id` 最小者；锁定事件永不删除；删除计数写入审计日志。
- **[`预计` 既是未来词又是不确定性关键词]** -> 含「预计」事件标 `expected` 反映不确定性，`start_at` 仍为发布时间，语义自洽，无需特殊处理。

## Migration Plan

1. **实现**：修改 `extractor.py`（决策 1-4）、`database.py`（决策 5 新增迁移版本）、更新 `services.py` 若 `start_at` 落库有特殊分支。
2. **测试**：更新 `tests/test_extractor.py` 中断言正文日期作为 `start_at` 的用例为发布时间；新增"正文历史日期不覆盖发布时间""外部模型返回值被覆盖""未来时态取发布时间""迁移修复 + 幂等 + 跳过锁定 + 冲突合并"等用例；跑全量测试。
3. **文档**：更新需求规格说明书、设计说明书、README.md（事件时间口径说明）、Jenkinsfile（如涉及部署步骤）。
4. **部署**：提交 Github → 手动触发 Jenkins → 部署前备份 `data/app.sqlite3` → 启动时迁移自动执行 → 用户访问服务验证（重点核验「李强签署国务院令」类事件时间已改为发布时间）。
5. **回滚**：还原部署前备份的 `app.sqlite3` 并回退代码版本。

## Open Questions

- 无遗留决策点。两条关键问题（未来事件口径、是否修复存量）已由用户确认。
