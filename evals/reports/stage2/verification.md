# ProcureFlow v0.1.0a2 · 复核与阶段二交付报告

日期：2026-09-22。对象：用户提供的 v0.1.0a1 源码及本轮增量。定位仍为本地 Alpha；不是原规划的四周 MVP、生产发布或真实采购系统完成报告。

## 1. 结论：可以沿现有代码推进，但不能宣布原规划验收完成

首版“确定性业务内核 + 本地模拟 ERP 闭环”的结果基本可复现：原始源码重新运行得到 **80 passed, 2 skipped**。但其测试不覆盖所有风险，本轮补充反例后发现了实际缺陷。

按原规划，第 1 周包含真实 ERPNext，第 2 周包含浏览器报价核对，第 4 周要求真实 Agent→ERPNext 草稿闭环。这些目前仍有未满足项。不能把 80 项或本轮 111 项工程测试，改称 60 个采购任务的成功率，更不能据此声称“全部 MVP 功能完成”。

本轮在原代码上实际推进：原生前端路径补齐、共用传输层测试、ERP/模型集成安全修复、强制终止 Worker 恢复验证和严格验收入口。**里程碑 A 的代码与验收设施已推进，但其 Next.js 构建和原生浏览器门槛未通过；里程碑 B 仅推进适配器与只读预检，未完成真实系统接入。**

## 2. 本轮发现与修复

| 问题 | 首版证据 | a2 处理与验证边界 |
| --- | --- | --- |
| 演示身份能配置真实 ERPNext | 对未经修改的 a1 加入反例，Settings 未拒绝 demo+erpnext | 配置启动阶段拒绝，包括只读访问；真实 ERP 必须 private 身份；新回归通过 |
| 模型返回 message=null 导致未处理异常 | 同样在 a1 复现 AttributeError | 结构化消息校验；空消息、非法工具格式、重复调用 ID、截断输出得到明确失败，不进入业务执行 |
| 模型未读取比较数据也能返回解释 | 增加针对输出前置条件的测试 | 最终解释必须先成功调用 get_comparison；仍不宣称自动验证了解释文本的全部事实蕴含 |
| Next.js 缺示例加载/需求修改入口 | 对照 TSX 与现有静态页面、验收脚本检查 | 补三文件加载、字段显式确认、需求修改、拒绝审批、失效状态、身份切换与刷新；原生 UI 运行验收尚未完成 |
| 现有浏览器测试只针对静态工作台 | 原测试访问 FastAPI 根路径，并未运行 Next 生产服务 | 新增专门启动 Next 的严格门槛和 4 条原生页面用例；缺依赖即失败/阻断，不回退到静态页面 |
| ERP 创建仅检查返回记录不够 | 新增 MockTransport 回读篡改及格式错误案例 | POST 后再 GET 持久化文档；核对键、快照、公司、物料、数量、单价、总价、单位、日期、草稿状态 |
| 旧 HTTP smoke 只有优雅重启 | 原报告已明确不等于 SIGKILL | 新增两个真实子进程 SIGKILL 场景；模拟 ERP 数据库独立持久化；租约过期采用显式测试注入 |

`baseline-regression-proof.log` 中的 2 个失败是**对原版 a1 的预期失败证明**，不是 a2 核心回归失败。a2 对应回归通过。另一个 `strict-browser.log` 中的 2 个失败则是真实浏览器验收被环境策略阻断，不能与前者混淆。

## 3. 本轮新增代码

### 原生 Next.js 工作台

`apps/web/components/workbench.tsx` 增加示例导入、创建/修改需求、人工确认所有报价字段、审批/拒绝、版本状态、按能力显示模拟/真实 ERP 边界。示例导入不自动确认字段、不代替独立审批。金额保持后端 Decimal 字符串；前端不自行计算采购金额。

身份和需求切换清空旧证据/方案，迟到的请求结果通过请求序号与身份范围校验丢弃。访问令牌只留在内存，刷新后重新登录；业务数据保存在后端。每个写操作使用进行中锁，传输层不自动重试业务写入。

`apps/web/lib/transport.mjs` 是前端实际引用的共用传输实现：认证请求、multipart、错误规范、SSE UTF-8 分片解码、断线游标/去重、取消和鉴权失败终止。Node 测试使用同一份实现，不是另写一套“测试专用前端”。这些测试不验证 React 渲染、页面布局或 Next 构建。

### 集成防护

`config.py` 禁止 demo 接真实 ERP，新增显式 CORS origin 列表。演示样例端点受认证、角色、模式及固定文件白名单限制。

`agent.py` 加入返回结构、工具 ID、输出截断和比较工具前置检查；模型/工具事件可写审计，记录参数哈希而非把报价正文复制到新增工具事件中。工具权限仍只读；没有模型实际调用成绩，也不是 LangGraph 集成。

`erp.py` 加入两个 Custom Field 检查与创建后独立回读。`scripts/preflight.py` 默认仅离线检查环境；显式 `--erp-read-only` 且私有身份/ERP 配置完整时才调用只读预检。该 CLI 将适配器写权限强制为 false，只做 GET，不创建、提交或删除任何单据。元数据 unique=1 不等于已经验证真实数据库唯一约束。

### 验收设施

新增 `scripts/verify_next.py`、4 条 Next 原生浏览器用例、14 条前端传输单测、3 条真实 HTTP 前端流程测试、强制终止 Worker 场景及 ERP/模型回归。CI 增加独立 native-next-browser job，明确不以静态页面替代原生 Next。

