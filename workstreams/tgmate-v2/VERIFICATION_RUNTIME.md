# 003 欢迎与验证运行说明

状态：按钮验证及其持久化流程已实现，使用本地PostgreSQL和测试适配器验证；尚未进行真实Telegram联调。该按钮用于确认当前Telegram用户本人操作，不应宣称具有第三方真人认证或CAPTCHA的强度。

## 流程
1. 每个新成员独立建立欢迎投递任务。欢迎发送确认后才创建验证会话；未确认投递不伪装已发送。
2. 可选临时限制通过原moderation_actions执行；只对当前无既有限制的普通成员施加，记录有限到期时间及实际权限指纹。
3. 限制动作结束后发送验证按钮；随机按钮令牌加密保存，数据库会话只保存哈希及密文引用，callback_data小于64字节。
4. 回调核对用户、群组、Bot、已确认的按钮消息和令牌；错误用户不能消耗他人的尝试次数。重复回调不重复计数或再次排队。
5. 验证通过后排入恢复任务；确认恢复完成或确认无需恢复后，才发送配置的第二条成功消息。恢复失败不会发出误导性的成功通知。
6. 超时、次数耗尽、挑战投递失败等转人工审核并排队清理。默认不封禁；清理本模块临时限制不因额度用尽而被拒绝。
7. 回调的answerCallbackQuery反馈独立排队并有限重试；其失败不改变已经落库的验证决定。

## 限制恢复
执行前核对原restrict动作、目标用户、到期时间及完整权限指纹；发现管理员已改动限制则不自动覆盖。只解除本模块明确记录的限制，不恢复不明来源的限制。
按Telegram官方语义，解除个人限制需要在restrictChatMember中对ChatPermissions字段传True；这不同于修改群默认权限。实现不调用setChatPermissions，并在解除后读取成员状态确认。将群默认权限中的False写回个人、同时设置永久期限，会留下新的个人限制，因此不能用于“恢复”。
临时限制设置有限期限，且发送前拒绝过近的到期时间，避免Telegram将不足30秒的期限解释为永久限制。远端读取和写入之间没有数据库式条件更新，检测后发生的并发管理员操作仍须在实网验收中检查；不能据本地指纹测试宣称跨系统原子性。

## 人工处理
POST /api/moderation/verification/{session_id}/review：decision=approve/reject，evidence；仅管理员且对应有效租户成员可处理pending/manual_review会话。
POST /api/moderation/notices/{outbox_id}/resolve：decision=confirm_sent/confirm_failed/retry/cancel，evidence；确认已送达需remote_message_id。结果不明不能直接重发或取消。已结束的挑战不能复用旧按钮重新发送，可转人工审核。
现有群管理Webhook会把callback_query交给验证服务；正式接入前仍需实现统一Bot入口，因为一个Bot只有一个Webhook，不能给每个群分别注册地址。

## 调度与证据
python scripts/run_moderation_once.py执行本地沙盒单步，包括过期扫描、规则/动作、欢迎/验证通知和回调反馈，不调用真实Telegram。生产须持续运行工作器，不能用单步脚本替代服务部署。
测试见tests/test_verification.py及tests/test_telegram_adapters.py，最终全量结果见TEST_RESULTS.xml。任何测试适配器的succeeded仅代表沙盒流程确认，不代表真实消息送达。

## 后续
申诉提交和审核、统一Bot入口、配置授权与启用向导、控制台仍待推进。简单问题、外部人机验证等可在相同会话模型上扩展；本轮只创建button类型，其他类型须人工审核，不会自动绕过外部验证。

协议依据：https://core.telegram.org/bots/api#restrictchatmember 、https://core.telegram.org/bots/api#chatpermissions 、https://core.telegram.org/bots/api#callbackquery 。
