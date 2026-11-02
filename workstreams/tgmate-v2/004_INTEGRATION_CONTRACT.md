# 004 双向机器人：客服中转 MVP

状态：设计与 PostgreSQL SQL 草稿已交付；未执行数据库迁移、未实现运行服务。本文取代旧的双人配对/群镜像范围。

用户主动私聊 Bot，服务将消息复制至已授权运营者私聊；运营者对该消息点击回复，服务核验发送人、聊天、reply_to 映射后送回原用户。用户无需邀请配对。Bot 为独立联系入口，不解除账号限制。禁止主动联系从未发起会话的用户。

## 数据映射
relay_configs 绑定 bidirectional 实例、同一Bot的send执行绑定及运营者Telegram ID；默认暂停。relay_conversations 每Bot每客户一条，记录首次主动联系时间及隐私告知版本；支持暂停、屏蔽、关闭。relay_operations 保存入站事件唯一键、方向、来源坐标、不可变内容引用、配置generation、回复关联和投递结果。客户回复必须关联本会话已送达运营者的消息。
001核心、002目标和媒体、003群管理仍为前置基线；不修改其原文或产品目录。任务用task_registry的bidirectional，调度复用001租约及outbox；本迁移仅增加领域表，不另建全局调度器。

## 必须实现的服务事务
1. 入站验签并确定Bot和租户；只接受private聊天及真实用户消息。inbox_events以source_system及external_event_id去重，拒绝客户冒充运营者。首次联系先告知隐私/使用规则，再创建会话；运营者必须经平台认证并验证Telegram身份才能配置，不能只填写一个ID。
2. 收件、领域操作及outbox_events同事务提交。aggregate_type=relay_operation，aggregate_id=操作UUID，event_type=relay.dispatch，idempotency_key=relay:<operation UUID>，payload只含操作引用；不得包含Bot Token或明文媒体。双事件重复不重复排队。
3. 运营者回复按当前Bot、运营者chat及reply_to_message_id查找已成功to_operator操作，禁止接受客户端自定customer目标。查不到、多个匹配或已屏蔽均拒绝；不将无回复关联的运营者普通消息群发。
4. worker领取共享租约，加载操作/会话/配置，核验generation、状态、Bot与绑定授权，目标从会话或配置派生。发送前再次检查；暂停与撤销不能保证阻止已在网络中的请求。网络调用不持数据库事务锁。
5. 成功记录目标消息ID并更新共享投递记录；超时结果不明进入uncertain，不直接重发。限流保留retry_after。成功后本地提交前崩溃须人工对账，不能宣称exactly-once。运营者身份切换旧映射停止回复，既有已成功记录保留。
6. 媒体使用租户隔离内容库、受控保留期；默认copy而非暴露客户来源的forward；联系方式等消息需明确提示。日志仅记安全引用；禁用公开展示客户Telegram ID。屏蔽、举报与客服授权变更写audit_logs。

## MVP与后续
MVP仅运营者私聊、文本/允许的基础媒体、回复映射、屏蔽、暂停、状态与失败反馈。群/Topics、多客服权限及分配后续实施；不使用002仅支持group/supergroup的目录冒充私聊目录。CRM/AI客服复用会话，另行分阶段实现。任意群镜像与历史克隆属于005。

## 静态与运行边界
SQL包含三张租户表、复合外键、产品校验、RLS及默认撤销PUBLIC表权限。没有角色授权脚本，部署前需按真实运行角色最小授权。SQL触发器不能替代Webhook身份校验、实时权限检查、租约fencing、配额事务和不确定发送对账。当前未实现以上服务，不能上线。
隔离库待验证：001→004全链迁移/失败回滚、跨租户读写、错误机器人类型/绑定、重复事件、串客回复、客户屏蔽、身份切换、暂停在途、限流、发送后崩溃、媒体失败、过期租约。迁移未执行，以上均非通过项。

## 本轮验证与字段补充
SQL现在将target_chat_id、conversation_generation作为不可变发送快照；会话状态变更增加generation，旧操作不得在恢复后直接续发。运营者回复查找需额外校验会话generation，不能只看config generation。操作按pending→sending→结果推进，uncertain不得自动重发。外层SQL及三段PL/pgSQL函数已通过解析；不是数据库执行结果。离线原型10项测试通过，不代表持久化/网络集成通过。完整边界见DELIVERY_STATUS.md。
