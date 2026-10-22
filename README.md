# ProcureFlow · 可验证采购工作台

**开发 Alpha：FastAPI + 原生 Next.js/React + PostgreSQL/SQLite + 独立 Python Worker。不是生产采购系统。**

将报价原文转为可核对的字段与证据，由确定性 Decimal 规则计算，再让独立身份批准具体快照，最后创建并回读 ERPNext 报价草稿。模型只生成建议或候选，不能审批、确认事实或获得 ERP 写权限。默认演示不调用真实模型、不连接真实 ERP。

本说明对应协议集成开发分支。旧 README 原字节保存在 [历史快照](docs/history/README-before-protocol-interop.md)；历史材料中的“当前”、测试数和否定结论只适用于其记录的提交。应用版本仍沿用 `0.1.0a2`，不是一次正式发布声明。

## 能力与证据边界

| 范围 | 已有实现 | 不应据此宣称 |
|---|---|---|
| 报价与证据 | 固定键值文本、文本 PDF、显式 CSV/XLSX 列映射；哈希、页/行/单元格定位；人工修订、确认、版本历史 | 任意扫描件、OCR、多模态或任意布局自动理解 |
| 业务计算 | CNY/EA；单物料及最多20个唯一SKU的完整供应商篮子；明确税折扣与运费；未知不补零 | 通用税务模型、币种/单位换算、跨供应商自动拼单 |
| 权限与执行 | 租户隔离、pilot邀请/会话/撤权、审批快照、事务Outbox、独立Worker、不确定结果只读恢复 | 企业SSO、全自主采购、采购订单提交、删除或支付 |
| 候选 Agent | 原文候选存储、排队、独立Worker、取消、人工采纳为未确认报价；有界工具、证据与权限复核 | 模型生成即为已确认事实、重启自动重放不确定调用 |
| 建议编排 | LangGraph节点、建议运行记录和中断状态 | 逐工具持久化checkpoint或任意中断位置的模型恢复 |
| 评测 | A规则/B单次LLM/C工具Agent运行与比较、Dev20及60条分组提取数据、私有人工审阅工具 | 脚本提供方成绩等于真实模型效果；公开合成Evaluation等于未见Holdout |
| **MCP（本分支新增）** | **官方SDK的本地stdio服务；一个指定需求、五个只读工具** | 远端HTTP MCP/OAuth、自动调用采购写操作 |
| **AG-UI（本分支新增）** | **官方事件模型与编码器；鉴权后的有限时长只读观察流、游标续读和撤权停止** | 原生工作台已换为AG-UI；观察结束等于Agent或采购成功 |
| 政策 | 租户隔离、版本化结构化规则、预约生效、审批绑定 | 文档向量RAG、语义检索或LLM直接发布规则 |

MCP和AG-UI为可选依赖与独立启动入口，**不修改默认应用、数据库迁移、既有前端或采购权限**。完整接口、测试和限制见 [协议互通说明](docs/protocol-interop.md)。

尚未完成：文档向量检索、远端MCP认证传输、原生UI的AG-UI消费者、实际模型效果与费用对照、真人语义校准、外部试用、独立未见样本和正式发布审核。结构化政策、协议通过或大量工程测试不能代替这些工作。

## 立即运行：不需要模型密钥

Linux/POSIX环境，Python 3.13；原生前端另需Node 22。从项目根目录：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r services/api/requirements.txt
python scripts/start.py
```

打开 `http://127.0.0.1:8000`，这是FastAPI提供的轻量静态演示页；API文档在 `/docs`。默认绑定回环地址、数据在 `.data/`，启动脚本不会自动加载 `.env.example`。

演示身份 `demo-buyer`、`demo-approver`、`demo-auditor` 只用于隔离本地演示，不可暴露公网或用于真实ERP。用户角色由服务端决定，不能靠前端切换获得真实权限。解析上传需要POSIX资源限制；不支持的系统会拒绝解析，不回退无隔离模式。

载入三份示例并分别核对/确认后，可进行确定性比较。B缺运费，不参与最低价推荐；预期选择SUP-C，合成总价24,200.00元。金额与数量使用十进制字符串，不在前端重新用浮点数计算。

### 原生 Next.js 页面

保持后端运行，在另一个终端：

```bash
cd apps/web
npm ci
npm run typecheck
npm run build
npm run dev
```

前端默认地址 `http://127.0.0.1:3000`。它提供需求、来源、修订、确认、政策、历史评估、审批、执行、独立回读、候选任务、建议和审计工作台；仍使用既有业务事件接口，不自动启用本分支新增AG-UI路由。

