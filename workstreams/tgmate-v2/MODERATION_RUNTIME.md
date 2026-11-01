# 003 群管理运行进度

本轮交付：真实003表结构上的消息收件、规则/违规计数、动作执行及人工审核。按钮欢迎验证及恢复流程已实现；申诉及统一Bot接入尚未完成，不能宣称群管理模块整体完成。

## 已实现
- 已绑定群组的Webhook密钥认证、群坐标校验、重复事件去重、加密消息引用。匿名管理员和Bot普通消息不冒充真实用户处罚。
- 关键词、带超时保护的正则、链接、邀请链接、刷屏、重复内容和提及过多规则。未知旧规则转人工审核；不以猜测结果直接处罚。
- 每个事件最多一次违规计数；多条命中保留多份规则证据，但合并相同动作。按窗口计数，过期重开，历史不删除；默认达到阈值转审核，自动封禁关闭。
- 每次规则评估及实际动作前刷新成员身份。群主、管理员和有效白名单豁免；排队后被提升为管理员也不会处罚。
- 动作通过共享任务/outbox/租约执行，发送前核对Bot权限并更新权限快照。无权限仅记录及转人工审核，不调用被禁止操作。
- 动作/尝试历史、额度预留结算、429延期、已知失败人工重试、结果不明人工核对；过期在途租约不自动重复执行。人工封禁需有效租户成员授权。
- 每个新入群成员独立建立事件；欢迎确认后创建本人验证、恢复及第二条消息，异常转人工。详见VERIFICATION_RUNTIME.md。

## 当前接口
POST /api/moderation/configs 创建默认停用的群配置；启用向导与真实Bot授权校验尚未完成。
POST /api/moderation/configs/{id}/rules 创建规则。
GET /api/moderation/reviews 查看待审。
POST /api/moderation/reviews/{id}/resolve：decision=dismiss/log/delete/warn/restrict/ban，必须提供note和可信登录身份对应的有效租户成员。
POST /api/moderation/actions/{id}/resolve：decision=confirm_succeeded/confirm_failed/cancel/retry，必须提供evidence；结果不明不能直接重试或取消。
POST /webhooks/moderation/{config_id}：入口配置与Bot校验尚待授权向导接入，密钥仅保存在本地配置中。
python scripts/run_moderation_once.py 仅执行沙盒规则/动作步骤，绝不联网。

## 尚未完成
真实欢迎/验证联调、申诉处理、统一Bot入口；群配置启用/授权向导；群管理控制台；真实Bot权限与行为联调。AI字段仅保留基线，AI服务后续实施，不阻塞核心MVP。

## 运行边界
ModerationBot适配器代码支持删消息、警告、有限时长限制、封禁及解封；未在真实群中运行。验证恢复支持原限制来源、到期时间和权限指纹核对，未实网联调。所有当前测试均使用测试适配器和本机真实PostgreSQL，不能替代Telegram实网验收。
