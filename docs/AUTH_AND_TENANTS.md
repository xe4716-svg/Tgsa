# SaaS 身份认证与租户权限 — 阶段 3

## 用户身份
- 邮箱 + 密码注册；邮箱验证后才允许登录和绑定 Telegram。
- 密码使用 scrypt（独立随机盐）保存，不保存明文密码。
- 邮箱验证和密码重置使用一次性随机 token；数据库只保存 SHA-256 摘要，30 分钟过期。
- 邮箱重发、登录、注册、找回密码按 IP/邮箱限流；Redis 不可用时认证接口失败关闭。
- 邮箱发送通过 SMTP；本地 Docker 使用 Mailpit（`http://localhost:8025`），生产使用已配置 TLS 的 SMTP 服务。

## Token 生命周期
- Access Token：默认 15 分钟，HS256 签名。
- Refresh Token：随机不透明 token，默认 30 天；数据库只保存哈希。
- 每次 refresh 都轮换 token；重复使用旧 token 会撤销整个 token family。
- Logout 和密码重置会撤销 token family / 用户刷新会话。
- 每次访问受保护接口都会检查对应 token family 是否仍有有效 refresh token，从而让 logout 即时生效。

## 租户
- 注册时创建一个默认租户，并将创建者设为 OWNER。
- 一个用户可以加入多个租户；切换租户时重新签发独立会话。
- 所有租户业务数据由 PostgreSQL RLS 隔离。
- `tenant_id` 只从已签名 Access Token 或已验证的 scoped refresh/email token 获取，不能信任请求体单独提供的租户 ID。

## API
- `POST /auth/register`
- `POST /auth/verify-email`
- `POST /auth/resend-verification`
- `POST /auth/login`
- `POST /auth/refresh`
- `POST /auth/logout`
- `GET /auth/me`
- `POST /auth/switch-tenant`
- `POST /auth/forgot-password`
- `POST /auth/reset-password`

## 上线前必做
- 配置至少 32 字符的随机 `JWT_SIGNING_SECRET`，并放入 Secret Manager；启动时会验证密钥长度。
- 配置生产 SMTP TLS；禁止在生产使用 Mailpit。启动时会验证 Session KEK 和 SMTP 配置。
- 修改 `.env` 所有默认密码和密钥。
- 数据库首次初始化才会自动执行 migrations；已有数据库需使用受控 migration runner 执行新迁移，不能删除生产数据卷。
- 配置 HTTPS、可信反向代理和真实域名。
- 当前 API 尚无前端页面；注册、登录、邮箱验证页面需由前端接入以上 API。
