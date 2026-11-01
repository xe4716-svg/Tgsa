# Telegram 登录与协议号文件 — 阶段 2

## 登录方式
- QR 登录：Telethon QR login；前端将 `tg://login` URL 渲染为二维码。二维码 token 约 25 秒刷新；整个登录流程有独立过期时间。
- 手机号登录：E.164 手机号、Telegram 登录验证码。
- 2FA：若账号启用了 Telegram 两步验证，要求用户输入自己的密码。
- 登录流程绑定创建它的 tenant_id/user_id；其他用户不能查询、提交验证码或消费该流程。
- 登录成功只向内部调用方提供账号基本资料和临时 Session 字符串；调用方必须立即用 SessionVault 加密后入库。手机号仅返回掩码。

## 协议号文件
- 产品名称为“协议号文件”，底层为 MTProto StringSession。
- `.tgsession` 文件支持一个文件包含多个账号。
- 文件使用 scrypt + AES-256-GCM 加密，导入时需要用户提供导出密码。
- 不支持明文导出，也不写入普通日志。
- 当前不接受任意未知格式的 SQLite `.session` 文件；完成来源验证和兼容测试后再开放。

## Session 加密
- 每个账号 Session 使用独立随机 DEK。
- DEK 使用平台 KEK/KMS 包装。
- AAD 绑定 tenant_id 和 account_id，阻止跨租户/跨账号替换密文。
- 当前代码支持由 Secret Manager 注入 32 字节 KEK；生产部署应接入 KMS/HSM，并完成密钥轮换流程。

## 代理选择与连接
- 登录前必须明确选择 PROXY 或 DIRECT；系统不会默认替用户选择，也不会在代理故障时静默回退直连。
- PROXY 模式只允许使用已完成连接检测且处于 ACTIVE 状态的代理。
- SOCKS5 使用 Telethon 当前推荐的 python-socks asyncio 代理配置。
- MTProto 使用 `ConnectionTcpMTProxyRandomizedIntermediate`。
- HTTP CONNECT 暂不开放，等传输适配器通过端到端测试再启用。
- 用户确认连接模式及风险告知版本会写入审计记录。
- “独享 IP”只有在实际出口 IP 检测与分配记录核验后才可标记为已验证独享。

## 账号入库
账号授权成功后，必须在单一数据库事务内：
1. 再次校验用户租户成员身份。
2. 检查代理状态。
3. 创建 Telegram 账号记录。
4. 使用 SessionVault 加密并保存 Session。
5. 保存用户的连接模式确认记录。
6. 写入审计日志。
任一步失败都回滚，避免出现“账号已建但 Session 未保存”的半成品。

## 部署限制
登录协调器当前使用进程内存保存短期 QR/验证码流程对象，因此同一个登录流程必须固定路由到创建它的 account-gateway worker。正式部署使用单独 gateway 进程或粘性路由；不能把 Telethon Client 对象序列化到 Redis。
