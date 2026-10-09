# Changelog

## 本地待验收：备份加密与来源签名

- 标准紧凑 JWS Ed25519 签名及 JWE dir/A256GCM 加密，可单独或组合使用；固定可信公钥验证，不加载包内密钥或网络信任源
- CLI 必须显式选择保护模式；坏包、错密钥、缺签名或模式不符均拒绝，不自动降级到明文
- 保护模式只在内存中处理原始 ZIP，验证保护层和原有源码／模式／来源文件绑定后才能创建全新恢复目标
- 恢复暂停、旧授权撤销及未确定操作永久 hold 保持；调用方负责真实密钥的独立授权、保管、轮换和恢复
- 本地合成验证，未推送、合并、部署或完成独立安全审查。详见 [阶段十八](docs/stage18-backup-protection.md)

## 待发布：多物料完整供应商采购闭环

- 最多 20 个唯一 SKU 的 CNY/EA 需求、显式多行表格导入、逐行证据、修正与完整报价确认
- 逐行 Decimal 税/折扣/舍入与整单运费，完整 SKU/数量覆盖与预算/交期政策共同约束
- 完整报价集合审批绑定、任意行变更失效、持久化幂等、不确定结果只读核对和恢复 hold 继续生效
- 原生 Next 多行编辑/来源/成本/审批展示，保留旧单行 API 和静态 demo
- ERPNext 多行费用映射限定零税率/零折扣/同税价模式/整数 EA；已公开源码并启动合成隔离环境的真实 ERP 验收
- 本地验收与限制见 [阶段十七](docs/stage17-multi-item-procurement.md)，公开后的修正和证据入口见 [验收续记](docs/stage17-publication.md)；尚未合并或部署


## 待发布：恢复后只读诊断与对账

- 初次公开提交 e6afb8b 与本地 6f36d19 源码 tree 相同；后续补齐 PostgreSQL 对新增恢复诊断用例的显式选择，最终远端结果以精确提交验收记录为准

- 新增租户隔离的恢复 hold 队列和本地快照、审批、来源文件完整性详情
- 只读 ERP 核对呈现明确的预期值／读回值／差异；缺失或失败结果不构成重发许可
- 保留 RECOVERY、旧会话撤销和永久 hold；无 HTTP resume、hold 释放、自动重放、核对确认或 ERP 写入授权
- 更新源码证据基线至 `a10f62b65263c9e6bb53219a6296dfb9a67de527`；旧 a2 MANIFEST 原字节归档，基线 CI 与本地增量结果分开
- 本地 Python 回归 1,782 项通过（含 47 项恢复诊断），15 项未选择；前端单元 56 项、类型检查／构建、合同检查、HTTP smoke 及合成 SQLite 恢复通过，各集合不相加
- 本地原生浏览器在 Chromium 启动前因 `CHROMIUM_UNIX_SOCKET_DENIED` 被阻断，两次均未执行 UI 断言；无截图。本地阶段未运行实时 PostgreSQL、容器或真实 ERP；后续远端 CI 结果另行记录
- 尚未合并或部署，未完成独立安全审查。实施合同、原始记录及验收边界见 [阶段十六](docs/stage16-recovery-diagnostics.md)

## 待发布：预览归档与成对备份恢复

- 过期未导入预览不再永久占满 100 个有效预览配额；新增有界计划、显式可逆归档与审计，不删除来源文件
- 新增跨进程维护暂停，等待完整 ERP 调用及回执结束后才备份数据库和对应文件
- 严格格式／版本／引用／哈希校验后仅恢复到新目录和可选新 PostgreSQL schema；旧会话撤销，恢复保持暂停，旧未完成操作永久禁止重放
- 新增合成恢复验收和严格 CI 测试；实际 PostgreSQL、浏览器、容器结果必须以当前提交的执行记录为准
- 不包含生产备份／恢复、覆盖现有数据、永久清除、自动定时、SSO、OCR 或多 SKU

## 待发布：受控试点身份与会话

- 新增数据库身份与短期内存会话、一次性邀请、退出／到期／撤销及原生身份边界
- 首次 ERP 写入同时核验买方与审批方授权版本；撤权后不确定结果仍仅回读，不重发
- 新增迁移与严格 pilot 浏览器／隔离真实 ERP 联合验收入口；脚本存在不代表已经运行
- 不包含真实账号发放、SSO、生产部署、付费模型、备份保留或 OCR／多 SKU

## Unreleased — ordinary table import

CSV/XLSX worksheet/row selection, bilingual header suggestions, explicit mapping previews, source cell provenance, durable expiring import receipts, tenant-scoped deduplication and revision-bound confirmation. Existing correction reasons, quote confirmation and stale approval/ERP guards retained. Bounded decoder subprocess and streaming ingress byte limits added; not a production sandbox. Verification and limits: `docs/stage12-tabular-import.md`.

## 0.1.0a2 — 2026-09-22

### Implemented

Native Next.js sample loading, request edits, explicit quote confirmation, rejection and scope reset; shared authenticated fetch/SSE transport; authenticated synthetic samples and capability metadata; explicit CORS origins; demo/real-ERP separation; strict model message validation and comparison-tool prerequisite; redacted-by-design tool event metadata; read-only ERP preflight; persisted ERP draft readback verification.

### Tested

111 Python passes, 2 environment skips; 14 frontend transport unit passes; 3 shared transport real-HTTP passes; original 14-step HTTP/worker smoke passed. Two SIGKILL scenarios (included in the 111) use separate processes and a persistent simulated ERP, with lease expiration injected by the test.

### Not accepted / not executed

Strict static browser gate fails twice due managed environment policy. Native Next dependency installation failed with registry DNS resolution error; strict native build/E2E gate is blocked. Four native browser cases are authored, not executed. No live ERPNext, live model, PostgreSQL, Docker or remote CI acceptance. Still local Alpha, not a complete MVP.

## 0.1.0a1 — historical baseline

User-supplied initial source package: deterministic business core, four fixed-layout import formats, snapshot-bound independent approval, operation ledger/outbox and persistent mock ERP. Prior reports retained. Baseline test rerun: 80 passed, 2 skipped; added counterexamples reproduced demo/ERP configuration and null model-message defects subsequently fixed in a2.
