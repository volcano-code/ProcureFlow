> **LangGraph 执行增量**：只读建议现通过实际有界图节点执行，保留持久化账本、不重放与证据校验；提供合成模型验收入口，见 [阶段九说明](docs/stage9-langgraph-runtime.md)。真实提供方调用与质量仍须单独授权验收。

> **只读 Agent 建议增量**：原生工作台新增持久化建议、来源读取校验、版本过期提示和中断不重放，见 [阶段八说明](docs/stage8-durable-advice.md)。新增数据库迁移；真实提供方质量与 LangGraph 仍未验收。

> **联合验收增量**：PostgreSQL + 真实 ERPNext 双后端验收、负向 REST 权限探测和聚合 OpenAPI 漂移检查，见 [阶段七说明](docs/stage7-combined-acceptance.md)。以对应提交的实际 CI 结果为准。

> **开发分支 P0 更新**：新增可复现 npm 安装与 PostgreSQL/Compose 验收入口，见 [阶段三说明](docs/stage3-postgres.md)。下文 a2 测试数字与未验收说明是历史交付快照；当前提交的实际状态以 GitHub Actions 对应提交日志为准，不能混用不同版本的结果。

# ProcureFlow · 可验证采购工作台

**v0.1.0a2 / 本地开发 Alpha / 2026-09-22**

从固定格式报价中提取字段与证据，使用确定性规则比较成本，让独立身份审批绑定具体快照，再通过持久化操作账本创建模拟 ERP 草稿。

**这是在 a1 上完成安全修复与阶段二增量的源码，不是完整四周 MVP，更不是已上线的企业采购系统。** 默认没有真实模型调用、没有真实 ERP 写入，也不会提交采购订单。原生 Next.js 前端源代码另附；其构建尚未通过环境验收。

## a2 本轮变化与验收状态

本轮最终 Python 默认回归 **111 passed, 2 skipped**；共用前端传输层 **14 项通过**，真实 HTTP 前端流程 **3 项通过**。真实 SIGKILL 恢复两例已包含在 111 项中。严格静态浏览器模式因环境策略 **2 项失败**，原生 Next 门槛因缺依赖阻断，不能宣布前端验收完成。

已修复 demo 身份可连接真实 ERP 和畸形模型消息导致未处理异常。补齐 Next 示例加载、需求修改/审批失效、人工确认和拒绝路径，加入共用传输层、创建后 ERP 独立回读、只读预检和独立 native UI CI 门槛。**原生页面仍未实际构建/浏览器验收；真实模型及 ERPNext 仍未联调。**

详细结论与原始记录索引见 [阶段二验收报告](docs/stage2-review.md) 和 `evals/reports/stage2/verification.json`。原先 `evals/reports/verification.md` 保留的是 a1 历史报告。

## 立即运行：不需要模型密钥或 ERPNext

推荐 Python 3.13（本次实际测试为 3.13.5）。在解压后的 `procureflow` 根目录执行：

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 改用：.venv\Scripts\Activate.ps1
python -m pip install -r services/api/requirements.txt
python scripts/start.py
```

浏览器打开 `http://127.0.0.1:8000`。交互式 API 文档位于 `/docs`。默认绑定回环地址，不向公网开放。

`requirements.txt` 固定直接依赖；`requirements.lock` 是本次 Python 3.13 Linux 环境的传递依赖版本快照，不是跨平台锁定或依赖漏洞审计。新环境安装需要能访问 Python 包源。

在工作台点击“载入三份示例”，查看报价字段证据，逐份“确认字段”，再点击“校验并生成方案”。右上角切换独立审批人并批准快照，切回采购员创建模拟草稿。报价 B 缺运费，确认也不会让它自动合格；本例推荐总价 24,200.00 的 SUP-C。

演示令牌仅供本地测试：`demo-buyer`、`demo-approver`、`demo-auditor`。身份由服务端映射，不接受客户端自报角色。不可把这些令牌用于公网服务。

数据写入项目的 `.data/`，刷新或重新启动后仍保留；发布包不包含任何运行数据库。指定独立目录：

