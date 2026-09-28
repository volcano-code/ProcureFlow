# 当前 Alpha 架构与业务合同

## 已实现架构

```text
本地静态工作台 / 原生 Next.js 源码 / HTTP 客户端
                    │ Bearer + typed JSON / SSE
                    ▼
              FastAPI Business API
         身份解析 → 租户作用域 → 业务服务
                    │
  文档解析 / Decimal / 版本 / 审批快照 / 操作账本
                    │
      SQLAlchemy + SQLite（本轮已验证）
       ├─原文件：受控本地存储，SHA-256
       ├─Outbox：独立 Python Worker 或 API 触发
       └─审计事件：业务事实，不含模型隐藏思考过程
                    │
             ERPPort（草稿边界）
       ├─独立 SQLite 模拟 ERP（已测试）
       └─ERPNext REST（契约测试，真实联调未做）

可选 ReadOnlyAgent → get_comparison / search_policy / get_evidence
模型输出只有解释权限，不能授权、批准、创建外部对象。
```

不把当前实现称为 LangGraph、AG-UI、向量 RAG 或 Celery。业务规则不依赖运行时名称，后续框架应通过明确接口复用已有服务。

## 数据表

`procurement_requests`：需求、当前业务状态、版本与方案快照。
`documents`：原始文件的租户/需求归属、哈希、受控存储位置、文本片段。
`quotes`：逻辑报价与当前版本。
`quote_versions`：字段、证据、解析警告、确认信息；修改产生新版本。
`approvals`：审批人、快照哈希、过期时间、批准/拒绝/失效状态。
`external_operations`：稳定操作键、批准快照、执行状态、租约、远端 ID。
`outbox`：与操作预留同一事务产生的执行意图。
`audit_events`：带租户与 actor 的业务审计。

Alembic revision 创建上述 8 表及索引/唯一约束；SQLite 的 upgrade/downgrade/upgrade 已实测。数据库管理员仍可篡改审计，当前不是密码学不可抵赖的审计系统。

## 金额合同

所有 API 金额/数量使用十进制字符串；禁止使用 JSON number/float 替代金额。

先将 quantity × unit_price 舍入到分，扣除明确的商品绝对折扣；若不含税，则单独计算并舍入税额；最后加明确的最终含税运费。采用 ROUND_HALF_UP。

“含税”报价可不额外要求税率来计算总成本，但该字段仍可保持未知；“不含税”报价缺税率则总成本未知。缺运费、缺折扣值、税价模式不明等均不默认为零。单位、币种、SKU、数量不匹配不做隐式换算。该简化合同不是任意 ERP 税务模型，更不是税务建议。

## 证据和版本

文档不可原位修改；导入时哈希，查看证据及生成/执行快照时重新验证实际文件哈希。源文件不在公共静态目录，证据读接口仍校验租户。

PDF 提供页码与抽取文本行号，不伪造视觉坐标；XLSX 提供真实 sheet 与 cell range；CSV 和 TXT 分别记录行/列或行号。重复同一需求的相同文件可去重。

人工修改不会改写源文件或冒充抽取证据。新版本记录 `manual` 来源、修正原因、操作者与前一版本，等待再次确认。关键字段仍是 unknown 时，“人工确认”不会自动使报价符合规则。

## 审批合同

服务端重建规范 JSON 并计算 SHA-256。快照含需求和报价版本、全部报价值、确认人、证据和源文件哈希、规则版本、总价、ERP 目标指纹、公司和日期。

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

PostgreSQL 路径保留 aggregate row locking 代码，但本轮没有服务实例、驱动和并发实测，不能把 SQLite 的测试结果推广为 PostgreSQL 保证。真实 ERP 唯一键和读写一致性也必须另测。

## 恢复测试的准确范围

已有：对象/数据库重开、模拟成功后丢回执、过期在途租约、并发执行、实际 HTTP 服务优雅重启、独立 Worker 进程双次 drain。

没有：SIGKILL 全窗口故障注入、机器断电、磁盘损坏、跨主机租约、真实 ERP 服务重启、读副本延迟测试。不能把当前结果写成“所有崩溃场景均已验证”。


## v0.1.0a2 增量（2026-09-22）

业务表与审批/Outbox 授权源不变。新增 capabilities、合成样例 API、严格模型消息 Schema、要求读取比较工具后才允许解释、逐工具审计事件、ERPNext Custom Field/持久化回读核对。真实 ERP 目标仅 private 模式可用；CORS origin 由显式配置限定。

原生 Next 共用 `transport.mjs` 可由 Node 对真实 HTTP 检验，但这不替代 React/Next 浏览器验收。严格 native UI gate 当前因依赖缺失阻断。真实 SIGKILL 两例仍只针对模拟 ERP；详细边界与结果见 `stage2-review.md`。
