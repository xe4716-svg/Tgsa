# 002 双通道群发运行说明

状态：立即/定时文本群发的持久化运行流程已实现并使用测试适配器验证；不是实网验收。

## 已实现的闭环
计划创建（不可变版本）→预览→创建运行快照→共享outbox排队→任务租约→实时权限预检→分别建立Bot或账号投递记录→额度预留→发送→结果/尝试历史→额度结算与审计。

两个通道完全独立。Bot通道必须绑定broadcast类型实例和对应send执行器；账号通道不绑定机器人，使用一个明确授权的账号会话。只接受本租户已登记的group/supergroup目标，不轮换账号或代理规避限制。

支持：持久定时、暂停/恢复/取消、在途结果补记、429/FloodWait延期、有限次数重试、已知失败的人工重试/跳过、结果不明人工确认。结果不明不自动重试，额度保留直到人工核对；已知未发送取消时释放预留。租约过期后的在途发送进入人工处理，不以任务重启为理由重复投递。

## API
POST /api/broadcast/plans：request_key、module、text、targets，以及Bot的robot/binding或账号的session；可选scheduled_at（未来且带时区）。
GET /api/broadcast/plans/{task_id}/preview
POST /api/broadcast/plans/{task_id}/start：同一计划重复启动返回同一运行，不重复入队。
POST /api/broadcast/runs/{run_id}/state：action=pause/resume/cancel。
POST /api/broadcast/runs/{run_id}/review：destination_id、action=retry/skip/confirm_sent/confirm_failed、evidence；确认成功需remote_message_id。核对后默认暂停，需显式恢复；已取消运行只能核对在途结果，不能重新启动。
以上均需本地管理员Bearer密钥，租户从认证上下文取得。当前尚无完整账号登录界面，仍为本地沙盒。

## 执行与适配器
python scripts/run_broadcast_once.py 单步执行沙盒消息，绝不联网；沙盒权限结果仅用于模拟。
app.telegram_adapters.BotBroadcast与AccountBroadcast提供实网适配器代码，但未实网运行。Bot调用getMe/getChat/getChatMember确认身份、群类型及发言权限，再调用sendMessage。账号使用已授权StringSession，核对账号身份和群成员权限；最多发现200个已加入对话，找不到则拒绝。发送使用稳定random_id，关闭库的自动请求重试/自动重连和限流自动睡眠，不切换身份。
任何真实凭据接入、Bot授权、账号会话、出口代理、远端权限及平台行为仍须在专用测试环境验证，不能以本地测试通过代替。

## 后续工作
控制台群发计划表单/目标管理/运行进度；周期任务、编辑新版本、多账号各自任务、复杂媒体与通知中心按路线图扩展。现有MVP计划入口只开放立即/定时文本，不假装支持全部002目录中的扩展能力。

参考：Telegram Bot API https://core.telegram.org/bots/api ，MTProto消息接口 https://core.telegram.org/method/messages.sendMessage ，Telethon客户端文档 https://docs.telethon.dev/en/stable/modules/client.html 。实网适配器仅通过模拟HTTP合约及权限判定单元测试，账号网络行为尚未验证。
