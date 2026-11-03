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
