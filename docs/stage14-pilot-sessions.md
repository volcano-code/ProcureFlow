# 阶段十四：受控试点身份与会话

这是封闭、受控环境中的开发增量，仍不是企业 SSO、公网服务或生产采购上线。默认不创建账号、不调用真实模型、不连接实际 ERP。真实账户发放、凭证交付、部署、安全设置及真实数据使用都必须由有权限的操作员另行安排。

## 模式与边界

- `PF_MODE=pilot` 使用业务数据库中的租户、成员、一次性邀请和短期会话；API 与独立 Worker 读同一份数据库，没有启动时身份缓存
- `demo` 保留隔离本地合成演示；`private` 保留旧静态 `PF_AUTH_TOKENS` 兼容。它们不具备本阶段的持久会话撤销保证，不能据此声称已启用试点登录
- pilot 明确拒绝 `PF_AUTH_TOKENS`。旧审批和旧操作不会自动升级为试点授权，缺少相应授权版本时不能首次写 ERP
- pilot 只使用原生 Next.js 工作台，不提供旧静态演示页 `/`、`/assets`。API 的 `/health`、`/ready` 和登录配置仍可读
- ERPNext 可在 private 或 pilot 中显式启用，但演示身份仍不能连接。真实 ERP 写入仍只支持受限 Supplier Quotation 草稿，没有提交、Purchase Order、删除或支付

## 登录、退出和失效

操作员先创建租户及 buyer／approver／auditor 成员，再为指定成员发放一次性邀请。浏览器通过 `POST /api/v1/auth/login` 兑换随机会话，不能自报租户或角色。邀请默认 24 小时有效（可配 60 秒至 7 天），会话默认 30 分钟（可配 60 秒至 1 小时）。成员可另设 UTC 到期时间。

邀请和会话使用独立的 256-bit 随机值；数据库只存 SHA-256，不存明文。邀请仅可成功兑换一次，并发登录也只能产生一个会话。浏览器把会话保留在当前页面内存中，不写入 Cookie、localStorage、sessionStorage、URL 或可见 DOM。刷新／离开页面需重新登录；没有静默续期或自动重放登录。

工作台明确显示用户、租户、角色和到期时间。退出先清空本地视图并取消请求，再确认服务端撤销；网络失败时明确提示服务端撤销未确认，原会话可能有效至到期，应联系操作员撤销。失效会卸载整个工作台、证据和对话框，关闭 SSE 并拒绝晚到的旧身份响应。

API 每次请求重新查询身份；业务读写在实际事务边界再次核验，长读返回前和本地写提交前也会核验。SSE 每轮查询重新核验，失效发送固定 `auth_invalid` 事件后关闭。业务写、成员／会话撤销按同一租户锁顺序串行化：已提交的撤销先于动作则拒绝；动作先取得授权锁则撤销需等待该动作结束。已在网络中的请求不能被退出按钮撤回。

仅使用显式 Bearer header，不接受 Cookie、URL token 或客户端身份字段；请求不携带浏览器 Cookie。pilot 登录／退出对提供的 Origin 做显式检查，拒绝其他站点；CORS 仅允许配置的精确来源。没有 Cookie 会话，因此不依赖跨站 Cookie 或 CSRF token。回环 HTTP 只适合本地合成测试，跨机器试用必须使用 HTTPS、可信反向代理和受限网络；这些部署设置不由本增量自动完成。

## 已接受的审批和执行

退出登录或浏览器会话到期只停止该会话的新操作，不会悄悄撤回已接受的业务审批或执行预留。

- 审批绑定审批成员与租户的授权版本；执行预留另外绑定采购员授权版本
- 成员撤销、角色变更、成员到期或租户停用后，Worker 首次 ERP POST 前拒绝旧授权。撤销后重新启用不会复活旧审批、旧执行预留、邀请或会话
- Worker 在领取和外部查询之后、首次 POST 之前再次核验买方和审批方，并持有授权／政策锁直到该次外部调用结束。慢查询不能让已撤销的授权继续写入
- 会话在外部调用期间失效，已发生的结果仍写回操作账本，避免丢失回执；该用户不会因此获得失效后的结果内容
- 已处于 IN_FLIGHT／RECONCILING 的不确定结果只做远端查询，撤权后仍可由 Worker 对账。查不到不等于没创建，不会再 POST
- 原生 pilot UI 只预留执行，由独立 Worker 处理；受保护的直接处理 API 仍保留并执行相同授权检查

撤销成员会影响其尚未发送的执行，不是 ERP 草稿撤销接口。若首次 POST 已先获得授权并开始，之后的撤销不能撤回该调用；应依据账本和独立读回处理结果。

## 操作员入口

运行代码不等于账号已发放。以下是隔离测试实例的命令示例，不应指向生产数据库。实际发放须有相应授权，凭证应通过获准的安全渠道交付。

先停止相关进程、备份并在副本验证迁移；使用同一 `PF_DATABASE_URL`／`PF_DATA_DIR` 启动 API 与 Worker。迁移头为 `d261a40ce712`，包含新身份表及旧审批／操作上的可空授权版本字段。