### PostgreSQL＋原生前端容器

仅在新建本地演示目录操作；已有 `.env.postgres` 不要重新生成，已有数据不要用 `down -v` 删除：

```bash
python scripts/init_postgres_env.py
docker compose --env-file .env.postgres \
  -f compose.postgres.yaml -f compose.web.yaml \
  up --build -d --wait
```

`.env.postgres`含本地数据库密码，不要提交或分享。默认仍是模拟ERP。真实ERP映射与费用限制见 [费用合同](docs/erp-cost-mapping.md) 和 [ERP集成](integrations/erpnext/README.md)；多物料/非零费用源码不代表每种组合都已通过真实ERP验收。

## 可选协议互通

```bash
python -m pip install -r services/api/requirements-dev.txt \
  -r integrations/protocols/requirements.txt
export PYTHONPATH="$PWD/services/api:$PWD"
python -m pytest -q integrations/protocols/tests
```

MCP只读工具为 `get_request`、`get_quotes`、`get_comparison`、`get_policy`、`get_evidence`。启动前由操作者设置固定API源、需求ID和仅本人可读的令牌文件；使用MCP客户端启动 `python -m integrations.protocols.mcp_server`。证据是数据，不是指令；源文件不会获得审批权限。

AG-UI通过可选factory启用：

```bash
python -m uvicorn integrations.protocols.agui:create_protocol_app \
  --factory --host 127.0.0.1 --port 8000
```

新增GET接口 `/api/v1/requests/{request_id}/ag-ui/events`，沿用现有身份与工作区检查。只发状态与允许的审计元数据；`RUN_FINISHED`表示本次观察结束，**不是采购或模型任务完成**。不要与同端口的默认API同时启动。

## 模型与评测

生产候选/建议模块和离线评测分开。真实调用需要服务端提供方配置与显式授权，密钥不得进入聊天、Git、前端或公开报告。默认CI不会调用付费模型。

[候选队列](docs/remediation-candidate-worker.md)、[候选原生容器验收](docs/remediation-candidate-stack.md)、[候选提取评测](docs/remediation-candidate-quality.md)、[B对照与人工审阅](docs/remediation-completion.md) 和 [分组数据/收尾](docs/keyless-closeout.md) 分别说明范围。

已公开数据是开发者编写的合成数据。规则解析器的7/20及分组Dev30=7/30、Validation10=0/10、Evaluation20=0/20属于历史基线测量；不是模型成功率或真实采购成功率。未识别输入保留在分母，人工语义评价未完成时保持未知。重复尝试、重叠套件和三个重复实验不能算成新增独立任务。

## 验收与升级

```bash
python -m pip install -r services/api/requirements-dev.txt
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
npm run test:unit --prefix apps/web
python scripts/smoke.py
python scripts/web_http_smoke.py
```

严格浏览器与数据库入口见 `scripts/verify_next.py`、`scripts/verify_postgres.py` 和 `.github/workflows/`。每次验收必须绑定确切HEAD或同树PR合并提交，读取原始JUnit/JSON，不把文件存在或workflow成功但测试跳过当作通过。

基线 `3401ace` 已有三套独立环境的原生页面/pilot身份/PostgreSQL/两个Worker/真实ERPNext联合记录，模型使用无上游脚本服务。这是工程闭环，不是真实LLM或真人研究；本分支协议验收是另一项独立工作，不继承旧提交作为本次通过证明。见 [收尾修复记录](docs/keyless-closeout-repair.md)。

升级已有数据前，停止相关进程，备份并在副本验证迁移：

```bash
cd services/api
PYTHONPATH=. python -m alembic upgrade head
```

保留 [版本绑定的成对备份恢复](docs/stage15-recovery.md) 与 [可选加密/签名保护](docs/stage18-backup-protection.md) 限制。不能通过改快照或重新发送旧任务来绕过恢复hold；查询不到远端单据也不代表可以安全重发。协议增量没有新增业务迁移。

## 目录

`services/api/procureflow/`为业务内核、API与Worker；`apps/web/`为原生前端；`apps/demo/`为静态演示；`integrations/protocols/`为可选只读协议；`integrations/erpnext/`为受限映射与一次性沙箱；`evals/`、`tests/`、`scripts/`为任务数据、独立检查和验收；`docs/`保留各阶段范围与原始历史。

当前开发分支并不等于默认main、已合并版本或正式发布。默认主线保护、生产运维认证、企业SSO、通用自主采购与真实效果结论仍未由本项目证明。
