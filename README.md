# ProcureFlow · 可验证采购工作台

**开发 Alpha，FastAPI + 原生 Next.js + 独立 Python Worker。不是生产采购系统。**

从固定键值报价或显式映射的普通表格中提取字段和来源证据，用确定性 Decimal 规则比较成本，让独立审批人批准具体快照，再通过持久化操作账本创建并独立核对 ERP 草稿。可选只读 Agent 通过真实 LangGraph 图执行，只能解释与读取证据，不能审批、下单或支付。

默认不调用真实模型、不连接真实 ERP。无需密钥即可运行完整 mock 演示。仓库保留早期 a1/a2 报告作为历史快照；其测试数字、环境限制和“未实现”说明不能代表当前代码。

## 当前能力与边界

- 单 SKU、CNY、EA；保留 TXT/CSV/文本 PDF/XLSX 固定键值布局，并支持普通 CSV/XLSX 的工作表/报价行选择、显式列映射和持久预览（见 [阶段十二](docs/stage12-tabular-import.md)）。普通表格到隔离真实 ERP 的严格验收合同见 [阶段十三](docs/stage13-tabular-erp-acceptance.md)。没有 OCR、多商品合并或任意单位/币种换算
- 原文件 SHA-256、页/行/单元格证据、人工修正历史与显式确认；未知值不会被默认为零
- 版本化需求、租户隔离的不可变政策/预约生效与变更历史，预算/交期上限及最低有效供应商数；完整报价集合和政策绑定的审批、失效/撤权检查（见 [阶段十一](docs/stage11-tenant-policy.md)）
- 受控 pilot 登录：一次性邀请、短期内存会话、退出／到期／跨进程撤权、用户／租户／角色展示；买方与审批方授权版本在首次 ERP 写入前重验（见 [阶段十四](docs/stage14-pilot-sessions.md)），不自动发放真实账号
- SQLite 与 PostgreSQL 业务存储；Alembic 迁移；独立 Worker、事务 Outbox、持久化幂等键及不确定结果只读恢复
- 过期未导入预览的可逆归档与有效配额；数据库＋来源文件的成对备份、校验和新隔离目标恢复。恢复默认暂停写入并永久拦截旧待处理操作重放；[恢复诊断工作台](docs/stage16-recovery-diagnostics.md) 提供待处理 hold、证据和只读 ERP 字段差异，不释放 hold 或授权写入。见 [预览保留](docs/table-preview-retention.md) 和 [备份恢复](docs/stage15-recovery.md)。归档不回收磁盘，不支持原地覆盖恢复或永久清除
- ERPNext Supplier Quotation 草稿和独立读回。支持范围与税/运费/折扣合同以 [费用映射边界](docs/erp-cost-mapping.md) 和 [ERP 说明](integrations/erpnext/README.md) 为准；没有 Submit、Purchase Order、删除或支付功能
- 原生 Next.js 工作台，以及 FastAPI 提供的轻量静态演示页；锁定 npm 安装、类型检查、生产构建、浏览器与容器验收入口
- 持久化只读建议：一次性领取、来源读取、过期/中断回执、不自动重放；真实 LangGraph 节点和有界工具白名单。不是逐工具 checkpoint 或跨进程模型恢复
- [10 个冻结合成开发案例](docs/stage10-evaluation.md)、确定性与协议评分、独立解释评分流程。fixture 通过不证明真实模型质量，未选择真实提供方/型号/费用预算；不是 60-task benchmark 或 Holdout

本项目仍无企业 SSO、生产运维认证、通用自主采购、MCP/AG-UI、向量 RAG、Celery/Redis 或真实模型质量/费用结论。

## 验收证据如何读

先看**被测试的确切提交**，再看该提交的 CI 和原始记录。测试命令或 workflow 存在不代表已运行；mock/fixture 不能替代真实服务；不同提交、数据库后端和重叠测试数量不能相加成采购任务数。

