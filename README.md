# PublicFiguresTrackingSystem

公开人物行程动态言论跟踪系统（PFTS）把公开新闻、RSS、网页和人工材料整理成可核验的统一时间线。系统保存原始材料与证据片段，区分预计、已确认、已发生、存疑和有争议状态，并提供任务追踪、全文搜索及地点视图；采集到的事件默认进入时间线，管理员可删除不需要保留的事件。

系统定位是公开信息研究工具，不用于私人实时定位，不绕过登录、付费墙或反爬措施。

## 主要能力

- 用户登录、管理员/普通用户角色及页面权限。
- 公开人物、跨语言别名和来源关联管理。
- RSS、Atom、单篇网页、网站站内自动发现和人工材料采集。
- 自动识别网页元数据或 URL 中的发布日期；事件发生时间一律取文章的发布时间（`published_at`，缺失时回退 `collected_at`），不再从事件正文中抽取或推断日期，避免「正文里的 2014 年施行日期」覆盖「文章发布于 2026-08-03」之类的错误；时间精度有发布时间时记 `day`，无法解析时记 `unknown`。
- 原始文档去重、任务运行记录及逐条日志。
- 本地确定性抽取与可选 OpenAI-compatible 外部模型；逐个动作/言论谓词验证人物主体，拒绝仅被引用、作为背景或身份修饰的人物，并从事件相邻句补充公开地点。
- 中国日报网域族正文净化与文章质量门槛：去除首页、频道、推荐阅读和页尾，聚合页不进入分析。
- 行程、动态、言论统一时间线及证据链详情；时间线直接显示具体来源，同篇材料的言论优先于“其他”。
- 置信度、确认状态、证据链和人工锁定机制；管理员可在时间线删除事件。
- 搜索、地点兼容视图、仪表盘和审计日志。
- 邮件事件推送：采集任务运行结束后即时发送新增事件邮件，支持持久化重试与投递记录查阅。
- 动态推送：按规则配置人物、信息源、事件类型、收件人、北京时间发送时刻和汇总周期，默认每天 `08:30` 汇总昨天自然日并按发生时间升序发送；信息源可选，留空匹配全部信息源。
- Windows/Linux 启停脚本和 Jenkins 流水线。

## 技术架构

- 后端：Python 3.9+、FastAPI、Uvicorn。
- 前端：Vue 3、Vite。
- 数据库：SQLite（WAL、外键约束）。
- 密码：Python 标准库 scrypt 加盐哈希。
- 会话：可撤销的数据库会话 + HttpOnly Cookie。
- 采集：集中 WebFetch 服务负责 HTTP/Playwright、缓存、重试、限流与 SSRF；PFTS 负责 RSS 解析和业务入库。

系统默认使用本地规则抽取，因此不配置外部模型也可完整运行。设计细节见 [系统设计说明书](系统设计说明书.md)，需求边界见 [软件需求规格说明书](软件需求规格说明书.md)。

## 页面介绍

| 页面 | 用途 |
|---|---|
| 总览 | 人物、来源、今日材料/事件和异常任务统计 |
| 时间线 | 按人物、类型、确认状态、日期、排序、地点和关键词筛选三类事件；地点使用紧凑多选面板，筛选控件在桌面与移动端保持等高，卡片显示具体来源名称，管理员可删除事件 |
| 人物 | 新增、编辑或软删除人物，维护别名、组织和身份信息；删除不影响历史事件与证据 |
| 地图 | 在未配置地图时按公开地点展示兼容视图，不推断实时路线 |
| 搜索 | 跨事件、言论、地点和原始材料搜索 |
| 信息源 | 创建、编辑、测试或软删除来源；网站模式自动发现关联人物资讯；也可录入人工材料 |
| 地图 | 使用 Leaflet 和配置的瓦片服务展示带公开地点坐标的行程；无坐标行程保留为地点卡片 |
| 任务中心 | 手工运行任务，查看状态、计数和运行记录；管理员可预览并执行事件归属重验及中国日报正文清理 |
| 推送管理 | 配置邮件通道；维护按发生时间汇总的动态推送规则（人物/信息源/事件类型/发送时间/汇总周期），预览窗口、补跑、查看或重试批次；查阅历史投递记录 |
| 用户权限 | 为 `password.txt` 中的普通用户配置可访问页面 |
| 系统配置 | 查看合并后的生效配置，敏感字段自动脱敏 |
| 审计日志 | 查询登录、配置、来源、任务、事件删除和推送等关键操作 |