```bash
python scripts/start.py --port 8010 --data-dir ./my-local-data
```

本地 UI 由 FastAPI 提供静态资源，不依赖 Node 构建。**浏览器自动化在当前开发环境被访问策略阻止，视觉与交互 E2E 尚未验证；真实 HTTP 全链路已通过。** 所以遇到页面问题应检查浏览器控制台及后端日志，不应把本文件视为已完成浏览器验收的证明。

## 当前交付了什么

| 模块 | 已实现 | 当前边界 |
| --- | --- | --- |
| 需求管理 | 创建、查询、版本化修改、工作区隔离 | 单 SKU / CNY / EA |
| 报价导入 | TXT、CSV、文本 PDF、XLSX，原文件持久化 | 固定键值布局，不是任意商业报价识别 |
| 字段证据 | SHA-256、原文片段、页码/行号/单元格 | PDF 没有视觉 bbox；不支持 OCR |
| 金额与规则 | Decimal、未知传播、预算/交期/数量校验 | 简化单行税费合同，见架构文档 |
| 报价核对 | 人工确认、人工修正原因、历史版本 | 确认不是自动补全未知值 |
| 方案与审批 | 快照哈希、审批人分离、过期/撤权校验 | 本地令牌，不是企业 SSO/RBAC 管理后台 |
| 外部操作 | 操作账本、事务 Outbox、租约、只读核对 | SQLite + 独立持久化 mock ERP |
| HTTP Worker | 单次/连续 drain，可独立进程运行 | 尚未接入 Celery/Redis |
| 审计与事件流 | 业务事件持久化、带鉴权 SSE | 自定义事件，不宣称 AG-UI 合规 |
| 只读 LLM 适配器 | 有界 tool loop、结构化输出、工具白名单 | mock transport 测试；真实模型未联调 |
| ERPNext 适配器 | Token REST、供应商查询、受限草稿映射 | 写入默认关闭；真实实例未联调 |
| 前端 | 可直接提供的本地工作台 + 原生 Next.js TSX 源码 | Next.js 尚未安装依赖、完整类型检查或构建 |
| 数据迁移 | Alembic 8 张表及约束 | SQLite 往返验证，PostgreSQL 未验证 |

## 三份样例的预期结果

| 文件 | 供应商 | 关键信息 | 可比较总价 |
| --- | --- | --- | --- |
| supplier-a.txt | SUP-A | 20 × 1,200.00，含税，运费 800.00 | 24,800.00 |
| supplier-b.csv | SUP-B | 20 × 1,120.00，不含税，税率 0.13，运费未知 | 未知，不能参加最低价推荐 |
| supplier-c.pdf | SUP-C | 20 × 1,180.00，含税，运费 600.00 | 24,200.00 |

另附 `supplier-a.xlsx`，与 A 的字段相同，用来测试单元格定位。全部为合成数据。

支持的文本布局示例（API 中金额和数量也必须为字符串）：

```text
supplier_id: SUP-A
sku: STAND-01
quantity: 20
uom: EA
unit_price: 1200.00
tax_mode: included
tax_rate: 0.13
shipping_cost: 800.00
discount: 0.00
delivery_days: 7
currency: CNY
```

CSV 使用两列键/值；XLSX 使用 A 列键、B 列值。XLSX 公式不计算、不执行；字段冲突和非法数值保持未知。上传上限 2 MiB，文本 PDF 最多 10 页。解析器还不是进程隔离沙箱，不应接收公开网络的不可信文件。

## 验证，而不是只看界面

```bash
python -m pip install -r services/api/requirements-dev.txt
python scripts/test.py
python scripts/smoke.py
```

`smoke.py` 会使用临时数据库启动真实 HTTP 服务，上传三份样例、独立身份审批，启动另一 Python 进程执行 Outbox，重启 API，再确认同一操作只对应一个模拟草稿。结束后清理临时数据；不触碰运行中的 `.data`。

安装浏览器并执行严格的浏览器验收：

