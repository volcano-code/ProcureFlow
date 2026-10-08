# Changelog

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
