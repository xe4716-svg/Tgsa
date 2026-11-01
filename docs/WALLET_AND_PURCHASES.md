# TG SaaS V1.0 — 钱包、充值与会员购买

> 当前已实现钱包购买、人工入账、支付订单编排和多个支付渠道适配器。第三方商户密钥尚未配置到部署环境，因此代码就绪不代表商户通道已经真实开通或完成生产联调。

## 1. 金额与币种

- 所有金额使用整数最小单位存储，禁止使用浮点数记账。
- CNY：amount_minor 单位为分；USD：单位为美分；USDT：单位为 0.000001 USDT。
- 一个钱包账户只对应一个币种；不同币种余额绝不相加。
- 升级抵扣必须与原会员剩余价值使用同一币种；不在平台内部猜测或自动换汇。
- 套餐价格由超管设置。仓库不预置虚构价格；未配置价格的套餐/周期/币种不可购买。

## 2. 数据模型

| 表 | 用途 |
|---|---|
| membership_plan_prices | 套餐价格、周期、币种和价格版本 |
| tenant_wallet_accounts | 每租户、每币种的钱包余额 |
| wallet_ledger_entries | 不可变余额流水 |
| billing_orders | 会员购买订单与价格快照 |
| subscription_terms | 每笔已支付会员期限，用于续费顺延和升级折算 |
| payment_provider_configs | 支付渠道展示、启停、币种和金额限制 |
| wallet_topup_orders | 在线钱包充值订单、支付链接或 TRC20 收款地址 |
| payment_webhook_events | 已验签支付事件的幂等收件箱 |
| platform_audit_logs | 全局套餐价格及支付配置审计 |

钱包流水只授予运行角色 SELECT、INSERT，不授予 UPDATE、DELETE。每笔入账/扣款使用租户范围唯一幂等键；余额与流水必须在同一个数据库事务内更新。

租户钱包、订单、期限、充值订单和流水均启用并强制 PostgreSQL RLS。支付回调通过随机商户订单号调用受限的 SECURITY DEFINER 解析函数，取得订单租户和预期金额后再设置事务级租户上下文。函数不返回用户资料或钱包余额。

## 3. 当前接口

### 用户侧

- GET /billing/prices：查看当前生效价格。
- GET /billing/wallet：查看各币种余额。
- GET /billing/wallet/ledger：查看最近钱包流水。
- GET /billing/orders：查看会员订单历史。
- POST /billing/purchases/quote：生成购买/续费/升级报价，不扣款。
- POST /billing/purchases：使用钱包余额购买、续费或升级。
- GET /billing/payment-methods：查看平台启用且凭据齐备的支付方式。
- POST /billing/wallet/topups：创建在线钱包充值订单。
- GET /billing/wallet/topups：查看充值订单历史。
- GET /billing/wallet/topups/{order_no}：查看单笔充值订单。

在线充值请求必须携带客户端生成的 idempotency_key（至少 16 个字符）。同一租户重复提交同一幂等键和相同请求，只返回原订单；同一幂等键对应不同金额、币种、渠道则拒绝。

### 支付回调

- POST /webhooks/stripe：Stripe Checkout。
- POST /webhooks/alipay：支付宝 RSA2 通知。
- POST /webhooks/wechat：微信支付 API v3 签名校验及 AES-GCM 解密。
- GET/POST /webhooks/easypay：易支付兼容协议 MD5 通知。

回调必须验签；浏览器 return_url 或客户端显示的“支付成功”不能给钱包入账。服务端核对随机商户订单号、支付渠道、币种、金额和支付状态。事件 ID 唯一约束、订单状态锁、钱包流水幂等键共同防止重复回调重复入账。金额不符或订单不存在会写入失败事件，不会给钱包加钱。

### TRC20

- USDT 充值订单使用 BIP44 公共扩展密钥为每笔订单分配唯一地址；不需要在 API 服务器保存助记词或私钥。
- 后台 watcher 使用 TronGrid 查询指定 USDT 合约的已确认 TRC20 转账；只接受收款地址、合约、币种和金额全部匹配的交易。
- watcher 使用 PostgreSQL advisory lock，确保多 API 实例只有一个扫描者；订单扫描通过数据库函数原子领取。
- 金额不符的链上转账进入 REVIEW，不自动入账，留待人工核实。
- 订单过期后仍在有限观察窗口内扫描；实际到账晚于订单有效期但金额正确时会入账并记录 paid_after_expiry。

### 超级管理员侧

