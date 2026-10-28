"use strict";
let purchaseImportState=null;
const purchaseActions={created:'新增',updated:'更新',unchanged:'相同跳过'};
function purchaseCSV(params={}){const a=document.createElement('a');a.href='api/costs/tickets/export.csv?'+new URLSearchParams(params);a.download='purchase-costs.csv';a.click();}
function openPurchaseImport(){
 const editor={csv:null,preview:null};purchaseImportState=editor;
 $('costDialog').innerHTML=`<div class="dialog-head"><h2>采购成本批量导入</h2><button class="button" id="purchaseImportClose">关闭</button></div><div class="cost-form-error" id="purchaseImportError" role="alert" hidden></div>
 <div class="cost-actions cost-bulk-actions"><button class="button" id="purchaseImportTemplate">下载空模板</button><button class="button" id="purchaseImportCatalog">导出费用类型</button></div>
 <p class="cost-footnote">按单号及 SKU / 费用类型更新，文件中未包含的明细保留。空白金额表示未录入；采购金额为空且有单价时按数量 × 单价计算，计量费用按数量 × 单价计算。采购与费用分别使用原币。新单版本为 0；已有单和模板使用当前版本。最多 200 单、5000 行、700 KB。</p>
 <form id="purchaseImportForm"><div class="cost-form-grid cost-note-field"><label class="field cost-import-field">CSV 文件<input type="file" id="purchaseImportFile" accept=".csv,text/csv" required></label><label class="field cost-import-field">来源凭证<input id="purchaseImportReference" required maxlength="1000"></label></div>
 <div class="cost-dialog-actions"><button class="button" type="submit" id="purchaseImportPreview">预览导入</button><button class="button primary" type="button" id="purchaseImportSave" disabled>保存导入</button></div></form><div id="purchaseImportResult"></div>
 <details class="cost-import-history"><summary>导入记录（最近 20 次）</summary><div id="purchaseImportHistory">正在读取</div></details>`;
 $('costDialog').showModal();$('purchaseImportClose').onclick=()=>{$('costDialog').close();purchaseImportState=null;};
 $('purchaseImportTemplate').onclick=()=>purchaseCSV({template_only:'1'});$('purchaseImportCatalog').onclick=()=>purchaseCSV({catalog:'1'});
 const invalidate=()=>{editor.preview=null;$('purchaseImportSave').disabled=true;$('purchaseImportResult').innerHTML='';};
 $('purchaseImportReference').oninput=invalidate;
 $('purchaseImportFile').onchange=()=>{editor.csv=null;invalidate();};
 $('purchaseImportForm').onsubmit=event=>{event.preventDefault();purchaseImportAction(editor,async()=>{
  const file=$('purchaseImportFile').files[0];if(!file||file.size>700000)throw Error('CSV 文件须不超过 700 KB');
  try{editor.csv=new TextDecoder('utf-8',{fatal:true}).decode(await file.arrayBuffer());}catch{throw Error('CSV 须为 UTF-8 编码');}
  const payload={csv:editor.csv,reference:$('purchaseImportReference').value};purchaseCheckSize(payload);
  const result=await api('costs/tickets/import/preview','POST',payload);if(purchaseImportState!==editor)return;
  editor.preview={...result,request:payload};$('purchaseImportResult').innerHTML=purchaseRenderPlan(result);
 });};
 $('purchaseImportSave').onclick=()=>purchaseImportAction(editor,async()=>{
  if(!editor.preview)return;const p=editor.preview;const result=await api('costs/tickets/import','POST',{...p.request,revision:p.revision});
  if(purchaseImportState===editor){$('costDialog').close();purchaseImportState=null;}
  fifoState.data=null;fifoState.report=null;accountingState.data=null;await loadCosts();
  toast(`新增 ${result.summary.created} 单，更新 ${result.summary.updated} 单，跳过 ${result.summary.unchanged} 单`);
 });
 loadPurchaseImportHistory(editor);
}
function purchaseCheckSize(payload){if(new TextEncoder().encode(JSON.stringify(payload)).length>1048576)throw Error('请求超过 1 MB，请拆分 CSV');}
async function purchaseImportAction(editor,fn){
 if(purchaseImportState!==editor)return;const dialog=$('costDialog');const controls=[...dialog.querySelectorAll('button,input')];controls.forEach(el=>el.disabled=true);$('purchaseImportError').hidden=true;
 try{await fn();}catch(error){if(purchaseImportState===editor){editor.preview=null;$('purchaseImportError').hidden=false;$('purchaseImportError').textContent=error.message;$('purchaseImportError').scrollIntoView({block:'nearest'});}}
 finally{if(purchaseImportState===editor){controls.forEach(el=>el.disabled=false);$('purchaseImportSave').disabled=!editor.preview||!(editor.preview.summary.created+editor.preview.summary.updated);}}
}
function purchaseNative(t){return t?`${esc(t.purchase_currency)} ${esc(t.purchase_amount??'未录入')}${t.missing_purchase_amounts?`<small class="cost-note">${t.missing_purchase_amounts} 行未录金额</small>`:''}`:'—';}
function purchaseFees(t){return t?Object.entries(t.fee_totals).map(([code,amount])=>`${esc(code)} ${esc(amount)}`).join('<br>')||'未录入':'—';}
function purchaseChangeRows(row){return row.changes.map(c=>`<tr><td>${esc(c.kind)}</td><td>${esc(c.key)}</td><td>${esc(c.field)}</td><td>${esc(c.before??'未录入')}</td><td>${esc(c.after??'未录入')}</td></tr>`).join('')||'<tr><td colspan="5">无变化</td></tr>';}
function purchaseRenderPlan(plan){
 const s=plan.summary;
 return `<div class="cost-counts cost-import-summary"><span>库存单 <strong>${s.tickets}</strong></span><span>新增 <strong>${s.created}</strong></span><span>更新 <strong>${s.updated}</strong></span><span>相同跳过 <strong>${s.unchanged}</strong></span></div><p class="cost-footnote">更新采购单后，关联入库批次须重新核对。未出现的 SKU / 费用行保留，不自动生成入库记录。</p>
 <div class="cost-scroll"><table><thead><tr><th>库存单 / 版本</th><th>操作</th><th>原采购金额</th><th>新采购金额</th><th>原附加费用</th><th>新附加费用</th><th>SKU / 生效日期</th><th>关联入库</th></tr></thead><tbody>${plan.rows.map(r=>`<tr><td>${esc(r.ticket_no)}<small class="cost-note">版本 ${r.version}${r.action!=='unchanged'?' → '+(r.version+1):''} · ${esc(r.template||'空模板')}</small></td><td>${purchaseActions[r.action]}</td><td>${purchaseNative(r.before)}</td><td>${purchaseNative(r.after)}</td><td>${purchaseFees(r.before)}</td><td>${purchaseFees(r.after)}</td><td>${r.before?.line_count??0} → ${r.after.line_count}<small class="cost-note">${esc(r.before?.effective_date||'未录入')} → ${esc(r.after.effective_date||'未录入')}</small></td><td>${r.linked_receipts} 条</td></tr><tr><td colspan="8"><details><summary>变更明细（${r.changes.length} 项）</summary><table class="cost-import-changes"><thead><tr><th>类型</th><th>SKU / 费用</th><th>字段</th><th>原值</th><th>新值</th></tr></thead><tbody>${purchaseChangeRows(r)}</tbody></table></details></td></tr>`).join('')}</tbody></table></div><p class="cost-footnote fifo-digest">CSV 文本 SHA256：${esc(plan.file_sha256)}</p>`;
}
async function loadPurchaseImportHistory(editor){
 try{const data=await api('costs/tickets/imports');if(purchaseImportState!==editor)return;
  $('purchaseImportHistory').innerHTML=`<div class="cost-scroll"><table><thead><tr><th>时间 / 凭证</th><th>新增 / 更新 / 跳过</th><th>操作</th></tr></thead><tbody>${data.rows.map(r=>`<tr><td>${esc(formatTime(r.created_at))}<small class="cost-note">${esc(r.reference)}</small></td><td>${r.summary.created} / ${r.summary.updated} / ${r.summary.unchanged}</td><td><button class="text-button" data-purchase-import-history="${r.id}">查看变更</button> <a class="text-button" href="api/costs/tickets/imports/${r.id}?export=1">导出导入明细</a></td></tr>`).join('')||'<tr><td colspan="3">无导入记录</td></tr>'}</tbody></table></div><div id="purchaseImportHistoryDetail"></div>`;
  document.querySelectorAll('[data-purchase-import-history]').forEach(button=>button.onclick=()=>purchaseImportAction(editor,async()=>{const r=await api('costs/tickets/imports/'+button.dataset.purchaseImportHistory);if(purchaseImportState===editor)$('purchaseImportHistoryDetail').innerHTML=`<h3>导入记录 ${r.id}</h3>`+purchaseRenderPlan(r.result);}));
 }catch(error){if(purchaseImportState===editor)$('purchaseImportHistory').textContent=error.message;}
}
