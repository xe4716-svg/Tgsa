# TG SaaS V1.0 — 持续迭代工程

当前工程包含：
- FastAPI、PostgreSQL、Redis、租户 RLS。
- Telegram 登录服务层、Session 加密、多账号协议号文件。
- 显式代理/直连策略、SOCKS5 / MTProto 配置。
- SaaS 邮箱注册、验证、登录、密码重置、Refresh Token 轮换、租户切换。
- FREE / BASIC / PRO / ENTERPRISE 会员等级和功能权益管理。
- 频道克隆、表情包克隆权限门槛；账号数量上限在入库时执行。
- 超管可单独覆盖租户功能与账号/任务额度，保留审计日志（迁移 005）。
- 钱包与会员购买基础：多币种余额、不可变流水、价格版本、订单快照、购买/续费/升级钱包扣款和人工入账 API（迁移 006）。
- 第三方支付回调、自动续费、退款及到期任务恢复仍未实现；套餐价格需超管配置后才能购买。
- 本地 Mailpit 邮件测试服务。

## 本地启动
1. 安装 Docker 与 Docker Compose。
2. `cp .env.example .env`。
3. 执行 `python scripts/generate_dev_secrets.py`，将生成的 `JWT_SIGNING_SECRET` 和 `SESSION_KEK_BASE64` 填入 `.env`；同时设置强随机数据库密码。
4. `docker compose up --build`。
5. API：`http://localhost:8000/docs`；健康检查：`/health/live`、`/health/ready`。
6. 本地邮件收件箱：`http://localhost:8025`。
7. 钱包/会员购买接口见 `docs/WALLET_AND_PURCHASES.md`；现有数据库需先备份并手动执行新迁移。

## 注意
- 这是持续开发工程包，不是已经上线的完整产品。
- 新迁移只会在 PostgreSQL 数据卷首次初始化时自动执行；已有数据库必须使用受控 migration runner 执行。
- 生产必须使用 HTTPS、Secret Manager/KMS、SMTP TLS 和强随机密钥。
- Telegram 真实登录、数据库迁移和邮件联调仍需在 Docker/Telegram 凭据环境完成。
