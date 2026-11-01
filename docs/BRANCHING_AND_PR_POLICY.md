# Git 分支与 PR 协作规范

## 分支
- main：正式稳定分支；禁止 AI 直接推送，禁止未经审核的直接合并。
- integration/mvp：MVP 集成分支；由总协调维护。
- ai/01-platform-security：AI-1 平台安全与数据库。
- ai/02-telegram-clone：AI-2 Telegram 频道/表情包克隆。
- ai/03-bot-runtime：AI-3 Bot、Worker 和任务运行时。
- ai/04-billing-membership：AI-4 会员、支付、钱包和财务。
- ai/05-frontend-admin：AI-5 顾客端、管理后台和教程。

## 工作规则
1. 每个 AI 只在分配的分支工作，不直接修改 main 或 integration/mvp。
2. 各 AI 分支从 integration/mvp 最新基线同步；开始任务前先拉取最新基线。
3. 一个 PR 只处理一个明确工作包；PR 目标分支统一为 integration/mvp。
4. PR 必须包含：目的、改动文件、数据库迁移、API 变化、威胁/权限影响、测试命令及结果、回滚说明、截图（UI 变更）。
5. 需要 CI 通过；至少一名非作者审查；安全、财务、跨租户、密钥相关改动必须额外审查。
6. 不允许提交 .env、真实 Token、Session、API Hash、支付密钥、私钥、真实用户数据或生产日志。
7. 不允许在 PR 中直接重写历史财务流水；修正使用冲正/补偿交易。
8. 数据库迁移必须向前兼容，提供升级和回滚/前向修复说明。
9. 不同工作包不得擅自修改共享 API Schema、核心租户模型和状态枚举；需要变更时先在 PR 中提出契约变更。
10. 冲突由总协调在 integration/mvp 解决，禁止强推覆盖他人提交。

## PR 检查清单
- [ ] 只改所属模块
- [ ] 遵守 docs/API_CONTRACT_MVP.md
- [ ] 没有密钥或敏感数据
- [ ] 权限和租户归属由服务端确定
- [ ] 幂等、重试、并发和错误状态已处理
- [ ] 新增/更新测试
- [ ] CI 通过
- [ ] 文档和迁移同步
- [ ] 不会破坏已有接口/业务规则
