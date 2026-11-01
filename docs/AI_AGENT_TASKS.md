# 十个 AI 工作席位任务说明

所有外部 AI 开发工具必须由仓库所有者自行连接 GitHub 私有仓库。分支和 Issue 已建立不代表外部 AI 已被自动启动；每个 AI 必须在相应工具中授权仓库并按下述范围工作。

## AI-1：平台安全与数据库
分支：ai/01-platform-security
负责：认证授权、租户上下文、RLS、复合外键、数据库迁移、管理员 MFA/RBAC、审计、安全测试、CI 安全检查。
首批任务：审计现有 migrations/app/auth；修复请求级事务和租户上下文生命周期；收紧 DB 角色权限；增加跨租户负向测试、连接池复用测试、迁移测试和密钥扫描。
验收：跨租户读写拒绝；无租户上下文 fail closed；空库/升级库迁移通过；无秘密进入 Git。

## AI-2：频道历史克隆与实时同步
分支：ai/02-telegram-clone
负责：一对一授权源→目标频道路由、历史回补、实时缓冲、媒体/相册、编辑/删除能力边界、消息映射和断点。
首批任务：审计历史工程；先建立实时事件缓冲再历史回补最后排空；实现 Inbox 去重、路由级映射、暂停/恢复；覆盖文字、图片、视频、文件、相册。
验收：重启不重复成功投递；历史/实时交界有测试证明无漏消息；无目标发布权限拒绝启动；严格遵守 Telegram 限速。账号授权和代理由 AI-6 负责。

## AI-3：Bot 与任务运行时
分支：ai/03-bot-runtime
负责：Worker、Outbox、任务租约、fencing token、资源预留、基础群发/定时任务、基础群管理。
首批任务：审计现有任务系统；实现 PostgreSQL transactional outbox；Worker lease/heartbeat/fencing token；基础群发、暂停/恢复/取消；429/backoff。
验收：Worker 崩溃可恢复；旧 Worker 不能写入；不确定投递不盲目重发；资源超限拒绝或排队。

## AI-4：会员与商业化
分支：ai/04-billing-membership
负责：套餐/权益版本、订单、人工支付、支付适配器测试模式、USDT 核验边界、钱包和账本。
首批任务：审计已有 billing/wallet；价格快照；人工支付始终展示；自动支付仅在配置验证启用后展示；回调幂等、TXID 唯一分配、部分/超额付款；账本不可变和冲正。
验收：重复回调不重复入账；同一链上事件不分配两单；账本按资产平衡；MVP 使用模拟支付/沙箱，不接生产资金。

## AI-5：顾客端核心页面
分支：ai/05-frontend-admin
负责：顾客控制台、API client、认证状态、移动端响应式页面。
首批任务：盘点前端栈；实现登录/注册、首页、Telegram 账号/代理、频道克隆、贴纸任务、任务进度、会员/支付测试模式、钱包流水。
验收：手机端可用；写操作防重复提交；前端不保存秘密；后端再次授权。管理员后台由 AI-8 负责。

## AI-6：Telegram 账号授权与代理
分支：ai/06-telegram-auth-proxy
负责：QR/手机号/2FA 授权、协议号文件导入、账号资产基本信息、SOCKS5/MTProto Proxy/HTTP CONNECT。
首批任务：复用现有 Telethon 登录；将登录状态放入 account gateway owner/lease；代理连接测试、绑定/解绑；Session 服务端加密保存并清理临时文件。
验收：授权成功/失败/取消/超时/2FA 路径有测试；代理覆盖认证失败/协议不匹配/超时/不可达；Session 不入日志、不回传前端。

## AI-7：表情包克隆与媒体处理
分支：ai/07-sticker-media
负责：贴纸包克隆 MVP、媒体格式校验、文件适配、与频道克隆共享媒体处理契约。
首批任务：审计历史贴纸工程；实现获准来源的贴纸包复制；显示进度和失败项；对不支持类型记录原因；幂等防重复创建。
验收：使用自有测试贴纸包；不绕过受保护内容；对损坏/过大/不支持媒体有测试。

## AI-8：超级管理员后台与教程中心
分支：ai/08-admin-tutorial
负责：超管用户/会员/任务/支付配置/审计/IP 白名单页面和教程中心。
首批任务：复用 AI-5 前端栈和组件；实现管理员用户、会员、任务、审计页面；IP 白名单开启前验证当前 IP 并提供恢复路径；所有教程入口统一“使用教程”，按按钮配置文字/图文/视频。
验收：后端 RBAC 强制鉴权；敏感操作审计；移动端基本可用。

## AI-9：自动化测试与集成验收
分支：ai/09-qa-integration
负责：pytest/CI、OpenAPI 契约测试、租户隔离、幂等、Worker 恢复、端到端冒烟和缺陷清单。
首批任务：跑现有基线；添加 API schema tests；跨租户拒绝、重复回调、重复投递、Worker lease、权限缺失测试；生成可复现测试报告。
验收：CI 有真实记录；区分单元、集成和外部真实联调；不得虚报测试通过。

## AI-10：测试部署、CI/CD 与可观测性
分支：ai/10-devops-deploy
负责：Docker 测试环境、部署脚本、CI/CD、健康检查、日志指标、备份恢复和回滚。
首批任务：审计 Docker/环境变量/Actions；建立 staging profile；DB/Redis 不暴露公网；API、Worker、account gateway 分离；lint/pytest/migration/secret scan/container build；备份恢复步骤。
验收：干净环境可按文档启动；日志无 Token/Session/API Hash/支付私钥；真实部署需附环境证据。

## 所有 AI 的共同规则
- 只在分配分支工作；PR 目标统一为 integration/mvp；禁止直接写 main。
- 遵守 docs/API_CONTRACT_MVP.md 和 docs/BRANCHING_AND_PR_POLICY.md。
- 一个 PR 一个工作包；说明数据库迁移、API 变化、权限影响、测试命令/结果、回滚方式。
- 不提交 .env、真实 Token、Session、API Hash、支付密钥、私钥或真实用户数据。
- 未运行的测试不得写通过；设计完成不等于代码完成。
- 跨模块接口变更先提出契约变更，不擅自更改共享 schema。