```bash
python -m playwright install chromium
# Linux/macOS；Windows PowerShell 使用 $env:PF_REQUIRE_BROWSER="1"
PF_REQUIRE_BROWSER=1 python scripts/test.py -m browser -q
```

默认在 Chromium 缺失或系统策略禁止访问本地服务时，浏览器测试被标记为 skipped，不能算通过；`PF_REQUIRE_BROWSER=1` 下必须报失败。当前交付的阻断原因是 `ERR_BLOCKED_BY_ADMINISTRATOR`。没有绕过浏览器管理策略，没有伪造运行截图。

本次原始结果在 `evals/reports/`：JUnit、pytest 日志、真实 HTTP smoke JSON、JS/TS 语法检查。准确状态以 `verification.md` 为准。**工程测试数量不等于原计划中的 60 个采购任务评测集；本版没有模型成功率、成本或 Holdout 分数。**

## Next.js 原生前端（尚未完成构建验收）

`apps/web` 是 React + TypeScript + App Router 实现，不是 iframe 包装静态页面。包含示例导入、需求创建/修改、报价编辑/显式确认、证据、方案、审批/拒绝、执行与 SSE 审计组件；身份切换清空旧上下文。

```bash
cd apps/web
npm install
npm run typecheck
npm run build
npm run dev
```

保持后端运行。前端默认连接 `http://127.0.0.1:8000`；可通过 `NEXT_PUBLIC_API_BASE_URL` 指定后端。Node 22 为目标运行环境。

当前环境无法从 npm 包源下载依赖，因此没有真实 `next build`、完整类型检查或已解析的 `package-lock.json`。首次成功安装后应审阅并提交 lockfile；当前 CI 有锁时使用 `npm ci`、无锁时仅允许 bootstrap 安装，正式可复现发布必须仅使用 `npm ci`。本次只完成 **5 个 TS/TSX 文件的转译语法检查**，不能代替 Next 构建。Tailwind、shadcn/ui、TanStack Query、RHF/Zod 尚未集成。

### a2 新增验证命令

从项目根目录运行，前两条只需要 Node 22 和 Python 依赖，不依赖 Next 包：

```bash
node --test apps/web/tests/transport.test.mjs
python scripts/web_http_smoke.py --output evals/reports/local/frontend-http.json
python scripts/preflight.py --output evals/reports/local/environment.json
```

共用传输测试不是浏览器测试。原生 Next 严格验收命令会执行完整 typecheck/build、启动临时 API 和 Next 生产服务，再运行 4 条真正针对 Next 页面而非静态 demo 的用例：

```bash
python -m playwright install chromium
python scripts/verify_next.py --output evals/reports/local/next-gate
```

缺依赖返回 exit 2；构建或浏览器失败返回 exit 1，绝不以 skipped 或静态页面冒充通过。当前执行记录为 `NEXT_DEPENDENCIES_MISSING`。自定义前端 origin 可用 `PF_WEB_ORIGINS`（逗号分隔，不含路径/尾斜杠）；默认允许 localhost/127.0.0.1 的 3000 端口。它不是认证替代品。

## 可选只读模型解释

只有显式调用 `POST /api/v1/requests/{id}/advice` 才会调用模型；默认“校验并生成方案”仍是确定性基线。

```bash
export LLM_BASE_URL=https://api.deepseek.com
export LLM_API_KEY='在服务端设置自己的密钥，不要提交到 Git'
export LLM_MODEL='填写账户实际可用的模型 ID'
python scripts/start.py
```

`.env.example` 只是配置清单，启动脚本不会自动读取 `.env`。Windows PowerShell 请使用 `$env:变量名="值"`。

只读工具是 `get_comparison`、`search_policy`、`get_evidence`。最多 4 次模型调用、8 次工具调用，并检查上下文/输出尺寸与软时间预算。业务审批、外部草稿写入、浏览器和 shell 均不在模型权限内。`search_policy` 目前返回当前发布的合成演示政策，不是向量 RAG。