本增量从已接收源码基线 `a10f62b65263c9e6bb53219a6296dfb9a67de527`（tree `90ec220e6c35a28add728d47e6cfa851f591808e`）继续。该基线验收包记录 [core/原生 UI/PostgreSQL/Compose CI](https://github.com/volcano-code/ProcureFlow/actions/runs/37780968155)、[原生全栈容器 CI](https://github.com/volcano-code/ProcureFlow/actions/runs/37780968093)、[SQLite/PostgreSQL + 真实 ERPNext 合成沙箱 CI](https://github.com/volcano-code/ProcureFlow/actions/runs/37780968107) 和 [原生 pilot + 真实 ERPNext 双数据库 CI](https://github.com/volcano-code/ProcureFlow/actions/runs/37780968132) 已完成且成功。包含 push/PR 重复门槛在内，共记录 6 个运行、14 个作业；不能作为 6 套不同业务场景。原始记录位置、文件哈希及完整运行列表见 [基线证据索引](docs/evidence/a10f62b-baseline.json)。这是接收包内的历史结果，不是本增量的新运行或安全认证。

当前增量的本地结果、尚未运行/失败的门槛和远端结果应在交付记录中分开列出。[阶段十六恢复诊断](docs/stage16-recovery-diagnostics.md) 已记录本地回归、前端单元、类型检查和构建通过；原生浏览器因 `CHROMIUM_UNIX_SOCKET_DENIED` 在 UI 执行前被阻断，未取得截图或 UI 验收。本增量未运行实时 PostgreSQL、容器、真实 ERP 或远端 CI，不能沿用上述基线通过结论。真实 ERP 使用一次性合成公司与账户，不是用户生产 ERP，也不是实际人工审批研究。独立安全审查未完成，不宣称通过独立审查或生产准入。

## 立即运行，不需要模型密钥或 ERPNext

推荐 Python 3.13、Node 22。项目根目录：

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r services/api/requirements.txt
python scripts/start.py
```

解析上传需要 POSIX 资源限制支持（例如 Linux）；不支持这些限制的系统会安全拒绝解析，不会回退到无隔离执行。

打开 `http://127.0.0.1:8000`；API 文档为 `/docs`。默认绑定回环地址，数据保存在 `.data/`。可指定 `--port 8010 --data-dir ./my-local-data`。`.env.example` 是配置说明，启动脚本不会自动加载它。

演示身份：`demo-buyer`、`demo-approver`、`demo-auditor`。它们只供隔离本地演示，不可用于公网或真实 ERP。服务端解析角色，不信任客户端自报身份。

点击“载入三份示例”，查看证据并逐份确认，然后生成方案。报价 B 运费未知，确认后仍不能参加最低价推荐；本例推荐 SUP-C：

| 样例 | 成本合同 | 可比较总价 |
| --- | --- | --- |
| A | 20 × 1,200.00，含税，最终含税运费 800.00 | 24,800.00 |
| B | 20 × 1,120.00，不含税，税率 0.13，运费未知 | 未知 |
| C | 20 × 1,180.00，含税，最终含税运费 600.00 | 24,200.00 |

所有金额/数量在 API 使用十进制字符串。先将商品金额舍入到分、扣除明确商品折扣；不含税时按明确税率计算商品税额，最后加最终含税运费。见 [架构与业务合同](docs/architecture.md)。这不是通用税务模型或税务建议。

固定文本布局：

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

供应商/物料 ID 大小写原样保留；只规范化币种和 UOM。原“上传报价”入口保留 CSV 两列键/值与 XLSX A/B 列；原生 Next 新增“导入普通表格”，要求明确选择工作表、表头行、报价行与列映射，先预览，再创建未确认报价。字段冲突、公式、缺失运费或不明确税价不会被补成零。上传最多 2 MiB，文本 PDF 最多 10 页；解析进程限制和实际保证见 [安全边界](docs/stage12-tabular-import.md#安全边界)，仍不宣称适合公开不可信上传。

## 原生 Next.js 工作台

```bash
cd apps/web
npm ci
npm run typecheck
npm run build
npm run dev
```

保持后端运行；开发前端默认连接 `http://127.0.0.1:8000`。可通过 `NEXT_PUBLIC_API_BASE_URL` 指定地址；服务端 `PF_WEB_ORIGINS` 限制浏览器 origin，不替代身份验证。包含需求、证据、确认/修正、租户政策/变更历史、历史评估与失效提示、审批/拒绝、执行、独立 ERP 回读、持久化建议和审计。恢复诊断入口只在已有有效授权下使用；恢复后应先离线审核并按操作员流程恢复新授权，浏览器不能登录绕过 RECOVERY 或直接恢复写入。

## 回归与验收

```bash
python -m pip install -r services/api/requirements-dev.txt
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
npm run test:unit --prefix apps/web
python scripts/smoke.py
python scripts/web_http_smoke.py
python scripts/verify_model_acceptance.py --fixture
python scripts/evaluate_procurement.py --fixture --output evals/reports/local/procurement-development.json
python scripts/verify_recovery.py --output evals/reports/local/recovery-acceptance.json
```

安装 Chromium 后运行严格浏览器门槛：

```bash
python -m playwright install chromium
PF_REQUIRE_BROWSER=1 python scripts/test.py -q -m browser
python scripts/verify_next.py --output evals/reports/local/next-gate
```

默认可跳过缺失的浏览器环境；严格模式必须失败，不能把 skipped 算通过。Node transport 和 HTTP smoke 不是浏览器测试。原生 gate 会执行类型检查、生产构建、真实 Next 服务和浏览器，不回退到静态 demo。

PostgreSQL、Compose 与真实 ERP 各有独立严格入口及临时数据保护，见 [阶段三](docs/stage3-postgres.md)、[阶段七联合验收](docs/stage7-combined-acceptance.md) 和 `.github/workflows/`。不得把测试脚本指向生产数据库/ERP。离线环境清单：`python scripts/preflight.py`。

## 可选只读 Agent

[阶段八](docs/stage8-durable-advice.md) 说明建议账本和迁移；[阶段九](docs/stage9-langgraph-runtime.md) 说明实际 LangGraph 图与隔离模型协议入口。

服务端配置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 和可选 `LLM_THINKING_MODE` 后，用户仍须显式新建/执行建议。凭据不进入前端或仓库。真实调用前须单独确定提供方、型号、数据范围和费用授权；日常测试不会读取真实模型配置或调用付费服务。

只读工具为 `get_comparison`、`search_policy`、`get_evidence`；默认上限 4 次模型调用和 8 次工具调用。审批、外部草稿写入、shell 和浏览器不在模型权限内。引用存在且已读取不等于推理成立，`semantic_factuality_verified=false` 保留。提供方 token 仅为报告值，不能替代账单或硬金额上限。

## 数据、迁移与安全

```bash
PYTHONPATH=services/api PF_DATA_DIR="$PWD/.data" python -m procureflow.worker --once
# 不带 --once 持续 drain
cd services/api
PYTHONPATH=. python -m alembic upgrade head
```

对已有实例先停止相关进程、备份并在副本验证迁移。 V1 `.pfb` 严格绑定源码指纹；本增量新增模块后，旧 `a10f62b` 备份仍须使用匹配的可信基线代码恢复，不提供旧包兼容迁移或放宽校验，见 [版本兼容边界](docs/stage16-recovery-diagnostics.md#exact-version-backup-compatibility)。政策版本增量需执行新的迁移（使用 `alembic heads` 核对当前迁移头）；不要用覆盖源码替代数据库迁移。降级会删除相应历史，不应无备份执行。

Worker 网络请求前先持久化 IN_FLIGHT。丢失回执后只读核对；没查到不等于没创建，不盲目重发。建议运行亦不自动重放中断/失败调用。旧 private 静态令牌模式和新 pilot 受控会话模式都不是企业 SSO；演示身份被禁止访问真实 ERP。详细限制见 [威胁模型](docs/threat-model.md)。

## 目录与历史

- `services/api/procureflow/`：合同、规则、状态机、API、Worker、只读 Agent、ERP 适配器
- `services/api/tests/`、`scripts/`：回归、验收、合同导出
- `apps/web/`、`apps/demo/`：原生工作台与静态演示
- `packages/contracts/`：Pydantic/FastAPI 生成的合同与漂移门槛
- `integrations/erpnext/`：受限映射与一次性真实沙箱
- `evals/`：合成 fixtures、评测和历史原始记录
- `docs/stage*.md`：按阶段保留的实现/验收说明，阶段性否定结论以其时间范围为准

版本元数据中的 `0.1.0a2` 是沿用的 Alpha 标识，不代表后续开发分支增量不存在，也不代表已发布新版本。根 [MANIFEST.json](MANIFEST.json) 仅指向固定基线和证据；旧 a2 文件清单已按原字节保存在 [历史清单](docs/history/MANIFEST-0.1.0a2.json)，其中哈希不能用于验证当前源码。未合并、未部署的修改只应称为待验收开发增量。
