# 当前整合分支运行与回滚说明

这是开发验证说明，不是“完整 TGMate 已可上线”的证明。主工程是本仓库根目录，不单独启动 workstreams 中的第二套生产网站。

## 本地基础栈

1. 使用 Python 3.12；安装根 requirements.txt。数据库以 PostgreSQL 16 为基线，Redis 7。
2. 依据 .env.example/.env.runtime.example 在私有位置创建 .env 和 .env.runtime，配置不同的数据库管理员密码、runtime 密码、JWT 密钥、Session KEK 和 SMTP。禁止提交实际配置。
3. 本仓库 Compose 是开发基础栈；使用 `docker compose up --build -d`。migrate 服务必须成功后 API/商业 Worker 才能启动。数据库与 Redis 不暴露宿主端口；API 仅绑定 loopback，由外层 HTTPS 代理接入。
4. `docker compose ps`、`docker compose logs migrate` 和 `/health/ready` 检查基础就绪；未登录资源请求必须拒绝。Mailpit 仅供开发测试，不能用作生产邮件投递。
5. 当前 Telegram 登录流程必须由单一 API 实例处理，不能配置多个 API Worker 或无会话路由的多副本。生产扩展需先完成账号网关。
6. 真实 Telegram 登录还需要平台自有 API ID/API Hash、用户明确授权和已配置网络路径；不得使用示例凭据或将协议号当成网站登录身份。

## 数据迁移

- 全新库使用 `python scripts/bootstrap_combined.py --enable-relay-control`。该入口依次执行根迁移、业务迁移和最小权限授权。
- root 账本为 schema_migrations；business_schema_migrations 保留独立发布文件顺序。原迁移文件不可修改；新修正采用追加迁移。
- 业务 004i 在原 12 份迁移之后执行，关闭 Telegram 内容 AI 评估；不能只执行旧手工迁移清单而漏掉此文件。
- 未被账本接管的旧 tgmate schema 必须先通过 adopt_business_schema.py 的默认只检查流程。明确 apply 前创建新备份、验证结构和依赖；禁止覆盖现有数据或 CASCADE 删除未知对象。
- 接管工具比较原 12 份基线。接管完成后运行 migrate_business.py 应用 004i；升级后的结构不再与原基线相等，不能再次接管。
- 当前未实施破坏性 schema 合并，不按邮箱静默合并身份。

## 本轮测试命令

- 模拟/单元：`python -m pytest tests -q`。数据库测试未显式开启时会 skip，不能把 skip 当通过。
- 真实 PostgreSQL：按 .github/workflows/root-integration.yml 为一次性集群配置 PG*，开启各 TGMATE_*_INTEGRATION 标志后执行根测试。
- 通知/登录专项：使用 NOTIFICATION_TEST_DATABASE_URL（一次性库管理员）和 NOTIFICATION_RUNTIME_DATABASE_URL（受限 runtime）；显式开启 TGMATE_NOTIFICATION_INTEGRATION、TGMATE_LOGIN_INTEGRATION。
- 原 V2 全量：仅在全新、专用、本地 PostgreSQL 测试集群，设 `TGMATE_V2_REGRESSION=1` 后执行 `python scripts/verify_v2_regression.py`。该辅助程序创建并删除随机测试数据库、创建并清理测试角色；拒绝既有 V2 角色。禁止对生产/共享集群运行。

## 回滚与备份

- 业务安全迁移不能通过重新启用 AI 作为回滚；代码退回仍须保留 AI 禁用规则。
- 代码回退使用上一已验证镜像；已有数据不可删除。新增迁移采用兼容性策略，回退前检查版本依赖。
- database_backup.py 仅提供 custom 格式备份及新数据库恢复；拒绝覆盖已有文件和数据库。该文件本身不加密，必须纳入私有加密备份流程后才能异地保存。
- 在隔离新库恢复后核对两套迁移账本、RLS、关键业务计数与财务余额；同时验证独立保存的 KMS/Session 密钥。未通过不得切换生产。
- 本轮未取得可用的目标服务器连接配置；Docker 可运行性由远端 CI 核验，目标服务器还需单独验收。

## 多账号网关与独立 Bot 进程

