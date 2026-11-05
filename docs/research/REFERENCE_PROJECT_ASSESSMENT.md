# TGM-022 成熟成品源码调研与 TGMate 可复用模块评估

- 审查日期：2026-11-05
- TGMate 仓库：`xe4716-svg/Tgsa`
- 本次审查基线：`main @ 04430ac3f552fb3631d6ea2df2a0bd3a9dad8555`
- 范围：只读研究与架构对照；本 PR 不移植产品代码，不修改数据库或运行配置。

## 1. 结论摘要

没有发现一个可以整体替换 TGMate 的成熟成品。当前最合理的策略是模块级借鉴：

1. **频道克隆引擎与持久化状态**：优先研究 `tamvcspk/tgmirror` 的 Gateway/Engine/Store 分层、批次写前记录、消息映射、相册单元和限流状态。
2. **原生批量转发与实时变更**：研究 `apppurchasespro-hash/telegram-forwarder` 的流式批量转发和编辑/删除映射，但绝不引入其受保护内容下载/上传回退。
3. **Telethon 媒体引用、相册处理**：研究 `MR-PR0G/Telegram-channel-cloner` 的媒体对象、`grouped_id` 和分页；不采用其通用异常盲重试。
4. **前端工程基线**：`fastapi/full-stack-fastapi-template` 可作为 React/Vite/TanStack/shadcn/ui、OpenAPI client、Playwright E2E 和 Docker/CI 的参考；不替换 TGMate 现有 asyncpg/PostgreSQL/RLS 数据模型。
5. **插件式内容处理**：`aahnik/tgcf` 的过滤、格式化和替换插件可作设计参考；它的实时映射部分依赖内存，媒体 sender 插件有文件读取/上传路径，不整体移植。

所有 Telegram 参考项目都是单账号工具或单进程应用，不是 TGMate 所需的多租户 SaaS。需要将有价值的纯业务逻辑重新实现到 TGMate 既有租户、RLS、Outbox 和 Gateway 边界内。

## 2. 已审查候选项目

