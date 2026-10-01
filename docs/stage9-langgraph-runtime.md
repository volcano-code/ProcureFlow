# 有界 LangGraph 只读执行与合成提供方验收入口

> 阶段九历史范围：本页的单条合成协议验收随后扩展为 [阶段十的十个开发案例与独立评分](stage10-evaluation.md)。真实模型质量仍需另行授权和验收。

本增量在阶段八持久化建议之上，用实际执行的 LangGraph `StateGraph` 替换手写模型/工具循环。采购金额、资格判定、审批、ERP 与 Outbox 仍由原确定性业务代码处理。

## 执行与权限边界

图按 `START → model → tools → model … → validate → END` 执行。`model` 发出一次 OpenAI-compatible 请求；`tools` 仅能调用 `get_comparison`、`search_policy`、`get_evidence`；`validate` 检查结构化输出、来源存在及已读取证明。没有把旧循环包成单一图节点，也没有把模型接入审批或付款工具。

- 默认每次运行至多 4 次模型请求、8 次工具调用、35 秒软时限、16,000 个提供方报告 token。模型输出上限每次 1,600 token；上下文、工具输出、HTTP 响应尺寸继续受限
- 图另设 `2 × max_model_calls + 2` 步上限，转换成固定 `BUDGET_EXCEEDED` 错误。HTTP 失败、工具失败和超时不会自动重试
- 最终输出标记 `runtime=langgraph-read-only-v1` 与实际依赖版本；审计只记录固定节点名称、调用计数和既有有界工具元数据
- 模型消息、来源文本和 provider reasoning 只在每次运行的私有闭包暂存，图状态仅含计数/路由/完成标记。图在独立 contextvars 上下文执行，显式关闭 LangSmith tracing、清空回调配置，阻断环境变量或已有 collector 隐式收集。节点错误在离开私有边界前转换为固定代码/通用文案，原始校验错误和工具异常链不会交给图回调
- 保留 `evidence_read_verified` 与 `semantic_factuality_verified=false`。来源已读取不等于语义蕴含已验证；模型没有定价/审批/采购决策权

固定直接依赖为 `langgraph==1.2.12`、`langsmith==0.14.2`。`requirements.lock` 是 Python 3.12 Linux 上的运行依赖快照，不是跨平台带哈希锁，也不是依赖安全审计。相关官方资料：[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[tracing 配置](https://docs.langchain.com/langsmith/trace-without-env-vars)、[PyPI 版本](https://pypi.org/project/langgraph/1.2.12/)。

## 持久化仍由业务账本负责

图没有 checkpointer、缓存、自动恢复或重放。`advice_runs` 的版本/输入哈希/源文件完整性检查、一次性领取、90 秒运行租约、完成/失败/过期/中断回执和租户隔离保持不变。进程中断后不会从上次模型步骤重放；用户须显式新建运行才能再次调用模型，可能产生新费用。

本增量不改数据库结构。仍使用阶段八 Alembic head `5ce3ab9b84a2`；升级旧实例须按阶段八流程备份并迁移。历史建议保留当时的 runtime 字段，不改写为 LangGraph。

## 提供方验收与质量边界

新增 `scripts/verify_model_acceptance.py` 对隔离临时库中的合成需求运行真实 API/图/适配器。默认阻断，不能因运行测试而读取生产数据库或使用真实模型。离线 fixture 明确只证明协议和工程路径；live 必须单独显式允许，并可能计费。不得把 fixture 通过说成真实厂商兼容或质量通过。

离线命令：`python scripts/verify_model_acceptance.py --fixture --output evals/reports/local/model-fixture.json`。真实模式以 `--allow-network` 代替 `--fixture`，且须先获得指定提供方/型号/费用权限，再由获授权的服务端环境配置 `LLM_*`。具体预算 CLI 参数以 `--help` 为准。真实模式只使用已获授权的服务端模型端点/型号/凭据和内置合成输入，不导出完整 prompt、来源、输出正文、reasoning 或凭据。记录 token 仅为提供方报告，`cost` 保持未知，硬金额上限必须由提供方账户控制，不能由 token 估算代替。

验收项包括已读取引用、确定性未知值、持久化读回、重复 process 不重放，以及采购/审批/ERP 业务状态无变更。这些是单一合成流程的兼容/安全指标，不是 10/60 个采购任务 benchmark、成功率或真实业务质量评价。

## 本地和 CI

常规回归会实际加载并运行 LangGraph；CI 中继续执行 SQLite/Core、真实 PostgreSQL（含实际图适配器的成功/失败/来源读取集成）、Next 原生浏览器、Compose 及真实 ERPNext 合成沙箱门槛。未运行的环境必须单独列为未验收。无需新增模型 secret 即可运行离线验收；没有自动 live-model CI。

```bash
python -m pip install -r services/api/requirements-dev.txt
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
python scripts/verify_model_acceptance.py --help
```

本轮实际测试数量、截图、失败或阻断、准确 commit 与原始记录以交付验收报告为准。阶段一至八报告是历史快照，不与本轮结果混算。
