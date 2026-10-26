# AI 接入交付验证记录

日期：2026-10-26，北京时间。

## 已实现并部署

业务 API、独立机器凭据、OAuth client_credentials 短期令牌、官方 Python MCP SDK 2.3.0 Streamable HTTP、网页 AI 接入组件、持久化幂等、异步任务、实时权限约束、审计和限流均已部署。实现提交为 `2d2c63e`；Jenkins 第 53 次构建 SUCCESS（包含前两次实现提交）。本记录的后续提交仅补充交付资料，按仓库要求也会触发部署构建。

| 入口 | 地址 |
|---|---|
| 网页 | https://persontracking.yuan-xin.top/ ，登录后「系统配置 → AI 接入」或导航「AI 接入」 |
| API | https://persontracking.yuan-xin.top/api/v1 |
| OpenAPI | https://persontracking.yuan-xin.top/openapi.json |
| 交互文档 | https://persontracking.yuan-xin.top/docs |
| MCP | https://persontracking.yuan-xin.top/mcp/ |
| 令牌 | https://persontracking.yuan-xin.top/oauth/token |

权限、11 个工具、完整客户端示例、凭据管理、配置、迁移和回滚见 [AI 接入指南](AI接入指南.md)。服务地址通过 systemd 环境注入为上述现有 HTTPS 域名，未增加监听端口或公网映射。

## 已完成验证

| 项目 | 结果及方式 |
|---|---|
| 后端回归 | 84 项 pytest 通过；Windows 独立环境和 Jenkins Linux 环境均执行 |
| 前端回归与构建 | 29 项 Vitest 通过，Vite 生产构建通过；含一次性密钥在列表刷新期间保留、确认关闭清除、复制函数、撤销确认、失败提示、权限变化单元测试 |
| OpenSpec | 7 项 strict 校验通过 |
| 机器认证 | 有效凭据成功；匿名、空值、错误、撤销与越权经实际 HTTPS 地址验证；凭据过期、令牌过期、用户降权、停用由隔离数据库/API 测试验证 |
| 用户隔离 | 普通用户创建管理员范围、伪造 user_id、撤销其他用户凭据均被服务器拒绝；列表仅本人元数据 |
| MCP 协议 | 实际 HTTPS 地址，官方 ClientSession 完成 2025-11-25 initialize、tools/list、人物查询与写入；另用 server/discover 完成当前 2026-07-28 查询及写入重放 |
| OAuth 互通 | 官方 ClientCredentialsOAuthProvider 完成真实资源发现、授权发现、Basic 客户端认证和资源绑定令牌换取；错误资源令牌不能混用 |
| 异步任务 | 实际 HTTPS 地址对停用的人工测试来源提交无外部抓取的任务，202 返回 ID，轮询得到 completed、成功结果；相同幂等键返回原 ID |
| 并发幂等 | 6 个并发创建请求仅产生一个人物；处理中返回 409，完成后重放；同键不同正文被拒绝 |
| 重启恢复 | 创建凭据和任务后触发 Jenkins 53，经过真实 systemd 停启，原令牌、任务结果及幂等记录保留；重复提交未新增任务。执行中进程中断标失败的分支由工作器恢复测试验证 |
| 撤销 | 实际撤销后，既有令牌请求和重新换取令牌均立即返回 401；所有本轮测试凭据均已撤销 |
| 密钥保护 | 创建响应 no-store；列表不含明文或摘要。Jenkins 在服务器本地扫描本轮生成密钥/令牌，未出现在服务器日志文件和前端构建产物；前端源代码不使用 localStorage/sessionStorage/URL 保存密钥 |
| 原业务兼容 | 实际地址登录、总览、时间线和旧冒烟测试通过。与首次部署前备份按 ID 对比，原 36 个人物、1,637 条事件、2,352 份材料均无缺失；config/app.json SHA-256 完全相同 |
| 部署 | 首先准备依赖、测试和构建，再 systemd 停服、SQLite 一致备份、更新、启动及业务冒烟；解决旧流水线与 systemd 重复启动导致端口冲突的问题 |

备份目录包括 `/opt/PublicFiguresTrackingSystem/data/backups/2026/10/26/build-50/`（首次更新前）和 `build-53/`，含 code.tar.gz、app.json、app.sqlite3。测试新增业务记录按既有软删除规则停用或软删除，审计、任务记录保留。原有一条 2026-08-01 起始的 running 任务记录属于部署前历史状态，本次未擅自改写。

## 未完成及限制

真实浏览器的创建、复制、列表、撤销、重新打开页面、浏览器存储和移动端视觉检查尚未完成。电脑操作工具多次连接超时，随后报告无法可靠识别 Windows 当前浏览器 URL，停止本轮电脑操作。接口与前端单元测试不能替代该项验收。

已实际验证的客户端为官方 Python SDK 2.3.0。其他支持 OAuth client_credentials、Basic、resource 参数与 MCP Streamable HTTP 的客户端仅属协议适配；未逐一实测。未实现交互授权码流程，不能宣称 ChatGPT/Claude Desktop/Codex 直接添加此远程服务已经兼容。

本记录证明部署和指定时点的业务检查通过，不代表已做长期稳定性、容量或多进程压力验证。异步工作器按现有单实例部署设计；强制中断正在执行的业务可能留下部分结果，恢复时会明确标为失败并要求核查，不能盲目换幂等键重试。
