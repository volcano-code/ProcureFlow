/** Display-only helpers. Eligibility and authorization are always server decisions. */
export function strictestBudget(requestBudget, policyCap) {
  if (policyCap == null) return requestBudget;
  const parts = value => {
    // Decimal JSON may preserve scientific notation (e.g. Python Decimal('1E+5')).
    const match = /^([+-]?)([0-9]+)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]+))?$/.exec(value);
    if (!match) throw new Error('Invalid monetary decimal returned by API');
    const digits = (match[2] + (match[3] || '')).replace(/^0+/, '') || '0';
    return {digits, magnitude: digits.length + Number(match[4] || 0) - (match[3] || '').length};
  };
  const a = parts(requestBudget), b = parts(policyCap);
  if (a.digits === '0') return requestBudget;
  if (b.digits === '0') return policyCap;
  if (a.magnitude !== b.magnitude) return a.magnitude < b.magnitude ? requestBudget : policyCap;
  const scale = Math.max(a.digits.length, b.digits.length);
  return BigInt(a.digits.padEnd(scale, '0')) <= BigInt(b.digits.padEnd(scale, '0')) ? requestBudget : policyCap;
}
export function bindingCurrent(binding, policyHash) {
  return !!binding && binding.current === true && !!policyHash && !!binding.policy_hash && binding.policy_hash === policyHash;
}
export function staleExplanation(reason) {
  const reasons = {
    POLICY_CHANGED: '生效策略已变化，请重新校验并由独立审批人审批',
    POLICY_VERSION_CHANGED: '生效策略版本已变化，请重新校验并由独立审批人审批',
    QUOTE_COLLECTION_CHANGED: '报价集合或确认状态已变化，请重新校验全部报价',
    REQUEST_VERSION_CHANGED: '需求版本已变化，请重新校验',
    ADVICE_INPUT_CHANGED: '需求、报价、证据或策略已变化，请显式创建新的只读建议',
    SOURCE_INTEGRITY_FAILED: '来源完整性无法核验，请检查原始文件',
  };
  return reasons[reason] || '绑定的需求、报价、策略或来源已变化，旧结果仅供历史查阅';
}
export function violationExplanation(code) {
  const reasons = {
    FIELDS_NOT_CONFIRMED: '报价尚未人工确认；请逐项核对原始文件后确认',
    BUDGET_EXCEEDED: '含税总价超过需求预算与策略预算上限中较严格的一项',
    POLICY_BUDGET_EXCEEDED: '含税总价超过当前策略的预算上限',
    DELIVERY_EXCEEDS_LIMIT: '交期超过需求与策略交期上限中较严格的一项',
    POLICY_DELIVERY_EXCEEDS_LIMIT: '交期超过当前策略的最长交期',
    MINIMUM_VALID_QUOTES_NOT_MET: '合规且已确认的报价数量不足，不能生成可审批方案',
    INSUFFICIENT_VALID_QUOTES: '不同供应商的合规且已确认报价数量不足，不能生成可审批方案',
    NO_ELIGIBLE_QUOTE: '没有同时满足全部规则的报价，不能生成可审批方案',
    SKU_MISMATCH: '报价型号与采购需求不一致',
    UOM_MISMATCH: '报价计量单位与采购需求不一致',
    QUANTITY_MISMATCH: '报价数量与采购需求不一致',
    UNSUPPORTED_CURRENCY: '币种不支持；不会推测汇率或换算',
    DISCOUNT_EXCEEDS_GOODS: '折扣额大于商品金额，无法形成有效总价',
  };
  if (reasons[code]) return reasons[code];
  if (code.startsWith('UNKNOWN_') || code.startsWith('MISSING_')) return '该必要字段缺失或未知，不能自动推测为零或补全';
  return '服务端规则未通过；请按此代码核对相关字段和策略';
}
