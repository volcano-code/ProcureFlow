# 当前 Alpha 架构与业务合同

## 已实现架构

```text
本地静态工作台 / 原生 Next.js 工作台 / HTTP 客户端
                    │ Bearer + typed JSON / SSE
                    ▼
              FastAPI Business API
         身份解析 → 租户作用域 → 业务服务
                    │
  文档解析 / Decimal / 版本 / 审批快照 / 操作账本
                    │
      SQLAlchemy + SQLite / PostgreSQL
       ├─原文件：受控本地存储，SHA-256
       ├─Outbox：独立 Python Worker 或 API 触发
       └─审计事件：业务事实，不含模型隐藏思考过程
                    │
             ERPPort（草稿边界）
       ├─独立 SQLite 模拟 ERP（已测试）
       └─ERPNext REST（合成隔离沙箱与独立回读）

可选 LangGraph ReadOnlyAgent → get_comparison / search_policy / get_evidence
模型输出只有解释权限，不能授权、批准、创建外部对象。
```

只读建议已使用真实 LangGraph 图；不宣称 AG-UI、向量 RAG、Celery、图 checkpoint 或可自动恢复模型调用。业务规则和审批仍由确定性服务负责。当前提交是否通过环境门槛必须核对对应 CI；早期阶段报告不是当前验收结果。

## 数据表

`procurement_requests`：需求、当前业务状态、版本与方案快照。
`documents`：原始文件的租户/需求归属、哈希、受控存储位置、文本片段。
`quotes`：逻辑报价与当前版本。
`quote_versions`：字段、证据、解析警告、确认信息；修改产生新版本。
`approvals`：审批人、快照哈希、过期时间、批准/拒绝/失效状态。
`external_operations`：稳定操作键、批准快照、执行状态、租约、远端 ID。
`outbox`：与操作预留同一事务产生的执行意图。
`audit_events`：带租户与 actor 的业务审计。
`advice_runs`：版本与来源绑定的只读建议运行/结果账本，含一次性领取、中断和过期状态。

后续表还包含租户政策及版本、`table_imports`、pilot 身份／会话、`system_state` 和永久 `recovery_holds`。Alembic 迁移创建业务表及索引/唯一约束；接收基线的 head 为 `e731bb62c905`（维护与恢复围栏），部署前仍需执行 `alembic heads` 核对。SQLite 与 PostgreSQL 有独立迁移门槛。数据库管理员仍可篡改审计，当前不是密码学不可抵赖的审计系统。

## 金额合同

所有 API 金额/数量使用十进制字符串；禁止使用 JSON number/float 替代金额。

先将 quantity × unit_price 舍入到分，扣除明确的商品绝对折扣；若不含税，则单独计算并舍入税额；最后加明确的最终含税运费。采用 ROUND_HALF_UP。

“含税”报价可不额外要求税率来计算总成本，但该字段仍可保持未知；“不含税”报价缺税率则总成本未知。缺运费、缺折扣值、税价模式不明等均不默认为零。单位、币种、SKU、数量不匹配不做隐式换算。该简化合同不是任意 ERP 税务模型，更不是税务建议。真实 ERP 映射还要求明确税率、整数 EA、受限金额及可逐项严格回读的舍入结果，见 [费用映射边界](erp-cost-mapping.md)；确定性可比较不代表 ERP 一定可表示。

## 证据和版本

文档不可原位修改；导入时哈希，查看证据及生成/执行快照时重新验证实际文件哈希。源文件不在公共静态目录，证据读接口仍校验租户。

PDF 提供页码与抽取文本行号，不伪造视觉坐标；XLSX 提供真实 sheet 与 cell range；CSV 和 TXT 分别记录行/列或行号。重复同一需求的相同文件可去重。

人工修改不会改写源文件或冒充抽取证据。新版本记录 `manual` 来源、修正原因、操作者与前一版本，等待再次确认。关键字段仍是 unknown 时，“人工确认”不会自动使报价符合规则。

## 审批合同

服务端重建规范 JSON 并计算 SHA-256。快照含需求和报价版本、全部报价值、确认人、证据和源文件哈希、规则版本、总价、ERP 目标指纹、费用映射版本、税/运费账户、公司和日期。历史快照不被补写；缺少新费用字段的旧快照在执行/回读前安全阻断，不自动重放。

审批请求必须提交用户正在看的哈希；服务端比较当前哈希，不接受只传 `approved=true`。采购创建人与审批人必须不同。执行预留与实际首次 dispatch 均检查审批仍有效、角色未撤销、快照未变。客户端无法使用 X-Role 或任意 approver ID 提权。

本地令牌允许在演示界面切换身份，目的是测试服务端权限，不代表经过了生产级身份鉴别。私有模式需要长令牌并禁用默认演示令牌，但也不是生产 SSO。

