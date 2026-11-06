# TGMate 根 API 接口面盘点与前端接入映射

- 日期：2026-11-05
- 基线：Tgsa PR #27 `work/v2-takeover`
- 状态：只读源码盘点 / 前端接入设计；不是已实现的 API 新增
- 关联：TGM-022、TGM-012、TGM-003、TGM-004、TGM-005、TGM-006、TGM-021

## 1. 总体结论

当前根 FastAPI 应用已经包含认证、Telegram 登录、账号/代理只读资产、Gateway route 开关、会员/账单/钱包/支付/续费通知、Relay 会话控制和部分业务只读目录 API。

但根 API 还没有可供前端使用的完整 Clone/Channel management、Bot broadcast command、Bot management、filter configuration、message map 或 clone run control API。现有 broadcast API 是只读任务目录，不是群发创建/执行 API。不能因为后台 worker 或数据库表存在就宣称前端功能已完成。

OpenAPI schema 由 FastAPI 自动暴露在 `/openapi.json`；前端可用 `@hey-api/openapi-ts` 生成类型和 SDK。前端不应自行维护重复 DTO。

## 2. 当前根 API 路由清单

### Health
| Method | Path | 用途 | 前端/运维 |
|---|---|---|---|
| GET | `/health/live` | 进程存活 | 仅探针 |
| GET | `/health/ready` | PostgreSQL + Redis readiness | 仅探针 |

### Auth / tenant session
| Method | Path | 用途 |
|---|---|---|
| POST | `/auth/register` | 注册 tenant + owner，发验证邮件 |
| POST | `/auth/verify-email` | 邮箱验证 |
| POST | `/auth/resend-verification` | 重发验证 |
| POST | `/auth/login` | 登录 |
| POST | `/auth/refresh` | refresh token rotation |
| POST | `/auth/logout` | 撤销 refresh token |
| GET | `/auth/me` | 当前用户/租户身份 |
| POST | `/auth/switch-tenant` | 切换 tenant 并签发新 token |
| POST | `/auth/forgot-password` | 请求重置密码 |
| POST | `/auth/reset-password` | 重置密码 |

注意：注册/登录/刷新响应为 token envelope；不能假设与其它 `success/data/request_id` 资源接口完全相同。登录/刷新、密码重置和租户切换需要单独做安全 UI 与状态管理。

### Telegram account / login / proxy / Gateway
| Method | Path | 用途 | 当前边界 |
|---|---|---|---|
| POST | `/telegram/login/qr` | 开始 QR 登录 | 临时登录会话 |
| POST | `/telegram/login/qr/refresh` | 刷新 QR | 临时登录会话 |
| POST | `/telegram/login/phone` | 开始手机号登录 | 临时登录会话 |
| POST | `/telegram/login/phone/code` | 提交验证码 | 临时登录会话 |
| POST | `/telegram/login/2fa` | 提交 2FA 密码 | 临时登录会话 |
| GET | `/telegram/login/{login_id}/status` | 查询登录状态 | 临时登录会话 |
| POST | `/telegram/login/{login_id}/cancel` | 取消登录 | 临时登录会话 |
| POST | `/telegram/login/{login_id}/consume` | 消费授权完成结果 | 受 consent/authority lock 保护 |
| GET | `/api/v1/telegram/accounts` | 分页列出账号资产 | 只读 |
| GET | `/api/v1/proxies` | 分页列出代理资产 | 只读 |
| GET | `/api/v1/telegram/accounts/{account_id}/gateway` | 读取 Gateway route | desired state |
| POST | `/api/v1/telegram/accounts/{account_id}/gateway` | 开/关 Gateway route | expected_generation CAS |

当前 Gateway route API 控制的是账号是否允许 Gateway 管理连接，不代表 Clone route。不要在 UI 中把两者混为同一开关。

