# 阶段十九：只读模型异常与等待恢复

基于 `5aa6092dbff6f34601c206dc686188bdac7deb70`（tree `957d25e88f31e9091fe67099b260364417360d90`）的本地开发增量。仅使用合成数据、离线 fake/spy transport 和本地测试服务；未调用真实模型、配置凭据、推送、合并或部署。验收原始结果以本轮交付包的确切提交与记录为准。

## 审计后的实际缺口

原有实现已具备响应上限、图与工具预算、来源范围/已读取检查、一次性建议领取、失效快照和身份切换保护。本轮保留这些实现，补充以下实际缺口：

- `iter_bytes(chunk_size=8192)` 会先聚合小数据块，导致时限检查等到缓冲满才执行。现在每个实际解码块后检查截止时间与取消条件；超过尺寸立即关闭流，不会静默截断。
- 连接建立前的短暂失败可以有限恢复，但读取/写入超时、HTTP 错误和不合格响应均可能已被提供方处理，不能盲目重发。
- 拒绝、空内容、错误 choice/role/finish_reason 以前可能被忽略或混为通用错误，现在有固定、可持久化的分类。
- 被截断/拒绝的响应可能已报告 token，以前在协议校验前丢失。现在先保存有界报告值，再记录失败；仍不推断账单或价格。
- 浏览器建议请求以前没有自身等待上限，且同一需求更新后的迟到预留可能继续发起模型执行。现在可停止等待、读取回执，且在快照变更后不再派发迟到预留。

## 重试合同

`ReadOnlyAgent(max_connection_retries=1)` 默认每个图模型节点至多重试一次，可配置为 0–2。仅限 `ConnectError`、`ConnectTimeout`、`PoolTimeout`，即 transport 表示尚未发送 HTTP 请求的失败。短退避为 0.1、0.2 秒，包含在原截止时间内。

每次 transport 尝试都占用原 `max_model_calls` 总上限（默认 4），不会因重试得到额外预算；输出 `provider_attempts` 是所有尝试，`model_calls` 是图模型轮数。调用接近上限时可能没有余量完成下一轮，安全停止。历史回执缺少 `provider_attempts` 时显示未知。

不重试 HTTP 429/5xx、读取/写入超时、连接中断、响应解析/校验失败、拒绝、工具失败或来源不合格。无 LangGraph retry/checkpoint/resume，也无失败账本重放。重复 `/process` 只回读原记录；再次调用仍需显式新建运行。`verify_model_acceptance.py` 的明确单次提供方验收继续将连接重试设为 0。

## 取消与时限的真实保证

- Agent 支持合作式取消回调，在节点、provider 请求、每个解码块及返回结果边界检查。
- 持久化建议绑定原需求/报价/政策/源文件与身份。当输入改变、来源损坏、会话退出/到期/撤权或运行租约失效时，停止后续模型/工具调用，不输出当前有效建议。
- 这是合作式检查，不能抢占阻塞的自定义 transport、工具或数据库操作。HTTP 每阶段 timeout（最多 10 秒）仍必要；35 秒是运行软时限，不是硬杀死保证。独立验收 worker 继续有进程级总时限。
- Next 的读取等待上限为 15 秒，预留/执行等待上限为 60 秒。“停止等待并回读状态”只取消浏览器等待，不声称服务端或提供方取消，不保证不计费，也不重新发送 POST。界面回读进行中/失败时不把旧回执标为当前有效。

## 响应与诊断

响应须为单个 choice、assistant 消息及受支持且一致的完成原因。为兼容既有适配器，缺省 role、choice index 和 finish_reason 仍可接受；存在但错误则拒绝。结构化 narrative 仍受现有严格字段/长度/来源合同约束。来源真实存在且已读取依旧不证明解释语义成立，`semantic_factuality_verified=false`。

新增 `MODEL_TIMEOUT`、`MODEL_CANCELLED`、`MODEL_REFUSED`、`MODEL_EMPTY_OUTPUT`。原有 `MODEL_PROTOCOL_INVALID`、`MODEL_OUTPUT_INCOMPLETE`、`MODEL_RESPONSE_LIMIT`、`MODEL_SCHEMA_INVALID`、来源/工具错误保持有效。finish_reason=content_filter 属于拒绝，length 属于截断。

失败回执仍 `output=null`，不会把半成品解释作为结果。审计增加固定 `model_attempt_failed`/`graph_node_failed` 元数据，不含原始 provider body、refusal、reasoning、凭据或异常链。`known_usage` 只表示已收到的合法报告计数；失败的总 `usage=null`、`usage_complete=false`、`cost=null`，无报告绝不变成零。成功运行任一轮缺少合法 usage 时，总 usage 仍未知。

采购金额、最低价/资格、审批和 ERP 操作继续由确定性代码控制。模型权限未扩大。数据库 schema、迁移头和实际提供方配置均未改变。

## 可重复的离线门槛

```bash
python scripts/test.py -q tests/test_model_failures.py tests/test_graph_runtime.py tests/test_advice_runs.py tests/test_pilot_workflow.py
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
python scripts/verify_model_acceptance.py --fixture
python scripts/evaluate_procurement.py --fixture --output evals/reports/local/procurement-development.json
npm run test:unit --prefix apps/web
npm run typecheck --prefix apps/web
python scripts/verify_next.py --output evals/reports/local/next-gate
```

故障矩阵覆盖连接重试耗尽/总预算、读取/写入超时、不重试状态码、畸形/截断/超大/错误结构、拒绝/空输出、伪造引用、未知计量、小数据块截止/取消、源/政策/需求变化、退出/到期/撤权和回执不重放。浏览器覆盖停止等待、迟到响应、同需求变化、失败说明与历史缺失指标。

这些结果只证明合成故障下的工程行为。不是实际厂商兼容性、真实模型质量、语义正确性或费用验收，也不能代表未运行的 PostgreSQL、真实 ERP、容器部署或独立安全审查。独立安全审查仍未完成。

## 本轮环境限制

本地 Python 3.12.14、Node 24.19.0。原生 Next 类型检查与生产构建通过，前端单测 96 项通过。Chromium 启动被运行环境拒绝 Unix socket 创建（`CHROMIUM_UNIX_SOCKET_DENIED`），42 个浏览器用例全部停在 setup，实际执行数为 0；新增的 10 个浏览器场景未验收，无截图。未更换路线绕过限制。完整后端计数和原始记录见交付包。
