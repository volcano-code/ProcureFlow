> 历史路线图（a2）。这里的“未完成”与测试数保留当时记录，不代表当前代码。政策版本、普通 CSV/XLSX 导入和最新验收入口请看 [README](../README.md)、[阶段十一](stage11-tenant-policy.md)、[阶段十二](stage12-tabular-import.md)。真实模型质量/预算、OCR、多商品与生产部署仍不属于本轮已验收能力。

# a2 后续验收：从本地 Alpha 到真实集成 Beta

这不是后台任务或已经完成的工作。下面是对本轮产物的下一批验收门槛，而不是再生成一份十二周技术清单。

## 当前已推进，但不标记完成

a2 已补 Next 示例加载/需求编辑/显式确认/拒绝与身份隔离路径，完成共用传输 14 单测和 3 条真实 HTTP 流程，编写了 4 条原生 Next 浏览器用例及严格 gate。ERP 侧增加只读预检和独立 GET 回读；两个子进程 SIGKILL 情景已验证。具体复核与未通过项见 `stage2-review.md`。

## 里程碑 A：先把现有界面验收完

在允许访问包源和本地服务的开发机安装 Next 依赖并锁定版本，运行 typecheck/build；修正真实浏览器发现的问题。运行已经补齐的演示加载、需求修改与审批失效路径。严格执行新增 4 条原生 Next 测试以及 2 条静态工作台兼容性测试，不允许因环境缺失被跳过后仍宣称前端完成。

退出条件：干净 clone 可启动；Next build 成功；三份报价与证据→审批→模拟草稿、审批后改数量被阻止均通过真实浏览器；不存在重大 console error/溢出或交互阻断。提交截图、视频、lockfile、原始 E2E 日志。

## 里程碑 B：真实 ERPNext 沙箱 + PostgreSQL

启动授权的本地/测试 ERPNext，不操作生产 ERP。创建测试 Company/Supplier/Item 和最小权限账户；建立真实唯一 operation key 与 snapshot 字段。先接通零税/零运费/零折扣单物料草稿，再扩展本轮样例用到的税费映射。

退出条件：真实 Supplier Quotation docstatus=0；回读物料、单位、公司、金额与快照一致；同键并发/响应丢失不会生成第二张草稿；未知结果可核对；没有 Submit。迁移/并发/权限测试在 PostgreSQL 重跑。记录准确 ERPNext/Frappe/数据库版本和配置。

## 里程碑 C：第一个经过实测的模型 Agent

接入账户实际可用的模型 ID，保留 read-only adapter 作基线。固定 LangGraph 依赖后增加 Planner/Tool/Verifier 有界图与持久化 checkpoint；业务授权仍走现有服务。把合成政策扩展到发布/有效期受控的制度片段，先做精确检索基线，不急着上 GraphRAG。

退出条件：真实模型调用和工具轨迹可复核；无来源字段保持未知；无权限写操作为 0；错误 JSON、缺失证据、预算耗尽有明确失败状态；重启不丢审批等待上下文。先做 10 个 development 任务，不把当前工程测试冒充模型评测。

## 阶段后的优先级

以上闭环稳定后接 MCP、AG-UI、Skills，之后再测试 Hybrid RAG、受控多 Agent 与跨进程 A2A。DeepSeek Harness、A2UI、GraphRAG、自动优化与 RL 进入独立实验目录，不在没有基线和数据时挤占主链路。
