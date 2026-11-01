# Bot 凭据登记阶段

004d_bot_credentials.sql新增全局唯一Bot外部ID登记和租户隔离密文表。跨工作区的同一个真实Bot登记失败，错误不返回对方工作区信息；同工作区更新凭据沿用Bot记录并增加凭据版本。已有但未纳入登记的历史Bot拒绝重复创建；POST /api/bots/{bot_id}/credentials以getMe确认身份匹配后迁移原记录，保留原实例/执行器/客服绑定，外部身份未知或存在重复身份时拒绝自动迁移。

POST /api/bots/register要求credentials.manage或工作区管理员权限。请求只接受token，外部ID/名称来自getMe，不接受提交者伪造。响应不包含Token、密文或指纹；审计只记录登记/更新和资源ID。getMe证明有效凭据的持有，不证明人的原始创建者身份。

此步骤不调用setWebhook，不修改远程Webhook，也不创建机器人产品实例或群配置。运行适配器已接入resolve_token(tenant,bot_id)：客服copyMessage、Bot群发和群管理/欢迎/私聊通知共用受租户约束的取密钥逻辑，不再读取明文bot_tokens。已撤销、跨租户、损坏密文、指纹或外部身份不一致均不可使用。外部Vault/KMS、Webhook登记向导尚未完成。当前加密复用本地Fernet密钥，不能把本机开发配置视为已完成生产密钥管理。

新增测试使用伪造身份探针/HTTP Mock；没有真实Telegram请求。数据库迁移在本机测试库执行，完整空库验证以CLEAN_REBUILD_RESULTS.json为准；SQL解析单独见VALIDATION_REPORT.json。

本轮35项相关测试通过，记录见RUNTIME_CREDENTIAL_TEST_RESULTS.xml；此前空库166项通过是该次数据库重建快照，不用其数量冒充本轮新增代码的全量结果。运行路径测试只使用HTTP Mock，没有实际发送消息。