本轮未改业务表结构；更新了 OpenAPI 合同。未推送 GitHub，CI 只是配置，尚无远程执行记录。

## 4. 实际运行结果

| 验证项 | 实际结果 | 能支持的结论 |
| --- | --- | --- |
| 未修改 a1 原测试复跑 | **80 passed, 2 skipped** | 旧报告核心结果可复现；浏览器仍未验收 |
| a2 默认 Python 全套 | **111 passed, 2 skipped**，0 failed/0 errors | 113 个收集项；包含参数化测试，不是 113 个独立采购任务 |
| 前端共用传输单测 | **14 passed** | 金额字符串、请求体、错误、SSE、取消等按案例成立 |
| 前端共用传输→真实 HTTP | **3 passed** | 经 Node fetch/FormData 访问真实 API；不是浏览器/React E2E |
| 原 HTTP + 独立 Worker smoke | **passed，14 步** | SUP-C、24,200.00 元、重放后 1 张模拟草稿 |
| Worker SIGKILL | **2 场景通过，已计入 111 项** | 两个明确 kill point 的恢复/保守停止按预期成立 |
| TypeScript 转译语法检查 | **5 文件通过** | 仅语法；没有完成完整类型检查和 Next build |
| 严格静态浏览器门槛 | **2 failed**，`ERR_BLOCKED_BY_ADMINISTRATOR` | 仍未完成浏览器验收；不能用于发布通过结论 |
| npm 依赖安装 | **失败**，`EAI_AGAIN registry.npmjs.org` | 包源 DNS 不可用；没有生成可审阅的 lockfile |
| 严格原生 Next 门槛 | **blocked / exit 2**，`NEXT_DEPENDENCIES_MISSING` | 4 条新页面用例尚未执行；没有回退或伪造截图 |
| ERPNext 协议与只读预检 | MockTransport 测试通过 | 不是实际 ERPNext 服务或真实数据库验证 |
| 真实 ERPNext / 真实模型 | **未执行** | 无连接凭据/测试实例，未调用、未写入 |
| PostgreSQL / Docker | **未执行** | 缺相关运行环境，不声明已验收 |
| 远程 CI / 60-task benchmark | **未执行 / 未完成** | 不报告成功率、成本、覆盖率或最终求职成绩 |

默认测试中的 2 个 skip 与严格模式中的 2 个环境失败是同样的浏览器用例；没有重复计成额外通过项。111 项包含 2 项 SIGKILL，不再另外相加夸大数量。

### 实际终止恢复行为

| 强制终止点 | 子进程 | 恢复后状态 | 模拟 ERP 草稿数 | 说明 |
| --- | --- | --- | ---: | --- |
| 在可能创建之前，已持久化 IN_FLIGHT | SIGKILL，返回码 -9 | NEEDS_HUMAN | 0 | 查不到对象并不能证明先前没有写入；不盲目补写 |
| 远端模拟库已提交，Worker 尚未收到回执 | SIGKILL，返回码 -9 | COMPLETED | 1 | 新 Worker 只读核对并确认同一对象，无第二次创建 |

两个场景都使用真实独立 Python 子进程及独立 SQLite 模拟库，恢复调用真实 Worker。租约过期通过修改测试时钟字段注入，不是等待真实租约时间。它们不证明跨主机网络、任意 kill point 或真实 ERP 都有 exactly-once 保证。

## 5. 原始证据位置

`evals/reports/stage2/verification.json` 汇总机器可读结论，记录原始 a1 ZIP 的 SHA-256。

- `baseline-pytest.log/xml`：a1 原测试复跑。
- `baseline-regression-proof.log`：在 a1 复现新发现的两个缺陷。
- `final-pytest.log/xml`：a2 最终 111/2 默认回归。
- `frontend-unit.log`、`frontend-http.json/log`：14+3 测试。
- `http-smoke.json/log`：真实 HTTP 与独立 Worker。
- `crash/*.json`：SIGKILL、返回码、恢复状态和草稿数。
- `strict-browser.log/xml`：严格浏览器失败证据。
- `npm-install.log`、`next-gate/next-gate.json`：安装失败和 Next 门槛阻断。
- `typescript-syntax.json`：仅语法检查。
- `environment-preflight.json`：离线环境和配置是否存在，不含密钥。

`evals/reports/` 根目录保留的 a1 日志是历史记录；`stage2/` 内 `core-progress.*` / `core-pytest.*` 是阶段中的中间运行。**最终数字以 final-pytest 和 verification.json 为准。**

## 6. 尚待跨过的下一阶段门槛

A：在获准访问依赖源与本地服务的环境完成 npm 安装、审阅 lockfile、完整 typecheck/build、4 条原生页面 E2E；保留真实截图和错误日志。现有 UI 功能源码已补，但不能宣布 A 完成。

B：授权 ERPNext 沙箱及 PostgreSQL；先只读预检，再通过独立审批测试零税/零运费/零折扣草稿和回读、同键并发与不确定结果。当前三供应商演示中 A/C 的税费运费映射仍不支持直接写入真实 ERP。

C：使用账户实际模型与可安装的 LangGraph 实测只读 Agent，再完善受控制度检索、MCP/Skills/AG-UI。后续才做冻结业务评测，不以工程测试替代模型任务评价。

无需推倒重写，也不应现在优先堆 Multi-Agent、GraphRAG 或 RL。保持现有确定性内核，逐项通过真实集成验收。
