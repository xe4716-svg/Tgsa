# TGMate V2.0

Telegram 社群运营项目，当前为本地开发验证版本，未通过生产上线验收。业务开发保持暂停，本次只整理规划、协作流程与 GitHub 上传候选文件。

## 产品范围

五类机器人：群管理、双向客服、频道群组克隆、贴纸克隆、机器人群发。TG 账号群发是独立通道，不增加机器人分类。004 是用户→Bot→客服运营者→Bot→用户的私聊中转；频道/群组镜像归005。

## 当前证据

001～004h已在全新隔离 PostgreSQL 17.11 实际执行，94张表、204项测试通过。见[CLEAN_REBUILD_RESULTS.json](CLEAN_REBUILD_RESULTS.json)和[TEST_RESULTS.xml](TEST_RESULTS.xml)。这是本地数据库与模拟外部接口测试，不是实网或生产验收。

核心后端已实现：客服中转及授权/告知门控、双通道文本群发、群管理/验证/申诉、登录与权限、Bot加密凭据及Webhook注册管理。界面目前只有登录与基本客服列表，完整管理后台、持续工作器、数据清理和部署恢复仍待完成。

## 项目导航

- [项目规划与依赖顺序](PROJECT_PLAN.md)
- [当前路线图](ROADMAP_PROGRESS.md)
- [开发、评审与发布工作流](WORKFLOW.md)
- [首批任务清单](BACKLOG.md)
- [当前交付状态](DELIVERY_STATUS.md)
- [GitHub上传准备说明](GITHUB_READINESS.md)
- [历史首发商业范围](FIRST_RELEASE_SCOPE.md)

## 本地运行边界

Python 3.11，依赖见requirements.txt。已有私有测试配置的环境可以安装依赖后运行：

```text
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8769 --no-access-log
python scripts/run_worker_once.py
```

打开 http://127.0.0.1:8769 使用管理员分配的邮箱、工作区与密码登录，不再通过页面输入旧访问密钥。单步工作器仅模拟，不发送真实Telegram消息。

新机器不能直接照搬旧bootstrap脚本：其迁移顺序尚未统一，已有记录列入BACKLOG TGM-001。verify_clean_rebuild.py已验证空库重建，但依赖本机私有数据库/角色配置，不是开箱即用部署器。不要把测试库切成生产。

## 检查范围

```text
python -m compileall -q app scripts tests
python validate_sql.py
```

SQL解析只证明语法及引用名称检查。数据库集成测试需专用配置，不能在缺少数据库时声称通过。GitHub Actions当前只配置静态检查，未在远端执行，也不会部署或发送Telegram消息。

私有连接、密码、Bot Token、TG会话和加密密钥留在.local或外部凭据系统；不提交仓库。上传候选包通过显式清单生成，禁止对原工作区直接git add .。本次没有提交、添加remote或推送。
