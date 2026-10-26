# AI 接入

本系统在现有 FastAPI `/api/v1` 上增加机器鉴权，并以官方 Python MCP SDK 2.3.0 提供 Streamable HTTP 服务。MCP 通过进程内 HTTP 调用同一组 API，复用参数校验、当前用户权限及原业务处理函数。

## 地址与网页入口

在原系统导航选择「系统配置 → AI 接入」，也可直接选择导航中的「AI 接入」。普通用户须先由管理员授予 `ai_access` 页面权限；新用户默认包含此页。用户只能创建和撤销自己的凭据，管理员也不能查看其他用户的密钥。

实际地址以 AI 接入页显示为准。部署保持原监听地址、端口和网络边界。

现有 HTTPS 访问地址为 `https://persontracking.yuan-xin.top/`，API 为 `https://persontracking.yuan-xin.top/api/v1`，MCP 为 `https://persontracking.yuan-xin.top/mcp/`。反向代理部署必须配置 `server.base_url` 或 `PFTS_SERVER__BASE_URL` 为已有 HTTPS 地址，避免代理内部 HTTP 协议影响 OAuth issuer/resource。当前通过 systemd 环境配置注入，不覆盖 app.json。

| 入口 | 路径 |
|---|---|
| 网页 | `/`，原登录与布局 |
| API | `/api/v1` |
| OpenAPI / 交互文档 | `/openapi.json`、`/docs` |
| MCP | `/mcp/`（资源标识为不带末尾斜杠的 `/mcp`） |
| OAuth 令牌 | `/oauth/token` |
| 受保护资源发现 | `/.well-known/oauth-protected-resource/mcp` |
| 授权服务器发现 | `/.well-known/oauth-authorization-server` |

公开入口仅包括静态前端、OpenAPI 文档、OAuth 发现信息、登录入口和健康检查 `/api/v1/health/live`、`/api/v1/health/ready`。`/oauth/token` 要求独立客户端凭据；`/oauth/authorize` 仅返回不支持交互流程的错误。所有业务数据均要求登录或有效机器令牌。

## 凭据与权限

创建时填写名称、1–365 天有效期，并主动勾选所需权限；默认不授予业务权限。服务端生成高熵密钥，仅保存 SHA-256 摘要。完整密钥只在成功创建的响应中返回一次，列表只返回名称、公开标识、范围、时间及状态。

创建后请先保存密钥再关闭对话框；页面支持复制，刷新列表不会清除尚未关闭的密钥。关闭、退出或卸载页面后清除内存中的密钥；不写入浏览器存储、URL、日志或下载文件。遗失时撤销旧凭据并重新创建。撤销会删除关联令牌，正在执行的业务操作不能回滚，排队任务在执行前会再次核验凭据和当前权限。

| 范围 | 业务入口 | 所属用户约束 |
|---|---|---|
| `persons:read` | GET `/persons`、`/persons/{id}` | 人物页面 |
| `persons:write` | POST `/persons`、PUT `/persons/{id}` | 当前管理员身份及人物权限 |
| `events:read` | GET `/events`、`/events/{id}`、`/documents`、`/documents/{id}` | 时间线页面 |
| `search:read` | GET `/search` | 搜索页面 |
| `sources:read` | GET `/sources` | 信息源页面 |
| `tasks:read` | GET `/tasks`、`/task-runs`、`/task-runs/{id}/logs` | 任务页面 |
| `tasks:execute` | POST `/ai/jobs`，kind=`collection` | 当前管理员身份及任务权限 |
| `documents:analyze` | POST `/ai/jobs`，kind=`reanalyze` | 当前管理员身份及时间线权限 |

API 表中路径均相对 `/api/v1`。查询异步任务 GET `/ai/jobs/{job_id}` 仅允许任务所属用户，并要求该业务的执行权限。所有机器调用还要求用户启用且具有 AI 接入页面权限。每次调用取凭据、令牌及所属用户当前权限的交集；降低权限立即生效。机器凭据不能管理用户、配置、凭据、邮件、删除或批量维护。