| 项目 | 许可证 / 技术 | 有价值的设计 | 不采用的部分 / 风险 | 结论 |
|---|---|---|---|---|
| [tgmirror](https://github.com/tamvcspk/tgmirror) | MIT；Telethon 1.45.0；SQLite；Typer/Rich；v0.3.0（2026-10-24） | Gateway protocol、纯 Engine、Store；pair cursor/msg_map；pending batch 写前记录；album Unit；forum topic map；FloodWait/暂停/恢复；历史 clone/delta；手动 sync-changes；edit/delete/repost/topic 操作；backup/restore；TUI/Docker | 单账号 CLI + SQLite，无 SaaS/RLS；没有持续实时事件流；策略含 download/reupload/restore；引用过期后会回退下载上传；Store 不持久化 random_id；pending 恢复使用目标尾部形态启发式，可能重新发送；不满足 TGMate 的强 UNKNOWN 对账要求 | **最强克隆架构参考，不直接移植** |
| [telegram-forwarder](https://github.com/apppurchasespro-hash/telegram-forwarder) | MIT；Telethon；Quart | 流式历史遍历；最多 100 条原生转发批次；源→目标映射；编辑/删除传播；pair lock；暂停开关；论坛 topic 转发 | 对 protected source 有 download/re-upload copy-mode；raw ForwardMessagesRequest 每次重新生成 random_id；部分失败批次会推进水位；JSON 文件状态不是多进程/多租户数据库；Web Basic Auth 在未配置密码时关闭 | **只研究原生路径和事件映射** |
| [Telegram Channel Clone Theta](https://github.com/MR-PR0G/Telegram-channel-cloner) | MIT；Telethon；Python 3.12+ | 传递现有 message.media；grouped_id 相册聚合；按消息 ID 正序分页；进度和失败记录；reply mapping 概念 | send helper 对通用异常重试，未处理响应丢失导致的 SEND_UNKNOWN；失败后仍可能推进进度；映射主要在内存；CLI 单账号 | **借鉴媒体/相册/分页，不照搬重试和状态** |
| [tgcf](https://github.com/aahnik/tgcf) | MIT；Telethon；最后推送 2024-05-06 | past/live 分离；事件式 New/Edit/Delete；插件化 filter/format/replace/caption；配置驱动 | live message map 是进程内字典；past async 路径使用阻塞 sleep；sender plugin 通过 get_file 处理媒体；项目维护活跃度偏低 | **仅借鉴纯过滤插件接口和事件模型** |
| [telemirror](https://github.com/khoben/telemirror) | GPL-3.0；Telethon；PostgreSQL 可选 | 多源多目标映射、话题配置、过滤、编辑/删除同步 | GPL-3.0 不适合直接并入当前产品代码，除非完成独立许可证合规评审；需另查媒体路径 | **只读概念参考，不复制代码** |
| [Clonechat](https://github.com/matheusbach/clonechat) | MIT；Pyrogram | 历史复制、续传、内容类型处理 | 与 TGMate 的 Telethon/Gateway 不一致；包含受保护内容下载上传路径 | **不作为直接移植来源** |
| [FastAPI Full Stack Template](https://github.com/fastapi/full-stack-fastapi-template) | MIT；FastAPI/React/TypeScript/Vite/TanStack/Tailwind/shadcn | OpenAPI client 生成、管理页组件、Playwright E2E、Docker Compose、CI、邮件测试环境 | 后端使用 SQLModel/JWT，与 TGMate 当前 asyncpg + PostgreSQL RLS/自有认证体系不同 | **前端和工程流程参考，不替换后端** |
| [Alexander-Kz/telegram-forwarder](https://github.com/Alexander-Kz/telegram-forwarder) | MIT；Telethon/SQLite | 消息映射、增量恢复、论坛话题、失败重试台账 | 备份/恢复包含媒体下载；单账号 CLI | **仅参考状态组织和 topic map** |
| [Grey-Time/Telethon-Channel-Clone](https://github.com/Grey-Time/Telethon-Channel-Clone) | MIT；Telethon | 基础历史遍历/发送 | 规模小、维护时间较早、可靠性能力不足 | **基础对照** |
| [Telegram-group-and-channel-cloner](https://github.com/maksemen2/Telegram-group-and-channel-cloner) | 未发现 LICENSE | 可观察 grouped_id 相册处理 | 未发现明确许可证 | **不复制代码** |

### tgmirror 版本与验证边界

- 本次读取的 GitHub main commit：`db8c1128572e683e8692ada09faae06a8959cb90`；GitHub Release `v0.3.0` 于 2026-10-24 发布，wheel/sdist、Docker 和 PyPI 发布作业成功；可选的 WinGet 提交作业失败，不影响核心发布。
- PyPI 搜索页的旧索引仍显示 `0.1.1`（2026-09-28），与 GitHub v0.3.0 release 不一致；本次以 GitHub tag/release artifacts 和 main commit 为当前源码依据，不把旧索引当最新版本。
- GitHub Actions：main CI 在该 commit 成功；release workflow 中 PyPI 与 Docker job 成功，WinGet job 失败。
- 仓库测试树包含 96 个 unit 文件和 22 个 integration 文件；README 的测试命令使用 FakeGateway/本地 SQLite，因此 CI 通过不等于所有 Telegram 路径都做了实网验证。
- README/roadmap 记录 v0.3.0 的 clone/delta、sync-changes、edit/delete、topic 操作等主要能力已在真实账号上试过；新菜单入口仅有测试覆盖。仍有未实测边界：`inspect --quick`、自适应 rate-limit 部分、非 forum 来源 forward 到 forum topic、album forward 到 topic 等。
- v0.3.0 的 `--keep-replies`、`--mode reupload`、backup/restore 会在特定路径重新发送或下载/上传媒体；这些路径不符合 TGMate 的无下载/无上传约束，必须排除。

## 3. Telegram 原生转发的关键技术判断

Telegram 官方 MTProto 文档：

- [messages.forwardMessages](https://core.telegram.org/method/messages.forwardMessages) 支持批量消息 ID 和每条消息的 `random_id`。
- [Working with Updates](https://core.telegram.org/api/updates) 说明 `updateMessageID` 可以将 `random_id` 映射到实际目标消息 ID，并通过更新差异恢复。
- [updates.getDifference](https://core.telegram.org/method/updates.getDifference) 用于恢复离线期间或响应丢失期间的更新。

TGMate 应采用以下更严格的策略：

1. 对每个 route/source-message 建立持久化 operation key 和稳定 random_id；重试不得生成新 ID。
2. 发送前持久化 pending/dispatch_started 状态；发送后持久化目标映射和 cursor。
3. 优先原生 `ForwardMessagesRequest`，解析 `UpdateMessageID`；响应丢失时用更新差异/更新事件对账。
4. 无法确认是否已投递时保留 UNKNOWN 并阻断该 route；不得用目标尾部消息形态猜测成功后盲目重发。
5. 相册作为不可拆分 Unit，所有源消息分别映射到对应目标消息；批次水位只在整批结果确认后推进。
6. 媒体引用过期时只允许用同一授权身份重新读取源消息并刷新引用一次；仍不可用就失败/跳过。删除所有 download/re-upload fallback。
7. 来源受保护、权限未知、执行身份无访问权或 API 禁止复制时 fail closed；不绕过限制。

## 4. TGMate 当前基线和缺口

### 已有基础

Tgsa 主仓库已有 FastAPI、PostgreSQL、Redis、租户/RLS、认证、加密 Session、Telethon 登录、会员/钱包/支付等基础。

- 主仓库 main 当前审查基线：`04430ac3f552fb3631d6ea2df2a0bd3a9dad8555`。
- PR #27 `work/v2-takeover`：Draft、未合并；有租户路由、Gateway lease/fencing、Worker、备份恢复等基础。
- PR #28 `fix/asd-30-account-broadcast-gateway`：Draft、未合并；有持久命令表/SQL 函数/注入式 executor，但当前命令范围为 `preflight_send_text`，生产 sender、结果读取、FloodWait 恢复、账号 Worker/service 仍有缺口。

### 克隆模块尚缺

- 历史消息只读分页和可恢复 cursor。
- 实时 New/Edit/Delete Inbox。
- 历史回填与实时事件的无缝衔接。
- 原生批量转发命令和媒体引用命令。
- 持久 random_id 与 updateMessageID 对账。
- 每个 route 独立的 message map、album map、cursor 和暂停状态。
- Bot API copyMessage/forwardMessage 独立路径。
- 权限、受保护内容、媒体引用失效、FloodWait、UNKNOWN、并发和恢复测试。

## 5. TGMate 的建议模块映射

| TGMate 目标模块 | 参考设计 | 接入边界 |
|---|---|---|
| `app/telegram/gateway_*` | tgmirror 的 Gateway protocol + Telethon adapter | 保留 TGMate Gateway lease/fencing；Gateway 是唯一 Telethon client owner |
| `app/clone/engine`（待建） | tgmirror Engine/Runner/Planner/Batcher | 纯业务逻辑，不直接 import Telethon |
| `app/clone/store`（待建） | tgmirror mirrors/runs/msg_map/topic_map | 改用 PostgreSQL + tenant_id + FORCE RLS；不引入 SQLite |
| `app/clone/strategies`（待建） | 原生 copy + media reference | 只保留 copy/reference；删除 reupload 策略 |
| `app/clone/events`（待建） | tgcf/telemirror 的事件处理概念 | 写入持久 Inbox；禁止内存字典作为唯一映射 |
| `app/content/filters`（现有/待核对） | tgcf plugin/filter/replace | 插件纯逻辑、可版本化、可单独启停 |
| 前端（待建设） | FastAPI Full Stack Template | 只复用 React/UI/API client/E2E 结构，后端仍走现有 API/RLS |
| Worker | 现有 PostgreSQL Outbox/Gateway command queue | 不另起 Celery/Redis broker，除非负载实测证明必要 |

## 6. 不可违反的整合规则

- 不下载媒体到 TGMate 服务器，不重新上传，不使用 Agent 上传兜底。
- 不绕过 Telegram 权限、受保护内容或转发限制；权限未知时 fail closed。
- MTProto 操作必须走统一 Gateway，不在业务 Worker 旁路创建 TelethonClient 或读取明文 Session。
- SEND_UNKNOWN 必须对账，不盲目重发。
- 不将 GPL 代码直接复制进产品；MIT 复用必须保留版权/许可证声明。
- 每个实际代码采用项独立分支、PR、实际测试和审查；本报告 PR 仅为研究文档，不包含产品代码。
- 不修改已确认的产品需求，不将研究结果或 fake 测试描述为产品验收。

## 7. 下一步

1. 完成 SaaS/RBAC、管理后台、Bot API、Worker、商业系统和部署运维候选项目的第二轮研究。
2. 将候选项目关键模块逐一映射到 TGMate 现有目录、表和 Gateway 命令契约。
3. 与 ASD-5/7/8/9 和 PR #27/#28 的真实审查状态对齐；未解除依赖前不做频道克隆产品写入。
4. 在专用授权测试账号和测试频道准备好后，先做原生 forward + 稳定 random_id + updateMessageID 对账 PoC。
5. 每个被选中的实现建立独立分支/PR，并运行数据库、并发、故障恢复和 Telegram sandbox 测试。

**本报告状态：第一轮研究成果。未移植产品代码；未在本轮运行 TGMate 测试；未进行真实 Telegram 发送或生产部署。**


## 8. 第二轮 SaaS / 多租户源码候选

### 首选 SaaS 参考：yezz123/ai-template

[仓库](https://github.com/yezz123/ai-template)；MIT；GitHub 元数据 258 stars / 41 forks；本次核验 main commit `3a98f05087ae03ce0ae5570bf8ecc0e4def54bda`（2026-09-19）。项目说明包含 FastAPI + AuthX + SQLAlchemy async + Alembic + PostgreSQL + Redis，以及 Next.js tenant app 和独立 super-admin portal。

**值得借鉴：**
- Organizations / memberships / invitations；Owner/Admin/Member 角色和 scope 映射。
- 切换 organization 时重新签发带 org_id、role、scopes 的 token。
- 邀请 token 只保存 SHA-256 hash，并有过期时间。
- API key 使用随机 secret，数据库只保存完整 key 的 SHA-256 hash 和 prefix；明文只在创建时返回一次。
- API key 请求的 scopes 必须是创建者自身 scopes 的子集，防止提权。
- 每租户 API key、audit log、plan limits、rate limiting、feature flags。
- 独立 tenant frontend 和 super-admin portal；有 organizations/RBAC/API keys/audit/billing 的集成测试文件和 backend/frontend/admin 三条 CI workflow。

**不能原样采用：**
- 它的租户隔离主要依赖 shared-schema 中的 org_id 和应用层 repository filter；TGMate 必须保留 PostgreSQL RLS/FORCE RLS，不能降级。
- role/scopes 放在 JWT claim 中；需额外验证角色变更、成员移除和 token 撤销是否即时生效。
- AuditLogMiddleware 在响应成功后单独写入，并吞掉写入异常；TGMate 关键审计事件必须确保事务一致性或持久化重试，不能静默丢失。
- SQLAlchemy ORM 与 TGMate 当前 asyncpg/SQL 迁移体系不同；不替换现有数据层。
- Stripe 订阅不能替代 TGMate 已确认的钱包、支付、动态返佣业务。

**采用决定：** 作为 SaaS 多租户、邀请、scope/API key、feature flag、管理端和测试矩阵的首选设计参考；仅摘取明确可适配的逻辑，保持 TGMate RLS、现有认证和商业模型。

### 其他 SaaS 候选排除
- [TinDang97/fastapi-multi-tenant](https://github.com/TinDang97/fastapi-multi-tenant)：Apache-2.0，但 SQLite-per-tenant，与 TGMate PostgreSQL RLS 不匹配，规模/活跃度证据有限，仅作 Clean Architecture 对照。
- [jeraldoalday/saas-starter-kit](https://github.com/jeraldoalday/saas-starter-kit)：GitHub 未识别许可证（NOASSERTION）、0 stars；不复制代码。
- [PythonCharmers/fastapi-saas-starter](https://github.com/PythonCharmers/fastapi-saas-starter)：0 stars、未发现明确许可证；不复制代码。
- 这些新发现不能证明它们就是历史研究记录中提到的那个“SaaS Starter Kit”；原始历史仓库身份仍待通过旧对话/工程证据定位。

## 9. Bot API 原生消息复制研究

官方 [Telegram Bot API](https://core.telegram.org/bots/api#forwardmessages) 定义了：
- `forwardMessage` / `forwardMessages`：原生转发；`forwardMessages` 接收 1–100 个严格递增的 message IDs；相册分组保持；受保护内容不能转发。
- `copyMessage` / `copyMessages`：复制消息但不带原消息链接；`copyMessages` 同样接收 1–100 个严格递增 IDs，保持相册分组；`copyMessage` 可在不上传媒体字节的情况下替换 caption。
- 不支持复制的类型包括 service messages、paid media、giveaway、giveaway winners 和 invoices；poll 还受 correct_option_ids 条件限制。

TGMate 约束：即使 Bot API 暴露 copy 方法，也不尝试绕过来源的 protected-content 标记；受保护或权限未知时 fail closed。Bot API 的 forward/copy 方法没有 MTProto 的 `random_id` 参数，因此 Outbox idempotency key 只能保护 TGMate 内部任务创建/调度，不能保证 Telegram 端 exactly-once。网络超时且结果未知时必须标记 UNKNOWN 并停止该目标的自动重发；只有明确失败或可核验成功时才能推进。

### TGMate 当前 Bot 代码对照
PR #27 的 `workstreams/tgmate-v2/app/bot_gateway.py` 已有：
- webhook secret 验证和 active bot 检查；
- 按 tenant + bot + update_id 的 inbox 去重；
- payload hash 冲突检测；
- ingress 与领域队列同事务提交。

同分支 `broadcast.py` 已有：
- text-only immutable plan；
- 目标数量校验；
- bot/account 两种独立执行通道；
- Outbox dispatch 和 run/target snapshot；
- fresh preflight 前不允许发送。

当前代码尚未实现 Bot API 的 `copyMessage/copyMessages/forwardMessage/forwardMessages` 媒体转发路径。后续应新增 Bot Gateway 原生 copy/forward adapter，按 message_id 逐条持久化结果；对 Bot API 无法提供 server-side idempotency 的 UNKNOWN 情况采用 fail-closed，而不是盲目重发。

## 10. 第二轮模块决策更新

| 模块 | 首选参考 | 采用方式 | 优先级 |
|---|---|---|---|
| MTProto 克隆 Engine / Store | tgmirror | 重实现 Gateway/Engine/Store、pair cursor、pending batch、album Unit、topic map；PostgreSQL + RLS | P0 |
| MTProto 原生转发可靠性 | Telegram 官方 MTProto docs | raw ForwardMessagesRequest + 持久 random_id + UpdateMessageID 对账；UNKNOWN 停止自动重发 | P0 |
| Bot API 媒体复制 | Telegram Bot API | copyMessages/forwardMessages 原生方法；保持 album；Bot API UNKNOWN fail-closed | P1 |
| SaaS 组织 / RBAC / API keys | yezz123/ai-template | 借鉴组织成员、scope、邀请 token hash、API key hash + scope subset；保留 TGMate RLS | P1 |
| 前端 / 管理端 / E2E | FastAPI Full Stack Template + ai-template | 比较后择一，不同时引入两套前端架构；保留 OpenAPI client 和 E2E 思路 | P1 |
| 内容过滤插件 | tgcf | 只借鉴纯函数 filter/transform 插件接口；持久状态不放内存 | P2 |
| 商业/支付/返佣 | 暂无合适整体候选 | 保留 TGMate 现有业务模型，后续只对账单、webhook、entitlement 细节做专项比较 | P1 |
| Worker / queue | tgmirror + TGMate Outbox | 借鉴 runner/checkpoint；不另加 Celery/Redis broker，先复用 PostgreSQL Outbox/Gateway queue | P0 |

## 11. 更新后的状态

- 已完成：Telegram clone 首轮审查；tgmirror 深度审查；Bot API 官方方法核对；SaaS 多租户候选首轮审查；GitHub 文档 PR #30。
- 正在执行：部署/监控/备份候选项目与商业模块专项对照；把上表细化到 TGMate 目标目录、数据库表和 Gateway 命令。
- 尚未完成：完整模块映射、真实 Telegram PoC、产品代码移植、TGMate 自动化测试和生产验收。
- 研究任务进度：50%（研究文档和首轮模块选型进度；不是产品代码或上线完成率）。


## 12. Root Gateway 与旧 workstream 的边界澄清

为避免把“仓库中存在代码”误报成“生产链路已经接通”，进一步核对 PR #27/#28 的实际文件和迁移：

- PR #27 的根运行时 `app/telegram/gateway_runtime.py` 只负责 account lease/fencing、Session/Proxy material 校验、连接授权和断开生命周期；它没有生产 send API。
- `client_factory.create_client(managed=True)` 明确关闭 auto_reconnect、connection_retries、request_retries，并设置 `receive_updates=False`。因此当前 Gateway 还没有实时克隆所需的 New/Edit/Delete/UpdateMessageID 事件接收链路。
- PR #28 仍为 Draft，base 是 PR #27 的 `work/v2-takeover`。它新增 PostgreSQL 持久命令队列、append-only events、FORCE RLS 和 lease/fencing transition functions；当前 `operation_kind` 只允许 `preflight_send_text`，payload 只允许 text，sender 通过依赖注入且没有安装生产 Telegram transport。
- PR #28 的 executor 对 dispatch_started 后异常采取 UNKNOWN 并停止，不盲目重发；这是后续 clone forward command 必须保留的安全语义。
- PR #27 中 `workstreams/tgmate-v2/app/bot_gateway.py` 和 `broadcast.py` 属于独立旧 workstream，不是根 `app/` 的生产运行路径。PR #28 的 Gateway contract 明确说明旧 `telegram_adapters.py` 会从 legacy credential 创建独立 TelethonClient，尚未接入 root Gateway。旧 workstream 的 webhook Inbox 和 text-only plan 只能作为研究材料，不能描述为已经完成的根产品 Bot Gateway。
- 当前根迁移已有 `telegram_accounts`、加密 `telegram_sessions`、`account_gateway_leases`、`account_gateway_routes` 和 FORCE RLS；尚未发现 clone route、clone batch、source→destination message map 或 clone event inbox 的根产品迁移。

### 新增设计交付
- [TGM006 Clone Engine / Gateway ADR](TGM006_CLONE_ENGINE_ADR.md)
- 逻辑上定义 Clone Engine / Store / Gateway 边界、typed read/write operations、PostgreSQL RLS 状态模型、stable random_id、UpdateMessageID 对账、album Unit、history/live 无缝衔接、Bot API UNKNOWN 处理和测试矩阵。
- ADR 是研究设计，不是 migration 或 implementation；产品代码整合仍等待 PR #27/#28 和 TGM-003/004/005 依赖审查。

**当前研究任务进度：55%。** 第一轮源码筛选、Telegram clone 深度审查、Bot API 语义、SaaS/RBAC 候选和 Clone Engine ADR 已有书面交付；部署/监控/备份、商业模块专项研究、完整接口映射、产品代码移植和实网验收仍未完成。

## 13. 部署、备份恢复与可观测性研究

### TGMate 当前实现核对

PR #27 根分支当前已有：
- `/health/live` 和 `/health/ready`；ready 检查 PostgreSQL、Redis 并在依赖不可用时返回 503。
- Compose 开发基础栈：PostgreSQL 16、Redis 7、migrate gate、API、billing worker；DB/Redis 不暴露宿主端口，API 绑定 loopback。
- 可选 Bot/MTProto worker overlay，两个 Telegram 进程使用独立私有 env；Gateway worker 的 restart 设为 no，避免不确定断开后自动重启。
- `scripts/database_backup.py`：pg_dump custom-format 逻辑备份；restore 只能创建新数据库，不 drop/clean 已有库，pg_restore 使用 single transaction。
- `scripts/encrypted_backup.py`：AES-256-GCM 流式加密；32-byte 私有 key file、O_NOFOLLOW、0600/0400 权限检查；恢复先完整校验 tag，再创建目标库；临时明文只写私有临时文件并在关闭时清理。
- 有 `tests/test_database_backup.py` 和 `tests/test_encrypted_backup.py` 覆盖覆盖保护、失败清理、错误 key/tag、临时文件和恢复安全。**本次只读审查，没有执行这些测试。**

部署文档明确说明当前脚本尚未实现 KMS、异地传输、保留策略、自动调度和生产切换；也未取得目标服务器连接配置。Compose 是开发基础栈，不是正式生产上线证明。

### 外部备份候选

| 项目 | 许可证/活跃度证据 | 能力 | TGMate 采用建议 |
|---|---|---|---|
| [pgBackRest](https://github.com/pgbackrest/pgbackrest) | GitHub API license field 为 NOASSERTION；2026-11-04 有 push；约 4.4k stars | Full/differential/incremental；WAL archive；加密；backup/restore verification；支持 POSIX、SFTP、S3、GCS、Azure、CIFS；版本化对象存储可按 target time 读取 | **首选物理备份/PITR PoC**；先核验发行包许可证、目标数据库版本、存储和恢复演练；不复制其源码 |
| [WAL-G](https://github.com/wal-g/wal-g) | GitHub API license field 为 NOASSERTION；2026-11-04 有 push；约 4.3k stars | PostgreSQL 加密/压缩备份、full/incremental、WAL push/fetch、wal-show/wal-verify；支持远程/流式备份 | **备选比较**；重点验证目标存储后端、WAL 完整性、恢复时间和操作复杂度；不复制源码 |

pgBackRest 官方文档列出多种 repository backend 和版本化存储恢复能力：[配置参考](https://pgbackrest.org/configuration.html)、[用户指南](https://pgbackrest.org/user-guide.html)。WAL-G PostgreSQL 文档说明 backup-push/backup-fetch、wal-push/wal-fetch 和 WAL integrity verification：[PostgreSQL guide](https://github.com/wal-g/wal-g/blob/master/docs/PostgreSQL.md)。

**不预设 AWS。** 先按服务器所在地、可用对象存储、带宽、数据库容量、WAL 生成量、恢复时间目标（RTO）和恢复点目标（RPO）测算；可以从 POSIX/SFTP 或 S3-compatible 开始，也可比较 Azure/GCS。未完成实际 restore drill 前不能标记备份验收。

### 可观测性参考

[blueswen/fastapi-observability](https://github.com/Blueswen/fastapi-observability)（MIT；约 1.1k stars；2026-04-12 有 push）展示：
- OpenTelemetry FastAPI/HTTPX tracing；
- trace_id/span_id 日志关联；
- Prometheus metrics + exemplars；
- Tempo traces、Loki logs、Grafana dashboards；
- Docker Compose + k6/Locust 压测样例。

它是三套 FastAPI demo 的 observability 示例，不是生产 SaaS。Docker Loki logging driver 需要额外插件，不照搬；TGMate 应优先采用 OTLP collector + structured stdout + Prometheus/Grafana/Loki/Tempo 的可替换部署方式。指标避免 tenant_id、account_id、peer_id 等高基数或敏感 label；将 request_id/trace_id 放日志与 trace，metrics 只保留低基数业务维度。

### 部署/运维采用结论

- 保留 TGMate 现有 health probes、migration gate、私网 DB/Redis、loopback API、独立 worker overlay 和加密逻辑备份。
- 追加物理备份/PITR 工具评估，不急于替换现有加密逻辑备份。
- 采用 OpenTelemetry trace context 贯穿 API → Outbox/Worker → Gateway；补充 queue age、lease expiry、FloodWait、UNKNOWN、backup age、restore result 指标。
- 不引入 Loki Docker plugin 作为强依赖；不把 Grafana 示例密码、默认公开 dashboard 配置带入生产。
- 目标服务器、对象存储、RPO/RTO 尚未确定，需在容量/成本评估后做部署 PoC。

## 14. 商业模块与任务队列复核

### 商业系统

PR #27 根迁移已有：
- plan price versioning、tenant wallet accounts、billing orders、wallet ledger entries、subscription terms；
- payment provider configs、wallet top-up orders、TRON sweep jobs、payment webhook events；
- renewal intents、tenant notifications、billing email outbox；
- order idempotency keys、provider/event_id unique constraint、renewal cycle keys、受限 SQL claim/resolve functions。

[yezz123/ai-template](https://github.com/yezz123/ai-template) 的 Stripe subscription/plan limit 适合参考 entitlement 和 SaaS 管理端，但不能替换 TGMate 已确认的 USDT/钱包/动态返佣/支付 provider 业务。当前没有找到足够匹配且许可证/安全性都更合适的完整商业源码。结论：**保留 TGMate 现有商业模型，专项审查 webhook 签名、重复事件、账本不可变性、退款/续费对账，不整体移植第三方支付模块。**

### Worker / queue

TGMate 根项目已使用 PostgreSQL 迁移和受限 SQL claim functions 管理 TRON sweeps、续费 intents、billing emails；PR #28 另建 account Gateway command queue。tgmirror 的 runner/checkpoint 设计可借鉴，但目前没有证据需要再引入 Celery/RQ/独立 Redis broker。结论：先复用 PostgreSQL 持久队列与 Gateway command queue，压测确认吞吐或调度瓶颈后再评估新 broker。

## 15. 更新后的研究状态

- 已完成：Telegram clone 参考源码、Bot API 原生复制语义、SaaS/RBAC 模板、Clone Engine/Gateway ADR、TGMate 现有备份/部署/商业迁移核对。
- 正在执行：补齐各候选项目版本/许可证/测试证据，完善 TGMate 模块到目录/表/命令的逐项映射。
- 尚未完成：正式前端选型、备份/PITR PoC、可观测性 PoC、完整接口映射、产品代码移植、TGMate 自动化测试和真实 Telegram 验收。
- 当前研究/设计完成度：65%。这不是产品代码完成率，也不是上线完成率。

## 16. TGMate 目标目录、现有测试与可复用边界

以下映射基于 PR #27 的根文件树，不把 `workstreams/tgmate-v2` 旧 workstream 当作根运行时：

| TGMate 模块 | 当前根目录/迁移/测试 | 可复用点 | 缺口与动作 |
|---|---|---|---|
| Auth / Tenant / RBAC | `app/auth/*`；`migrations/003_auth.sql`、`004_membership.sql`、`005_entitlement_overrides.sql`；`tests/test_auth_security.py` | 保留现有认证和 tenant context；参考 ai-template 的 API key hash、invitation token hash、scope subset | 逐入口核对撤权、角色变更和 RLS；不要把 JWT scope 当唯一权限真相 |
| MTProto Account / Session | `app/telegram/account_persistence.py`、`authorization.py`、`client_factory.py`、`login_manager.py`、`gateway_material.py`；`app/security/session_vault.py`、`session_bundle.py` | 保留 Session Vault 和 Gateway 单连接所有权 | Gateway managed client 当前关闭 updates 和重试；更新接收要有明确设计 |
| MTProto Gateway | `app/telegram/gateway_leases.py`、`gateway_router.py`、`gateway_routes.py`、`gateway_runtime.py`、`gateway_supervisor.py`；`app/workers/mtproto.py`；`migrations/011_gateway_routes.sql` | 借鉴 tgmirror Gateway protocol，但 Gateway 仍由 TGMate lease/fencing 约束 | PR #28 只实现 text preflight command；增加 typed read/forward/reference/update-reconcile operations 前必须先完成 PR #27/#28 审查 |
| Clone Engine | 根文件树没有 `app/clone/`，没有 clone route/message map/event inbox migration，也没有 `tests/test_clone_*` | 新建纯业务 Engine/Planner/Batcher/Reconciler；借鉴 tgmirror 的分层、album Unit 和 pending batch | 需新增 Postgres RLS schema、Gateway commands、history/live inbox、random_id map；TGM-006 仍未实现 |
| Bot API Worker | `app/workers/bot.py`、`app/telegram/bot_rate_limits.py`、`app/bot_main.py`；`tests/test_bot_worker.py`、`test_bot_ingress.py`、`test_bot_database.py` | 保留独立 Bot worker、restricted DB role、tenant allowlist、rate limiter；按官方 Bot API 增加 copy/forward adapter | 根 worker 尚无可核实的 copyMessage(s)/forwardMessage(s) 媒体执行实现；Bot API UNKNOWN 无 random_id，必须 fail closed |
| Relay 控制 | `app/integration/relay_control.py`、`docs/RELAY_CONTROL.md`；`tests/test_relay_control.py`、`test_relay_control_database.py` | 复用 expected_generation + idempotency key + transactional audit 的状态控制模式给 clone route pause/resume | 这只是业务会话状态控制，不是 Telegram channel clone 或 message sync |
| Commerce / Wallet | `app/billing/automation.py`、`commerce.py`、`payment_gateway.py`、`payments.py`、`service.py`、`tron_watcher.py`；`app/workers/billing.py`；`migrations/005–009`；`tests/test_billing_*` | 保留现有钱包账本、支付 webhook、TRON sweep、renewal 和邮件 outbox | 不用 Stripe starter 替换业务；专项做账本不可变、webhook 重放、退款和续费对账 |
| Frontend / Admin | PR #27 根树没有 `frontend/`、`web/` 或 `admin/` 目录 | 参考 ai-template 的双 portal 和 full-stack-fastapi-template 的 OpenAPI client/E2E | TGM-012 需求/UX 定稿后再选 Next.js 或 React/Vite；本轮不擅自创建 UI |
| Backup / Restore | `scripts/database_backup.py`、`scripts/encrypted_backup.py`；`tests/test_database_backup.py`、`test_encrypted_backup.py`；`docs/DATABASE_RECOVERY.md` | 保留加密逻辑备份；比较 pgBackRest/WAL-G 做物理备份和 PITR | 缺 KMS、异地存储、retention、自动调度和真实 restore drill |
| Health / Observability | `app/health.py`；`tests/test_health.py`；`docs/validation/health-probes.md` | 保留 liveness/readiness；参考 OpenTelemetry/Prometheus/Grafana | 根树未发现 metrics/tracing 模块；增加 trace propagation、低基数 metrics 和 Gateway/Worker correlation |
| Deployment | `docker-compose.yml`、`docker-compose.workers.yml`、`scripts/migrate.py`、`scripts/bootstrap_combined.py`、`docs/takeover/DEPLOYMENT.md` | 保留 migrate gate、DB/Redis 私网、独立 worker overlay、受限角色 | 目标服务器、TLS ingress、对象存储、容量和成本尚未验收 |

### 现有测试树盘点
根树已有 auth、billing、bot、business reads、gateway、health、identity、login、migration、backup、proxy、relay、session、Telegram resources、worker 等测试文件。未发现 clone-specific test 文件，也未发现 frontend test tree。此处只确认测试文件存在，不代表本轮执行或通过。

### 当前最小可复用方案
1. 不整体复制任何成品仓库。
2. 以 tgmirror 的纯 Engine/Gateway/Store 分层作为 Clone 模块结构参考。
3. 以 TGMate 自己的 RLS、Gateway lease/fencing、持久 command queue 和审计模式作为安全/运行边界。
4. SaaS 的 API key/invitation/scope 参考 ai-template；前端在 TGM-012 UX 确认后只选一个模板路线。
5. 备份继续保留 AES-GCM 逻辑备份；通过 pgBackRest/WAL-G 对照确定 PITR 方案；可观测性先补 OTel trace/metrics，不引入不必要的消息 broker。


### tgmirror v0.3.0 的实网验证补充（2026-11-05）

本次对照 GitHub README、roadmap 和 `docs/07-inspect-transform.md`，修正了此前仅依据 PyPI 页面得到的过时版本判断。v0.3.0 不只是一个基础历史克隆 CLI：它已有手动 delta、sync-changes（编辑/删除同步）、topic 操作、备份恢复、filter/transform、TUI 和 Docker。主要 clone/change/topic 功能有真实账号试用记录；但测试套件本身仍使用 FakeGateway，且 README 明确列出若干未实测边界。

对 TGMate 的新结论：
- tgmirror 是当前首选的**克隆业务流程和持久化状态模型**参考，优先级高于早期简单克隆脚本。
- 可以进一步研究它的 `sync-changes`、`edit`、`delete`、topic map 和 preflight/confirmation plan。
- 不采纳它的 `reupload`、`restore from disk`、protected-content 下载上传路径。
- 不采纳它的 UNKNOWN 恢复策略：其 `msg_map` 未持久化每条 operation 的 random_id，重启恢复主要依赖目标端尾部消息形态；TGMate 必须用稳定 random_id + UpdateMessageID/update difference，无法确认时保持 UNKNOWN。
- 它的 `sync-changes` 是显式增量同步操作，不等同于持续实时 update subscription；TGMate 仍需设计同一 Gateway 内的持久 update capture。

### telemirror 实时同步源码深审补充

[telemirror](https://github.com/khoben/telemirror) 的 `telemirror/mirroring.py` 使用 Telethon 的 NewMessage/Album/Edit/Delete 事件处理器；`storage.py` 提供 PostgreSQL 与 InMemoryDatabase 两种消息映射存储；`messagefilters` 是可配置的过滤器链，方向配置可实现多源多目标和 topic-to-topic 路由。

**可借鉴的概念：**
- 事件入口按 source peer → 多个 destination peer 分发。
- 每条源消息分别记录多个目标映射，reply 时通过映射查目标 reply ID。
- 编辑和删除事件通过映射传播；不同 route 可单独配置是否传播。
- Filter pipeline 按顺序执行，允许 DISCARD/CONTINUE 和文本/实体变换。
- Postgres storage 抽象与内存实现分离，便于 FakeGateway/测试替身。

**明确拒绝的部分：**
- 许可证为 GPL-3.0，不复制代码。
- `RestrictSavingContentBypassFilter` 明确演示下载受保护媒体、重新上传并替换媒体对象；README 也警告受保护来源可能导致账号封禁。TGMate 不使用该路径，也不使用任何同类绕过过滤器。
- Session String 通过环境变量注入，示例提示不要用自己的账号；与 TGMate 的加密 Session Vault、租户 RLS 和 Gateway lease/fencing 不匹配。
- Postgres message map 没有 tenant_id / RLS；异常装饰器记录后吞掉错误，缺少持久失败重试和 UNKNOWN 对账。
- 事件 handler 直接持有 TelethonClient 并直接发送，不能照搬到 TGMate；必须迁移为 Gateway-owned event capture → durable Inbox → CloneWorker。

**采用决定：** telemirror 是实时 New/Edit/Delete、many-to-many route 和 topic mapping 的概念参考；由于 GPL 和 protected-content bypass 代码，绝不直接移植。TGMate 的实时同步实现应重新编写在自身 Gateway + PostgreSQL RLS 架构内。

## 17. 前端模板专项比较与 TGMate 初步选型

### 候选 A：FastAPI Full Stack Template

[fastapi/full-stack-fastapi-template](https://github.com/fastapi/full-stack-fastapi-template)（MIT）：
- FastAPI + PostgreSQL 后端；React 19 + TypeScript + Vite 8 前端。
- Tailwind CSS v4 + shadcn/ui / Radix UI。
- TanStack Router、TanStack Query、TanStack Table。
- `@hey-api/openapi-ts` 根据 FastAPI OpenAPI 生成 TypeScript SDK/client。
- Playwright E2E；Pytest backend；GitHub Actions 覆盖 backend、Docker Compose、Playwright。
- React SPA 与 FastAPI 同域部署，可通过 Compose/Traefik 自托管。

**可复用：** 前端工程结构、OpenAPI client generator、路由/查询/表格、Playwright E2E、CI 测试门禁、同域反向代理模式。

**不复用：** SQLModel/ORM、JWT 登录、用户模型、CRUD/API routes、Alembic schema、部署 secrets 和默认 auth，因为 TGMate 已有独立数据库模型、PostgreSQL RLS、Session Vault、Gateway 和 worker 部署边界。

### 候选 B：yezz123/ai-template

[yezz123/ai-template](https://github.com/yezz123/ai-template)（MIT）：
- FastAPI + authx + SQLAlchemy async + PostgreSQL 17 + Redis。
- 两个独立 Next.js 16 / React 19 应用：tenant-facing frontend 和 super-admin portal。
- Tailwind v4、Framer Motion、Zod；各自 Vitest + Testing Library。
- Compose + Nginx 同域入口；backend/frontend/admin 分离 CI。
- 组织/成员/邀请、API key、audit、feature flags 和 plan limit。

**可复用：** tenant/admin portal 信息架构、组织切换 UX、成员/邀请/API Key 页面、feature flags UI、双 portal 导航结构。

**不复用：** authx/SQLAlchemy/Postgres schema、org_id 应用层隔离、JWT 中 scopes 作为唯一授权依据、Stripe billing、默认 Cookie/CSRF 方案；必须接 TGMate 自己的 API contract/RLS 和认证模型。该模板两个 Next.js 应用增加构建、运行、发布和版本维护面，且仓库未展示与 Full Stack FastAPI Template 同等的 Playwright E2E 路径。

### 初步选择（可执行建议）

**优先以 FastAPI Full Stack Template 的 React/Vite 前端层作为 TGMate UI 起点；从 ai-template 只借鉴租户后台与超级管理员后台的信息架构。**

理由：
1. TGMate 已经是 FastAPI 后端；OpenAPI 生成 client 可避免前后端重复手写 DTO/API path。
2. React/Vite 静态产物可由现有 API/反向代理同域服务，避免额外运行两套 Next.js SSR 服务。
3. 单 SPA 通过 route groups/layouts 区分 tenant app 与 operator/admin；所有权限仍由后端鉴权/RLS 执行，前端隐藏按钮不是安全边界。
4. Playwright E2E 可直接覆盖登录、租户切换、成员/套餐、频道/账号管理、群发和克隆控制台关键路径。
5. TGMate 当前没有 frontend 目录；在 TGM-012 UX/页面清单未定稿前，不创建前端代码，不复制模板 backend。

### 需要在 TGM-012 明确的页面边界

- Tenant：登录/账户、组织与成员、套餐/账单/钱包、Telegram 账号与授权、代理、频道路由/克隆、Bot/群发、任务历史/审计。
- Operator/Super Admin：租户检索与状态、套餐/权益配置、支付渠道与回调审计、Gateway/Worker 状态、全局任务/错误/安全事件。
- 所有写操作经 TGMate API，tenant_id 不由浏览器作为可信输入；后端从认证上下文确定租户并执行 RLS。
- OpenAPI schema 是前端 client 的唯一生成来源；禁止手工维护第二份 API 类型。
- Playwright 重点测试跨租户访问拒绝、角色权限、暂停/恢复、重复提交、UNKNOWN 结果呈现和关键业务流程。

**选型状态：** 前端框架建议已形成；正式 UI 页面/交互仍依赖 TGM-012 产品 UX 决策。没有创建前端代码或声称前端已开发。


## 18. 备份/PITR 与可观测性候选最新核验（2026-11-05）

### PostgreSQL 物理备份与 PITR

本轮直接核对项目官方仓库、许可证与 release：

| 候选 | 最新核验版本 | 许可证 | 能力与取舍 |
|---|---|---|---|
| [pgBackRest](https://github.com/pgbackrest/pgbackrest) | v2.60.0，2026-10-18 | MIT | PostgreSQL 专用；full/differential/incremental、并行备份恢复、多 repository、加密、校验和、S3/Azure/GCS/SFTP、WAL archive 与 retention。更贴合 TGMate 当前单一 PostgreSQL 栈。 |
| [WAL-G](https://github.com/wal-g/wal-g) | v3.0.9，2026-08-20 | Apache-2.0 | Go 工具；PostgreSQL WAL archive、backup-push/fetch、restore/PITR 和多种对象存储，也覆盖其他数据库。适合作为跨数据库/统一备份工具的替代评估。 |

**决策：优先以 pgBackRest 作为 PostgreSQL PITR PoC 候选，WAL-G 保留为备选；不把任一工具的内部源码复制进 Python API。** 采用成熟备份工具作为独立运维组件，减少自研 WAL/恢复协议风险。该选择不代表已经部署或通过恢复验收。

PoC 需要先明确服务器和存储条件，再完成：
1. 选择本地/POSIX/SFTP 或 S3-compatible repository；不预设 AWS。
2. 配置 base backup、WAL archive、加密密钥和 retention。
3. 记录目标 RPO/RTO 与备份窗口、WAL 产生速率、带宽和存储成本。
4. 在隔离环境执行完整 restore 与 timestamp PITR。
5. 验证 API readiness、租户 RLS、账本余额/不可变性、加密 Session 数据可读性和业务回归。
6. 记录实际恢复耗时、恢复点、校验和与失败回滚步骤。

pgBackRest v2.60.0 的 release note 特别说明：restore 时遇到无效 page checksum 会停止并要求重新执行恢复；这类 fail-closed 行为应纳入恢复演练和验收，而不是将“备份命令成功”当成“可恢复”。

### OpenTelemetry 与 FastAPI 可观测性

- OpenTelemetry Python API/SDK 与 OTLP HTTP exporter 当前稳定版本核验为 1.45.2（Apache-2.0，2026-10-29 发布）。
- OpenTelemetry FastAPI contrib instrumentation 当前为 0.66b2，仍标记 Beta；本轮不将 Beta contrib middleware 作为运行时硬依赖。
- [blueswen/fastapi-observability](https://github.com/Blueswen/fastapi-observability) 为 MIT 示例，演示 OTel traces、Prometheus metrics、Tempo/Loki/Grafana 关联；其 Loki Docker logging plugin、demo 默认账号不适合直接带入 TGMate。
- 第一阶段采用官方稳定 API/SDK + OTLP HTTP exporter，自建最小 ASGI request middleware；只记录 method、route template、status、service/environment。health probes 排除；不采集 query、body、headers、tenant/account/peer ID 或 exception message。
- 不自动 instrument asyncpg/Redis，避免 SQL bind values、Redis keys 泄露；等 attribute redaction 审查后再加。
- 代码 PoC 已在独立 PR [#42](https://github.com/xe4716-svg/Tgsa/pull/42) 开发，目标 PR #27 候选分支。PR #42 的 exact-head CI 仍待完成；内存 exporter 单元测试不能代替真实 Collector 部署。
- 后续需单独补 worker/Gateway trace context propagation、queue/lease/FloodWait/UNKNOWN 指标、structured log correlation 和 Collector/Tempo/Prometheus/Grafana 部署验收。

### 当前边界

本轮完成候选版本/许可证核验与技术决策；没有配置生产备份、没有运行 PITR restore drill、没有部署 Collector，也没有把示例项目代码复制到产品。目标服务器、对象存储、RPO/RTO 和运维成本仍未确定。


## 2026-11-06｜可观测性模块代码整合进展

### API tracing PoC — PR #42

- 独立分支 `feat/tgm-022-otel-api-tracing`；exact HEAD `14bb18624201c40b6f740e426912144d9cb16598`；API-only W3C tracing，opt-in，隐私字段排除。
- Root Integration run 42061360065：3/3 jobs success（311 passed / 23 skipped；notification/login 27 passed；preserved V2 208 passed）；Billing unit run 42061360407：1/1 success。
- PR #42 Ready for Review；GitHub formal Review API 尚无非作者 Review；未合并。

### API metrics PoC — PR #45（PR #42 的 stacked follow-up）

- 独立分支 `feat/tgm-022-otel-api-metrics`，基于 PR #42 exact HEAD，目标 base 为 PR #42 分支；PR #45 当前 Draft。
- exact HEAD：`7b9c872be6ec328525cf644c4437b389141e3629`；4 files，+251/-5。
- 新增 opt-in OTLP HTTP metrics：`http.server.request.count`、`http.server.request.duration`；标签仅 normalized method、FastAPI route template 或固定 `_unmatched`、HTTP status。未知 method 映射 OTHER；排除 health probes；不采集原始 URL/query/header/tenant/account/peer/exception message。
- 默认不配置 endpoint 时关闭；API shutdown 时 flush。没有数据库/Redis 自动埋点、Worker/Gateway/业务指标、Collector 部署或生产配置。
- 新增 InMemoryMetricReader 测试：默认关闭、counter/histogram、route label、隐私排除、health probe 排除和 method bounded label。
- Exact-head Root Integration run 42082645858：container 与 preserved-v2 已 success；Root test job 仍运行中，尚未报告全绿。PR #45 暂不转 Ready、不合并，等待最终 CI 结论。

### 研究整合状态

- 成熟源码借鉴的第一个实际可测试工程切片是 API tracing（PR #42）；第二个切片是 API metrics（PR #45）。两者使用 OpenTelemetry 官方 API/SDK/OTLP exporter，不复制参考项目代码。
- 下一步：确认 PR #45 完整 CI；通过后请求独立 Review。随后评估 worker/Gateway trace context propagation 与 structured log correlation，必须使用持久队列 context 字段且不得记录 tenant/account/session/peer 等敏感值。
- 以上为 API 层 PoC，不等于生产 Collector 部署、全链路 metrics/tracing 或 Telegram 模块验收。
