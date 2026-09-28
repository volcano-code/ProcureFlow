# 原规划审核与本轮范围修订

审核日期：2026-09-22。依据用户提供的《ProcureFlow：面向秋招的企业采购 Agent 平台开发与研究蓝图》（990 行 Markdown），不是之前聊天中的“每日 AI 简报”项目。以下区分附件观点、审核判断和已经实施的内容。

## 结论：方向合理，可以开始；不宜按原表同时铺满技术

附件第 107–112 行把字段级 Evidence Provenance、Approval Snapshot Binding、Deterministic Business Core、Idempotent External Execution 列为差异化。这些目标共同约束同一个真实业务闭环，比简单堆叠框架更值得优先实现。附件第 257–274 行提出模型不能授权业务状态迁移，也应保留。

本轮已把上述原则落实为业务合同、数据库、审批服务、Outbox、适配器与测试，而不是声称完整 Agent 系统已完成。

## 必要修订

**1. 工时与排期。** 附件第 326 行写总投入约 274 小时；第 332–343 行逐周合计为 278 小时。278 仍只是预算，尤其 ERPNext 初始化、通用文档抽取、前端交互、恢复测试很容易产生额外耗时。建议保留缓冲，而不是把每个框架写成同周硬性完成项。这是工作量判断，不是经过实测的个人速度估计。

**2. 第一个月先以业务链验收，不以协议数量验收。** 附件第 347 行提出“三份不同报价 → 带证据标准比较表”是早期关键里程碑，这比 MCP/多 Agent 是否已经接入更重要。MCP、AG-UI 可在数据合同和执行网关稳定后接入；它们不应推迟第一条可验证链路。当前 Alpha 只是键值布局四格式解析，不代表任意商业报价单已经解决。

**3. 不把结果未知当作安全重试。** 附件第 800–803 行的成功后超时场景应进一步要求远端唯一操作键/原子幂等端点；“查一下，没有就再创建”并不能单独解决并发与读写可见性。当前 mock ERP 在自己的持久化数据库中对 operation key 做唯一约束；遇到不确定结果，服务改为只读核对，缺少确定证据时进入 NEEDS_HUMAN。本实现不宣称跨所有真实外部系统的 exactly-once 保证。

**4. 审批绑定更完整的执行意图。** 除数量、金额和报价版本，还绑定需求版本、供应商、单位、确认人、证据与源文件哈希、政策版本/哈希、ERP 模式/公司/目标指纹、交易日期。远端返回还校验公司、单位、物料、数量、总额与快照，不只看“返回了一个 ID”。审批之后修改字段需要重新核对和分析；进入执行预留后冻结需求，以防在途漂移。

**5. 可运行 Alpha 与四周 MVP 分开。** 附件目标栈仍可保留。但没有真实 ERPNext、模型密钥、PostgreSQL 实例时，模拟模式是开发替身，不是集成验收证据。本轮交付的定位是“采购业务内核与本地模拟闭环 Alpha”。原生 Next.js、真实 ERP 与模型联调、LangGraph 等都必须分别通过验收后才计入完成。

**6. 60-task benchmark 应逐步冻结，不用工程测试冒充。** 附件第 589–610 行的 30/10/20 任务集划分可以作为后续起点，但场景数较少，不能据此保证未知真实业务的稳健性。当前工程测试只证明被测试的业务合同和失败路径，不给模型质量、成功率或成本下结论，也不宣称已有 60 任务集。

## 外部核查与取舍

DeepSeek Harness 官方 README 当前仍写 Developer Preview，并提示破坏兼容的更新；因此保留实验适配层、后置接入是合理的。这里不复述附件中模型 ID 的时效性断言：本实现让 `LLM_MODEL` 由实际账户配置，未在真实账户验证任何模型可用性。

Celery 官方 Tasks 文档明确讨论幂等与消息确认/重投递，不能把队列语义直接当成业务副作用只发生一次的保证。本轮先实现业务 Outbox 与操作账本；Celery 后续只能替换调度，不应重新实现授权与幂等规则。

Frappe REST 的资源读写和 token 认证适合作为真实 ERP 适配基础，但正式写入还需要本地 ERPNext 配置、权限、DocType 字段与税费映射验证，不能只看 HTTP 200。

## 当前实现与最终目标的偏差记录

| 目标 | 当前 Alpha | 后续退出条件 |
| --- | --- | --- |
| Next.js 主前端 | 源码 + 另附不需构建的工作台 | npm 锁文件、完整类型检查、构建、浏览器 E2E |
| PostgreSQL | SQLAlchemy + 已测 SQLite | 迁移、并发/隔离/恢复在 PostgreSQL 重跑 |
| LangGraph | 确定性业务流 + 原生只读 tool loop | 固定版本、真实模型轨迹、持久化检查点 |
| RAG | 发布的合成政策对象 | 制度导入/版本/检索、独立引用评测 |
| Celery/Redis | 数据库 Outbox + Python Worker | 队列重投递、租约与并发故障验收 |
| ERPNext | 受限 REST adapter + 独立 mock ERP | 测试实例真实 draft/唯一键/回执丢失验收 |
| AG-UI/MCP/Skills | 尚未实现协议/加载器 | 实际 client discover/invoke 与事件兼容测试 |

## 官方参考（非代码已完成证明）

- DeepSeek Harness: https://github.com/deepseek-ai/deepseek-harness ，README 的 Developer preview。
- Celery tasks: https://docs.celeryq.dev/en/stable/userguide/tasks.html ，幂等、确认与重投递。
- Frappe REST: https://docs.frappe.io/framework/user/en/api/rest ，资源 API 与认证。
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence ，执行状态持久化边界。
- Next.js installation: https://nextjs.org/docs/app/getting-started/installation ，App Router 与运行环境。

当前版本的实现证据位于源码、`evals/reports/pytest.xml` 与 `http-smoke.json`；参考文档不代替运行结果。