## 目录结构

```text
PublicFiguresTrackingSystem/
├── README.md
├── 软件需求规格说明书.md
├── 系统设计说明书.md
└── src/
    ├── app/backend/          # FastAPI、数据库、采集、分析、任务
    ├── app/frontend/         # Vue 3 SPA
    ├── config/app.json       # 主配置
    ├── data/                 # SQLite、password.txt、下载归档
    ├── JenkinsConfig/        # Jenkinsfile
    ├── tests/                # 后端测试和真实服务冒烟脚本
    ├── logs/                 # 运行日志与 PID（被 Git 忽略）
    ├── requirements.txt
    └── start/status/stop.*   # Windows/Linux 运维脚本
```

## 配置文件说明

主配置是 `src/config/app.json`：

| 区域 | 说明 |
|---|---|
| `server` | 监听地址、端口和外部基础 URL |
| `database` | SQLite 相对路径和忙等待时间 |
| `security` | 密码文件、会话有效期、Cookie 和登录限速 |
| `tasks` | 调度器开关、轮询周期、单次最大条目数 |
| `collector` | 集中 WebFetch 地址、API Key 环境变量、缓存/代理策略、超时和直连降级开关 |
| `ai` | 模型供应方式、兼容接口、模型名、密钥环境变量和置信度阈值 |
| `map` | 地图供应方式、瓦片 URL 和密钥环境变量 |
| `notifications.email` | SMTP、发件人/收件人、页面密码主密钥引用、邮件分片、Worker 轮询和重试策略 |
| `notifications.daily_digest` | 动态推送时区、默认发送时间（`08:30`）、默认汇总模式（昨天自然日）、滚动小时范围和调度轮询 |
| `logging` | 日志级别、保留天数和路径 |

配置优先级为：代码默认值 < `app.json` < 环境变量。环境变量格式为 `PFTS_区域__字段`，例如：

```text
PFTS_SERVER__HOST=0.0.0.0
PFTS_SERVER__PORT=28000
PFTS_AI_API_KEY=your-secret
PFTS_WEBFETCH_API_KEY=your-webfetch-api-key
PFTS_SMTP_PASSWORD=your-smtp-password
PFTS_NOTIFICATION_CREDENTIAL_KEY=your-fernet-key
```

如需外部大模型，将 `ai.provider` 改为非 `local` 值，填写 OpenAI-compatible `base_url` 与 `model`，密钥放入 `ai.api_key_env` 指向的环境变量。外部调用失败时会自动使用本地规则抽取，并记录降级原因。

## 邮件推送

邮件推送默认关闭。管理员可在“推送管理”页面配置 SMTP 通道，也可在 `app.json` 的 `notifications.email` 中配置。优先级为：代码默认值 < `app.json` < `PFTS_NOTIFICATIONS__EMAIL__字段` 环境变量 < 页面非空字段；页面可一键清除覆盖并恢复文件/环境配置。

页面保存 SMTP 密码前，必须生成 Fernet 主密钥并通过 `PFTS_NOTIFICATION_CREDENTIAL_KEY` 提供：

