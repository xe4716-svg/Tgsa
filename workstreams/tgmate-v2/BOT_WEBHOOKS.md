# 统一Bot Webhook阶段

004e_bot_webhooks.sql保存每个Bot唯一入口、加密认证信息、摘要、注册状态和操作标识。表启用并强制租户RLS。入口URL只由服务器配置的HTTPS public_origin生成，不接受任意提交地址；预览只写本地数据库，不调用Telegram。

接口均要求credentials.manage（或工作区管理员），写操作沿用Cookie/CSRF控制：
- POST /api/bots/{bot_id}/webhook/prepare：生成可核对的地址，验证本工作区Bot已登记凭据，重复预览不换密钥。
- POST /api/bots/{bot_id}/webhook/apply：先读getWebhookInfo，发现其他地址默认不覆盖。明确提交allow_replace=true后才允许替换；始终drop_pending_updates=false。
- POST /api/bots/{bot_id}/webhook/inspect：只查询远程信息；URL一致不能证明密钥一致。

apply先持久化registering及操作标识，再调用远程接口；超时/未知返回保留uncertain，禁止自动重试。进程中断超过90秒后inspect可转为uncertain。管理员明确提交retry_uncertain=true才能再次应用相同认证信息，不自动丢弃待处理事件。成功响应或使用正确密钥的入站认证会记录active；active表示入口配置/认证状态，不代表业务消息已成功投递。

入站secret_token是带用途标记和随机数的加密路由信封，符合Telegram字符/长度限制。服务先验证信封和Bot路径，再用其中租户上下文读取RLS表、常量时间比对持久化摘要，并检查Bot active。HTTP响应不返回secret。无效密钥不能指定任意租户。数据库同时保存再次加密的secret，仅应用持有本地开发密钥时可取出；生产仍需外部Vault/KMS和密钥轮换方案。

旧settings.bot_webhooks仅sandbox兼容，生产不使用。统一入口继续复用Bot级去重与业务队列同事务提交。应用代码没有自动执行真实setWebhook；本轮测试只用Fake/HTTP Mock。

边界：Telegram不提供Webhook配置CAS，getWebhookInfo只是当时快照，无法防止其他工具在预检后并发改写。持有secret的人可以构造认证请求，不能把secret认证描述为Telegram官方签名。公开地址变更、Webhook密钥平滑轮换、停用/删除远程Webhook和完整操作向导仍需实现。本轮不含真实公网HTTPS或Telegram联调。

数据库执行与全量结果见CLEAN_REBUILD_RESULTS.json；本轮28项针对性结果见WEBHOOK_TEST_RESULTS.xml。只解析SQL见VALIDATION_REPORT.json，不能与实际执行混同。
