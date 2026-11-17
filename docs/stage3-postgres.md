# P0 PostgreSQL 与可复现前端安装

本次以开发分支 e53a6bd 为代码基线，保留 Next.js/FastAPI/Python Worker；不迁移到 Floot 应用框架。

## 实现范围

前端 package-lock.json 直接取自此前已通过的 GitHub Actions 产物 10977761574，未重新解析依赖树。已校验根依赖与 package.json 一致，所有远端包来自 registry.npmjs.org 且有 SHA-512 integrity；CI 只用 npm ci，不允许缺锁时退回 npm install。这不是漏洞审计。

PostgreSQL URL 使用同步 psycopg 驱动。postgres://、postgresql:// 规范化为 postgresql+psycopg://；拒绝未经支持的异步驱动。驱动固定为 psycopg[binary] 3.3.6；核心 Python 传递依赖锁仍为 a2 的环境快照。Docker 基础镜像/PG 镜像未固定 digest，不宣称字节级可复现镜像。

只有本地 SQLite demo 自动建表。PostgreSQL 必须显式执行 Alembic；/health 为进程存活，/ready 检查连接、8 张业务表及迁移 head。未就绪返回 503，不返回 DSN/SQL/凭据。

事务采用 READ COMMITTED，沿用请求行锁作为采购事务串行化边界；锁等待上限 10 秒、SQL 上限 30 秒，不自动重试外部写。锁前读过的 QuoteVersion 在锁后显式刷新，避免旧 confirmed_by 缓存触发重复确认。新增可控制交错顺序的 PostgreSQL 并发反例。

Worker 选择任务时排除尚未过期的 IN_FLIGHT 租约；选取不是抢占，实际抢占继续由 process_operation 的行锁/持久化租约完成。这不是 Celery/Redis 集成，也不宣称 exactly-once。

## 本地 SQLite（不需要 PostgreSQL）

```bash
python -m pip install -r services/api/requirements-dev.txt
python scripts/test.py -m 'not postgres'
python scripts/start.py
```

## 本地 PostgreSQL + Docker Compose

只供可信本机演示。公开 demo 身份不可用于公网。默认绑定 127.0.0.1，数据库不映射主机端口，ERP 固定 mock。不会访问或创建真实采购单。

```bash
python scripts/init_postgres_env.py
docker compose --env-file .env.postgres -f compose.postgres.yaml up --build -d --wait
python scripts/compose_smoke.py --output evals/reports/local/compose.json
```

上述 smoke 会创建合成需求和模拟草稿，仅接受 loopback、demo、mock、PostgreSQL 四条件同时满足的服务。启动顺序为 DB health → 单次 Alembic migration 成功 → API ready → Worker。

.env.postgres 默认以 0600 权限生成随机密码且拒绝覆盖旧文件；不要提交/分享该文件，也不要把 `docker compose config` 的展开结果公开，它包含 DSN。密码只适用于首次创建数据卷；不要在保留旧数据库卷时直接换密码。

停止但保留数据：

```bash
docker compose --env-file .env.postgres -f compose.postgres.yaml down
```

不要对现有业务卷运行 down -v。CI 使用自己新建的临时卷，只有 CI 清理环节删除这些临时卷。

## 非容器 PostgreSQL

```bash
python -m pip install -r services/api/requirements.txt -r services/api/requirements-postgres.txt
# 在本机环境变量中设置 PF_DATABASE_URL；不要把含密码的值粘贴进聊天。
cd services/api
PYTHONPATH=. python -m alembic upgrade head
cd ../..
python scripts/start.py
```

PG 数据与 documents 目录需同时备份；模拟 ERP 还依赖同一共享目录下的独立 SQLite 文件。旧 SQLite 数据不会自动搬进 PG。本轮不提供静默数据库迁移，保留旧 .data 再从空 PG 环境验证。

## 严格 PostgreSQL 回归

只能使用独立名称以 _test 结尾的测试数据库。需要显式 PF_ALLOW_DATABASE_TESTS=1，不读取 PF_DATABASE_URL 来选择被测库。

```bash
python -m pip install -r services/api/requirements-dev.txt -r services/api/requirements-postgres.txt
# 在本机设置 PF_TEST_DATABASE_URL，目标为独立的 procureflow_test
export PF_ALLOW_DATABASE_TESTS=1
python scripts/verify_postgres.py --output evals/reports/local/postgres
```

每例只创建/删除自己的 pf_test_<随机串> schema，运行真实 Alembic，不用 create_all 代替；缺服务/驱动返回 blocked，严格 CI 不允许 skip。原 25 项 workflow 也在该数据库 fixture 上执行。新增9例：migration往返、独立Worker及应用重建、6路并发预留、6路并发执行、并发编辑冲突、确认缓存竞态、丢失回执核对、数据库唯一约束、事务回滚。

## 边界和后续

真实 ERPNext、真实 LLM、LangGraph、MCP、AG-UI、Redis/Celery、企业 SSO 和 60-task benchmark 本轮没有新增联调成绩。数据库/Compose/浏览器通过只说明对应测试场景，不等于生产安全认证。

标准参考：SQLAlchemy 2.0 psycopg dialect、PostgreSQL row-level locks、npm ci、Docker Compose startup order；使用实际工具输出记录版本和结果，不将源码存在写成验证通过。
