# 原生前端容器与受限集成探针

从 `8082853` 开发快照继续，不迁移架构、不合并 main。历史 a2 MANIFEST 和旧验收记录不是本轮结果。最终状态以对应提交的 Actions artifacts 为准，不能把配置文件存在视为运行验收。

## 本地原生 Next.js 全栈

```bash
python scripts/init_postgres_env.py
docker compose --env-file .env.postgres -f compose.postgres.yaml -f compose.web.yaml up --build -d --wait
```

打开 `http://127.0.0.1:3000` 是原生 Next 生产服务器；8000 仍是 FastAPI 静态工作台。web 容器以非 root 用户运行，使用 standalone 输出。构建阶段固定 `NEXT_PUBLIC_API_BASE_URL=/backend` 与内部 upstream `http://api:8000`，没有密钥 build arg。更换 upstream 需要重建镜像，不是运行时可由用户输入控制。

浏览器请求经 `/backend/api/v1/*` 同源转发到 API，保留 Bearer 鉴权、文件上传及 SSE。只转发现有业务 API 与 health/ready，不是任意 URL 代理；不暴露 Frappe RPC。服务端权限仍然是最终边界。局域网明文 provider URL 现在拒绝：集成须 HTTPS（本机 loopback HTTP 用于开发夹具）。

这仍是绑定回环地址的 demo/mock 环境，不是企业 SSO 或公网生产部署。示例身份不能用于公网。没有在 Floot 托管应用或调用真实 ERP。已有 PostgreSQL 目录不要 `down -v`。原 SQLite 数据不会自动迁移。

## 原生容器验收

下面命令只适用于可丢弃的本地演示数据，实际创建合成采购记录：

```bash
python scripts/compose_smoke.py --base-url http://127.0.0.1:3000 --api-prefix /backend --output evals/reports/local/native-smoke.json
python scripts/verify_compose_web.py --allow-test-mutations --output evals/reports/local/native-container
```

Gate 先确认 loopback、demo、mock、PostgreSQL 与真正 Next HTML，再运行 7 条指定用例：原有 4 条原生 E2E，加同源请求、鉴权/路由范围、SSE 3 条。旧四项不是新增独立业务场景。缺失 XML、用例缺失/重复、跳过/失败或超时都不通过，旧 XML 运行前删除。对应 CI 为 `compose-native-web`，另外检查 proxy → 独立 Worker 仅一张模拟草稿，并在重启 web 后读取同一操作。

## 真实集成的配置入口（默认完全不联网）

默认命令返回 `blocked` / exit 2，且在创建 HTTP client 前停止：

```bash
python scripts/verify_integrations.py --target model --output evals/reports/local/model-probe.json
python scripts/verify_integrations.py --target erp --output evals/reports/local/erp-probe.json
```

授权测试环境中在本机安全配置这些服务端环境变量，不在聊天、源码、PR、日志、截图中提供密钥。脚本不自动 source `.env`：

- 共用：`PF_MODE=private`，`PF_INTEGRATION_ENVIRONMENT=sandbox`，符合现有格式的 `PF_AUTH_TOKENS`（每个至少 32 字符、非 demo 的随机令牌映射到 user_id/tenant_id/role）。sandbox 是操作者声明，不是软件识别生产环境的证明。
- 模型：`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`；可选 `LLM_THINKING_MODE=default|enabled|disabled`。model ID 用账号实测可用值，不硬编码供应商模型版本。
- ERP：`PF_ERP_MODE=erpnext`、`ERP_BASE_URL`、`ERP_API_KEY`、`ERP_API_SECRET`、`ERP_COMPANY`、`ERP_EXPECTED_INTEGRATION_USER`。期望身份不得是 Administrator/Guest。

显式授权单次联网：

```bash
python scripts/verify_integrations.py --target model --allow-network --output evals/reports/local/model-probe.json
python scripts/verify_integrations.py --target erp --allow-network --output evals/reports/local/erp-probe.json
```

模型调用可能产生费用。模型探针只发送固定合成内容，不加载业务数据库/真实报价；必须读取比较结果与合成制度，输出结构和引用 ID 必须通过校验。成功仅表示兼容性探针通过，不证明解释语义、金额理解或真实业务成功率。它仍是只读有界 tool loop，不是 LangGraph、RAG 或模型训练。不会授予采购审批、ERP 写入、shell 或通用网页工具。

ERP 探针强制只读，即使环境中 ERP_ALLOW_DRAFT_WRITES=true。校验实际登录身份、Company、两个 Custom Field、供应商第一页。身份相同不等于最小权限证明；`unique=1` 元数据不等于验证数据库唯一索引。没有提供绕过业务审批的 write probe。真实草稿只能由现有业务服务在独立人工批准后写入，并独立回读；仍仅支持已实现的零税/零运费/零折扣单物料映射。

## 模型与 HTTP 防护变化

- 保留 provider `reasoning_content` 供下一轮 API 续传，只留在当前调用的临时上下文，不输出、不写 audit；长度计入上下文限制。
- 所有轮次的工具调用 ID 必须唯一，不仅同一轮。每个工具执行前后检查截止预算，未知/写入工具仍拒绝。
- 响应采用流式读取，在 JSON 解析前限制解码字节数（模型 200,000；ERP 2 MiB）；拒绝跳转、非 loopback HTTP、内嵌凭据 URL；不读取环境代理。
- provider usage 只采纳非负整数 prompt/completion token 及一致的 total，拒绝将任意 provider JSON 写审计。缺失或畸形 usage 保持 unknown，不按 0 计费；探针要求有效 usage。
- model/tool 次数、上下文、单次 max_tokens、provider-reported 累积 token 有边界。token 值由供应商自报，成本未配置为 null。墙钟在调用/读取间检查，不能保证抢占任意阻塞工具或所有网络读，不是 CPU/内存沙箱证明。
- 真实 HTTP 探针测试使用本机自建的 provider/ERP 协议夹具，不是实连账户，不能宣传为真实 LLM/ERPNext 联调。

## 官方接口依据

- Next standalone：https://nextjs.org/docs/app/api-reference/config/next-config-js/output
- Next rewrites：https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites
- DeepSeek thinking/tool continuation：https://api-docs.deepseek.com/guides/thinking_mode/
- DeepSeek chat completion schema：https://api-docs.deepseek.com/api/create-chat-completion/
- Frappe Token/REST 与 get_logged_user：https://docs.frappe.io/framework/user/en/api/rest

接口文档仅支持实现选择，不代表本项目已完成真实服务联调。
