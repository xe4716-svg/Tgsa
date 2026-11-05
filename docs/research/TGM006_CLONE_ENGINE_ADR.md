# TGM-006 频道克隆引擎与 Gateway 命令契约（研究 ADR）

- 状态：Proposed / 研究设计，未实现、未验收
- 关联：TGM-022、TGM-006、TGM-005、TGM-004、TGM-003
- 审查日期：2026-11-05
- 目标仓库基线：Tgsa PR #27 的 `work/v2-takeover`；Gateway 命令传输参考 PR #28
- 本文不创建数据库迁移、不修改运行配置、不发送真实 Telegram 消息。

## 1. 设计结论

TGMate 不应把任何单账号 CLI 克隆器直接嵌入 SaaS。采用模块级架构：

- **Clone Engine**：纯业务编排，负责历史分页、消息单元、相册聚合、过滤、cursor、实时事件合并；不 import Telethon。
- **Clone Store**：PostgreSQL + tenant_id + FORCE RLS，负责 route、run、batch、逐消息映射、更新事件和持久 cursor。
- **Telegram Gateway**：唯一持有 TelethonClient/Session 的进程。所有 MTProto 读取、转发、媒体引用和 update 对账都通过它的窄接口执行。
- **Outbox / Gateway command queue**：复用 TGMate 的持久任务和命令模式；扩展必须单独迁移、单独测试，不在业务 worker 中创建第二个 TelethonClient。
- **Bot API**：保持独立通道，使用 Bot API copy/forward 方法，不共享 MTProto Session 或 random_id 语义。

首轮源码研究中，`tgmirror` 的 Gateway/Engine/Store 分层、pending batch、pair cursor、msg_map、album Unit、topic map 最值得借鉴；其 SQLite、reupload fallback 和基于目标尾部形态的 resend 不采用。

## 2. TGMate 当前真实基线

### PR #27：账号和 Gateway 生命周期
根迁移中已有：
- `telegram_accounts`、加密 `telegram_sessions`；
- `account_gateway_leases` 和 `account_gateway_routes`；
- account identity、proxy binding、security events、audit logs；
- RLS/FORCE RLS 及 tenant scope policy；
- membership、subscription、wallet、payment、renewal、notification 等业务迁移。

`GatewayRunner` 通过 lease/fencing 控制一个 account 的连接生命周期；`GatewaySupervisor` 对显式 tenant allowlist 做全局/租户容量控制和轮转调度，不自动重连。

当前 `client_factory.create_client(managed=True)` 会关闭自动重连、请求重试和连接重试，并设置 `receive_updates=False`。因此现在还没有实时 New/Edit/Delete/UpdateMessageID 事件接收链路。要做实时同步，必须在同一个 Gateway-owned client 上明确启用受控 update 接收和持久化处理，不能另建旁路客户端。

### PR #28：持久 Gateway command transport
PR #28 仍为 Draft，base 是 PR #27 的 head。它提供：
- `gateway_account_commands` 和 append-only `gateway_account_command_events`；
- tenant/account/route_generation/operation_key 绑定；
- FORCE RLS、独立 submitter/executor 数据库角色和受限 SQL transition functions；
- dispatch_started 前可安全回收；dispatch_started 后 worker 丢失会标记 UNKNOWN，不自动重发；
- executor 通过注入 sender 执行，不创建 client。

当前 operation kind 仅允许 `preflight_send_text`，payload 仅允许 text；sender 不是生产 Telegram sender。故此基础尚不能执行 forward、media reference、history read 或 update reconciliation。

### 旧 workstream 不等于根产品运行时
PR #27 中的 `workstreams/tgmate-v2/app/bot_gateway.py` 和 `broadcast.py` 属于独立旧 workstream。PR #28 的 Gateway contract 明确指出旧 `telegram_adapters.py` 会从 legacy credential 创建 TelethonClient，尚未接入根 Gateway。不能把旧 workstream 的 Inbox/Outbox 和 text-only plan 描述为已在根产品运行。

## 3. 建议模块边界

```text
API / Control Plane
  └── CloneRouteService (tenant scoped)
        ├── CloneRouteStore (PostgreSQL + RLS)
        └── CloneScheduler / Outbox
              └── CloneWorker (no Telethon import)
                    ├── HistoryPlanner
                    ├── AlbumAssembler
                    ├── FilterPipeline (versioned pure functions)
                    ├── BatchPlanner
                    ├── Reconciler
                    └── TelegramGatewayPort (typed internal contract)
                          └── GatewayRunner-owned TelethonClient
                                ├── history/read methods
                                ├── ForwardMessagesRequest
                                ├── media-reference send
                                └── update receiver / getDifference
```

业务 worker 只处理 DTO、peer/message IDs 和 opaque media reference token；不得获得 TelethonClient、SessionString、明文 Session、原始 proxy secret 或可执行任意 RPC 的能力。

## 4. 数据模型建议（待 TGM-003/PR #27 schema review 后定稿）

以下是逻辑实体，不是已批准迁移：

