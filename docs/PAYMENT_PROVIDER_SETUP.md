# TGMate 支付渠道部署配置

支付适配器已经进入代码仓库。真实支付仍需要平台自己的商户账号、密钥、证书、公开 HTTPS 回调域名和商户侧开通权限。**不要把真实密钥提交到 GitHub。** 使用部署平台 Secret Manager 注入环境变量。

## 1. 开通顺序

1. 先在支付服务商后台创建商户/应用，开通对应产品能力。
2. 配置公开 HTTPS 域名，确保公网可以访问本服务回调地址。
3. 将密钥、私钥和证书写入部署 Secret Manager。
4. 部署并重启 API。
5. 检查 GET /admin/billing/payment-methods 的 credentials_ready。
6. 先在服务商沙箱或小额测试环境创建支付，确认异步回调成功且钱包流水只入账一次。
7. 最后通过超管接口启用渠道；渠道未启用时用户端不会显示为可用。

回调必须由支付服务商直接调用。浏览器返回页、前端截图或用户提交的付款凭证不能触发自动入账。

## 2. 渠道环境变量

### Stripe 信用卡

- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET

Webhook URL：HTTPS://你的API域名/webhooks/stripe

创建 Stripe Checkout Session；仅在收到签名有效且 payment_status=paid 的完成事件后入账。当前支持配置为 CNY/USD，最终能否收款取决于 Stripe 商户国家/地区、账户能力和币种可用性。

官方资料：
- https://docs.stripe.com/api/checkout/sessions/create
- https://docs.stripe.com/webhooks/signature

### 支付宝

- ALIPAY_APP_ID
- ALIPAY_PRIVATE_KEY_PEM
- ALIPAY_PUBLIC_KEY_PEM
- 可选：ALIPAY_GATEWAY_URL，默认正式网关

通知 URL：HTTPS://你的API域名/webhooks/alipay

当前使用移动网页支付 alipay.trade.wap.pay 和 RSA2 通知验签。商户应用需开通相应产品权限；如果使用支付宝证书模式而不是公钥模式，需要后续扩展证书链验证配置。

官方资料：
- https://developer.alibaba.com/
- https://developer.alibaba.com/docs/api.htm?apiId=1056&docType=4

### 微信支付 API v3

- WECHAT_MCHID
- WECHAT_APPID
- WECHAT_SERIAL_NO（商户 API 证书序列号）
- WECHAT_PRIVATE_KEY_PEM
- WECHAT_API_V3_KEY（32 字节）
- WECHAT_PLATFORM_CERT_PEM（用于验签的微信支付平台证书）
- 可选：WECHAT_API_BASE_URL，默认 https://api.mch.weixin.qq.com

通知 URL：HTTPS://你的API域名/webhooks/wechat

当前使用 Native 下单，返回 code_url 供前端生成二维码；回调先用平台证书验 RSA 签名，再用 API v3 Key 解密 AES-GCM 资源，并核对商户号、AppID、金额和币种。平台证书轮换后必须及时更新 Secret Manager 中的证书；当前不是自动证书下载/轮换服务。

官方资料：
- https://pay.wechatpay.cn/doc/v3/merchant/4012525171
- https://pay.wechatpay.cn/doc/v3/merchant/4012062522

### 易支付兼容接口

- EASYPAY_GATEWAY_URL（应填写服务商的下单入口，例如其 submit.php）
- EASYPAY_PID
- EASYPAY_KEY

通知 URL：HTTPS://你的API域名/webhooks/easypay

当前实现常见 Epay 兼容协议的 MD5 签名，支持 alipay 和 wxpay 渠道。易支付不是单一官方标准，不同服务商可能改动参数、签名拼接、返回字段或支付状态值；接入前必须取得服务商接口文档并在沙箱验证。不要仅凭“支持易支付”宣传就直接启用。

### USDT TRC20

- TRON_API_BASE_URL，默认 https://api.trongrid.io
- TRONGRID_API_KEY
- TRON_USDT_CONTRACT，主网 USDT 默认合约地址
- TRON_ACCOUNT_XPUB
- TRON_WATCHER_ENABLED=true
- 可选：TRON_WATCH_INTERVAL_SECONDS、TRON_WATCH_BATCH_SIZE

TRON_ACCOUNT_XPUB 只能是 TRON BIP44 账户层 m/44'/195'/0' 或外部链层 m/44'/195'/0'/0 的扩展**公钥**。系统为每笔充值订单派生一个不同地址，不需要服务器接触私钥。禁止填写助记词、seed、xprv 或私钥。

