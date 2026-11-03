# Web登录与权限运行说明

状态：密码登录、会话、权限与HTTP接口已实现并通过专项测试；页面尚未接入登录表单，公开注册、邀请/找回密码和MFA尚未实现。

## 数据与角色
004c新增web_credentials、web_sessions和web_login_attempts，全局认证表不向业务运行角色开放。登录角色tgmate_auth只能调用固定认证函数，不能直接读密码表。函数由NOLOGIN、NOSUPERUSER、NOBYPASSRLS的tgmate_auth_owner持有，固定search_path；只有验证得到的工作区上下文才能读取租户成员和角色。
密码使用带随机盐的scrypt（N=32768、r=8、p=3），数据库不存明文。会话与CSRF只存SHA-256摘要，密码版本、用户/租户/成员状态随每次认证检查；版本改变或撤销后旧会话失效。创建会话核对密码验证时的版本，不能在改密竞争中用旧密码换取新版本会话。
业务权限复用001的成员/角色/权限表。查看和管理权限分开，客服操作员不能因此访问群管理或凭据功能；所有者使用tenant.admin。当前界面没有团队权限管理，角色配置仍由受控初始化或管理员脚本完成。

## 浏览器接口
POST /auth/login：email、workspace、password。成功设置HttpOnly会话Cookie和可读CSRF Cookie；响应不返回会话令牌。工作区使用管理员提供的slug，不以客户端tenant_id决定归属。
GET /auth/me：当前用户、工作区、角色和权限。
POST /auth/logout：撤销当前会话并清Cookie。
POST /auth/password：old_password、new_password；新密码12–128字符，需旧密码验证，成功撤销该用户所有工作区的旧会话并要求重新登录。
Cookie写请求需X-CSRF-Token、同站来源和有效会话；production模式要求配置public_origin且使用HTTPS。Secure Cookie的测试仅为ASGI协议模拟，不等同于真实TLS部署。访问Host有白名单；请求验证错误不回显密码输入。
登录失败消息不区分用户是否存在；失败尝试跨请求持久限流，按真实用户及IP的私有HMAC键计数。更换邮箱大小写或工作区不会给同一全局密码绕开账户计数。

## 初始化
本地：先执行001→004、004a/004b，再运行python scripts/bootstrap_auth.py；它执行004c并配置专用角色，只支持sandbox。初始登录信息只在.local/owner_login.json，不输出到对话、不打包。初始明文口令仅作为本机交付秘密保存；该初始用户成功改密后从该文件清除旧口令。
公共注册没有默认开放；没有自动创建生产管理员。生产初始化、凭据管理和部署验证仍需后续完成，不能把沙盒初始化脚本用于生产。

## 验证与限制
专项测试：密码盐与工作因子边界、通用失败信息、持久限流、会话撤销、跨租户权限、成员撤权、改密失效、旧版本竞态、无权读取认证表/函数、CSRF/来源/Host、只读角色、模块操作员以及Cookie属性。
完整空库重建与回归见CLEAN_REBUILD_RESULTS.json；最新全量见TEST_RESULTS.xml。真实HTTPS、真实用户账号和外部邮件/OTP联调尚未执行。沙盒固定Bearer密钥只供兼容测试，非sandbox已禁用。
密码策略依据：https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html 。
