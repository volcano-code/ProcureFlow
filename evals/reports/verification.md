# ProcureFlow v0.1.0a1 · 本轮验证报告

日期：2026-09-22。验证对象：本次交付的采购业务内核与本地模拟闭环 Alpha。没有真实 ERPNext 或真实模型账户联调，不是原规划的四周 MVP 完成报告。

## 自动化结果

命令：

```bash
python scripts/test.py -q --junitxml=../../evals/reports/pytest.xml
```

最终结果：**80 passed, 2 skipped，0 failed，0 errors；约 9.49 秒。** 82 个收集项包括参数化的边界用例，不等于 82 个独立采购场景。

| 测试模块 | 通过 | 跳过 | 说明 |
| --- | ---: | ---: | --- |
| test_domain | 23 | 0 | Decimal、舍入、未知传播、数据类型和哈希 |
| test_parsers | 14 | 0 | 4 格式、定位、冲突、公式不执行及文件限制 |
| test_workflow | 25 | 0 | 权限、审批、版本、操作、并发和模拟恢复 |
| test_adapters | 17 | 0 | 模型只读工具与 ERP REST 的 mock-transport 合同 |
| test_migrations | 1 | 0 | Alembic upgrade → downgrade → upgrade |
| test_browser | 0 | 2 | 环境策略阻止 Chromium 访问本地测试服务，尚未验收 |

原始日志：`pytest.log`；机器可读记录：`pytest.xml`、`verification.json`。

浏览器初次执行真实报错为 `Page.goto: net::ERR_BLOCKED_BY_ADMINISTRATOR`。保留初始阻断记录 `browser-attempt.log`。当前测试在这类已识别的环境阻断下标记 skipped；严格模式 `PF_REQUIRE_BROWSER=1` 会失败，不允许把 skipped 当成功。本轮没有真实工作台运行截图、点击链路验收或视觉回归结果。

## 真实 HTTP + 独立进程 smoke

命令：

```bash
python scripts/smoke.py --output evals/reports/http-smoke.json
```

结果：**passed**。不是 FastAPI TestClient 内部请求：脚本启动实际 HTTP 服务，通过本地网络发送请求，另起 Python Worker 进程，并重新启动 API 进程。

测试路径：创建需求；导入 TXT/CSV/文本 PDF 三份样例；确认字段；确定性比较；采购员自行审批被拒；独立审批人批准快照；持久化 Outbox；另一 Worker 进程执行；第二次 drain；API 优雅重启；重新读取需求与操作；同请求重放；跨租户读取被拒；直接读取独立模拟 ERP 数据库确认只有一张草稿。

实际样例推荐：SUP-C，总价 **24,200.00 CNY**。最终需求状态 ERP_CREATED，操作状态 COMPLETED。远端 ID 带 `MOCK-SQ-`，明确不是 ERPNext 业务 ID。完整 14 项步骤及实际 ID 位于 `http-smoke.json`。

该测试覆盖的是优雅 API 重启与独立 Worker drain，**不等于 SIGKILL/断电/跨主机混沌验证**。

## 针对关键风险的结果

审批后修改数量：旧审批拒绝执行；报价修改产生新版本；人工修改保留 manual evidence；政策或 ERP 目标变化使快照失效；审批过期/审批人撤权阻止首次写入；执行预留后冻结需求。

模拟 ERP 创建成功但响应丢失：进入 RECONCILING；重新打开数据库/适配器后只读核对，返回同一草稿。未知远端缺失时进入 NEEDS_HUMAN，不盲目重建。5 路并发和重复执行在测试情景中保持一个逻辑草稿。没有声称任意外部系统拥有 exactly-once 保证。

模型要求未知写入/审批/shell 工具：白名单拒绝；非法结构或不存在的证据 ID 拒绝。此类测试使用模拟模型返回值，不证明真实模型面对所有注入的防护效果。解释文本的事实蕴含也没有完成自动验证。

## 构建、环境和产物检查

| 项目 | 本轮状态 |
| --- | --- |
| FastAPI 真正启动、静态 HTML/JS 资源可经 HTTP 获取 | 已通过 |
| JavaScript `node --check` | 已通过，记录在 javascript-syntax.json |
| 5 个 TS/TSX 文件的 TypeScript 转译语法检查 | 已通过，非完整类型检查 |
| Next.js `tsc` / `next build` | 未完成；缺依赖且包源下载失败，没有 npm lockfile |
| 浏览器 E2E / UI 截图 | 环境阻断，未完成 |
| SQLite migration 往返 | 已通过 |
| PostgreSQL / Redis / Celery / LangGraph | 未验收，后者三项未集成 |
| Docker / Compose | 已写配置，环境无 Docker，未执行 |
| ERPNext 真实调用 | 未完成；适配器写入默认关闭 |
| 真实模型调用 | 未完成；无实际账户调用记录 |
| 60-task Dev/Val/Holdout 评测 | 未构建，不报告模型成功率或成本 |
| PDF 合成样例 | 已渲染并检查；pdf-fixture-preview.png 是样例预览，不是 UI 截图 |

Python 3.13.5、Node 22.16.0；依赖详情在 `environment.json`，工具探测在 `environment-probes.json`。测试版本快照不等于安全漏洞审计。

## 发布判断

**可交付：源码、测试、固定格式合成样例、本地运行入口、独立 HTTP smoke、迁移与接口合同。**

**不满足：生产上线、原生 Next.js 浏览器验收、真实采购系统闭环、完整 Agent MVP、最终求职量化评测。**

下一里程碑应先完成当前前端验收，再联调测试 ERPNext/PostgreSQL 和真实模型，不应现在转向更多实验框架。
