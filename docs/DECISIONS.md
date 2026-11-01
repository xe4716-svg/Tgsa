# V1.0 技术决策

1. 模块化单体 + 一个任务执行层，不拆微服务。
2. FastAPI、PostgreSQL、Redis、Docker。
3. 每个 Telegram 账号独立授权记录、Session、代理绑定和任务状态。
4. 同一账号同一时刻只允许一个主连接所有者；使用数据库租约和 fencing token 防止双主。
5. 租户业务表使用 tenant_id + PostgreSQL RLS。
6. 平台 API ID/API Hash 放 Secret Manager；Session 和代理凭据加密保存。
7. 代理断开不得静默回退直连；只有核实的出口才能标记为独享。
8. 用户界面称“协议号文件”，底层仍是 MTProto Session，不是 API ID/API Hash。
9. 历史扫描与实时更新分离；消息映射唯一约束、持久化队列、断点续传。
10. 遵守 Telegram 限流与访问控制；不模拟真人、不绕过 CAPTCHA、不轮换 IP/账号规避限制。
11. 账号资产扫描默认只读元数据；不擅自下载全部私聊内容或媒体。
12. 新设备检查通过后，仍需用户主动启动同步。
