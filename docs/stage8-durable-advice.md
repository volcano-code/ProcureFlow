# 持久化、证据绑定的只读 Agent 建议

本增量沿用 FastAPI + 原生 Next.js + 独立 Worker，把已有可选只读模型适配器接入采购工作台。它不替代确定性比价、独立审批或 ERP 操作账本。

## 可实际使用的功能

- 采购员在原生工作台显式新建建议，先预留运行，再执行一次模型调用流程。接口：`POST /api/v1/requests/{id}/advice-runs`，请求包含当前 `expected_version` 和 8–80 位 ASCII 字母、数字、`_`、`-` 组成的 `idempotency_key`；同键同版本返回原运行，同键换版本拒绝。
- `POST /api/v1/advice-runs/{id}/process` 只能领取 PENDING 运行一次；`GET /api/v1/advice-runs/{id}` 和 `GET /api/v1/requests/{id}/advice-runs` 可读回持久化结果。列表最多返回最近 20 条。审批人、审计员可读，只有采购员可新建/执行；跨工作区返回 404。
- 输入 SHA-256 绑定需求版本、当前报价/确认/人工修正、源文件内容哈希与片段、当前政策。请求和报价在同一业务聚合锁下读取；模型联网期间不持有数据库锁。
- 执行前发现变更或来源损坏，直接 STALE 且不调用模型；执行期间变更，结果也不能发布为当前建议。已完成的历史记录保留，读取时 `current=false` 和 `stale_reason` 明确标记过期。普通审批和 ERP 执行不会因为查看建议而被修改。
- 模型必须读取服务端比较。新持久化路径还要求每个引用片段先通过受限 `get_evidence` 工具读取；存在来源时不能空引用。禁止陌生工具、其他需求的文档和不存在的引用。`evidence_read_verified=true` 只说明引用存在且已读取，`semantic_factuality_verified=false` 始终保留，不声称机器验证了推理或事实蕴含。
- 输出、固定错误码、时间、运行元数据保存在新 `advice_runs` 表。不会保存模型私有推理、原始异常、API 密钥或完整模型消息。模型费用未知；token 只按提供方报告，不估算为真实费用。

## 中断与重复请求

状态为 PENDING → RUNNING → COMPLETED / FAILED / STALE。RUNNING 在 90 秒租约后视为 INTERRUPTED；读取只计算状态，显式 process 可保存中断收据。中断、失败和完成运行都不自动重放。超时之后才返回的旧调用不能覆盖中断结果。再次调用必须由采购员显式新建运行，并可能产生新的模型费用。

工作台切换身份/需求/版本会取消旧浏览器等待并丢弃迟到结果；取消浏览器等待不等于取消服务器模型请求。刷新和轮询只 GET 持久化状态，永不自动 POST process。预留后离开页面的运行保持 PENDING，回来后需要显式执行。

这是持久化运行/结果账本，不是逐工具 checkpoint 或跨模型调用恢复引擎。既有 `/advice` 临时接口保留兼容；它没有新的持久化与严格来源读取语义。新的工作台只使用 advice-runs。

## 配置与迁移

仍使用服务端 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 和可选 `LLM_THINKING_MODE`。没有附带或创建真实凭据；默认模型未配置。capabilities 的 `advice_configured` 仅检测 key/model 是否存在，不是连通性、兼容性或质量证据。只在对服务端配置的提供方及所发采购数据已有适当授权后显式运行；不要将凭据写入前端或仓库。

新增 Alembic revision `5ce3ab9b84a2`，父版本 `426d852ce82c`，只新增表和索引，不重写业务记录。已有私有/ PostgreSQL 实例须先备份，并在副本验证后运行：

```bash
cd services/api
PYTHONPATH=. python -m alembic upgrade head
```

本地 SQLite demo 继续使用 create_all；私有模式和 PostgreSQL 的 readiness 会要求新表与迁移头。降级会删除 advice_runs 的建议历史，不能在没有备份时随意执行。迁移往返测试仅针对临时测试数据库。

## 验证及边界

新增 API 测试覆盖重复预留、并发领取、重启、崩溃中断、输入变化、文档篡改、政策快照、角色/租户隔离、错误脱敏，以及所有采购业务表保持不变。适配器测试包括缺来源、未读取引用、错误工具/JSON 既有回归，以及真实适配器代码 + 合成 provider transport 的持久化集成。PostgreSQL gate 同时运行新 advice API 套件。

原生 Next 用例覆盖未配置提示、历史恢复、重复点击、失败/过期、中断、切换请求/身份后的迟到响应和历史读取错误。新建议成功/失败浏览器场景使用明确标记的 HTTP 路由 fixture；这不是实际提供方调用或模型质量评估。全部通过情况必须以对应提交的真实 CI 记录为准，不能把用例存在当作通过。

没有接入 LangGraph、MCP、AG-UI 或新的支付/ERP 权限；没有真实模型质量/成本结论，也没有把这些工程测试计为 10-task 或 60-task benchmark。本增量不启用生产访问，不提交采购订单，不合并或部署。
