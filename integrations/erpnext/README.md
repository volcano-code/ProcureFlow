# ERPNext 适配器：已写源码，不代表真实联调成功

实现位于 `services/api/procureflow/erp.py`。当前只有 httpx MockTransport 的 REST 契约测试，**没有连接或修改任何用户的 ERPNext 实例**。

## 默认安全边界

`PF_ERP_MODE=mock`；`ERP_ALLOW_DRAFT_WRITES=false`。不存在 Submit、支付或删除工具。真实分支仅创建 `Supplier Quotation` 的 `docstatus=0` 草稿，不是正式 Purchase Order。

当前映射只接受：一个物料；单位 EA；币种 CNY；税率必须明确为 0；运费/折扣均明确为 0。含运费/税费的演示 A/C 报价可通过 mock 流程，但真实适配器会返回 `ERP_COMPLEX_TAX_FREIGHT_MAPPING_NOT_IMPLEMENTED`。必须先完善税费映射再宣称相同样例能写入真实 ERP。

## 测试实例前置配置

需要实际创建且验证的 Company、Supplier、Item 与单位等主数据。API token 对应的集成账号必须只拥有所需读权限及创建 Supplier Quotation 草稿的权限，不授权正式提交。

Supplier Quotation 需要两个 Custom Field：

- `custom_procureflow_operation_key`：Data，长度足够容纳 64 位哈希，真实数据库唯一约束。
- `custom_procureflow_snapshot_hash`：Data，保留 64 位快照哈希。

适配器会读取 Custom Field 元数据验证 `unique=1`，因此测试集成账号还需要对此元数据的适当只读权限。不要为此授予系统管理员。后续生产化应考虑更窄的、经批准的幂等服务端端点。元数据 flag 检查本身不等于已经验证数据库并发行为，仍须真实故障测试。

服务端配置：`ERP_BASE_URL`、`ERP_API_KEY`、`ERP_API_SECRET`、`ERP_COMPANY`。只指向你授权的测试端点。HTTP 仅适合回环测试，远程部署必须另行加入 HTTPS、目标 allowlist 与网络策略。

## 已实现的 REST 合同

`GET api/resource/Supplier` 返回至多 100 个供应商，不冒称完整分页目录。

`GET api/resource/Supplier Quotation` 按唯一键查找，随后获取完整文档，不能只凭列表返回的 ID 报成功。

`POST api/resource/Supplier Quotation` 显式给出 docstatus=0、公司、日期、物料、数量、单位、单价以及两个自定义字段。使用 token 认证，不跟随重定向。公司必须与批准快照一致。

超时、5xx、模糊返回等统一视为结果不确定。读到多条同键对象、远端公司/单位/金额/快照不匹配等会拒绝报成功。

## 完成真实集成的验收清单

先只读核对主数据，随后在测试环境由独立身份批准受限报价；检查草稿回读。验证相同键重复请求、并发创建、成功后响应丢失、Worker 重启、错误权限和错误税费映射。保留原始 API 记录、远端对象数量及版本配置，不只保存成功截图。

本仓库不含真实测试实例、测试账号、ERP 密钥或已经执行的 seed/删除脚本；相关空白尚未完成，不能用 mock 结果替代。

官方 API 参考：https://docs.frappe.io/framework/user/en/api/rest


## a2：只读预检与创建后回读

现在连只读 ERPNext 访问也需要 `PF_MODE=private`、`PF_ERP_MODE=erpnext` 和非演示私有身份；绝不使用公开的 demo token 访问真实系统。

先运行默认离线清单：

```bash
python scripts/preflight.py --output evals/reports/local/environment.json
```

在你授权的测试 ERPNext 及私有身份环境变量配置完整后，可显式执行：

```bash
python scripts/preflight.py --erp-read-only --output evals/reports/local/erp-readonly.json
```

该脚本将适配器写权限固定为 false，只读两个 Custom Field 元数据、Company 和首批 Supplier；缺配置返回阻断，不打印账号/密钥/供应商明细。唯一字段元数据不是远端数据库并发正确性的证明。**本轮没有执行真实端点预检；仅 MockTransport 合同测试通过。**

创建草稿现在要求 snapshot 字段也存在，POST 后独立 GET 已存文档，并核对操作键、快照、总价、单价、数量、单位、公司、物料、币种、日期及 docstatus=0。成功创建但无法确认回读的情形不能报告业务成功；转入现有核对/人工处理流程。没有增加 Submit、删除或支付权限。