### Membership / entitlement / billing
| Method | Path | 用途 |
|---|---|---|
| GET | `/billing/plans` | 公共套餐列表 |
| GET | `/billing/entitlements` | 当前 tenant 权益 |
| GET | `/admin/plans` | 平台管理员查看套餐 |
| PUT | `/admin/plans/{plan_code}/features/{feature_key}` | 修改套餐 feature |
| POST | `/admin/tenants/{tenant_id}/subscription` | 管理员分配订阅 |
| PUT | `/admin/tenants/{tenant_id}/feature-overrides/{feature_key}` | 设置 tenant 权益覆盖 |
| DELETE | `/admin/tenants/{tenant_id}/feature-overrides/{feature_key}` | 删除权益覆盖 |
| GET | `/billing/prices` | 当前价格 |
| GET | `/billing/wallet` | 钱包余额 |
| GET | `/billing/wallet/ledger` | 钱包账本 |
| GET | `/billing/orders` | 账单订单 |
| POST | `/billing/purchases/quote` | 购买报价 |
| POST | `/billing/purchases` | 购买套餐 |
| PUT | `/admin/plans/{plan_code}/prices/{currency}/{duration_days}` | 管理价格 |
| POST | `/admin/tenants/{tenant_id}/wallet/credits` | 管理员钱包入账 |

### Payment / auto-renew / notifications
| Method | Path | 用途 | 边界 |
|---|---|---|---|
| GET | `/billing/payment-methods` | 可用支付方式 | tenant UI |
| POST | `/billing/wallet/topups` | 创建充值订单 | tenant UI |
| GET | `/billing/wallet/topups` | 充值订单列表 | tenant UI |
| GET | `/billing/wallet/topups/{order_no}` | 充值详情 | tenant UI |
| GET | `/admin/billing/tron-sweeps` | 查看 TRON sweep | admin |
| POST | `/admin/billing/tron-sweeps` | 手动创建 sweep | admin |
| GET | `/admin/billing/payment-methods` | 管理支付渠道 | admin |
| PUT | `/admin/billing/payment-methods/{provider}` | 修改支付渠道 | admin |
| POST | `/webhooks/stripe` | Stripe webhook | provider only |
| POST | `/webhooks/alipay` | Alipay webhook | provider only |
| POST | `/webhooks/wechat` | WeChat webhook | provider only |
| GET/PUT | `/billing/auto-renew` | 查询/修改自动续费 | tenant UI |
| GET/PUT | `/billing/notification-preferences` | 查询/修改通知偏好 | tenant UI |
| GET/POST/DELETE | `/billing/telegram-link` | 绑定/查询/解除通知 Bot | tenant UI |
| GET | `/billing/notifications` | 通知列表 | tenant UI |
| GET | `/billing/notifications/unread-count` | 未读数 | tenant UI |
| POST | `/billing/notifications/read-all` | 全部标记已读 | tenant UI |
| POST | `/billing/notifications/{notification_id}/read` | 单条标记已读 | tenant UI |
| POST | `/webhooks/platform-telegram` | 平台通知 Bot webhook | provider/worker only |
| GET/PUT | `/admin/billing/automation-settings` | 续费自动化配置 | admin |

Webhook endpoints 不应作为浏览器可操作页面；后台只展示经过脱敏的事件状态/审计记录。

### Business reads / relay control / catalog
| Method | Path | 用途 | 边界 |
|---|---|---|---|
| GET | `/api/v1/relay/conversations` | Relay 会话目录 | 只读 |
| POST | `/api/v1/relay/conversations/{identifier}/state` | Relay 状态控制 | expected_generation + idempotency |
| GET | `/api/v1/broadcast/bot-tasks` | Bot 任务目录 | 只读，不创建/执行群发 |
| GET | `/api/v1/broadcast/account-tasks` | Account 任务目录 | 只读，不创建/执行群发 |
| GET | `/api/v1/moderation/groups` | Moderation group 目录 | 只读 |

## 3. 前端页面与现有 API 对应

| UI 页面域 | 可用 API | 需要补齐的 API/依赖 |
|---|---|---|
| 登录/注册/租户切换 | `/auth/*` | 认证 token 存储/刷新与 401/403 UX；严格按后端 contract |
| Telegram 账号连接 | `/telegram/login/*` | 登录流程状态轮询/过期/取消 UX |
| 账号/代理资产 | `/api/v1/telegram/accounts`、`/api/v1/proxies` | 账号详情、删除/解绑/代理 CRUD 需确认 TGM-004 后再定义 |
| Gateway 状态 | `/api/v1/telegram/accounts/{id}/gateway` | Gateway runtime health/last heartbeat/lease state API 需定义；route desired state 不等于真实连接状态 |
| 套餐/权益/钱包 | `/billing/*`、`/admin/*` | 当前 API 覆盖较多；前端按公开/tenant/platform admin 三类权限分区 |
| 支付/充值/续费/通知 | payment + automation endpoints | 支付渠道敏感字段只允许 admin 安全配置；webhook 不开放给浏览器 |
| Relay | relay read/control | 当前仅 Relay conversation，不等于 channel clone |
| Bot 群发 | broadcast task read-only | 创建计划、预览、fresh preflight、dispatch、cancel、per-target result API 尚需实现/审查 |
| Clone / channel routing | 暂无 | clone route CRUD、preflight、history run、live sync toggle、pause/resume、batch status、message mapping、filter profile、edit/delete policy API 均缺失 |
| Super Admin | admin plan/payment/automation | tenant search/detail/status、security events、Gateway/worker ops API 需按 TGM-012/安全审查补齐 |

