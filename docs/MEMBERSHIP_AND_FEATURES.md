# 会员等级与功能权限 — 阶段 4

## 默认等级矩阵
| 等级 | 搜索/导入 | 频道克隆 | 表情包克隆 | Telegram 账号上限 | 同时克隆任务 |
|---|---|---:|---:|---:|---:|
| FREE | 群组、Bot、链接导入、资产检查 | 否 | 否 | 1 | 0 |
| BASIC | 增加全局搜索 | 是 | 否 | 2 | 2 |
| PRO | 全部搜索与同步能力 | 是 | 是 | 10 | 10 |
| ENTERPRISE | 全部 | 是 | 是 | 不限 | 不限 |

此矩阵是可调整的初始默认配置，不含最终价格。平台管理员可通过后台 API 按等级开关功能和修改数量限制，不需要改代码。

## 功能权限
- `channel_clone`：频道克隆权限。
- `sticker_clone`：表情包克隆权限。
- `realtime_sync`：实时同步权限。
- `global_search` / `group_search` / `bot_search`：不同搜索能力。
- `max_telegram_accounts`：账号数量上限。
- `max_active_clone_tasks`：同时运行的克隆任务上限。

任何克隆任务创建 API 必须使用 `require_feature("channel_clone")` 或 `require_feature("sticker_clone")`，不能只在前端隐藏按钮。账号入库时会在数据库事务中检查账号数量上限。

## 管理 API
- `GET /billing/plans`：公开查看等级和权益。
- `GET /billing/entitlements`：查看当前租户实际权益。
- `GET /admin/plans`：平台管理员查看所有等级。
- `PUT /admin/plans/{plan_code}/features/{feature_key}`：调整等级权益。
- `POST /admin/tenants/{tenant_id}/subscription`：为租户分配等级和有效期。

## 平台管理员
新注册账号默认不是平台管理员。首次创建管理员后，由数据库管理员执行：
`UPDATE users SET platform_admin = TRUE WHERE lower(email) = lower('管理员邮箱');`

不要把平台管理员权限授予普通租户 OWNER。支付接口将在后续商业化阶段接入；当前订阅变更由平台管理员手动执行。

## 租户单独权益调整（阶段 4 补充）

无需修改整个套餐，可由**平台超级管理员**为指定租户单独控制功能开关、增减额度：
- `PUT /admin/tenants/{tenant_id}/feature-overrides/{feature_key}`：写入 `enabled_override`、`limit_delta`（仅 `max_*`）和必填操作原因。
- `DELETE /admin/tenants/{tenant_id}/feature-overrides/{feature_key}`：删除该租户例外，恢复套餐默认权益。
- `GET /billing/entitlements` 返回合并后的实际权益；所有变更写入审计日志。
- 超额资源保留，不强制删除；需在后续任务执行层对超额运行任务实施暂停。
- 已有数据库部署新服务前必须先执行 `005_entitlement_overrides.sql`；本仓库的 Docker 初始化迁移仅针对新数据库卷。

会员续费、升级折算、自动续费、到期暂停和任务恢复的业务规则详见 [SUBSCRIPTION_LIFECYCLE.md](SUBSCRIPTION_LIFECYCLE.md)。目前**只是规格定稿**，支付和任务恢复链路尚未完成。
