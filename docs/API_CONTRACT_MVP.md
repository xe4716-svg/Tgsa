# TGMate MVP API 契约

版本：/api/v1。本文是前后端与五个 AI 工作流共享的接口基线；实现若与本文冲突，先提契约变更 PR，不得自行发明字段。

## 通用约束
- JSON UTF-8；时间使用 ISO-8601 UTC；用户时区作为明确字段保存，不从设备语言猜测。
- 顾客接口使用 Bearer access token；管理员使用独立认证域和独立权限；Webhook 使用各渠道独立签名验证。
- tenant_id、user_id、角色和价格由服务端认证/业务上下文确定；不信任客户端传入的归属字段或金额。
- 写请求支持 Idempotency-Key；相同 key + 相同请求返回同一业务结果，相同 key + 不同请求返回 409。
- 分页采用 cursor + limit；默认 limit 20，最大 100。
- 成功响应：{ "success": true, "data": {}, "request_id": "..." }。
- 错误响应：{ "success": false, "error": { "code": "VALIDATION_ERROR", "message": "可安全展示的说明", "retryable": false }, "request_id": "..." }。
- 不向客户端返回 Token、Session 明文、API Hash、支付密钥、内部 SQL 或其他租户数据。
- 状态字段只读；状态变更必须调用动作接口，由服务端状态机检查。
- 所有顾客私有资源都必须按认证上下文做租户过滤，并由数据库 RLS/复合外键再次约束。

## MVP 接口目录

### 账户与租户
- POST /auth/sessions
- DELETE /auth/sessions/current
- GET /me
- GET /me/entitlements

### Telegram 账号与代理
- POST /telegram/accounts/authorization-sessions
- POST /telegram/accounts/authorization-sessions/{id}/complete
- POST /telegram/accounts/session-imports/validate
- POST /telegram/accounts/session-imports
- GET /telegram/accounts
- GET /telegram/accounts/{id}
- POST /telegram/accounts/{id}/connection-tests
- POST /proxies
- GET /proxies
- POST /proxies/{id}/test
- POST /telegram/accounts/{id}/proxy-binding

授权验证码、2FA 密码和 Session 内容只能经 TLS 传输；Session 只允许服务端加密保存，导入文件临时处理后删除，不进入日志。

### 频道克隆
- POST /clone-routes
- GET /clone-routes
- GET /clone-routes/{id}
- POST /clone-routes/{id}/start
- POST /clone-routes/{id}/pause
- POST /clone-routes/{id}/resume
- POST /clone-routes/{id}/cancel
- GET /clone-routes/{id}/progress
- GET /clone-routes/{id}/deliveries

每条路由固定一个 source 和一个 destination；路由进度、消息映射和幂等键独立。执行前校验源访问权、目标发布权和用户授权。

### 表情包
- POST /sticker-clone-jobs
- GET /sticker-clone-jobs
- GET /sticker-clone-jobs/{id}
- POST /sticker-clone-jobs/{id}/start
- POST /sticker-clone-jobs/{id}/cancel

仅处理用户拥有或明确获准复制的贴纸包；不绕过受保护内容或 Telegram 限制。

### Bot 与任务
- POST /bots
- GET /bots
- GET /bots/{id}
- POST /bots/{id}/verify
- POST /bots/{id}/actions (pause|resume|disable)
- POST /broadcast-tasks
- GET /broadcast-tasks
- GET /broadcast-tasks/{id}
- POST /broadcast-tasks/{id}/actions (pause|resume|cancel)
- GET /broadcast-tasks/{id}/deliveries

### 会员与支付
- GET /plans
- GET /subscriptions/current
- GET /payment-methods
- POST /payment-orders
- GET /payment-orders/{id}
- POST /payment-orders/{id}/manual-payment-submissions
- POST /payment-orders/{id}/txid-submissions
- GET /wallets
- GET /wallets/{id}/ledger

订单金额由服务端根据当前价格版本计算。人工支付入口始终展示；其他支付方式只有完成配置验证且启用后展示。USDT 必须核验网络、资产合约、收款地址、真实转账事件和确认数。

### 教程与管理员
- GET /tutorials
- GET /tutorial-bindings
- GET /admin/dashboard
- GET /admin/customers
- GET /admin/plans
- GET /admin/payment-methods
- GET /admin/audit-events
- GET /admin/security/ip-allowlist

管理员 API 必须独立认证、MFA 和 RBAC；IP 白名单启用时同时保护管理页面和 API。

## 核心数据约定
- ID：内部 UUID；顾客展示 ID 为 6～8 位纯数字，不作认证凭证。
- 货币：法币金额用 minor units + ISO currency；链上资产用 atomic units + asset/decimals；禁止浮点数。
- 任务状态与单条投递状态分离；支付状态与业务履行状态分离。
- Telegram 发送超时且无法确认结果时标记 uncertain，不盲目重发。
