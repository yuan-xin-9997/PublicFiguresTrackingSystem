# PublicFiguresTrackingSystem

公开人物行程动态言论跟踪系统（PFTS）把公开新闻、RSS、网页和人工材料整理成可核验的统一时间线。系统保存原始材料与证据片段，区分预计、已确认、已发生、存疑和有争议状态，并提供人工审核、任务追踪、全文搜索及地点视图。

系统定位是公开信息研究工具，不用于私人实时定位，不绕过登录、付费墙或反爬措施。

## 主要能力

- 用户登录、管理员/普通用户角色及页面权限。
- 公开人物、跨语言别名和来源关联管理。
- RSS、Atom、单篇网页、网站站内自动发现和人工材料采集。
- 自动识别网页元数据、正文或 URL 中的发布日期，并用于时间线日期回退。
- 原始文档去重、任务运行记录及逐条日志。
- 本地确定性抽取与可选 OpenAI-compatible 外部模型。
- 行程、动态、言论统一时间线及证据链详情。
- 置信度、确认状态、待审核和人工锁定机制。
- 搜索、地点兼容视图、仪表盘和审计日志。
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
| 总览 | 人物、来源、今日材料/事件、待审核和异常任务统计 |
| 时间线 | 按人物、类型、确认状态和关键词筛选三类事件 |
| 人物 | 新增、编辑或软删除人物，维护别名、组织和身份信息；删除不影响历史事件与证据 |
| 地图 | 在未配置地图时按公开地点展示兼容视图，不推断实时路线 |
| 搜索 | 跨事件、言论、地点和原始材料搜索 |
| 审核中心 | 审核低置信或需要复核的事件，保留审核历史 |
| 信息源 | 创建、编辑、测试或软删除来源；网站模式自动发现关联人物资讯；也可录入人工材料 |
| 地图 | 使用 Leaflet 和配置的瓦片服务展示带公开地点坐标的行程；无坐标行程保留为地点卡片 |
| 任务中心 | 手工运行任务，查看状态、计数和运行记录 |
| 用户权限 | 为 `password.txt` 中的普通用户配置可访问页面 |
| 系统配置 | 查看合并后的生效配置，敏感字段自动脱敏 |
| 审计日志 | 查询登录、配置、来源、任务和审核等关键操作 |

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
| `ai` | 模型供应方式、兼容接口、模型名、密钥环境变量和审核阈值 |
| `map` | 地图供应方式、瓦片 URL 和密钥环境变量 |
| `logging` | 日志级别、保留天数和路径 |

配置优先级为：代码默认值 < `app.json` < 环境变量。环境变量格式为 `PFTS_区域__字段`，例如：

```text
PFTS_SERVER__HOST=0.0.0.0
PFTS_SERVER__PORT=8000
PFTS_AI_API_KEY=your-secret
PFTS_WEBFETCH_API_KEY=your-webfetch-api-key
```

如需外部大模型，将 `ai.provider` 改为非 `local` 值，填写 OpenAI-compatible `base_url` 与 `model`，密钥放入 `ai.api_key_env` 指向的环境变量。外部调用失败时会自动使用本地规则抽取，并记录降级原因。

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

发现范围包含同一机构的子域名；对已适配的网站（当前含中国政府网、人民网）会优先调用站内搜索，新华社同时识别 `xinhuanet.com` 与 `news.cn`。任务日志记录扫描页、提取链接、候选文章和最终命中数；可访问但零命中时会给出警告。未适配网站仍采用有边界的栏目链接发现，必要时可把入口设置为网站搜索结果页或另行添加 RSS。删除信息源会停用关联采集任务，但不会删除已经保存的材料、事件和证据。

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

Vite 会把 `/api` 代理到 `127.0.0.1:8000`。生产构建由 FastAPI 同源托管。

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

备份至少应包含 `data/app.sqlite3`、`data/downloads/`、`config/app.json` 和受保护的 `data/password.txt`。复制正在写入的 SQLite 前应停止服务，或使用 SQLite Backup API。

## Jenkins 持续集成

流水线文件位于 `src/JenkinsConfig/Jenkinsfile`，包含：

1. 从 Pipeline SCM 检出 GitHub 提交。
2. 安装依赖并运行后端测试。
3. 运行前端测试和生产构建。
4. 停止 `/opt/PublicFiguresTrackingSystem` 现有服务。
5. 使用 `rsync` 更新代码，保留 `data/` 和 `logs/`。
6. 启动服务并执行就绪检查。

Jenkins 任务应选择 “Pipeline script from SCM”，使用 SSH 仓库地址，脚本路径设置为 `src/JenkinsConfig/Jenkinsfile`。流水线内已配置每三分钟轮询 SCM。

## 访问方式

默认仅监听本机：

- Web 页面：[http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- OpenAPI：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- 健康检查：[http://127.0.0.1:8000/api/v1/health/ready](http://127.0.0.1:8000/api/v1/health/ready)

若供局域网或反向代理访问，请通过环境变量把监听地址改为 `0.0.0.0`，并在防火墙、反向代理和 HTTPS 层限制访问。

## 数据与内容边界

- 只录入合法公开信息并保留原文链接和证据片段。
- “预计”“存疑”“有争议”不能作为已发生事实统计。
- 精确地点只有在可靠公开来源明确披露且具有公共意义时保存。
- 系统不会绕过登录、验证码、付费墙或访问控制。
- 自动摘要不是独立来源；高风险内容应人工复核。

## 许可证

见 [LICENSE](LICENSE)。