```bash
export PF_MODE=pilot
unset PF_AUTH_TOKENS
# 指向受控测试实例，目录和数据库由操作员指定
export PF_DATA_DIR=/absolute/path/to/pilot-data
export PF_DATABASE_URL=sqlite:////absolute/path/to/pilot-data/procureflow.sqlite3
cd services/api
PYTHONPATH=. python -m alembic upgrade head
```

离线管理入口 `python -m procureflow.auth_cli` 不提供 HTTP 管理接口。输入为最多 4 KiB 的单个 JSON 对象；每次写入必须加 `--apply`。CLI 要求数据库迁移已完成，不隐式建库或迁移。

```bash
printf '%s' '{"tenant_id":"synthetic-pilot"}' | PYTHONPATH=. python -m procureflow.auth_cli create-tenant --apply
printf '%s' '{"tenant_id":"synthetic-pilot","user_id":"buyer","role":"buyer"}' | PYTHONPATH=. python -m procureflow.auth_cli set-membership --apply
printf '%s' '{"tenant_id":"synthetic-pilot","user_id":"approver","role":"approver"}' | PYTHONPATH=. python -m procureflow.auth_cli set-membership --apply
printf '%s' '{"tenant_id":"synthetic-pilot","user_id":"buyer"}' | PYTHONPATH=. python -m procureflow.auth_cli issue-invite --apply --credential-output /private/new-buyer-invite.txt
```

邀请明文只写入新建的、权限 `0600` 的文件，拒绝覆盖现有路径，不输出到 stdout、审计或数据库。该一次性交付文件是数据库 hash-only 之外的明确例外；交付后应妥善删除，不能放入 Git、CI artifact、聊天记录或普通日志。若写文件失败，可能已产生未领取邀请，操作员应撤销对应邀请而不是盲目再次发放。

常用撤销示例：

```bash
# 只撤销某用户当前会话，保留业务成员权限及已接受的决定
printf '%s' '{"tenant_id":"synthetic-pilot","user_id":"buyer"}' | PYTHONPATH=. python -m procureflow.auth_cli revoke-user-sessions --apply
# 撤销成员权限，使旧会话、邀请及待发送执行失效
printf '%s' '{"tenant_id":"synthetic-pilot","user_id":"approver","role":"approver","active":false}' | PYTHONPATH=. python -m procureflow.auth_cli set-membership --apply
```

还支持 `revoke-invite`、`revoke-session`、`set-tenant-active`。`set-membership` 的 `expires_at` 为带时区的 ISO 日期，省略保留现有值，显式 `null` 清除到期时间；修改到期时间也会更换授权版本。管理者掌握数据库写权限，属于可信操作员，不是通过采购员角色取得后台管理权。

## 验收入口与证据解释

```bash
python scripts/test.py -q -m 'not postgres and not browser'
python scripts/export_contracts.py --check
node --test apps/web/tests/*.test.mjs
python scripts/verify_next.py --pilot --output evals/reports/local/pilot-next
python scripts/verify_pilot_combined.py --fixture --output evals/reports/local/pilot-combined-fixture/roundtrip.json
python scripts/verify_postgres.py --output evals/reports/local/postgres
```

`verify_next --pilot` 针对真实临时 API 验证两个独立浏览器上下文、退出／失效、SSE、对话框清理、取消登录与晚到响应。为避免失败诊断泄漏凭据，pilot 不发布原始 Playwright trace、页面 HTML、截图、原始 pytest traceback 或服务器日志，JUnit 仅保留用例名／计数／固定状态。

单独的 `pilot-erp.yml` 在隔离真实 ERPNext 沙箱运行 SQLite／PostgreSQL 双矩阵：普通 CSV/XLSX 从原生浏览器导入确认、独立审批、独立 Worker、丢回执只读恢复及撤权拒绝；严格核验两份草稿、两次 POST、零 Purchase Order／提交及来源证据。现有八场景真实 ERP gate 不被替代。`--fixture` 使用 MockERP，即使成功也不是真实 ERP 证据。

脚本和 workflow 存在不代表已执行。交付记录必须对当前确切提交分别报告后端／HTTP／Node、生产构建、浏览器、PostgreSQL、Docker 和真实 ERP 的 passed、failed、blocked／never-run，不能用前一提交的成功覆盖本次缺失门槛。没有付费模型调用或真实模型质量结论。

## 仍未覆盖

企业 SSO、密码／MFA、自助注册或恢复、邀请自动发送、生产反向代理／限流配置、完整运维认证、账户保留清理、备份恢复、OCR、多 SKU 均不在本增量。文件解析仍沿用已有受限进程方案，真实不可信文件和生产部署仍需部署级安全评估。


后续增量：本阶段“未包含备份恢复”是阶段十四范围说明；配对备份、隔离恢复和旧会话撤销见 [阶段十五](stage15-recovery.md)，恢复后的只读诊断及其授权边界见 [阶段十六](stage16-recovery-diagnostics.md)。这些功能不建立生产运维认证。
