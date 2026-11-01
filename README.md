# TG SaaS V1.0 — 持续迭代工程

当前工程包含：
- FastAPI、PostgreSQL、Redis、租户 RLS。
- Telegram 登录服务层、Session 加密、多账号协议号文件。
- 显式代理/直连策略、SOCKS5 / MTProto 配置。
- SaaS 邮箱注册、验证、登录、密码重置、Refresh Token 轮换、租户切换。
- FREE / BASIC / PRO / ENTERPRISE 会员等级和功能权益管理。
- 频道克隆、表情包克隆权限门槛；账号数量上限在入库时执行。
- 本地 Mailpit 邮件测试服务。

## 本地启动
1. 安装 Docker 与 Docker Compose。
2. `cp .env.example .env`。
3. 执行 `python scripts/generate_dev_secrets.py`，将生成的 `JWT_SIGNING_SECRET` 和 `SESSION_KEK_BASE64` 填入 `.env`；同时设置强随机数据库密码。
4. `docker compose up --build`。
5. API：`http://localhost:8000/docs`；健康检查：`/health/live`、`/health/ready`。
6. 本地邮件收件箱：`http://localhost:8025`。

## 注意
- 这是持续开发工程包，不是已经上线的完整产品。
- 新迁移只会在 PostgreSQL 数据卷首次初始化时自动执行；已有数据库必须使用受控 migration runner 执行。
- 生产必须使用 HTTPS、Secret Manager/KMS、SMTP TLS 和强随机密钥。
- Telegram 真实登录、数据库迁移和邮件联调仍需在 Docker/Telegram 凭据环境完成。
