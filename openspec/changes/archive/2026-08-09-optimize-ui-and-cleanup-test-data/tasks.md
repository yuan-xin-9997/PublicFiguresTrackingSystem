## 1. 前端 — 时间线页面修改

- [x] 1.1 移除时间线卡片模板中的可信度显示（`<span>可信度 {{ percent(event.confidence) }}</span>`）
- [x] 1.2 移除事件详情面板中的可信度显示（`<div><small>可信度</small><strong>{{ percent(data.selectedEvent.confidence) }}</strong></div>`）
- [x] 1.3 移除 `main.js` 中 `percent` 的 import 和 return 导出（如无其他使用）
- [x] 1.4 添加 `page` 和 `pageSize` 响应式状态（`page` ref 默认 1，`pageSize` ref 默认 20）
- [x] 1.5 修改 `loadPage()` 中时间线 API 调用，传入 `page` 和 `pageSize` 参数
- [x] 1.6 在时间线模板中添加分页控件（上一页/页码/下一页 + 每页条数选择器 + 总条数）
- [x] 1.7 在 `styles.css` 中添加分页控件样式
- [x] 1.8 修改筛选条件 watch，任何筛选变更时重置 `page = 1`

## 2. 前端 — 推送管理页面修改

- [x] 2.1 移除投递记录面板模板（`<section class="panel">` 投递记录部分）
- [x] 2.2 移除投递记录相关状态变量（`deliveryFilters`、`deliveryTotal`、`selectedDelivery`）
- [x] 2.3 移除投递记录相关函数（`openDelivery`、`retryDelivery`）
- [x] 2.4 移除 `loadPage()` 中 `notifications` 分支的投递 API 调用
- [x] 2.5 移除 `selectedDelivery` 详情面板模板
- [x] 2.6 移除 return 中投递记录相关的导出

## 3. 后端 — 投递记录 API 移除

- [x] 3.1 移除 `/api/v1/notifications/deliveries` 端点（`main.py` 行 1046-1076）
- [x] 3.2 移除 `/api/v1/notifications/deliveries/{batch_id}` 端点（`main.py` 行 1078-1096）
- [x] 3.3 移除 `/api/v1/notifications/deliveries/{batch_id}/retry` 端点（`main.py` 行 1098-1119）
- [x] 3.4 移除任务运行查询中的 `notification_batches`/`notification_items`/`notification_error` 统计（`main.py` 行 547-549）
- [x] 3.5 移除 `/api/v1/notifications/options` 中的 `tasks` 选项（如仅用于投递记录筛选）

## 4. 冒烟测试清理

- [x] 4.1 修改 `smoke_live.py`，在 try/finally 块中删除创建的人物和来源
- [x] 4.2 在 `finally` 中添加 `DELETE /api/v1/persons/{person_id}` 调用
- [x] 4.3 在 `finally` 中添加 `DELETE /api/v1/sources/{source_id}` 调用

## 5. 测试更新

- [x] 5.1 更新 `timeline-filter-layout.test.js` — 移除可信度相关断言，添加分页控件存在性断言
- [x] 5.2 更新 `notifications-ui.test.js` — 移除投递记录模块断言，添加投递记录不存在的断言
- [x] 5.3 更新或移除 `test_notifications.py` 中投递记录相关测试用例
- [x] 5.4 运行全部测试确保通过

## 6. 文档与部署

- [x] 6.1 更新 README.md（如涉及页面功能变更）
- [x] 6.2 更新需求规格说明书和设计说明书（如存在）
- [x] 6.3 检查 Jenkinsfile 是否需要更新
- [x] 6.4 提交代码并推送到 GitHub main 分支
- [x] 6.5 触发 Jenkins 构建验证部署