列表支持 `page`、`page_size`；机器调用默认第一页，最多每页 200 条（搜索最多 100 条）。原网页登录列表在不传 page 时保留完整结果。统一错误格式为 `{"error":{"code":"FORBIDDEN","message":"…"}}`。401 表示认证无效，403 表示权限不足，409 表示幂等冲突/执行中，422 表示参数错误，429 表示限流。

## 鉴权和调用

使用 OAuth `client_credentials`：HTTP Basic 的用户名是公开 `client_id`，密码是一次性 `client_secret`。请求为表单 `grant_type=client_credentials&resource=<完整目标地址>`，可选 `scope` 必须是当前有效范围的子集。省略 scope 时签发当前交集。响应含 `access_token`、`token_type=Bearer`、`expires_in`、`scope`，禁止缓存。

令牌默认最长 600 秒，且不超过凭据剩余有效期。API 和 MCP 分别绑定各自的 resource，不能混用。每次 HTTP 请求通过 Authorization 头传递令牌；不接受 URL 参数中的令牌、浏览器 Cookie 作为 MCP 鉴权、或长期密钥直接作为 Bearer。无 refresh token，机器客户端以原凭据自动续取短期令牌。

凭据由环境变量或受保护存储注入，不提交 Git。以下均为占位符：

```sh
export PFTS_BASE_URL="https://your-system.example"
export PFTS_CLIENT_ID="<页面创建的公开标识>"
export PFTS_CLIENT_SECRET="<由受保护凭据存储注入>"
python src/examples/ai_client.py
```

示例使用官方 SDK 的 `ClientCredentialsOAuthProvider` 完成发现、换取令牌及续取，固定预期 issuer 防止把客户端密钥发给其他授权服务器，令牌仅保留在进程内存。默认查询人物，无写入副作用。`MachineAuth` 类可用于相同方式自动获取 API 令牌：

```python
import os
import httpx2
from examples.ai_client import MachineAuth

base = os.environ['PFTS_BASE_URL'].rstrip('/')
async with httpx2.AsyncClient(auth=MachineAuth(base, base + '/api/v1')) as client:
    response = await client.get(base + '/api/v1/persons', params={'page': 1, 'page_size': 20})
    response.raise_for_status()
    persons = response.json()
```

创建人物请求示例：`POST /api/v1/persons`，头 `Idempotency-Key: create-person-001`，正文 `{"name":"公开人物示例","aliases":["公开别名"]}`。201 返回含 id 的人物详情。

异步采集示例：`POST /api/v1/ai/jobs`，头 `Idempotency-Key: collect-source-001`，正文 `{"kind":"collection","target_id":12}`。202 返回 `{"id":"<job-id>","status":"queued","status_url":"/api/v1/ai/jobs/<job-id>"}`。轮询 status_url 获取 queued/running/completed/failed、result、error、finished_at。重分析将 kind 改为 reanalyze，target_id 为材料 ID。

所有机器写入需要 8–100 位稳定幂等键。相同凭据、同键、同请求只执行一次；并发请求未完成时返回 409，完成后返回原响应；同键不同请求返回 409。幂等记录持久保存。进程在业务写入后、响应保存前中断时保留待核查状态，禁止盲目换键重试。PUT 人物是完整替换，机器请求必须另带 `X-Confirm-Replace: true`，未填写字段会重置。

## MCP 工具与客户端

| 工具 | 说明 | 属性 |
|---|---|---|
| `list_persons` / `get_person` | 人物列表及详情 | 只读、幂等 |
| `list_events` / `get_event` | 事件列表及证据详情 | 只读、幂等 |
| `search_public_information` | 分页全文搜索 | 只读、幂等 |
| `list_sources` / `list_tasks` | 来源和采集任务 | 只读、幂等 |
| `create_person` | 创建人物，需要 idempotency_key | 写入、非破坏性、幂等 |
| `update_person` | 替换资料，需要 confirm_replace=true 和幂等键 | 修改、破坏性、幂等 |
| `submit_task` | 提交采集或重分析任务 | 写入、幂等 |
| `get_task_result` | 本人任务状态、结果和失败原因 | 只读、幂等 |

