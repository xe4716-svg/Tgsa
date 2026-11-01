# 初始代码审查记录（MVP 协作启动）

审查基线：main，提交 04430ac3f552fb3631d6ea2df2a0bd3a9dad8555。
范围：当前可读取的认证、RLS、Docker、应用启动和 Telegram 登录实现。此记录是静态代码审查，不代表已执行生产环境测试。

## P0：请求级租户上下文生命周期必须修复
证据：
- app/auth/dependencies.py 的 get_current_principal 在 pool.acquire() 内开启 transaction，调用 set_config('app.tenant_id', ..., true)，查询会话和成员关系后返回。
- 该 transaction 随 async with 退出而结束，连接随后归还连接池。
- set_config(..., true) 是事务局部设置，事务结束后不会自然延续到后续业务查询。
风险：后续路由若使用另一个连接/事务，RLS 看不到租户上下文，可能出现查询为空、操作失败；若某些路径使用非统一上下文，隔离行为也可能不一致。
要求：实现 request-scoped Unit of Work/依赖，业务 SQL 在同一显式事务和同一连接中执行；在该事务中完成可信身份验证和 tenant context 设置。所有租户私有查询必须走统一依赖。增加连接池复用、并发租户和无上下文 fail-closed 测试。

## P0：RLS 当前依赖可由数据库会话设置的普通 GUC
证据：
- migrations/002_rls.sql 的策略直接读取 current_setting('app.tenant_id', true)。
- app/tenant_context.py 仅将 UUID 写入 app.tenant_id。
风险：该方案是防止遗漏 WHERE tenant_id 的纵深防御，但不能抵御拥有数据库连接的受损应用角色任意设置其他 tenant_id。不得把它描述为密码学可信租户证明。
要求：明确威胁模型；收紧运行角色权限、禁止表所有权/BYPASSRLS；将所有租户访问集中到受控事务边界。若要求抵御运行角色被滥用，需增加独立身份签名服务和数据库可验证的短期签名上下文（签名私钥不得在 API/DB 运行角色中），并用攻击测试验证。

## P0：后台循环在 API lifespan 内启动，横向扩容可能重复运行
证据：
- app/main.py 在每个 FastAPI lifespan 启动 run_billing_automation；TRON watcher 也在 tron_watcher_enabled 时启动。
风险：多个 API 副本可能各自启动相同循环，造成重复调度、竞争、额外 RPC/数据库压力。即使当前有幂等约束，也不应依赖它代替单一职责。
要求：将调度循环迁移到独立 worker 服务；或使用可验证的分布式 leader lease/锁，并证明多副本不会重复执行。推荐独立 Worker。

## P1：Telegram 登录流程保存在单进程内存
证据：
- app/telegram/login_manager.py 的 TelegramLoginManager 将 Telethon client 和 PendingLogin 保存在 self._pending。
- 类注释说明它是 single-process coordinator。
风险：多 API 副本、进程重启或流量切换会丢失待完成登录状态。
要求：将登录流程路由到专属 account-gateway worker，使用持久化 owner/lease 记录和会话亲和；不要把活的 Telethon client 对象塞入 Redis。超时/取消/失败必须断开 client 并清理敏感数据。

## P1：Docker Compose 只适用于本地开发，不得直接作为生产部署
证据：
- docker-compose.yml 将 PostgreSQL 5432 映射到宿主机所有接口。
- POSTGRES_PASSWORD、APP_DB_PASSWORD 存在弱默认值。
- Mailpit 暴露 8025。
风险：若直接用于公网服务器，数据库和邮件测试服务可能暴露，默认口令不安全。
要求：增加 production compose/profile；数据库和 Redis 不发布公网端口，只放私有网络；本地开发端口绑定 127.0.0.1；生产必须显式注入强密钥，不允许弱默认值启动。

## P1：Session 明文交接边界需要审计
证据：
- TelegramLoginManager.consume_authorized() 将 session_string 返回给调用方后清空内存字段。
风险：调用方必须立即加密保存；任何日志、异常追踪、响应序列化都不能暴露明文 Session。
要求：增加调用链审查和测试，保证 session 明文只在受控服务内短暂存在，使用 KMS/信封加密持久化。

## 下一步
1. AI-1 优先解决请求级事务/租户上下文和数据库最小权限。
2. AI-3 将后台循环迁移到独立 Worker 并加多副本测试。
3. AI-2 将 Telegram 登录流程纳入 account-gateway 所有权/租约设计。
4. 所有修复通过各自分支 PR 进入 integration/mvp，不直接修改 main。
