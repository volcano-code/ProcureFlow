const $ = (selector) => document.querySelector(selector);
const escape = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {token: '', me: null, requests: [], request: null, quotes: [], events: [], operation: null, stream: null, health: null};
const statusNames = {DRAFT:'待录入报价',NEEDS_CONFIRMATION:'待核对字段',READY_FOR_REVIEW:'待审批',APPROVED:'已批准',APPROVAL_STALE:'审批已失效',REJECTED:'已拒绝',BLOCKED:'规则未通过',ERP_PENDING:'执行已预留',ERP_CREATED:'草稿已创建',RECONCILING:'结果待核对',NEEDS_HUMAN:'需要人工处理'};
const fieldNames = {supplier_id:'供应商编码',sku:'型号 / SKU',quantity:'数量',uom:'单位',unit_price:'单价',tax_mode:'税价模式',tax_rate:'税率（小数）',shipping_cost:'运费（最终含税金额）',discount:'商品折扣额',delivery_days:'交期天数',currency:'币种'};
const taxNames = {included:'含税',excluded:'不含税',unknown:'未知'};
function badge(status) { return `<span class="badge ${['APPROVED','ERP_CREATED'].includes(status)?'ok':['APPROVAL_STALE','BLOCKED','NEEDS_HUMAN','REJECTED'].includes(status)?'bad':'warn'}">${escape(statusNames[status] || status)}</span>`; }
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(()=>$('#toast').hidden=true, 6000); }
async function api(path, options={}) {
  const {body, ...rest} = options;
  const isForm = body instanceof FormData;
  const response = await fetch('/api/v1'+path, {...rest, headers:{'Authorization':'Bearer '+state.token, ...(!isForm && body ? {'Content-Type':'application/json'} : {}), ...rest.headers}, body:isForm?body:body?JSON.stringify(body):undefined});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ? `${data.error.code}：${data.error.message}` : '输入校验失败：'+JSON.stringify(data.detail));
  return data;
}
async function safe(action) { try { await action(); } catch(error) { toast(error.message); } }
const buyer = () => state.me?.role === 'buyer';
const frozen = () => ['ERP_PENDING','ERP_CREATED','RECONCILING','NEEDS_HUMAN'].includes(state.request?.status);
function money(value) { return value === null || value === undefined ? '<span class="unknown">未知</span>' : '¥ '+escape(value); }
function render() {
  $('#request-list').innerHTML = state.requests.map(r=>`<button data-request="${escape(r.id)}" class="${state.request?.id===r.id?'selected':''}">${escape(r.title)}<small>${escape(statusNames[r.status] || r.status)}</small></button>`).join('') || '<p class="form-hint">还没有采购需求</p>';
  const metrics=[['采购需求',state.requests.length,'当前工作区'],['本次报价',state.quotes.length,'保留原始文件与版本'],['已确认报价',state.quotes.filter(q=>q.confirmed_by).length,'人工核对后参与比较'],['审计记录',state.events.length,'当前需求 · 最近 1,000 条']];
  $('#metrics').innerHTML=metrics.map(([title,value,note])=>`<div class="metric"><label>${title}</label><strong>${value.toString().padStart(2,'0')}</strong><small>${note}</small></div>`).join('');
  $('#new-request').disabled=!buyer(); $('#load-demo').disabled=!buyer();
  const r=state.request; $('#empty').hidden=!!r; $('#detail').hidden=!r;
  if (!r) return;
  $('#request-code').textContent=r.id.toUpperCase(); $('#request-title').textContent=r.title;
  $('#request-meta').textContent=`${r.sku}  ·  数量 ${r.quantity} ${r.uom}  ·  预算 ¥${r.budget}  ·  交期 ≤ ${r.max_delivery_days} 天  ·  需求 v${r.version}`;
  $('#request-status').innerHTML=badge(r.status); $('#quote-count').textContent=state.quotes.length;
  ['upload','analyze','edit-request'].forEach(id=>$('#'+id).disabled=!buyer()||frozen());
  const cell=(q,key,content)=>`<button class="field-link" data-evidence="${escape(q.id)}" data-field="${key}">${content}</button>`;
  $('#quote-body').innerHTML=state.quotes.map(q=>`<tr><td><strong>${cell(q,'supplier_id',escape(q.values.supplier_id||'未知供应商'))}</strong><small title="${escape(q.filename)}">${escape(q.filename)} · v${q.version}</small></td><td>${cell(q,'quantity',escape(q.values.quantity||'未知'))}</td><td>${cell(q,'unit_price',money(q.values.unit_price))}</td><td>${cell(q,'tax_mode',escape(taxNames[q.values.tax_mode]))}</td><td>${cell(q,'shipping_cost',money(q.values.shipping_cost))}</td><td class="total">${money(q.calculation.total)}${q.calculation.violations.length?`<small title="${escape(q.calculation.violations.join(', '))}">${q.calculation.violations.length} 项待处理</small>`:'<small>满足比较规则</small>'}</td><td><span class="badge ${q.confirmed_by?'ok':'warn'}">${q.confirmed_by?'已确认':'待核对'}</span><br><button class="table-action" data-review="${escape(q.id)}" ${!buyer()||frozen()?'disabled':''}>核对 / 编辑</button><button class="table-action" data-confirm="${escape(q.id)}" ${!buyer()||frozen()||q.confirmed_by?'disabled':''}>确认字段</button></td></tr>`).join('') || '<tr><td colspan="7" class="subtle-empty">还没有报价，请上传固定键值格式的文件。</td></tr>';
  const p=r.proposal;
  $('#proposal-body').innerHTML=p?`<div class="decision-content"><div class="decision-label">规则推荐 · ${escape(p.quote_values.supplier_id)} · 报价 v${p.quote_version}</div><div class="decision-amount">${money(p.total)} <small>CNY / 统一总成本</small></div><p>确定性基线选择满足预算、交期与完整性规则的最低总价报价。此推荐不是 LLM 生成。</p><div class="hash">SNAPSHOT SHA-256<br>${escape(p.snapshot_hash)}</div>${state.me.role==='approver'&&!frozen()?'<button id="approve" class="primary">批准当前展示的快照</button>':''}${buyer()&&r.status==='APPROVED'?'<button id="execute" class="primary">预留执行并创建模拟 ERP 草稿</button>':''}${buyer()&&state.operation&&['ERP_PENDING','RECONCILING','NEEDS_HUMAN'].includes(r.status)?'<button id="reconcile" class="secondary">处理 / 核对已有操作</button>':''}${!['APPROVED','ERP_CREATED'].includes(r.status)&&state.me.role!=='approver'?'<p class="form-hint">切换为独立审批人后批准；修改需求或报价将使旧快照失效。</p>':''}${state.operation?`<div class="decision-result">操作：${escape(state.operation.status)}<br>${escape(state.operation.remote_id||state.operation.error||'外部执行尚未完成')}</div>`:''}</div>`:'<div class="decision-content"><div class="subtle-empty">尚无可审批的方案<br><small>先核对报价字段，再运行确定性规则校验。</small></div></div>';
  renderEvents();
}
function renderEvents() { $('#events').innerHTML=[...state.events].reverse().map(event=>`<div class="event"><time>${escape(event.created_at.slice(11,19))}</time><span class="event-mark"></span><div><strong>${escape(event.type)}</strong><p>${escape(event.actor_id)} · ${escape(JSON.stringify(event.payload))}</p></div></div>`).join('')||'<div class="subtle-empty">暂时没有审计记录</div>'; }
async function refreshRequests() { state.requests=await api('/requests'); render(); }
async function detail(id, reconnect=false) {
  const [r,q,e]=await Promise.all([api('/requests/'+id),api('/requests/'+id+'/quotes'),api('/requests/'+id+'/events')]);
  const changed=state.request?.id!==id;
  state.request=r; state.quotes=q; state.events=e;
  if(changed) { state.operation=null; $('#evidence-body').innerHTML='<div class="subtle-empty">点击报价字段查看来源</div>'; }
  const reserved=[...e].reverse().find(x=>x.type==='ERP_OPERATION_RESERVED');
  if(reserved) state.operation=await api('/operations/'+reserved.payload.operation_id);
  state.requests=await api('/requests'); render();
  if(changed||reconnect) startStream(id);
}
function startStream(id) {
  state.stream?.abort(); const controller=new AbortController(); state.stream=controller;
  const token=state.token;
  (async()=>{
    let cursor=state.events.at(-1)?.id||0;
    while(!controller.signal.aborted) {
      try {
        const response=await fetch(`/api/v1/requests/${id}/events/stream?after=${cursor}`,{headers:{Authorization:'Bearer '+token},signal:controller.signal});
        if(!response.ok) return;
        const reader=response.body.getReader(), decoder=new TextDecoder(); let buffer='';
        while(true) { const {done,value}=await reader.read(); if(done) break; buffer+=decoder.decode(value,{stream:true}); let pos;
          while((pos=buffer.indexOf('\n\n'))>=0) { const part=buffer.slice(0,pos);buffer=buffer.slice(pos+2);const line=part.split('\n').find(x=>x.startsWith('data: ')); if(line){const ev=JSON.parse(line.slice(6));cursor=Math.max(cursor,ev.id);if(state.request?.id===id&&!state.events.some(x=>x.id===ev.id)){state.events.push(ev);renderEvents();}} }
        }
      } catch(error) { if(controller.signal.aborted) return; }
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
  })();
}
async function login(token) { state.stream?.abort(); state.token=token; state.me=await api('/me'); state.request=null;state.quotes=[];state.events=[]; await refreshRequests();if(state.requests.length)await detail(state.requests[0].id,true); }
function openModal(title,html) { $('#modal-title').textContent=title;$('#modal-body').innerHTML=html;$('#modal').showModal(); }
function input(key,label,value,type='text') {return `<label class="form-field">${escape(label)}<input name="${escape(key)}" type="${type}" value="${escape(value??'')}" ${key==='reason'?'minlength="5"':''}></label>`;}
function formError(error) { const el=$('#form-error'); if(el)el.textContent=error.message; else toast(error.message); }
function requestForm(edit=false) {
 const r=edit?state.request:{title:'研发工位支架采购',sku:'STAND-01',quantity:'20',budget:'30000.00',max_delivery_days:14};
 openModal(edit?'编辑采购需求':'新建采购需求',`<form id="request-form"><div class="form-grid">${input('title','需求名称',r.title)}${input('sku','型号 / SKU',r.sku)}${input('quantity','采购数量（EA）',r.quantity)}${input('budget','预算上限（CNY）',r.budget)}${input('max_delivery_days','最长交期（天）',r.max_delivery_days,'number')}</div><p class="form-hint">首版仅支持单一 SKU、CNY 与 EA 单位。任何修改都需要重新分析和审批。</p><p class="form-error" id="form-error"></p><div class="form-actions"><button class="primary" type="submit">保存需求</button></div></form>`);
 $('#request-form').addEventListener('submit',async event=>{event.preventDefault();try{const f=new FormData(event.target),body={title:f.get('title'),sku:f.get('sku'),quantity:f.get('quantity'),budget:f.get('budget'),max_delivery_days:Number(f.get('max_delivery_days')),currency:'CNY',uom:'EA'};const saved=edit?await api('/requests/'+r.id,{method:'PUT',body:{...body,expected_version:r.version}}):await api('/requests',{method:'POST',body});$('#modal').close();await detail(saved.id);toast(edit?'需求已修改；旧审批已失效。':'需求已创建并写入数据库。');}catch(error){formError(error);}});
}
function reviewQuote(q) {
 openModal('核对 / 修改报价 · v'+q.version,`<form id="quote-form"><div class="form-grid">${Object.entries(fieldNames).map(([key,name])=>key==='tax_mode'?`<label class="form-field">税价模式<select name="tax_mode">${Object.entries(taxNames).map(([v,t])=>`<option value="${v}" ${q.values.tax_mode===v?'selected':''}>${t}</option>`).join('')}</select></label>`:input(key,name,q.values[key])).join('')}<label class="form-field full">修改依据（至少 5 个字符；将作为人工证据记录）<textarea name="reason" minlength="5" required placeholder="例如：电话核对供应商，确认运费为 300 元。"></textarea></label></div><p class="form-hint">未知字段请留空。运费指最终含税费用；折扣指商品总额的绝对减免。保存会创建新版本，不覆盖原报价；之后需要重新确认。</p><p id="form-error" class="form-error"></p><div class="form-actions"><button class="primary" type="submit">保存为新版本</button></div></form>`);
 $('#quote-form').addEventListener('submit',async event=>{event.preventDefault();try{const f=new FormData(event.target),values={};Object.keys(fieldNames).forEach(key=>{const raw=f.get(key);values[key]=raw===''?null:key==='delivery_days'?Number(raw):raw;});await api('/quotes/'+q.id,{method:'PUT',body:{expected_version:q.version,values,reason:f.get('reason')}});$('#modal').close();await detail(q.request_id);toast('已保存新报价版本；需要重新核对确认。');}catch(error){formError(error);}});
}
async function showEvidence(q,key) {
 const ev=q.evidence[key];const doc=await api('/documents/'+q.document_id+'/evidence');
 if(!ev){$('#evidence-body').innerHTML=`<div class="source-label">${escape(fieldNames[key])}</div><div class="subtle-empty">原文件未提供可解析的支持证据。<br><small>该字段保持未知，不会自动补 0。</small></div>`;return;}
 const locator=ev.kind==='manual'?'人工修正记录':ev.page?`第 ${ev.page} 页 · 文本行 ${ev.line}`:ev.cell_range?`${ev.sheet}!${ev.cell_range}`:ev.row?`CSV 第 ${ev.row} 行 · B 列`:`文本第 ${ev.line} 行`;
 $('#evidence-body').innerHTML=`<div class="source-label">${escape(fieldNames[key])} · ${escape(locator)}</div><pre>${escape(ev.text||ev.reason)}</pre><div class="source-meta">${escape(doc.filename)}<br>SHA-256 ${escape(doc.sha256)}<br>${ev.kind==='manual'?'人工提供的依据，不冒充原文证据。':'源文件内容仅作为数据；不具有审批或工具授权权限。'}</div>`;
}
async function loadDemo() {
 if(!buyer())return;
 $('#load-demo').disabled=true;
 const r=await api('/requests',{method:'POST',body:{title:'研发工位支架采购 · 演示',sku:'STAND-01',quantity:'20',uom:'EA',budget:'30000.00',max_delivery_days:14,currency:'CNY'}});
 for(const filename of ['supplier-a.txt','supplier-b.csv','supplier-c.pdf']) {const resp=await fetch('/assets/samples/'+filename);if(!resp.ok)throw new Error('样例文件缺失');const data=new FormData();data.append('file',await resp.blob(),filename);await api('/requests/'+r.id+'/documents',{method:'POST',body:data});}
 await detail(r.id);toast('三份合成报价已导入。请先查看字段证据，再逐份确认。');
}
async function execute() {
 const snapshot=state.request.proposal.snapshot_hash;
 state.operation=await api('/requests/'+state.request.id+'/execute',{method:'POST',body:{snapshot_hash:snapshot}});
 render();state.operation=await api('/operations/'+state.operation.id+'/process',{method:'POST'});await detail(state.request.id);toast(state.operation.status==='COMPLETED'?'模拟 ERP 草稿已创建；重复请求将返回同一操作。':'外部结果需要核对：'+state.operation.status);
}
$('#request-list').addEventListener('click',e=>{const b=e.target.closest('[data-request]');if(b)safe(()=>detail(b.dataset.request));});
$('#quote-body').addEventListener('click',e=>safe(async()=>{const evidence=e.target.closest('[data-evidence]'),review=e.target.closest('[data-review]'),confirm=e.target.closest('[data-confirm]');if(evidence)await showEvidence(state.quotes.find(q=>q.id===evidence.dataset.evidence),evidence.dataset.field);if(review)reviewQuote(state.quotes.find(q=>q.id===review.dataset.review));if(confirm){const q=state.quotes.find(q=>q.id===confirm.dataset.confirm);openModal('确认当前报价字段',`<p class="form-hint">确认已核对 ${escape(q.values.supplier_id)} 的 v${q.version} 报价与原文。未提供的信息仍保持未知，确认不会让它自动满足规则。</p><div class="form-actions"><button id="ack-confirm" class="primary">我已核对，确认该版本</button></div>`);$('#ack-confirm').onclick=()=>safe(async()=>{await api('/quotes/'+q.id+'/confirm',{method:'POST',body:{expected_version:q.version,acknowledge:true}});$('#modal').close();await detail(q.request_id);});}}));
$('#proposal-body').addEventListener('click',e=>safe(async()=>{if(e.target.id==='approve'){await api('/requests/'+state.request.id+'/approval',{method:'POST',body:{snapshot_hash:state.request.proposal.snapshot_hash,decision:'approve',note:'已核对当前展示快照'}});await detail(state.request.id);toast('已批准该快照。切换采购员后才能执行。');}if(e.target.id==='execute')await execute();if(e.target.id==='reconcile'){state.operation=await api('/operations/'+state.operation.id+'/process',{method:'POST'});await detail(state.request.id);}}));
$('#new-request').onclick=()=>requestForm();$('#edit-request').onclick=()=>requestForm(true);$('#close-modal').onclick=()=>$('#modal').close();
$('#load-demo').onclick=()=>safe(loadDemo);$('#empty-demo').onclick=()=>safe(loadDemo);$('#refresh').onclick=()=>safe(async()=>{await refreshRequests();if(state.request)await detail(state.request.id);});
$('#role').onchange=()=>safe(()=>login($('#role').value));
$('#login').onclick=()=>{openModal('使用服务端分配的令牌',`<form id="login-form"><label class="form-field">访问令牌<input type="password" name="token" autocomplete="off" required></label><p class="form-hint">令牌仅保留在本页内存，不写入 localStorage，也不会进入模型上下文。</p><p id="form-error" class="form-error"></p><div class="form-actions"><button class="primary" type="submit">登录</button></div></form>`);$('#login-form').onsubmit=async e=>{e.preventDefault();try{await login(new FormData(e.target).get('token'));$('#modal').close();}catch(error){formError(error);}};};
$('#upload').onclick=()=>$('#file-input').click();$('#file-input').onchange=()=>safe(async()=>{const file=$('#file-input').files[0];if(!file)return;const data=new FormData();data.append('file',file);await api('/requests/'+state.request.id+'/documents',{method:'POST',body:data});$('#file-input').value='';await detail(state.request.id);toast('报价已解析；所有字段仍需人工确认。');});
$('#analyze').onclick=()=>safe(async()=>{const result=await api('/requests/'+state.request.id+'/analyze',{method:'POST',body:{}});await detail(state.request.id);toast(result.proposal?'确定性校验完成，已生成可审批快照。':'没有满足全部规则的已确认报价，请检查缺失项。');});
$('#nav-policy').onclick=()=>safe(async()=>{const p=await api('/policy');openModal('演示规则与当前边界',`<div class="rule-list">${p.clauses.map(x=>`<p><strong>${escape(x.id)}</strong> ${escape(x.text)}</p>`).join('')}<p>单一 SKU / CNY / EA；固定键值格式抽取；不包含 OCR 或语义表格解析。身份令牌仅用于本地开发，不是企业 SSO。</p><p>模拟 ERP 独立持久化；真实 ERPNext 写入默认关闭。此版本不能用于公网生产采购。</p></div>`);});
$('#nav-audit').onclick=()=>$('#audit-panel').scrollIntoView({behavior:'smooth'});$('#nav-workbench').onclick=()=>window.scrollTo({top:0,behavior:'smooth'});
await safe(async()=>{state.health=await (await fetch('/health')).json();$('#health').textContent=state.health.erp==='mock'?'● 本地模拟 ERP':'● ERPNext 适配模式';if(state.health.mode==='demo'){await login('demo-buyer');}else{$('#role').hidden=true;render();$('#login').click();}});