已用真实官方 Python MCP SDK 2.3.0 ClientSession、Streamable HTTP 客户端及其 OAuth 客户端凭据 provider 完成协议初始化、发现、查询和安全写入测试。支持 MCP 2026-07-28 协议，由 SDK 处理协商。其他支持 OAuth client_credentials、Basic 客户端认证、RFC9728/RFC8414 发现和 resource 参数的远程客户端在协议上适配，未逐一实测。

仅支持交互授权码的客户端不在本版支持范围；ChatGPT 交互连接器、Claude Desktop 和 Codex 的直接添加服务流程未验证，不应把它们视为已支持。API Key 输入框不等于可用的 OAuth 客户端凭据支持。也可由自有程序运行附带的官方 SDK 客户端。

## 配置、审计与限流

`ai_access.token_seconds` 默认 600（实际签发限制 30–3600 秒），`requests_per_minute` 默认 120，`max_body_bytes` 默认 524288。可在 app.json 或 `PFTS_AI_ACCESS__…` 环境变量设置。令牌端点和管理请求按来源地址限流，业务请求按凭据限流；MCP 工具进入 API 时也计数。单实例 SQLite 部署，多个进程不应同时运行任务恢复器。

服务端依赖数据库内高熵摘要校验，不需要额外签名私钥。所有 AI 写请求、凭据创建/撤销、异步任务执行和拒绝请求复用 audit_logs，记录用户、凭据公开标识、动作、目标、状态和 UTC 时间。UI 同时显示北京时间。审计不记录请求正文、密钥或令牌。网页管理要求会话、同源 Origin 和自定义请求标识，不要求用户手工处理 Cookie/CSRF。

## 部署、迁移与回滚

迁移 v12 仅新增 ai_credentials、ai_tokens、ai_requests、ai_jobs、ai_rate_limits，保留原有业务数据和权限。旧用户不自动提升页面权限；管理员可在用户权限页授予 AI 接入。

Jenkins 先执行完整测试、前端构建及发布环境依赖安装，再备份代码和配置。停服时 systemd 等待正在运行的采集/AI 工作结束，然后使用 SQLite backup API 保存包含 WAL 的一致数据库备份。备份路径为 `data/backups/YYYY/MM/DD/build-<编号>/`。增量更新始终排除 data、logs 和已有 config/app.json。已有 systemd 服务使用 systemctl 停启，避免脚本启动第二个实例；服务停止超时设为 1800 秒，超过后需核查任务结果。

回滚：停止 pfts.service；将对应备份 code.tar.gz 解压到部署目录，按备份 requirements.txt 安装依赖；保留当前 config/app.json；重新启动并检查登录、健康、人物和时间线。v12 为加法迁移，旧代码可保留新增表。只有明确接受丢失备份后新增数据时才恢复备份 app.sqlite3（停服，先另存当前数据库及 WAL，再恢复）。不要把旧 app.json 覆盖当前运维配置。若要移除 AI 表，先撤销全部凭据并归档审计，在停服期间按 ai_tokens、ai_jobs、ai_requests、ai_rate_limits、ai_credentials 顺序 DROP TABLE，再删除 schema_version=12；不需要修改任何业务表。

## 验证边界

自动化测试覆盖认证失败矩阵、所有者隔离、普通用户伪造越权、实时权限变化、CSRF、并发幂等、限流、任务和中断恢复、真实官方 SDK 查询/写入。真实浏览器创建、复制、撤销及移动端外观验证因本轮电脑操作工具无法可靠识别 Windows 浏览器 URL而受阻；这些项目必须在浏览器恢复后补验，不能视作已通过。部署状态及实际地址复核结果另见交付记录。
