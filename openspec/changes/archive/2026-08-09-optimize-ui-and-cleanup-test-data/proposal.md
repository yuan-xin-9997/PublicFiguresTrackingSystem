## Why

时间线页面存在两个体验问题：事件可信度数值对用户决策帮助有限且与证书验证状态重复，仅展示 100 条无分页导致浏览体验差。冒烟测试每次创建人物/信息源后未清理，长期累积污染数据库。推送管理页面的投递记录模块基于已废弃的旧通知系统，不再产生新记录，属于无效模块。

## What Changes

1. **时间线页面移除可信度展示**：从时间线卡片和事件详情面板中移除"可信度"数值显示，仅保留确认状态标记。
2. **时间线页面增加分页功能**：后端已支持分页参数，前端增加页码导航、每页条数选择器和页码跳转控件。
3. **冒烟测试清理**：修改 `smoke_live.py` 在测试结束后删除创建的人物和信息源，避免残留测试数据。
4. **移除投递记录模块**：从推送管理页面移除"投递记录"面板及其后端 API 端点（`/notifications/deliveries`、`/notifications/deliveries/{id}`、`/notifications/deliveries/{batch_id}/retry`），同时移除任务中心运行记录表中对旧投递批次的统计查询。

## Capabilities

### New Capabilities
- `timeline-pagination`: 时间线事件列表分页功能，包含页码导航、每页条数选择、页码跳转，后端 API 已支持分页参数，仅需前端实现。

### Modified Capabilities
- `dynamic-push`: 推送管理页面移除"投递记录"（历史）模块及相关后端接口。**BREAKING**: 删除 `/api/v1/notifications/deliveries`、`/api/v1/notifications/deliveries/{id}`、`/api/v1/notifications/deliveries/{batch_id}/retry` 三个 API 端点。

## Impact

- **前端**: `src/app/frontend/src/main.js` — 修改时间线模板（移除可信度、添加分页）、移除投递记录模板和状态管理代码、移除投递记录相关函数（`openDelivery`、`retryDelivery`、`selectedDelivery`、`deliveryTotal`、`deliveryFilters`）；`src/app/frontend/src/utils.js` — 移除 `percent()` 函数（如仅用于可信度显示）；`src/app/frontend/src/styles.css` — 添加分页样式。
- **后端**: `src/app/backend/main.py` — 移除投递记录相关端点（行 1046-1119）、移除任务运行查询中的旧投递批次统计（行 547-549）。
- **测试**: `src/tests/smoke_live.py` — 添加测试数据清理逻辑；`src/app/frontend/src/timeline-filter-layout.test.js` — 更新 UI 测试（移除可信度断言、添加分页断言）；`src/app/frontend/src/notifications-ui.test.js` — 更新 UI 测试（移除投递记录断言）；`src/tests/test_notifications.py` — 移除投递记录相关测试。
- **数据库**: 无 schema 变更（`email_delivery_batches` 和 `email_delivery_items` 表保留作为历史数据，仅移除接口和 UI）。