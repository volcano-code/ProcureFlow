import type {QuoteValues,QuoteCalculation,EvidenceRef} from "@/lib/api";
import {LINE_FIELDS,FIELD_LABELS,lineEvidence,quoteLines} from "@/lib/procurement-lines.mjs";
import {violationExplanation} from "@/lib/policy.mjs";

/** Render the saved deterministic calculation, never recompute monetary values in JavaScript. */
export default function QuoteLineDetails({values,calculation,evidence,onEvidence,testId="quote-line-details",forceLines=false}:{values:QuoteValues;calculation?:QuoteCalculation;evidence?:Record<string,EvidenceRef>;onEvidence?:(value:EvidenceRef)=>void;testId?:string;forceLines?:boolean}) {
  const lines=values.lines||(forceLines?quoteLines(values):null);
  if(!lines?.length)return null;
  return <section className="quote-lines" data-testid={testId} aria-label="逐项报价与确定性金额">
    {calculation?.coverage&&<p data-testid="quote-coverage-status">物料覆盖：{calculation.coverage.complete?"完整":"不完整"}{!!calculation.coverage.missing_skus.length&&` · 缺少型号：${calculation.coverage.missing_skus.join("、")}`}{!!calculation.coverage.unexpected_skus.length&&` · 额外型号：${calculation.coverage.unexpected_skus.join("、")}`}</p>}
    <p>{lines.length} 项物料 · 每项商品金额和新增税额分别四舍五入到分，再汇总；整单运费只计一次。</p>
    <div className="table-scroll"><table><thead><tr><th scope="col">项</th>{LINE_FIELDS.map(field=><th scope="col" key={field}>{FIELD_LABELS[field]}</th>)}<th scope="col">商品金额</th><th scope="col">新增税额</th><th scope="col">该项含税金额</th></tr></thead>
      <tbody>{lines.map((line,index)=>{const calc=calculation?.lines?.[index];return <tr key={index} data-testid="quote-line-detail"><th scope="row">{index+1}</th>
        {LINE_FIELDS.map(field=><td key={field}>{onEvidence?<button className="field-link" data-testid={`line-${index}-${field}`} aria-label={`物料 ${index+1} ${FIELD_LABELS[field]}来源`} onClick={()=>onEvidence(values.lines?lineEvidence(evidence,index,field):evidence?.[field]||{kind:"unknown"})}>{line[field]===null||line[field]==="unknown"?"未知":String(line[field])}</button>:line[field]===null||line[field]==="unknown"?"未知":String(line[field])}</td>)}
        <td>{calc?.goods??"未知"}</td><td>{calc?.added_tax??"未知"}</td><td><strong>{calc?.total??"未知"}</strong>{!!calc?.violations?.length&&<ul className="violation-list">{calc.violations.map(code=><li key={code}>{violationExplanation(code)}（{code}）</li>)}</ul>}</td>
      </tr>;})}</tbody></table></div>
    <p data-testid="quote-cost-totals">商品金额 {calculation?.goods??"未知"} · 商品折扣 {calculation?.discount??"未知"} · 新增税额 {calculation?.added_tax??"未知"} · 整单含税运费 {values.shipping_cost??"未知"} · 总价 {calculation?.total??"未知"} {values.currency??"未知币种"}</p>
  </section>;
}