## 外部执行状态

```text
PENDING → 持久化 IN_FLIGHT + 租约 → 查询远端
          ├─同一正确草稿已存在 → COMPLETED
          ├─首次尝试且无草稿 → 再次验审批 → 尝试创建
          │                      ├─正确回执 → COMPLETED
          │                      └─不确定 → RECONCILING
          └─恢复/核对但无草稿 → NEEDS_HUMAN

RECONCILING / 过期 IN_FLIGHT → 只读核对，绝不自动重新创建
```

同一需求只允许一个逻辑 operation；重复请求返回原操作。需求在预留之后冻结，当前尚未实现取消预留或人工重启新业务操作的完整 UI。

SQLite 使用 BEGIN IMMEDIATE 序列化写入；操作在网络前先提交状态与 30 秒租约。远端 mock 用自己的数据库唯一键原子插入；它不是业务数据库里的假状态变量。回执按供应商、物料、数量、单位、币种、公司、总价、草稿状态及快照校验。

远端“没有查到”不是“确定没创建”；当前选择停住而不是冒险重复。NEEDS_HUMAN 仍可人工触发只读核对，但没有实现“人工断言没有副作用后重开操作”的完整流程。这是有意的安全优先取舍。

PostgreSQL 使用 aggregate row locking，并有真实 PostgreSQL 并发/迁移/建议门槛。联合沙箱分别执行 SQLite/PostgreSQL 业务库 + ERPNext/MariaDB，独立审计真实唯一索引与持久化回读。各后端结果必须绑定同一确切提交，不能互相替代。

## 恢复测试的准确范围

已有测试路径：对象/数据库重开、模拟 ERP 成功后丢回执、模拟 ERP 的两个 SIGKILL 窗口、过期在途租约、并发执行、实际 HTTP 服务优雅重启、独立 Worker 进程双次 drain；真实 ERP 沙箱还有提交成功后 HTTP 504 丢回执及只读恢复。

没有：SIGKILL 全窗口故障注入、机器断电、磁盘损坏、跨主机租约、真实 ERP 服务重启、读副本延迟测试。不能把当前结果写成“所有崩溃场景均已验证”。


## v0.1.0a2 历史增量（2026-09-22）

本节仅记录当时状态；后续实现见阶段三至十及当前 README。

业务表与审批/Outbox 授权源不变。新增 capabilities、合成样例 API、严格模型消息 Schema、要求读取比较工具后才允许解释、逐工具审计事件、ERPNext Custom Field/持久化回读核对。真实 ERP 目标仅 private 模式可用；CORS origin 由显式配置限定。

原生 Next 共用 `transport.mjs` 可由 Node 对真实 HTTP 检验，但这不替代 React/Next 浏览器验收。严格 native UI gate 当前因依赖缺失阻断。真实 SIGKILL 两例仍只针对模拟 ERP；详细边界与结果见 `stage2-review.md`。


## 普通表格导入（阶段十二）

新的 `table_imports` 是租户/需求/源文件哈希唯一的持久预览，不是报价，更不是审批。
普通 CSV/XLSX 先上传，返回真实工作表/单元格和保守的表头建议；用户明确选择工作表、表头行、报价行及列映射。
`POST /table-imports/{id}/preview` 使用 revision CAS，并将映射结果绑定当前 request version。
`POST /table-imports/{id}/confirm` 只在该映射仍有效、源文件哈希未变且需求可编辑时创建一份未确认报价。
同一 revision 的重复确认只读回已有报价；不会重新创建或使过期审批重新有效。
后续人工纠错仍走带 reason 的不可变报价版本，明确核对后再确认；原有政策、建议、评估、审批和 ERP 快照门禁完全复用。
原文每个值包含文件 SHA-256、sheet、row、column/cell；未映射、公式、歧义值保持未知，禁止多商品或混币种合并。
30 分钟预览到期后需重新上传/映射；进程重启不丢预览。同租户最多保留 100 份有效 `OPEN` 预览；已过期、未导入且未被引用的预览及已归档预览不再占用有效配额，受保护或时间戳异常的项继续占用配额。可按 [预览保留](table-preview-retention.md) 显式可逆归档。归档保留原文件，不回收磁盘；没有自动保留任务。
详见 [导入协议与安全边界](stage12-tabular-import.md)。


## 恢复 hold 与只读诊断（阶段十六）

新隔离恢复保留操作账本并对旧非完成操作施加永久 hold，恢复状态为 `RECOVERY`。恢复队列、证据详情与只读 ERP 字段差异只提供诊断，不变更 operation/outbox、hold 或恢复授权。恢复后旧会话已撤销，必须先使用离线维护报告完成审核；UI/API 不提供登录旁路或 HTTP resume。具体接口、权限及当前验收状态见 [阶段十六](stage16-recovery-diagnostics.md)。
