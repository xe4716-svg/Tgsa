# TGMate MVP API 契约

版本：/api/v1。本文是前后端与十个 AI 工作流共享的接口基线；实现若与本文冲突，先提契约变更 PR，不得自行发明字段。

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

## 账户与租户
- POST /auth/sessions
- DELETE /auth/sessions/current
- GET /me
- GET /me/entitlements

## Telegram 账号与代理
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

## 频道克隆
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

## 表情包
- POST /sticker-clone-jobs
- GET /sticker-clone-jobs
- GET /sticker-clone-jobs/{id}
- POST /sticker-clone-jobs/{id}/start
- POST /sticker-clone-jobs/{id}/cancel

仅处理用户拥有或明确获准复制的贴纸包；不绕过受保护内容或 Telegram 限制。

## 群发中心：机器人群发与 TG 账号群发（两个独立入口）

前端必须展示两个明确入口：「机器人群发」与「TG 账号群发」。两者分别选择发送身份、目标、内容、计划和任务列表；不可混为同一发送身份，也不得在失败时自动切换发送方式。

### 机器人群发（Bot API）
- POST /broadcast/bot-tasks
- GET /broadcast/bot-tasks
- GET /broadcast/bot-tasks/{id}
- POST /broadcast/bot-tasks/{id}/actions (pause|resume|cancel)
- GET /broadcast/bot-tasks/{id}/deliveries

创建请求必须提供 bot_id、target_chat_ids 和 content；schedule_at 可选。服务端确认该 Bot 属于当前租户、已连接目标群/频道且拥有对应发送权限。Bot 不能主动向从未启动/未授权交互的用户任意发起私聊；私聊对象必须具备有效 opt-in/对话资格。

### TG 账号群发（MTProto）
- POST /broadcast/account-tasks
- GET /broadcast/account-tasks
- GET /broadcast/account-tasks/{id}
- POST /broadcast/account-tasks/{id}/actions (pause|resume|cancel)
- GET /broadcast/account-tasks/{id}/deliveries

创建请求必须提供 account_id、target_chat_ids 和 content；schedule_at 可选。服务端确认账号属于当前租户、仍有效且具备目标会话访问/发布权限。仅允许对用户有权管理且已授权的群组/频道执行；私聊仅限明确同意接收的对象。

### 两种群发的共同约束与隔离规则
- 内部任务模型必须显式记录 sender_type（bot 或 user_account）和 sender_id；创建端点固定 sender_type，禁止客户端伪造或切换。
- 两种发送使用独立的发送适配器、队列/并发预算、速率策略、任务列表和统计；可共用任务状态/投递记录基础结构，但发送身份、权限、凭据和限额必须隔离。
- 套餐权益和超级管理员额度分别配置 bot_broadcast 与 account_broadcast 的启用开关、并发任务数和资源上限；不能用一种类型的额度抵扣另一种类型。
- 幂等键至少包含租户、任务、sender_type、sender_id、目标会话和消息序号；不确定投递状态标记 uncertain，不盲目重发。
- 不允许通过账号/IP 轮换、代理切换、拆分任务或更换发送身份规避 Telegram 限速/反垃圾限制。遇到 Telegram FloodWait/429 必须按返回时间暂停/退避，必要时暂停任务并通知用户。
- 不支持抓取陌生用户或未经同意的私聊名单，不支持无授权批量私信。用户必须对目标有管理权限或有效接收同意。
- 发送身份故障时任务暂停并显示原因；不允许从 Bot 自动回退到个人账号，或反向回退。用户需显式创建新任务并再次通过权限检查。
- 发送前展示身份、目标数量、内容预览、权限检查、计划和限额；目标不符合权限时拒绝启动并列出原因。

## 会员与支付
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

## 教程与管理员
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
- 群发任务 sender_type 只能是 bot 或 user_account，创建后不可修改；sender_id 必须指向同租户下已授权的对应资产。
- 货币和消息内容都必须在服务端校验；所有群发目标必须经过权限/同意检查。
- Telegram 发送超时且无法确认结果时标记 uncertain，不盲目重发。
