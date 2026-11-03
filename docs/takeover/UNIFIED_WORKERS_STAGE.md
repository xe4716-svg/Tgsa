# 多账号路由、统一 Bot 执行与加密恢复

2026-11-03，接续已发布的 549dede；本轮变更提交及远端 CI 链接以 PR #27 当前 HEAD 为准。没有合并 main，没有连接真实 Telegram、支付平台或生产数据库。

## 已完成并验证

- 追加 root 011/012 和 business 004j，保留所有已发布 SQL 原文与迁移账本；fresh/replay/hash-drift/rollback 仍实际执行。
- GatewayRoutes 保存显式启停和 generation；API 使用当前网站会话、租户锁、账号锁及等待后撤权复核，变更与审计同事务。过期 generation 返回冲突，旧生命周期不能覆盖新操作。
- GatewaySupervisor 按部署租户白名单、公平轮询、总容量和租户容量启动只读连接；复用既有租约、fencing、加密材料和服务端等待。撤销路由、租户停用、数据库失败停止连接；故障暂停持久路由，无法确认断开则停止进程。不是新的业务队列。
- 修复租约未复核租户停用状态的问题。新增路由变更竞争、跨租户读写拒绝、旧版本暂停失败、撤权锁等待、故障关闭及容量分配测试。
- HTTP CONNECT 使用实际安装的 Telethon/python-socks 传输，通过 loopback 隧道、Basic 认证、407、502、超时和关闭测试；失败没有直连回退。没有进行公网代理出口实测。
- 保留 V2 业务源码改用包内相对导入，以 app.business 直接加载同一份源码，不复制数据库/队列，也不挂载旧网站认证服务。保留原独立回归入口兼容性。
- 独立 Bot Worker 复用私信、双向客服、Bot 群发、群管、验证通知、回调确认和过期处理。Bot 群发只领取 bot_broadcast，协议号群发不会被误领。未启用旧 AccountBroadcast 的独立连接代码。
- 新增独立 Bot HTTP 入口，复用加密 Webhook 密钥、租户白名单、原 Inbox 去重和群/私信路由，认证先于正文解析；限制正文 1 MiB 与并发入口。Worker 失效时就绪探针/入口返回不可用。
- 独立 tgs_bot_worker 角色复用已审查业务授权，不授予网站密码、root 用户或 root 加密会话读取权。每个短业务事务通过受限 SECURITY DEFINER 入口复核/锁住已有 root 映射和两套租户状态；没有按邮箱合并身份。
- Bot API 429 等待保存至 tenant/bot 维度的共享表；不同实例看到同一截止时间，较短新等待不能缩短旧等待，存储故障会关闭门禁并停止 Worker。修复原 RetryLater 将超过一天等待截短的问题。已经在途的请求无法被数据库追溯撤销。
- 加密备份直接以流式 AES-256-GCM 写入归档，不落地明文 dump；恢复先验证整个归档，再创建新数据库，以私有临时文件供 pg_restore 单事务恢复。错误密钥、篡改、截断均不启动数据库工具；拒绝覆盖既有库和文件。
- 真实 PG 恢复演练核对两套账本、映射、Bot 记录、钱包余额、会话解密及 RLS。生产 KMS/离线密钥托管、异地保存、恢复点目标仍需要部署环境配置。
- 40 个已有 Outbox 合成任务由 8 个并发执行线程领取，确认只有 40 次唯一模拟投递、40 条成功尝试；这是本地队列竞争测试，不是生产吞吐承诺。

## 测试证据

`docs/validation/unified-results.json` 汇总，原始结果为 unified-unit.xml、unified-database.xml、unified-transactions.xml、unified-v2.xml；队列时间与环境在 unified-queue-concurrency.json。套件重叠，不能相加作为独立覆盖率。

本轮测试中遇到并处理：

1. 一次性数据库名称未符合既有测试的安全前缀，测试主动拒绝；更正为 tgs_migration_* 后重跑。
2. 新 V2 测试插入位置错误，将原测试的末尾断言放进新测试；移动回原测试后重跑。
3. 本地 overlay 存储反复触发 PG “unexpected data beyond EOF”，不是略过失败；切换全新 tmpfs 一次性集群后完成 208 项原 V2 回归。远端独立 PG/Docker 结果仍需按当前 HEAD 核实。
4. 一条既有 Starlette/httpx TestClient 弃用警告仍在，未将其隐藏。

## 未完成与下一门禁

整体仍在开发中，尚未达到完整产品验收。API 登录仍需单实例；分布式登录命令、PENDING 受控恢复、MTProto 业务执行、历史/实时克隆、贴纸克隆、统一前端、完整支付返佣与高强度故障/压力测试仍有工作。没有生产目标、专用真实 Telegram 测试资源及支付测试配置，不将模拟结果标为真实外部验收。逐项状态见 ACCEPTANCE.md 和 OPEN_ISSUES.md。