Watcher 通过 TronGrid 的 GET /v1/accounts/{address}/transactions/trc20 查询 only_confirmed=true 的转账，并核对 USDT 合约、收款地址、6 位精度和订单金额。精确匹配才自动入账；金额不符转人工 REVIEW。过期订单在 7 天观察窗口内仍会扫描，以处理链上确认/回调延迟。

**自动归集使用独立签名器，不在 API 中保存私钥。** 配置以下变量后，达到每地址 10 USDT（TRON_SWEEP_THRESHOLD_MINOR 默认 10000000）才会进入自动归集队列；低于门槛保留在原地址：
- TRON_SWEEP_ENABLED=true
- TRON_SWEEP_SIGNER_URL
- TRON_SWEEP_SIGNER_TOKEN
- TRON_TREASURY_ADDRESS
- TRON_SWEEP_MAX_FEE_SUN（必须大于 0，作为单笔最高费用）

签名器需要实现两个受 Bearer Token 保护的 HTTPS 接口：
- POST /v1/tron/usdt/quote：接收 order_id、derivation_index、from_address、to_address、token_contract；返回 available_amount_minor、estimated_fee_sun、available_trx_sun 和明确的 can_sweep=true/false。
- POST /v1/tron/usdt/sweep：接收 job_id、order_id、derivation_index、from_address、to_address、token_contract、amount_minor、max_fee_sun；必须先独立核对派生地址、实际余额和资源费用，只有费用不超过 max_fee_sun 才能签名广播；使用 Idempotency-Key 防止重复广播；返回 txid、amount_minor、fee_sun、status（SUBMITTED/ALREADY_SUBMITTED）和可选 reference。

签名器必须把私钥保存在独立 HSM/KMS 或受控钱包服务中，拒绝任意地址、合约、金额和费用参数；绝不能把私钥返回给 API。API 会继续查询 TRON SolidityNode 的 solidified receipt，只有 receipt.result=SUCCESS 才把归集标记为 CONFIRMED。签名器未配置时队列保持待处理，不会假装归集成功。

低于门槛的地址不会自动归集。超管可通过 POST /admin/billing/tron-sweeps 提交人工归集，系统先向签名器询价，检查可用余额、TRX 资源和费用上限，再记录管理员、原因和审计日志。

官方资料：
- https://developers.tron.network/docs/get-trc20-transaction-history
- https://developers.tron.network/reference/get-trc20-transaction-info-by-account-address

## 3. 平台启停与金额限制

- 支付渠道配置保存在 payment_provider_configs。
- 第三方渠道迁移后默认关闭；人工付款默认显示。
- 超管通过 PUT /admin/billing/payment-methods/{provider} 设置 enabled、最小/最大金额、排序和用户展示说明。
- 后台启用不代表凭据就绪。用户端 available 还会检查必要环境变量；生产环境回调与支付返回 URL 必须使用 HTTPS。
- CNY/USD 使用分/美分；USDT 使用 0.000001 USDT。金额限制也使用相同最小单位。

## 4. 手动付款

手动付款不要求顾客先创建充值订单，也不要求上传付款截图。顾客联系平台并说明租户、金额和币种；管理员在核实实际到账后，调用现有钱包人工入账接口，填写原因和幂等键。系统记录操作者、金额、币种和余额变动。

## 5. 生产验收清单

- [ ] 商户账号已开通相应产品能力。
- [ ] Secret Manager 已注入全部必需密钥，GitHub 仓库没有真实密钥。
- [ ] 公网 HTTPS 回调可访问，支付服务商后台配置的 URL 与本服务一致。
- [ ] 支付通知签名验证通过；无效签名不会写入钱包。
- [ ] 正常支付只产生一条 PAYMENT_CREDIT 流水。
- [ ] 同一事件重复推送不会重复入账。
- [ ] 金额、币种、商户号或订单号不符时不会入账。
- [ ] 跨租户读取和写入被 RLS 拒绝。
- [ ] USDT 地址派生使用 xpub，运行环境没有助记词或私钥。
- [ ] USDT 未确认交易不会入账；金额异常进入 REVIEW。
- [ ] 签名器 quote 会检查实际 USDT 余额、TRX 资源和费用上限；sweep 使用幂等键，API 只接受 solidified receipt 成功。
- [ ] 完成服务商沙箱/小额真实测试、退款/拒付流程演练和财务对账后，才允许正式对外启用。