### clone_routes
- `id`, `tenant_id`, `account_id`
- `source_peer_id`, `destination_peer_id`
- `status`: draft / active / paused / blocked / completed
- `history_mode`, `live_sync_enabled`, `propagate_edits`, `propagate_deletes`
- `start_message_id`, `history_cursor_message_id`, `last_settled_message_id`
- `route_generation`, `filter_profile_version`, timestamps

### clone_runs / clone_batches
- 每次历史回填、实时追赶和人工恢复形成独立 run。
- Batch 保存严格递增的 source IDs、first/last ID、状态、尝试代次、dispatch 时间、remote result、error code。
- Batch 与 Gateway command 通过稳定 operation_key 关联。
- batch cursor 只能在其中每条消息都已明确映射、跳过或失败结案后推进；UNKNOWN 不算结案。

### clone_batch_items / clone_message_maps
- 每个 source message item 预先持久化一个 random_id。
- 映射字段：tenant_id、route_id、source_peer_id、source_message_id、destination_peer_id、destination_message_id、random_id、grouped_id、topic_id、source_edit_date、status。
- 唯一约束至少包括 (tenant_id, route_id, source_peer_id, source_message_id) 和 (account_id, random_id)。
- random_id 在同一 Telegram account 下必须稳定，不能在重试时重新生成，也不能被另一个消息复用。

### clone_event_inbox / account_update_state
- 持久化 New/Edit/Delete/UpdateMessageID 等事件及 pts/date/qts 或等价更新游标。
- 事件 key 必须可去重；payload 只存必要字段和引用，不存 Session/secret。
- 更新事件处理成功与 cursor 前进在同一数据库事务内完成。

### RLS / FK
- 所有租户业务表带 tenant_id。
- 复合 FK 同时包含 tenant_id 和 parent id，防止跨租户引用。
- ENABLE + FORCE ROW LEVEL SECURITY；使用既有 tenant context。
- account/session 表只能由 Gateway 受限角色读取；CloneWorker 不能读取 Session。

## 5. Gateway 命令契约建议

当前 PR #28 的 command table 是 text-only。未来需另开迁移扩展 allowlist 和 payload schema。建议先定义以下 typed operations：

| Operation | 类型 | 必需输入 | 输出 |
|---|---|---|---|
| `read_history_page` | 只读 | account, source_peer, min/max message id, bounded limit | MessageDTO 列表、snapshot cursor |
| `read_messages` | 只读 | source_peer, message_ids | DTO、grouped_id、media kind、protected flag、opaque media reference |
| `forward_batch` | 写入 | source/destination peer、严格递增 message IDs、稳定 random_id vector、topic | per-item result、UpdateMessageID map |
| `send_media_reference` | 写入 | source_peer/message_id、destination、稳定 random_id、可选 caption transform | target message id 或明确拒绝 |
| `reconcile_updates` | 对账 | account update cursor、operation keys/random_ids | UpdateMessageID、new/edit/delete events |

要求：
1. 每个 operation 有严格 allowlist 和 Pydantic/SQL payload validation；不接受 method name 或任意 RPC args。
2. 所有写操作必须持久化 pending 和 dispatch_started；dispatch_started 后异常必须 UNKNOWN。
3. 读操作可安全重试，但同样必须 tenant/account/route_generation 验证并限制结果大小。
4. Gateway 进程是唯一 Telethon client owner；业务 worker 通过持久 command/query contract 获取 DTO。
5. 每个 account 初期最多一个 in-flight write；多 route 在 account 内做公平调度。
6. route_generation、lease fencing token、session/proxy material revision 必须在 RPC 前复核。
7. 不把原始 Telethon Message 对象或 file_reference 字节串直接交给业务 worker；媒体引用只作为 Gateway 内部 opaque handle。

## 6. 原生转发、媒体引用与可靠性

### MTProto 原生转发
优先使用 `messages.forwardMessages`（Telethon raw `ForwardMessagesRequest`）：
- message IDs 必须严格排序；批次不跨 source peer/destination/topic。
- 每条消息在 dispatch 前持久化 random_id，重试沿用同一 random_id vector。
- 解析响应中的 `UpdateMessageID(random_id, id)`，持久化 source→destination 映射。
- 如果 RPC 响应丢失，通过 Gateway update receiver / `updates.getDifference` 对账。
- 不能证明成功或失败时保持 UNKNOWN 并阻断该 route；禁止生成新 random_id 后重发。

