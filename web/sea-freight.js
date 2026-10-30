"use strict";
const seaState = {data:null, loading:false, error:"", request:0, filters:new URLSearchParams(location.search).get("sea_q") ? {q:new URLSearchParams(location.search).get("sea_q")} : {}, page:1};
const seaMoney = value => value == null ? "未提供" : Number(value).toLocaleString("zh-CN", {minimumFractionDigits:2,maximumFractionDigits:6});
const seaAction = {new:"新增",updated:"更新",unchanged:"不变"};
const seaLabels = {"sequence": "序号", "departure_date": "开航日期", "arrival_date": "到港日期", "forwarder": "货代", "bill_no": "提单号", "destination": "目的港", "converted_usd": "折合美元合计", "sea_usd": "海运费(USD)", "tax_usd": "税金(USD)", "delivery_usd": "派送/清关(USD)", "ams_usd": "舱单/AMS(USD)", "other_usd": "其他项(USD)", "usd_total": "美元合计(USD)", "trucking_cny": "拖车费(RMB)", "port_cny": "港杂费(RMB)", "customs_cny": "报关费(RMB)", "other_cny": "其他项(RMB)", "cny_total": "人民币合计(RMB)", "converted_cny": "折合人民币合计", "vessel": "船名", "voyage": "航次", "order_no": "托单号/订单号", "container_no": "柜号", "container_type": "箱型", "origin": "起运港", "packages": "件数", "gross_weight": "毛重(KG)", "volume": "体积(CBM)", "goods_value": "货值(USD)", "payment_rule": "付款规则", "due_date": "付款到期日", "payment_status": "付款状态", "amount_basis": "金额口径", "source_reference": "来源文件", "notes": "备注", "warehouse_date": "入仓日期", "version": "版本", "source_rate": "参考汇率 USD/CNY", "mode": "运输方式", "country": "目的地"};
const seaQuery = () => new URLSearchParams(seaState.filters).toString();
const seaLink = (path, label) => `<a class="text-button" href="api/sea-freight/${path}">${label}</a>`;
const seaOptions = (values, selected, all="全部") => `<option value="">${all}</option>${values.map(v => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(v)}</option>`).join("")}`;

async function loadSea() {
  const request=++seaState.request;
  seaState.loading = true; seaState.error = "";
  try { const data=await api(`sea-freight?${seaQuery()}`); if(request===seaState.request) seaState.data=data; }
  catch (e) { if(request===seaState.request) seaState.error=e.message; }
  finally { if(request===seaState.request) {seaState.loading=false;if(state.view === "sea-freight") renderSeaWorkspace();} }
}

function renderSeaWorkspace() {
  $("pageTitle").textContent = $("breadcrumbTitle").textContent = "海运费用";
  document.querySelectorAll("[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === state.view));
  ["periodToolbar","categoryGrid","statsGrid","chartsGrid","skuPanel","sourcesPage","methodNote"].forEach(id => $(id).hidden = true);
  document.querySelector(".page-actions").hidden = document.querySelector(".page-footer").hidden = true;
  $("costsPage").hidden = false; disposeCharts();
  if (seaState.error) {
    $("costsPage").innerHTML = `<div class="error-banner">${esc(seaState.error)} <button class="button" id="seaRetry">重新读取</button></div>`;
    $("seaRetry").onclick = loadSea; return;
  }
  if (!seaState.data) { $("costsPage").innerHTML = '<div class="panel cost-empty">正在读取海运账单</div>'; if(!seaState.loading) loadSea(); return; }
  const d = seaState.data, s = d.summary, f = seaState.filters, pages = Math.max(1,Math.ceil(d.rows.length / 20));
  seaState.page = Math.min(seaState.page,pages);
  $("connection").className = "connection ok"; $("connection").innerHTML = "<span></span>海运账单已载入";
  const select = (key,label,values) => `<label class="field">${label}<select name="${key}">${seaOptions(values,f[key])}</select></label>`;
  $("costsPage").innerHTML = `<div class="cost-toolbar"><div class="cost-counts"><span>账单 <strong>${s.bills}</strong></span><span>人民币 <strong>${seaMoney(s.CNY)}</strong>${s.CNY_missing ? ` · ${s.CNY_missing} 票未提供` : ""}</span><span>美元 <strong>${seaMoney(s.USD)}</strong>${s.USD_missing ? ` · ${s.USD_missing} 票未提供` : ""}</span><span>已付 <strong>${s.paid}</strong></span><span>付款待确认 <strong>${s.unconfirmed}</strong></span></div><div class="cost-actions sea-actions"><button class="button" id="seaRefresh">刷新</button><button class="button" id="seaExport">导出 Excel</button><button class="button primary" id="seaImport">导入 Excel</button></div></div>
  <form class="panel cost-filters sea-filters" id="seaFilters"><label class="field">开航开始日期<input type="date" name="start" value="${esc(f.start || "")}"></label><label class="field">开航结束日期<input type="date" name="end" value="${esc(f.end || "")}"></label>${select("forwarder","货代",d.options.forwarder)}${select("mode","运输方式",d.options.mode)}${select("country","目的地",d.options.country)}<label class="field">付款状态<select name="payment"><option value="">全部</option>${[["paid","已付"],["unpaid","未付"],["partial","部分已付"],["unconfirmed","待确认"]].map(([k,v])=>`<option value="${k}" ${f.payment===k?"selected":""}>${v}</option>`).join("")}</select></label><label class="field">单号 / 柜号 / 备注<input name="q" value="${esc(f.q || "")}"></label><div class="cost-actions"><button class="button primary" type="submit">查询</button><button class="button" type="button" id="seaReset">重置</button></div></form>
  <div class="panel cost-table-panel"><div class="cost-scroll"><table class="sea-ledger-table"><thead><tr><th>托单号</th><th>提单号</th><th>柜号</th><th>货代</th><th>运输 / 目的港</th><th>开航日期</th><th class="num">人民币</th><th class="num">美元</th><th>付款状态</th><th>付款到期日</th><th>操作</th></tr></thead><tbody>${d.rows.slice((seaState.page-1)*20,seaState.page*20).map(r=>`<tr><td><strong>${esc(r.order_no || "未提供")}</strong></td><td>${esc(r.bill_no || "未提供")}</td><td>${esc(r.container_no || "未提供")}</td><td>${esc(r.forwarder)}</td><td>${esc(r.mode)} · ${esc(r.country)}<small class="cost-note">${esc(r.destination)}</small></td><td>${esc(r.departure_date)}</td><td class="num">${seaMoney(r.cny_total)}</td><td class="num">${seaMoney(r.usd_total)}</td><td>${esc(r.payment_status || "待确认")}</td><td>${esc(r.due_date || "未提供")}</td><td><button class="text-button" data-sea-detail="${r.id}">明细</button></td></tr>`).join("") || '<tr><td colspan="11" class="cost-empty">没有匹配的海运账单</td></tr>'}</tbody></table></div><div class="cost-pagination"><span>共 ${d.rows.length} 票 · 第 ${seaState.page} / ${pages} 页</span><div class="cost-actions"><button class="button small" id="seaPrev" ${seaState.page===1?"disabled":""}>上一页</button><button class="button small" id="seaNext" ${seaState.page===pages?"disabled":""}>下一页</button></div></div></div>
  <p class="cost-footnote">金额按原币汇总；空白表示未提供。付款状态及费用备注取自原表。</p>
  <details class="panel sea-files"><summary>原表异常费用清单（全部来源 · ${d.issues.length} 项）</summary><p class="cost-footnote">原表列示的差异与待核对事项；金额可能为多票合计，不追加到费用总额。</p><div class="cost-scroll"><table><thead><tr><th>提单号 / 单号</th><th>货代</th><th>异常项目</th><th>原表金额 / 币种</th><th>原表说明</th><th>凭证</th></tr></thead><tbody>${d.issues.map(r=>`<tr><td>${esc(r.identifier)}</td><td>${esc(r.forwarder)}</td><td>${esc(r.title)}</td><td>${seaMoney(r.amount)} ${esc(r.currency)}</td><td class="sea-filename">${esc(r.notes)}</td><td>${seaLink(`files/${r.file_id}`,"下载原表")}</td></tr>`).join("") || '<tr><td colspan="6" class="cost-empty">没有原表异常记录</td></tr>'}</tbody></table></div></details>
  <details class="panel sea-files"><summary>导入记录与原始文件（最近 ${d.files.length} 次）</summary><div class="cost-scroll"><table><thead><tr><th>文件名</th><th>导入时间</th><th>账单票数</th><th>来源凭证</th><th>操作</th></tr></thead><tbody>${d.files.map(r=>`<tr><td class="sea-filename">${esc(r.filename)}</td><td>${esc(formatTime(r.created_at))}</td><td>${r.manifest.bills}</td><td class="sea-filename">${esc(r.reference)}</td><td>${seaLink(`files/${r.id}`,"下载原表")}</td></tr>`).join("") || '<tr><td colspan="5" class="cost-empty">尚无导入记录</td></tr>'}</tbody></table></div></details>`;
  $("seaRefresh").onclick = loadSea; $("seaImport").onclick = openSeaImport;
  $("seaExport").onclick = () => { location.href = `api/sea-freight/export.xlsx?${seaQuery()}`; };
  $("seaFilters").onsubmit = e => { e.preventDefault(); seaState.filters = Object.fromEntries([...new FormData(e.target)].filter(([,v])=>v)); seaState.page=1; loadSea(); };
  $("seaReset").onclick = () => { seaState.filters={}; seaState.page=1; loadSea(); };
  $("seaPrev").onclick=()=>{seaState.page--;renderSeaWorkspace();}; $("seaNext").onclick=()=>{seaState.page++;renderSeaWorkspace();};
  document.querySelectorAll("[data-sea-detail]").forEach(b=>b.onclick=()=>openSeaDetail(b.dataset.seaDetail)); icons();
}

function seaError(e) { const el=$("seaFormError"); el.textContent=e.message; el.hidden=false; }
function seaDialog(html) { $("costDialog").innerHTML=html; $("seaClose").onclick=()=>$("costDialog").close(); if (!$("costDialog").open) $("costDialog").showModal(); }

function openSeaImport() {
  let upload=null, preview=null;
  seaDialog(`<form id="seaImportForm"><div class="dialog-head"><h2>导入海运账单</h2><button class="button" type="button" id="seaClose">关闭</button></div><div class="cost-form-error" id="seaFormError" role="alert" hidden></div><div class="cost-form-grid"><label class="field cost-import-field">Excel 文件<input type="file" id="seaFile" accept=".xlsx" required></label></div><p class="cost-footnote">仅导入海运明细表，汇总、报价与一口价估算页不追加计费。每次导入保存原始文件及修改记录。单个文件上限 6 MiB。</p><div id="seaPreview"></div><div class="cost-dialog-actions"><button class="button" type="submit" id="seaPreviewButton">预览导入</button><button class="button primary" type="button" id="seaCommit" disabled>导入账单</button></div></form>`);
  const reset=()=>{upload=preview=null;$("seaCommit").disabled=true;$("seaPreview").innerHTML="";};
  $("seaFile").onchange=reset;
  $("seaImportForm").onsubmit=async e=>{
    e.preventDefault(); $("seaFormError").hidden=true; $("seaPreviewButton").disabled=true; $("seaFile").disabled=true;
    try {
      const file=$("seaFile").files[0]; if (!file || file.size>6*1024*1024) throw Error("Excel 文件须为 .xlsx 格式且不超过 6 MiB");
      const encoded=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(",")[1]);r.onerror=()=>reject(Error("文件读取失败"));r.readAsDataURL(file);});
      upload={filename:file.name,xlsx_base64:encoded,reference:file.name};
      preview=await api("sea-freight/import/preview","POST",upload);
      const c=preview.counts;
      $("seaPreview").innerHTML=`<div class="cost-counts cost-import-summary"><span>账单 <strong>${preview.bills}</strong></span><span>新增 <strong>${c.new}</strong></span><span>更新 <strong>${c.updated}</strong></span><span>不变 <strong>${c.unchanged}</strong></span></div><ul class="sea-checks">${preview.warnings.map(w=>`<li>${esc(w)}</li>`).join("")}</ul><div class="cost-scroll"><table class="sea-preview-table"><thead><tr><th>操作</th><th>货代 / 单号</th><th>人民币</th><th>美元</th><th>付款状态</th><th>修改字段</th></tr></thead><tbody>${preview.rows.map(r=>`<tr><td>${seaAction[r.action]}</td><td>${esc(r.after.forwarder)}<br>${esc(r.after.order_no || r.after.bill_no)}</td><td>${seaMoney(r.after.cny_total)}</td><td>${seaMoney(r.after.usd_total)}</td><td>${esc(r.after.payment_status || "待确认")}</td><td>${r.action==="updated"?esc(r.changed_fields.filter(k=>seaLabels[k]).map(k=>seaLabels[k]).join("、")):"—"}</td></tr>`).join("")}</tbody></table></div>`;
      $("seaCommit").disabled=false;
    } catch(e) {reset();seaError(e);} finally {$("seaPreviewButton").disabled=false;$("seaFile").disabled=false;}
  };
  $("seaCommit").onclick=async()=>{
    if (!upload || !preview) return; $("seaCommit").disabled=true; $("seaPreviewButton").disabled=true;$("seaFile").disabled=true;
    try {const r=await api("sea-freight/import","POST",{...upload,revision:preview.revision});$("costDialog").close();toast(`新增 ${r.new} 票，更新 ${r.updated} 票，不变 ${r.unchanged} 票`);await loadSea();}
    catch(e){seaError(e);preview=null;} finally {if($("seaPreviewButton")) {$("seaPreviewButton").disabled=false;$("seaFile").disabled=false;}}
  };
}

async function openSeaDetail(id) {
  try {
    const r = await api(`sea-freight/bills/${id}`);
    const fields = [["货代",r.forwarder],["托单号",r.order_no],["提单号",r.bill_no],["柜号",r.container_no],["箱型",r.container_type],["船名",r.vessel],["航次",r.voyage],["起运港",r.origin],["目的港",r.destination],["开航日期",r.departure_date],["到港日期",r.arrival_date],["入仓日期",r.warehouse_date],["件数",r.packages],["毛重(KG)",r.gross_weight],["体积(CBM)",r.volume],["货值(USD)",r.goods_value],["付款规则",r.payment_rule],["付款到期日",r.due_date],["付款状态",r.payment_status],["金额口径",r.amount_basis],["来源文件",r.source_reference],["人民币合计",seaMoney(r.cny_total)],["美元合计",seaMoney(r.usd_total)]];
    seaDialog(`<div class="dialog-head"><h2>${esc(r.order_no || r.bill_no)}</h2><button class="button" type="button" id="seaClose">关闭</button></div><div class="sea-meta">${fields.map(([k,v])=>`<div><small>${k}</small>${esc(v || "未提供")}</div>`).join("")}</div><div class="cost-scroll"><table><thead><tr><th>费用项目</th><th>币种</th><th class="num">金额</th></tr></thead><tbody>${r.fees.map(f=>`<tr><td>${esc(f.name)}</td><td>${f.currency}</td><td class="num">${seaMoney(f.amount)}</td></tr>`).join("")}</tbody></table></div>${r.notes?`<div class="cost-footnote">备注：${esc(r.notes)}</div>`:""}<div class="cost-dialog-actions">${seaLink(`files/${r.source_file_id}`,"下载原表")}</div>`);
  } catch(e) {toast(e.message,true);}
}