```bash
cd src
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

主密钥不得写入仓库或普通备份。也可不在页面保存密码，只把密码放在 `PFTS_SMTP_PASSWORD`，并通过 `password_env` 引用。

应用内置持久化邮件 Worker，即使采集调度关闭也会恢复待处理批次。失败按配置指数退避，达到上限后可在页面手工重试。SMTP 采用至少一次投递语义：若 SMTP 已接收邮件但进程在状态写回前异常退出，重试可能再次提交相同稳定 `Message-ID`。Worker 同时处理动态推送批次与历史遗留的即时推送批次（仅重试存量，不再产生新的即时推送）。

## 动态推送

管理员在“推送管理 -> 动态推送”创建规则，每条规则必须选择至少一个人物、至少一个事件类型和至少一个收件人。发送时间按规则配置，使用北京时间 `HH:mm`，默认 `08:30`；人物停用或软删除后不再进入新推送，历史运行和投递记录保留。

信息源维度为可选多选：选中信息源后，候选事件必须存在至少一条证据来自所选信息源（通过 `event_evidence -> raw_documents -> information_sources` 关联判定）；清空信息源选择表示匹配全部信息源，因此升级前已存在的规则继续按全部信息源生效。

汇总周期支持：

- **昨天自然日（默认）**：例如 2026-07-30 08:30 的推送选择北京时间 `[2026-07-29 00:00, 2026-07-30 00:00)`。
- **发送前最近 N 小时**：以计划发送时刻为窗口终点，例如 08:30 的最近 12 小时为前一天 20:30 至当天 08:30。

候选事件必须匹配规则人物、信息源和类型，且未被删除或历史驳回。窗口归属优先使用事件发生时间；发生时间未知时使用入库时间确定所属推送，但邮件仍显示“时间未知”。正文按已知事件时间升序排列，未知时间置后，并以人物、类型和事件 ID 保持稳定顺序。

规则保存后可先预览下一次或指定业务日期的实际窗口与有限样例。自动调度使用北京时间日历时刻而不是固定 86400 秒间隔；应用错过发送时刻后恢复时只补最近一期，避免连续发送大量历史邮件，更早日期可由管理员显式补跑。同一规则和业务日期通过 SQLite 唯一约束保持幂等。

没有匹配事件时默认只记录 `empty` 运行、不发送邮件；管理员可为规则启用“无动态时也发送”。动态推送复用现有 SMTP 通道、凭证加密、事务外 Worker、稳定 `Message-ID`、指数退避和失败重试。推送失败不会影响采集任务或时间线数据。

## 集中网页抓取服务

自动网页和 RSS 来源默认使用 `collector.provider=webfetch`。网页使用 `auto` 模式并通过 `generic.article` 提取正文；RSS 使用 `http` 模式获取 XML 后由 PFTS 本地解析。WebFetch 返回的请求 ID、artifact、抓取策略、缓存和重试轨迹会随原始文档保存。

API Key 不得写入 `app.json`，启动前设置：

```powershell
$env:PFTS_WEBFETCH_API_KEY='your-webfetch-api-key'
```

Linux：

```bash
export PFTS_WEBFETCH_API_KEY='your-webfetch-api-key'
```

集中服务不可用时，自动采集任务默认失败并记录原因，不会静默绕过集中缓存、限流和 SSRF 策略。`collector.direct_fallback` 只建议在隔离的开发环境临时开启；直连模式仍默认禁止私网目标。

### 网站自动发现

新增来源时选择“网站（自动发现）”，填写网站入口并至少关联一个人物。系统会使用人物姓名和别名筛选同域资讯链接，再抓取匹配文章。可配置：

- 最多扫描页面：默认 12，范围 1～50；
- 最大站内层级：默认 1，范围 0～2；
- 采集周期：最低 60 秒，生产建议按站点更新频率设置为数小时。

发现范围包含同一机构的子域名；对已适配的网站（当前含中国政府网、人民网）会优先调用站内搜索，新华社同时识别 `xinhuanet.com` 与 `news.cn`。中国日报 `chinadaily.com.cn` 及其子域会在统一抓取结果上追加正文边界清洗，移除首页/频道框架、重复标题、推荐阅读和页尾；清洗后仍呈聚合结构或正文不足的候选会被拒绝。任务日志记录扫描页、提取链接、候选文章、正文清洗、拒绝原因和最终命中数；可访问但零命中时会给出警告。未适配网站仍采用有边界的栏目链接发现，必要时可把入口设置为网站搜索结果页或另行添加 RSS。删除信息源会停用关联采集任务，但不会删除已经保存的材料、事件和证据。

## 用户配置

可登录用户维护在 `src/data/password.txt`：

```text
username:password:role
```

角色只能是 `admin` 或 `user`。修改文件后，新用户或密码会在应用启动/下次登录时同步，数据库只保存 scrypt 哈希。默认账号：

```text
admin / admin123
```

首次部署后必须修改默认密码，并限制 `password.txt` 的文件读取权限。真实生产密码不要提交到公开仓库。

## Windows 部署

要求 Python 3.9+，建议安装 Node.js 20+ 以构建前端。在 PowerShell 中执行：

```powershell
cd src
.\start.ps1
.\status.ps1
```

如果本机 PowerShell 执行策略禁止运行脚本，可使用一次性绕过方式，不必修改系统级策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

首次启动会创建 `.venv`、安装锁定的 Python 依赖、安装并构建 Vue 前端，然后在后台启动服务。停止：

```powershell
.\stop.ps1
```

已手工准备依赖和前端时，可使用：

```powershell
.\start.ps1 -SkipInstall -SkipFrontend
```

## Linux 部署

要求 Python 3.9+、`python3-venv`，构建前端时还需 Node.js/npm：

```bash
cd src
chmod +x start.sh status.sh stop.sh
./start.sh
./status.sh
```

停止服务：

```bash
./stop.sh
```

个人服务器推荐部署到 `/opt/PublicFiguresTrackingSystem`，并通过 Nginx、Nginx Proxy Manager 或 Cloudflare Tunnel 提供 HTTPS。生产环境需把 `security.cookie_secure` 设为 `true`。

## 开发方式

后端：

```powershell
cd src
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH=(Get-Location).Path
.\.venv\Scripts\python.exe -m app.backend.main
```

前端开发服务器：

```powershell
cd src\app\frontend
npm.cmd install
npm.cmd run dev
```

Vite 会把 `/api` 代理到 `127.0.0.1:28000`。生产构建由 FastAPI 同源托管。

## 测试方式

后端单元与 API 集成测试：

```powershell
cd src
.\.venv\Scripts\python.exe -m pytest -q
```

前端测试与生产构建：

```powershell
cd src\app\frontend
npm.cmd test
npm.cmd run build
```

真实服务冒烟测试：

```powershell
cd src
.\.venv\Scripts\python.exe tests\smoke_live.py
```

测试使用临时 SQLite 和临时用户文件，不会覆盖 `src/data/`。

## 运维方式

- 运行日志：`src/logs/app.log`，每天自动轮转。
- 标准输出/错误：`server.stdout.log`、`server.stderr.log`。
- 进程号：`src/logs/server.pid`。
- 存活检查：`GET /api/v1/health/live`。
- 就绪检查：`GET /api/v1/health/ready`。
- SQLite 文件：`src/data/app.sqlite3`。
- 下载归档：`src/data/downloads/YYYY/MM/DD/`。
- 动态推送调度：应用进程内 `DailyDigestScheduler`，即使采集任务调度关闭也会按规则运行。

备份至少应包含 `data/app.sqlite3`、`data/downloads/`、`config/app.json` 和受保护的 `data/password.txt`。复制正在写入的 SQLite 前应停止服务，或使用 SQLite Backup API。

SQLite 备份中的页面 SMTP 密码是密文。`PFTS_NOTIFICATION_CREDENTIAL_KEY` 必须独立安全保存；密钥丢失时只能清除页面密码并重新录入。轮换主密钥前先记录 SMTP 密码，在页面清除旧密文、替换环境密钥后再保存。备份会同时保留动态推送规则、运行、批次与明细以及历史即时推送批次；恢复后未完成批次由 Worker 继续处理。邮件失败不会回滚采集结果，可在“推送管理”查看清洗后的错误摘要。

Linux 生产环境可从 `src/JenkinsConfig/pfts.service.example` 生成 systemd unit：把 `__DEPLOY_DIR__` 和 `__RUN_USER__` 替换为实际值后安装到 `/etc/systemd/system/pfts.service`，执行 `systemctl daemon-reload && systemctl enable --now pfts`。unit 使用部署目录下的 `.venv` 和可选 `data/runtime.env`，停止服务时 FastAPI lifespan 会依次停止采集、动态推送调度器和邮件 Worker。

### 数据质量维护

管理员可在“任务中心 → 数据质量维护”使用两项操作：

- **事件归属重验**：按人物、来源或全量重新检查每条证据的动作/言论主体；仅被提及、引用、作为指导思想或身份修饰的人物不会继续保留错误事件。
- **中国日报正文清理**：识别首页/频道/专题聚合页，清洗合法文章中的页面框架，并对清洗后的文章重新分析。

两项操作都必须先点“预览影响”。真实执行只修改自动生成且 `human_locked=0` 的数据：先删除无效证据，事件仍有其他有效证据时继续保留，完全失去证据的未锁定事件才会删除。执行会写入审计日志，重复运行保持幂等。

生产执行清单：

1. 停止服务或使用 SQLite Backup API 备份 `data/app.sqlite3`，同时保留配置和密码文件。
2. 在任务中心按指定人物/来源运行 dry-run，保存计数和样例；重点核对习近平时间线及中国日报来源。
3. 确认样例后执行维护，再运行受影响来源的采集任务。
4. 抽查“吉尔吉斯斯坦总统扎帕罗夫会见王毅”“王毅会见伊朗外长阿拉格齐”和“万山磅礴看主峰｜习近平谈亚太合作”等已知案例。
5. 如结果异常，停止服务、回滚应用版本并用执行前 SQLite 备份恢复。

## Jenkins 持续集成

流水线文件位于 `src/JenkinsConfig/Jenkinsfile`，包含：

1. 从 Pipeline SCM 检出 GitHub 提交。
2. 安装依赖并运行后端测试。
3. 运行前端测试和生产构建。
4. 严格校验 `schedule-incremental-event-push` OpenSpec 变更。
5. 停止 `/opt/PublicFiguresTrackingSystem` 现有服务。
6. 使用 `rsync` 更新代码，保留 `data/`、`logs/` 和服务器已有的 `config/app.json`；首次部署才复制默认配置。
7. 启动服务并执行就绪检查、邮件通道与动态推送只读冒烟检查。

Jenkins 任务应选择 “Pipeline script from SCM”，使用 SSH 仓库地址，脚本路径设置为 `src/JenkinsConfig/Jenkinsfile`。流水线内已配置每三分钟轮询 SCM。

## 访问方式

默认仅监听本机：

- Web 页面：[http://127.0.0.1:28000/](http://127.0.0.1:28000/)
- OpenAPI：[http://127.0.0.1:28000/docs](http://127.0.0.1:28000/docs)
- 健康检查：[http://127.0.0.1:28000/api/v1/health/ready](http://127.0.0.1:28000/api/v1/health/ready)

若供局域网或反向代理访问，请通过环境变量把监听地址改为 `0.0.0.0`，并在防火墙、反向代理和 HTTPS 层限制访问。

## 数据与内容边界

- 只录入合法公开信息并保留原文链接和证据片段。
- “预计”“存疑”“有争议”不能作为已发生事实统计。
- 精确地点只有在可靠公开来源明确披露且具有公共意义时保存。
- 系统不会绕过登录、验证码、付费墙或访问控制。
- 自动摘要不是独立来源；高风险内容应人工复核。

## 许可证

见 [LICENSE](LICENSE)。