官方依据：
- [messages.forwardMessages](https://core.telegram.org/method/messages.forwardMessages)
- [Working with Updates](https://core.telegram.org/api/updates)
- [updates.getDifference](https://core.telegram.org/method/updates.getDifference)

### 媒体引用复用
仅在 Telegram 明确允许复制且来源身份有权读取时，使用现有 message media/file reference 进行 server-side reference send：
- 不把媒体下载到 TGMate；
- 不上传文件 bytes；
- file_reference 过期时，只能由 Gateway 使用同一授权 account 重新读取源消息刷新一次；
- 刷新后仍不可用则记录明确失败/跳过，绝不 fallback 到 download/re-upload；
- source 有 protected/noforwards 标记、权限未知、Telegram 返回转发受限或 API 禁止复制时，fail closed。不能用 media-reference API 绕过内容保护。

### 相册
- 以 `grouped_id` 组装完整 Unit；不跨批次拆分。
- 每个 album item 都有独立 random_id 和 source→destination mapping。
- 部分结果必须逐条对账；不能只记录“相册成功”或整体推进 cursor。
- history page 边界切到相册中间时，Gateway 再读取同 grouped_id 的完整消息组后再 dispatch。

### UNKNOWN 状态
- MTProto：依靠持久 random_id + UpdateMessageID + update difference 对账。
- Bot API：copy/forward 没有 random_id 参数；外发请求超时且无可验证结果时，保持 UNKNOWN，不盲目重发。
- UNKNOWN 不可通过“目标末尾有几条消息/媒体类型看起来相似”自动推断成功。

## 7. 历史回填与实时同步无缝衔接

1. 建立 route 并启用同一 Gateway 的 update capture。
2. 记录 history snapshot max_message_id 和 update cursor。
3. 先把 live events 写入持久 clone_event_inbox，不在内存队列中作为唯一副本。
4. 按 source message ID 递增回填历史至 snapshot max ID。
5. 历史回填期间出现的 live event 通过 message_map / event key 去重。
6. 回填完成后按 peer/message/pts 顺序 drain inbox，再进入 live steady state。
7. 每条 route 独立 cursor、pause/resume 和 retry policy；一条 route 故障不能阻塞同账号其他 route 的管理控制。
8. `receive_updates=False` 是当前 managed Gateway 的默认行为；必须通过显式受控配置打开 update 接收，且不能另建第二个 TelethonClient。

## 8. Bot API 独立路径

Bot API 使用 `forwardMessages` / `copyMessages`（1–100 个递增 message IDs，保持 album grouping）或 `copyMessage` 的 caption override。受保护内容和不可复制类型必须 fail closed。

Bot API 没有 MTProto random_id。内部 Outbox key 不能提供 Telegram 端 exactly-once。对未知投递结果暂停该 target，利用可观察到的 Bot updates 做有限对账；仍不能确定时交给人工处理，不自动重复发送。

## 9. 限流、失败与模块独立性

- FloodWait 使用 Telegram 返回的秒数，持久化到 account Gateway route；等待期间不释放给另一个 sender、不切换账号、不切换代理。
- PeerFlood、权限变化、来源被删除、目标权限不足使用独立错误分类。
- 账号级 lease 与 route 级 cursor 分离；账号只有一个 Gateway owner，多个 route 通过公平调度共享。
- 每个 route 可独立 pause/resume/disable；Clone 模块可独立维护，不影响 Bot、商业或其他 Telegram 业务。
- 只记录 sanitized error code 和必要审计字段，不保存原始 RPC/消息内容。

## 10. 测试矩阵与验收门槛

### 单元测试
- History pagination 不重不漏；cursor 只在结案后推进。
- Album grouped_id 跨 page/batch 边界完整。
- Filter version 固定；纯变换不触碰 media bytes。
- random_id 在重复提交、进程重启和 retry 中保持稳定。
- UpdateMessageID 映射正确；重复 update 幂等。

### PostgreSQL 集成测试
- FORCE RLS、跨 tenant 读写拒绝、复合 FK。
- 相同 operation_key 幂等；不同 payload 冲突拒绝。
- 并发 claim 只有一个 Gateway owner。
- crash before dispatch 可回收；crash after dispatch 变 UNKNOWN。
- UNKNOWN 阻断同 route 后续 batch；不自动重发。
- route generation 变化、lease 过期、session material 变化拒绝执行。

### Telegram sandbox 测试
- 仅用授权测试账号和自有测试频道。
- 文字、单图、视频、文件、相册、论坛话题、reply mapping。
- native forward、媒体引用刷新、protected-content 拒绝。
- FloodWait、断网、RPC 响应丢失、进程杀死和重启。
- history/live 并发时无重复、无遗漏。
- Bot API copy/forward 的 UNKNOWN 处理。

### 合并门槛
- PR #27、#28 先通过各自审查并明确基线。
- TGM-001/003/004/005 依赖解除，数据库模型和 Gateway contract 确认。
- Clone 功能独立 branch + Draft PR；真实 PostgreSQL integration tests 通过。
- Telegram sandbox 证据单独记录；FakeGateway 通过不能代替真实 Telegram 验收。
- 未通过以上门槛不得进入 main，不得标记为产品完成。

## 11. 明确不做

- 不下载媒体再上传，不设置隐藏回退，不让 Agent 帮忙上传。
- 不绕过受保护内容或来源权限。
- 不在 CloneWorker 里创建 TelethonClient、读取 Session 或自行选代理。
- 不使用启发式猜测代替 random_id/update 对账。
- 不把旧 workstream 的 legacy bot/account adapters 当作已整合到 root Gateway。
- 本 ADR 不代表代码已开发、测试或上线。