模型解释必须先成功调用 `get_comparison`，且只是建议：引用 ID 可验证存在，不等于语义蕴含已验证；输出明确标记 `semantic_factuality_verified=false`。本次仅通过模拟 HTTP 返回的适配器契约测试，未实测真实模型质量或供应商兼容性。

## Worker、迁移与部署配置

```bash
# 从根目录执行；Linux/macOS
PYTHONPATH=services/api PF_DATA_DIR="$PWD/.data" python -m procureflow.worker --once
# 去掉 --once 后，在你自己的终端持续处理 pending outbox。
```

操作通过 `/execute` 预留，可由 Worker 执行，也可在内置 UI 中同步触发 `/operations/{id}/process`。网络请求前先落库 `IN_FLIGHT`，结果不明确则核对远端；未查到不等于未执行，不会盲目重试创建。

演示模式自动建表。需要显式迁移时，在相同环境变量下：

```bash
cd services/api
PYTHONPATH=. PF_DATA_DIR=/absolute/path/to/data python -m alembic upgrade head
```

`compose.yaml` 提供本地 API + Worker + 持久化卷的示例，端口仅发布到 `127.0.0.1`。**当前环境没有 Docker，未执行镜像构建和 Compose 验收。** 它采用 SQLite，不是原计划中的 PostgreSQL/Redis/MinIO 部署。不要因配置文件存在就认为已通过容器化验收。

a2 不允许 demo 身份连接真实 ERPNext，包括只读；必须显式设置 private 身份。真实 ERPNext 配置、唯一字段与已知限制见 `integrations/erpnext/README.md`。不要对生产 ERP 开启写入。现有适配器只支持税率明确为 0、运费为 0、折扣为 0 的单物料草稿，其他税费映射会拒绝处理而不是悄悄漏项。

## 仓库导览

```text
apps/demo/                  不依赖 Node 构建的本地工作台
apps/web/                   原生 Next.js 前端源码（构建待验证）
services/api/procureflow/    类型合同、业务规则、状态机、API、Worker、模型与 ERP 适配
services/api/tests/         金额/解析/权限/审批/故障/适配器/迁移/浏览器测试
services/api/alembic/       真实数据库迁移
packages/contracts/        从 Pydantic/FastAPI 导出的 JSON Schema 和 OpenAPI
integrations/erpnext/       ERPNext 接入边界及测试门槛
scripts/                    启动、测试、真实 HTTP smoke、合同导出、TS 语法检查
infra/ + compose.yaml       未执行的本地容器配置
.github/workflows/ci.yml    待在托管仓库实际运行的 CI 配置

docs/assessment.md          原规划审核与范围修订
docs/architecture.md        当前实现、事务和状态边界
docs/threat-model.md        已测与未解决的安全风险
docs/next-milestone.md      下一轮开发验收门槛
evals/fixtures/             合成报价样例
evals/reports/              本轮原始测试与验证状态
```

## 从 a1 升级

优先把 a2 完整包解压到新目录再安装依赖。不要把源码覆盖操作当成数据库迁移：本轮没有更改业务表结构，但仍应先停止旧 API/Worker、备份 `.data/`，在副本上验证后再复用原数据目录。源码包不包含 `.data/`、凭据、node_modules、.next 或 git 历史。

可用 `python scripts/start.py --data-dir /absolute/path/to/copied-data` 指定数据副本。本轮安全配置变更会拒绝以前允许的 demo+ERPNext 组合；不能通过改用演示令牌绕开它。

## 尚未完成，不能写成简历成绩

真实 LLM/ERPNext 联调、PostgreSQL/pgvector、Celery/Redis、对象存储、通用文档抽取、LangGraph Checkpointer、MCP、AG-UI、Skills 加载器、企业 SSO、真实浏览器 E2E、60-task benchmark、Multi-Agent、A2A/A2UI、DeepSeek Harness、GraphRAG、RL 均未在本次交付中完成验收。

当前合理的里程碑名称是：**“采购业务内核与本地模拟闭环 Alpha”**。不要称作完整 Agent MVP、已上线系统或生产安全认证。