## 4. 前端 SDK 与错误处理约定

1. 以 FastAPI 自动生成的 `/openapi.json` 为唯一 schema source，使用 `@hey-api/openapi-ts` 生成 TS types/SDK；生成文件不手改。
2. 每次 API schema 变更需在 PR 中附 OpenAPI diff 和 client regeneration 结果。
3. 前端统一处理 401（重新登录/刷新失败）、403（权限不足）、409（expected_generation/idempotency 冲突）、422（输入错误）、429（Retry-After）、503（依赖或 Gateway unavailable）。
4. API 有两种响应形态：auth token envelope 和资源 `success/data/request_id`；SDK 保留原始 schema，由 UI service adapter 归一化，不在生成层手工修改。
5. 所有 tenant-scoped request 从 token/current principal 确定 tenant；不信任浏览器传来的 tenant_id。平台管理员 API 必须单独显式授权。
6. Webhook、worker internal API 和 Gateway command transport 不暴露到 tenant SPA。
7. 写操作必须支持后端规定的 idempotency/CAS；前端禁用重复提交只是 UX，不替代服务端幂等。

## 5. Clone / Bot 新 API 的建议分组（待对应任务依赖解除）

建议后续按业务 contract 拆 router，不在一个巨型 router 中混杂 Telegram client：

- `/api/v1/clone/routes`：route CRUD、状态、generation、source/destination、policy profile。
- `/api/v1/clone/routes/{id}/preflight`：权限、来源/目标、受保护内容、filter、额度、速率和可用性检查。
- `/api/v1/clone/routes/{id}/runs`：历史回填/追赶 run 创建、暂停、恢复、取消和进度。
- `/api/v1/clone/routes/{id}/events`：可读状态/最近错误摘要，不暴露 Telegram raw payload。
- `/api/v1/broadcast/plans`：Bot/account 群发计划、preview、preflight、dispatch、cancel、per-target result；Bot API 与 MTProto adapter 语义分开。
- 所有写操作使用 idempotency key / expected_generation；未知外发结果必须呈现 UNKNOWN，不提供一键“重试发送”绕过对账。

这些是建议接口，不是当前已存在路径；应在 TGM-003/004/005 与 TGM-006/TGM-021 依赖审查通过后再落迁移和实现。

## 6. 验收与下一步

- 本轮完成路由静态盘点；没有调用运行中的 OpenAPI endpoint，没有执行 API 集成测试。
- 下一步：基于当前代码生成并审阅实际 `/openapi.json`；在 TGM-012 页面清单冻结后生成 TS SDK；新增 API 必须按模块单独 PR，并用 OpenAPI diff + backend tests + Playwright E2E 验收。
- 不能把当前只读 API 目录、数据库表、worker 代码或 ADR 当成已经可供 UI 调用的写 API。


## 7. API 请求追踪 PoC（2026-11-05）

PR #27 候选运行时的 API tracing 首个实现已在独立 PR [#42](https://github.com/xe4716-svg/Tgsa/pull/42) 提交，基于 OpenTelemetry API/SDK + OTLP HTTP exporter：

- W3C traceparent/tracestate 入站上下文提取。
- 仅记录 HTTP method、FastAPI route template、response status、service/environment。
- 不采集 request body、query、headers、tenant/account/peer identifiers 或 exception message。
- health probes 排除；未设置 OTLP endpoint 时默认关闭。
- exact-head Root Integration 3/3 jobs、Billing unit 1/1 job 成功；根测试 311 passed / 23 skipped。
- 当前为 API-only PoC；没有真实 Collector 部署，也没有 worker/Gateway 跨进程传播、metrics 或 log correlation。
- PR #42 已 Ready for Review，正式独立 Review 尚未提交，未合并。

这项实现不改变现有 API 路由/业务 contract；FastAPI OpenAPI runtime schema 仍需在可运行部署环境中核验。
