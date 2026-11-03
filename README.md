# TG SaaS V1.0 — 持续迭代工程

最新工程验收记录见 [实施状态](docs/IMPLEMENTATION_STATUS.md)。本轮新增可重入迁移、账号/代理只读接口、独立商业工作器及备份恢复工具；完整产品仍未验收。

API 默认不启动商业后台任务。Compose 已包含 `billing` 服务；手动启动时还需运行 `python -m app.workers.billing`。旧单进程部署可临时显式设置 `RUN_BACKGROUND_WORKERS=true`，详见 [工作器进程](docs/WORKER_PROCESSES.md)。

新库迁移入口为 `python scripts/migrate.py`，凭据通过私有 PG* 环境提供。已有未登记迁移账本的数据库不能直接自动接管，原 Docker 初始化与迁移账本需先核对，见 [迁移验证](docs/validation/migration-runtime.md)。恢复工具只恢复到新库，见 [恢复说明](docs/DATABASE_RECOVERY.md)。

当前工程包含：
- FastAPI、PostgreSQL、Redis、租户 RLS。
- Telegram 登录服务层、Session 加密、多账号协议号文件。
- 显式代理/直连策略、SOCKS5 / MTProto 配置。
- SaaS 邮箱注册、验证、登录、密码重置、Refresh Token 轮换、租户切换。
- FREE / BASIC / PRO / ENTERPRISE 会员等级和功能权益管理。
- 频道克隆、表情包克隆权限门槛；账号数量上限在入库时执行。
- 超管可单独覆盖租户功能与账号/任务额度，保留审计日志（迁移 005）。
- 钱包与会员购买：多币种余额、不可变流水、价格版本、订单快照、购买/续费/升级钱包扣款和人工入账 API（迁移 006）。
- 支付渠道适配器：Stripe、支付宝 RSA2、微信支付 API v3、易支付兼容协议、TRC20 xpub 独立地址和已确认转账 watcher（迁移 007）。
- 自动续费与通知：用户主动开启/关闭、钱包余额优先扣款、余额不足等待与到账唤醒、价格变更提前通知、最多 3 次技术重试、可配置到期提醒、租户/平台时区、站内通知、SMTP 邮件 outbox，以及用户主动绑定的 Telegram Bot 通知（迁移 008-009）。
- 支付渠道默认关闭，必须配置真实商户密钥并完成沙箱/小额验收后才能启用；TRC20 签名器生产部署、退款/拒付、Telegram Bot 生产凭据/Webhook 联调和到期任务实际暂停/恢复仍待完成。
- 本地 Mailpit 邮件测试服务。

## 本地启动
1. 安装 Docker 与 Docker Compose。
2. 将 `.env.example` 复制为 `.env`，将 `.env.runtime.example` 复制为 `.env.runtime`。前者仅供数据库初始化，后者供 API/工作器使用。
3. 执行 `python scripts/generate_dev_secrets.py`，将生成的 `JWT_SIGNING_SECRET` 和 `SESSION_KEK_BASE64` 填入 `.env.runtime`。设置 `.env` 的独立数据库密码；`.env.runtime` 中 DATABASE_URL 的运行角色密码须与 APP_DB_PASSWORD 一致，不放数据库管理员密码。
4. `docker compose up --build`。
5. API：`http://localhost:8000/docs`；健康检查：`/health/live`、`/health/ready`。
6. 本地邮件收件箱：`http://localhost:8025`。
7. 钱包/会员购买见 `docs/WALLET_AND_PURCHASES.md`；支付渠道配置见 `docs/PAYMENT_PROVIDER_SETUP.md`；会员生命周期见 `docs/SUBSCRIPTION_LIFECYCLE.md`。组合初始化和旧库限制见 `docs/COMBINED_DATABASE.md`。

## 注意
- 这是持续开发工程包，不是已经上线的完整产品。
- Compose 的 migrate 服务登记并执行根001–010与V2原始迁移，配置列级只读授权；成功后才启动 API/商业工作器。数据库与Redis不映射宿主端口，控制台端口仅绑定本机。
- 已有无迁移账本的数据库会被拒绝自动接管，须先备份并核对迁移来源，不删除数据卷来绕过。初始化不自动关联用户，关联工具见 `docs/IDENTITY_LINKING.md`。
- 生产必须使用 HTTPS、Secret Manager/KMS、SMTP TLS 和强随机密钥。
- Telegram 真实登录、数据库迁移和邮件联调仍需在 Docker/Telegram 凭据环境完成。