- root 迁移现在为 12 份，业务迁移为 14 份；以脚本动态文件清单/账本为准，不硬编码旧数量。先完成正常 migrate/bootstrap。
- 多账号只读入口：`python -m app.workers.gateway --tenant <ROOT_TENANT_UUID>`；可重复 --tenant，默认总容量 20、每租户 5。或显式 --from-env 读取私有 GATEWAY_TENANT_IDS。不会发现/扫描未分配租户，不会自动启用路由。
- 网站授权用户通过 GET/POST `/api/v1/telegram/accounts/{account_id}/gateway` 读取/改变期望状态。POST 包含 enabled 布尔和 expected_generation 整数；新路由版本为 0，冲突后必须重新读取。重复启用也生成新版本并重新交接，不作为自动重试按钮。API 返回的 lease_expires_at 不是远端连接已经关闭的证明。
- 连接失败暂停持久路由；人工核对原因及 Telegram 等待后，才可显式重新启用。断开不明还需终止/检查旧进程和恢复 PAUSED 账号；不要给该进程配置自动重启。
- Bot 角色初始化：在管理员 PG* 私有环境设置 BOT_WORKER_PASSWORD（至少 24 字符），执行 `python scripts/configure_bot_worker.py --worker <WORKER_UUID>`。角色固定为 tgs_bot_worker；既有密码不会静默修改，明确 --rotate-password 才轮换。这里不会登记或替换远端 Webhook。
- 保存已有业务 Fernet 密钥至独立 Bot 私有配置 BUSINESS_VAULT_KEY，禁止为了通过启动而更换现有密钥。BOT_WORKER_DATABASE_URL 必须使用上述专用角色；BOT_WORKER_ID 对应已注册 healthy 的 Bot worker；BOT_TENANT_IDS 是现有映射中的业务租户 UUID 白名单。
- Bot HTTP 与原队列同进程：`uvicorn app.bot_main:app --host 127.0.0.1 --port 8001`。只开放 `/webhooks/bots/{bot_id}` 及就绪检查，由外部 HTTPS 代理转发；不挂载原 standalone 网站登录，也不主动应用 Webhook。已有 prepared/active Webhook 迁移需核对域名、URL 和密钥，保留待处理更新。
- 仅运行队列时使用 `python -m app.workers.bot --tenant <BUSINESS_TENANT_UUID> --worker <WORKER_UUID> --live`。它使用原 Bot 业务队列；不会领取协议号群发，未确认的远端发送仍按原 unknown/manual review 处理。
- 可选 Compose 覆盖文件 docker-compose.workers.yml 使用独立 .env.bot.runtime 和 .env.mtproto.runtime，示例已提供。显式组合两个 Compose 文件并启动 bot/mtproto 前，先配置角色、白名单和原有密钥；不能把示例空值当作部署成功。基础 Compose 不自动启动外部 Telegram 连接。
- 仍保留商业 Worker 独立入口。API 登录在迁出内存协调器前继续只运行一个实例。完整三进程业务收敛仍未验收。

## 认证加密备份

`python scripts/database_backup.py backup /private/path/database.backup.enc --key-file /private/keys/backup-key`

`python scripts/database_backup.py restore /private/path/database.backup.enc NEW_DATABASE --key-file /private/keys/backup-key`

key-file 必须是权限 0600/0400 的普通文件，正好 32 个随机字节；拒绝符号链接。它由管理员通过密钥托管系统取得并独立保管，不放入仓库、镜像或归档。原有 Session KEK、业务 Fernet 密钥及其他外部密钥也须独立备份；加密数据库归档不能替代这些密钥。

备份以流式 AES-256-GCM 写出，出错清理临时文件，拒绝覆盖。恢复先完整认证再创建新库；pg_restore 使用私有临时明文文件和单事务，临时文件关闭即删除。生产恢复位置应使用私有加密临时卷。新库失败保留供排查，源库和已有目标不被覆盖。完整恢复后再验证密钥解密、RLS、两套账本、财务对账和实际业务，再决定是否切换。该工具不自行实现 KMS、异地传输、保留策略或生产切换。


## 显式账号的只读 MTProto 验证入口

在私有环境配置平台 TG_API_ID/TG_API_HASH、Session KEK 和受限数据库连接后，运行：

```bash
python -m app.workers.mtproto --tenant "$TGMATE_TENANT_ID" --account "$TGMATE_ACCOUNT_ID"
```

两个变量必须是平台已有租户/账号 UUID。仅使用明确授权的专用测试账号；账号须已完成原登录连接断开并处于 AUTHORIZED/ACTIVE。此入口仅读取身份元数据，不发送测试消息，不自动启动同步。不要配置自动重启；FLOOD_WAIT/WAIT_PERSIST_FAILED/断开不明时先遵守等待并处理故障。

退出码 0 表示正常收到停止信号；2 表示账号不可用、连接检查失败、连接关闭或要求等待；3 表示无法确认断开，需要检查并终止旧进程。此入口不是最终的多账号生产调度部署方式。数据库、Redis 和密钥仍须沿用既有私网与访问控制要求。