- PUT /admin/plans/{plan_code}/prices/{currency}/{duration_days}：设置套餐价格版本。
- POST /admin/tenants/{tenant_id}/wallet/credits：人工核实顾客付款后，直接为租户钱包入账；不要求顾客先创建充值订单或上传付款截图。
- GET /admin/billing/payment-methods：查看支付渠道配置和凭据就绪状态。
- PUT /admin/billing/payment-methods/{provider}：启用/停用渠道并设置金额范围、排序和展示说明。
- GET /admin/billing/tron-sweeps：查看全平台 TRC20 归集任务及异常状态。
- POST /admin/billing/tron-sweeps：对指定充值地址发起人工归集；先向独立签名器询价，检查余额、TRX 资源和最高费用，再记录管理员和原因。

支付渠道凭据只从部署环境变量/Secret Manager 读取，绝不通过管理 API 返回。新渠道默认关闭；即使后台打开，凭据不完整或生产回调 URL 非 HTTPS 时仍显示不可用。

## 4. 购买事务规则

1. 对租户行加锁，串行化同一租户并发购买。
2. 检查幂等键；已存在且请求指纹一致则返回原订单。
3. 读取当前价格版本和当前有效会员状态。
4. 检查是否降级；有效期内不允许降级。
5. 同套餐续费从当前到期时间顺延；已过期则从本次购买时间开始。
6. 升级计算尚未消费的已付款期限价值，按每段剩余秒数比例折算，整数向下取整；抵扣不超过新套餐价格，不足部分从钱包扣款。
7. 升级抵扣只可在同一币种内进行；剩余抵扣不提现、不转入钱包。
8. 钱包余额不足时整个事务回滚，不生成已付款订单、不变更会员权益。
9. 余额扣减、不可变流水、订单、会员期限、当前订阅和审计日志在同一事务内提交。
10. 成功后以新的会员权益立即生效；同一请求重试不会重复扣款或重复发放期限。

## 5. 当前已实现与待完成边界

**已进入代码仓库：**
- Stripe Checkout 创建会话和 webhook 签名验证。
- 支付宝移动网页支付下单和 RSA2 通知验签。
- 微信支付 Native 下单、平台证书签名验证和 API v3 AES-GCM 解密。
- 易支付兼容协议下单和 MD5 通知验签。不同易支付服务商协议可能有差异，必须先确认对方兼容字段。
- 每订单独立 TRC20 收款地址、已确认转账扫描、交易金额核对和自动钱包入账。
- 支付回调事件幂等、租户隔离、钱包不可变流水和审计日志。
- 支付渠道启停、金额限制和凭据就绪状态。

**仍待实现：**
- 各商户真实账号/证书配置后的沙箱与生产小额联调；当前仓库没有真实商户密钥。
- TRC20 自动归集：用户已确认的每地址达到 10 USDT 才归集，低于门槛留在原地址。归集必须接入独立签名器/HSM 或受控热钱包服务，不能把助记词/私钥放进普通 API 环境。
- 充值退款、信用卡拒付、资金冻结与账务冲正。
- 自动续费设置、余额不足提醒、待续费意图与扣款调度。
- 到期任务暂停、续费后资源选择与 Worker 恢复。
- 前端钱包充值 UI、支付二维码渲染和订单状态轮询。

在真实商户凭据、回调域名、商户沙箱/生产测试和资金对账完成前，不得对外宣称支付渠道已正式上线。自动提现也尚未实现。

## 6. 部署

迁移顺序为 001 至 007。新 PostgreSQL 数据卷会由 Docker 初始化脚本按文件名顺序执行迁移。已有数据卷不会因为重新启动容器而自动执行新增迁移；部署前需备份数据库，并由数据库管理员在维护窗口依次执行尚未应用的迁移。

新增环境变量见 .env.example。生产必须设置 HTTPS 的 PUBLIC_API_BASE_URL 与 PAYMENT_RETURN_URL；支付私钥、API key 和平台证书放在 Secret Manager，不要提交到 GitHub。TRC20 需要设置 TRON_ACCOUNT_XPUB、TRONGRID_API_KEY，并在确认 watcher 可用后设置 TRON_WATCHER_ENABLED=true。TRON_ACCOUNT_XPUB 只能是 m/44'/195'/0' 或 m/44'/195'/0'/0 的扩展公钥，禁止填入 xprv、助记词或种子。

GitHub Actions 在 PostgreSQL 16 中执行 001–007 全部迁移、账务单元测试、支付签名测试、TRON xpub 派生测试，并验证租户 RLS、支付订单解析函数和钱包流水不可修改。
