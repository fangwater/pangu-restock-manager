"use strict";
const costState = { data: null, loading: false, page: 1, q: "", template: "", fee: "", editing: null, config: null };
const costMoney = value => value == null ? "—" : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const costFeeTotals = values => Object.entries(values || {}).map(([code, value]) => `${esc(code || "币种未注明")} ${costMoney(value)}`).join("<br>") || "未录入";
const costCurrencyOptions = value => `<option value="">请选择</option>${costState.data.currencies.map(code => `<option value="${esc(code)}" ${code === value ? "selected" : ""}>${esc(code)}</option>`).join("")}`;

async function loadCosts() {
  if (costState.loading) return;
  costState.loading = true;
  try {
    costState.data = await api("costs");
    if (["costs", "cost-templates"].includes(state.view)) renderCostWorkspace();
  } catch (error) {
    if (["costs", "cost-templates"].includes(state.view)) {
      $("costsPage").innerHTML = `<div class="error-banner">${esc(error.message)} <button class="button" id="costRetry">重新读取</button></div>`;
      $("costRetry").onclick = loadCosts;
    }
  } finally { costState.loading = false; }
}

function renderCostWorkspace() {
  const templatesView = state.view === "cost-templates";
  $("pageTitle").textContent = templatesView ? "成本模板" : "采购与物流成本";
  $("breadcrumbTitle").textContent = $("pageTitle").textContent;
  document.querySelectorAll("[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === state.view));
  ["periodToolbar", "categoryGrid", "statsGrid", "chartsGrid", "skuPanel", "sourcesPage", "methodNote"].forEach(id => $(id).hidden = true);
  document.querySelector(".page-actions").hidden = true;
  document.querySelector(".page-footer").hidden = true;
  $("costsPage").hidden = false;
  disposeCharts();
  if (!costState.data) {
    $("costsPage").innerHTML = '<div class="panel cost-empty">正在读取成本记录</div>';
    loadCosts();
    return;
  }
  $("connection").className = "connection ok";
  $("connection").innerHTML = "<span></span>成本数据已载入";
  if (templatesView) { renderCostTemplates(); return; }
  const data = costState.data, s = data.summary;
  const rows = data.tickets.filter(t => (!costState.q || `${t.ticket_no} ${t.note || ""} ${(t.skus || []).join(" ")}`.toLowerCase().includes(costState.q.toLowerCase())) &&
    (!costState.template || String(t.cost_template_id || "") === costState.template) && (!costState.fee || (costState.fee === "missing" ? t.fee_entries === 0 : t.fee_entries > 0)));
  const pages = Math.max(1, Math.ceil(rows.length / 20));
  costState.page = Math.min(costState.page, pages);
  $("costsPage").innerHTML = `<div class="cost-toolbar"><div class="cost-counts"><span>库存单 <strong>${s.tickets}</strong></span><span>SKU <strong>${s.skus}</strong></span><span>采购明细 <strong>${s.sku_lines}</strong></span><span>费用记录 <strong>${s.fee_entries}</strong></span><span>未录附加费用 <strong>${s.without_fees}</strong></span></div><div class="cost-actions"><button class="button" id="costRefresh">刷新</button><button class="button primary" id="costCreate">新增库存单</button></div></div>
  <form class="panel cost-filters" id="costFilters"><label class="field">单号 / SKU<input id="costSearch" value="${esc(costState.q)}"></label><label class="field">成本模板<select id="costTemplateFilter"><option value="">全部模板</option>${data.templates.map(t => `<option value="${t.id}" ${String(t.id) === costState.template ? "selected" : ""}>${esc(t.name)}</option>`).join("")}</select></label><label class="field">附加费用<select id="costFeeFilter"><option value="">全部</option><option value="missing" ${costState.fee === "missing" ? "selected" : ""}>未录入</option><option value="recorded" ${costState.fee === "recorded" ? "selected" : ""}>有记录</option></select></label><div class="cost-actions"><button class="button primary" type="submit">查询</button><button class="button" type="button" id="costReset">重置</button></div></form>
  <div class="panel cost-table-panel"><div class="cost-scroll"><table><thead><tr><th>库存单号</th><th>成本模板</th><th class="num">SKU 行</th><th class="num">数量</th><th class="num">采购金额 / 币种</th><th class="num">附加费用</th><th>生效日期</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${rows.slice((costState.page - 1) * 20, costState.page * 20).map(t => `<tr><td><strong>${esc(t.ticket_no)}</strong>${t.note ? `<small class="cost-note">${esc(t.note)}</small>` : ""}</td><td>${esc(t.cost_template_name || "空模板")}</td><td class="num">${t.line_count}</td><td class="num">${num(t.quantity)}</td><td class="num">${costMoney(t.purchase_amount)}<small class="cost-note">${esc(t.purchase_currency || "币种未注明")}${t.missing_purchase_amounts ? ` · ${t.missing_purchase_amounts} 行未录金额` : ""}</small></td><td class="num">${costFeeTotals(t.fee_totals)}</td><td>${esc(t.effective_date || "未填写")}</td><td>${esc(formatTime(t.updated_at))}</td><td><button class="text-button" data-edit-cost="${t.id}">编辑</button></td></tr>`).join("") || '<tr><td colspan="9" class="cost-empty">没有匹配的库存单</td></tr>'}</tbody></table></div><div class="cost-pagination"><span>共 ${rows.length} 张 · 第 ${costState.page} / ${pages} 页</span><div class="cost-actions"><button class="button small" id="costPrev" ${costState.page === 1 ? "disabled" : ""}>上一页</button><button class="button small" id="costNext" ${costState.page === pages ? "disabled" : ""}>下一页</button></div></div></div>
  <div class="cost-footnote">采购金额按记录币种保存，附加费用按库存单记录；尚未分摊至销售订单。空白金额、币种及生效日期均表示未录入。</div>
  ${data.migration ? `<div class="cost-footnote">旧库存管理移交：${esc(formatTime(data.migration.imported_at))} · ${data.migration.counts.tickets} 张库存单 · ${data.migration.counts.lines} 条 SKU 明细 · ${data.migration.counts.costs} 条费用记录</div>` : ""}`;
  $("costFilters").onsubmit = e => { e.preventDefault(); costState.q = $("costSearch").value.trim(); costState.template = $("costTemplateFilter").value; costState.fee = $("costFeeFilter").value; costState.page = 1; renderCostWorkspace(); };
  $("costReset").onclick = () => { costState.q = costState.template = costState.fee = ""; costState.page = 1; renderCostWorkspace(); };
  $("costRefresh").onclick = loadCosts;
  $("costCreate").onclick = () => editCostTicket();
  $("costPrev").onclick = () => { costState.page--; renderCostWorkspace(); };
  $("costNext").onclick = () => { costState.page++; renderCostWorkspace(); };
  document.querySelectorAll("[data-edit-cost]").forEach(b => b.onclick = () => editCostTicket(b.dataset.editCost));
  icons();
}

async function editCostTicket(id) {
  try {
    costState.editing = id ? await api(`costs/tickets/${id}`) : {version: 0, ticket_no: "", purchase_currency: "", effective_date: "", cost_template_id: null, note: "", lines: [{warehouse_sku: "", quantity: "", unit_price: "", amount: "", note: ""}], costs: []};
    renderCostTicketForm();
    $("costDialog").showModal();
  } catch (error) { toast(error.message, true); }
}

function costInput(field, value, extra = "") { return `<input data-field="${field}" value="${esc(value ?? "")}" ${extra}>`; }
function readCostTicket() {
  const previous = costState.editing;
  return {...previous, ticket_no: $("costNumber").value.trim(), cost_template_id: Number($("costTicketTemplate").value) || null,
    purchase_currency: $("costCurrency").value, effective_date: $("costEffective").value, note: $("costNote").value,
    lines: [...document.querySelectorAll("#costLines tr")].map((tr, index) => ({id: previous.lines[index]?.id, ...Object.fromEntries([...tr.querySelectorAll("[data-field]")].map(el => [el.dataset.field, el.dataset.auto === "true" ? null : el.value]))})),
    costs: [...document.querySelectorAll("#costFees tr[data-type]")].map(tr => ({id: previous.costs.find(c => String(c.cost_type_id) === tr.dataset.type)?.id, cost_type_id: tr.dataset.type, ...Object.fromEntries([...tr.querySelectorAll("[data-field]")].map(el => [el.dataset.field, el.value]))}))};
}

function renderCostTicketForm() {
  const t = costState.editing, template = costState.data.templates.find(c => c.id === t.cost_template_id);
  const fees = new Map(t.costs.map(c => [String(c.cost_type_id), c]));
  $("costDialog").innerHTML = `<form id="costTicketForm"><div class="dialog-head"><h2>${t.id ? esc(t.ticket_no) : "新增库存单"}</h2><button type="button" class="button" id="costClose">关闭</button></div><div class="cost-form-error" id="costFormError" role="alert" hidden></div>
  <div class="cost-form-grid"><label class="field">库存单号<input id="costNumber" value="${esc(t.ticket_no)}" required maxlength="200"></label><label class="field">成本模板<select id="costTicketTemplate"><option value="">空模板</option>${costState.data.templates.map(c => `<option value="${c.id}" ${c.id === t.cost_template_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label><label class="field">采购币种<select id="costCurrency">${costCurrencyOptions(t.purchase_currency || "")}</select></label><label class="field">成本生效日期<input type="date" id="costEffective" value="${esc(t.effective_date || "")}"></label></div>
  <label class="field cost-note-field">备注<input id="costNote" value="${esc(t.note || "")}" maxlength="4000"></label><div class="cost-section-heading"><h3>SKU 采购明细</h3><button class="button small" type="button" id="costAddLine">新增 SKU</button></div>
  <datalist id="costSkuOptions">${(state.data?.rows || []).map(r => `<option value="${esc(r.sku)}"></option>`).join("")}</datalist>
  <div class="cost-scroll"><table class="cost-edit-table"><thead><tr><th>仓库 SKU</th><th>数量</th><th>采购单价</th><th>采购金额</th><th>备注</th><th></th></tr></thead><tbody id="costLines">${t.lines.map((l, i) => `<tr><td>${costInput("warehouse_sku", l.warehouse_sku, 'list="costSkuOptions" required')}</td><td>${costInput("quantity", l.quantity, 'type="number" step="any" min="0" required')}</td><td>${costInput("unit_price", l.unit_price, 'type="number" step="any"')}</td><td>${costInput("amount", l.amount, 'type="number" step="any"')}</td><td>${costInput("note", l.note, 'maxlength="2000"')}</td><td><button class="text-button" type="button" data-remove-line="${i}">删除</button></td></tr>`).join("")}</tbody></table></div>
  <div class="cost-section-heading"><h3>附加费用</h3></div><div class="cost-scroll"><table class="cost-edit-table"><thead><tr><th>费用分类 / 币种</th><th>费用名称</th><th>计算方式</th><th>数量</th><th>单价</th><th>金额</th><th>备注</th></tr></thead><tbody id="costFees">${(template?.categories || []).flatMap(c => c.types.map(kind => {
    const entry = fees.get(String(kind.id)) || {}, calculated = kind.calculation_mode === "quantity_x_unit";
    return `<tr data-type="${esc(kind.id)}"><td>${esc(c.name)}<small class="cost-note">${esc(entry.currency || c.currency)}</small></td><td>${esc(kind.name)}</td><td>${calculated ? "数量 × 单价" : "直接金额"}</td><td>${costInput("quantity", entry.quantity, `type="number" step="any" min="0" ${calculated ? "" : "disabled"}`)}</td><td>${costInput("unit_price", entry.unit_price, `type="number" step="any" ${calculated ? "" : "disabled"}`)}</td><td>${costInput("amount", entry.amount, `type="number" step="any" ${calculated ? "readonly" : ""}`)}</td><td>${costInput("note", entry.note, 'maxlength="2000"')}</td></tr>`;
  })).join("") || '<tr><td colspan="7" class="cost-empty">当前模板没有费用类型</td></tr>'}</tbody></table></div><div class="cost-footnote">采购金额为空且已填单价时，按数量 × 单价计算。空白费用保持未录入。</div>
  <div class="cost-dialog-actions">${t.id ? '<button type="button" class="button danger" id="costDelete">删除库存单</button>' : ""}<button class="button primary" type="submit" id="costSave">保存库存单</button></div></form>`;
  $("costClose").onclick = () => $("costDialog").close();
  $("costAddLine").onclick = () => { costState.editing = readCostTicket(); costState.editing.lines.push({warehouse_sku: "", quantity: "", unit_price: "", amount: "", note: ""}); renderCostTicketForm(); };
  document.querySelectorAll("[data-remove-line]").forEach(b => b.onclick = () => { costState.editing = readCostTicket(); costState.editing.lines.splice(Number(b.dataset.removeLine), 1); renderCostTicketForm(); });
  $("costTicketTemplate").onchange = () => {
    const next = $("costTicketTemplate").value;
    const current = readCostTicket();
    if (current.costs.some(c => [c.amount, c.quantity, c.unit_price, c.note].some(v => v != null && v !== "")) && !confirm("切换模板将清除本次编辑中的附加费用。是否继续？")) { $("costTicketTemplate").value = t.cost_template_id || ""; return; }
    costState.editing = current; costState.editing.cost_template_id = Number(next) || null; costState.editing.costs = []; renderCostTicketForm();
  };
  document.querySelectorAll("#costLines input, #costFees input").forEach(input => input.addEventListener("input", () => {
    if (input.dataset.field === "amount") { delete input.dataset.auto; return; }
    if (!["quantity", "unit_price"].includes(input.dataset.field)) return;
    const row = input.closest("tr"), q = row.querySelector('[data-field="quantity"]').value, p = row.querySelector('[data-field="unit_price"]').value, a = row.querySelector('[data-field="amount"]');
    a.value = q !== "" && p !== "" ? Number((Number(q) * Number(p)).toFixed(10)).toString() : "";
    if (row.parentElement.id === "costLines") a.dataset.auto = "true";
  }));
  $("costTicketForm").onsubmit = async e => { e.preventDefault(); await costFormAction(async () => { await api("costs/tickets", "PUT", readCostTicket()); $("costDialog").close(); await loadCosts(); toast("库存单已保存"); }); };
  if (t.id) $("costDelete").onclick = async () => {
    if (!confirm(`删除库存单 ${t.ticket_no}？`)) return;
    await costFormAction(async () => { await api("costs/tickets/remove", "POST", {id: t.id, version: t.version}); $("costDialog").close(); await loadCosts(); toast("库存单已删除"); });
  };
}

async function costFormAction(action) {
  const dialog = $("costDialog");
  dialog.querySelectorAll("button").forEach(b => b.disabled = true);
  $("costFormError").hidden = true;
  try { await action(); }
  catch (error) { $("costFormError").textContent = error.message; $("costFormError").hidden = false; $("costFormError").scrollIntoView({block: "nearest"}); }
  finally { dialog.querySelectorAll("button").forEach(b => b.disabled = false); }
}

function renderCostTemplates() {
  $("costsPage").innerHTML = `<div class="cost-toolbar"><span>${costState.data.templates.length} 套成本模板</span><div class="cost-actions"><button class="button" id="costRefresh">刷新</button><button class="button primary" id="costTemplateCreate">新增模板</button></div></div><div class="panel cost-table-panel"><div class="cost-scroll"><table><thead><tr><th>模板名称</th><th class="num">费用分类</th><th class="num">费用类型</th><th>币种</th><th>操作</th></tr></thead><tbody>${costState.data.templates.map(t => `<tr><td>${esc(t.name)}</td><td class="num">${t.categories.length}</td><td class="num">${t.categories.reduce((n,c) => n + c.types.length, 0)}</td><td>${esc([...new Set(t.categories.map(c => c.currency))].join(" / ") || "—")}</td><td><button class="text-button" data-config-cost="${t.id}">编辑模板</button></td></tr>`).join("") || '<tr><td colspan="5" class="cost-empty">暂无成本模板</td></tr>'}</tbody></table></div></div>`;
  $("costRefresh").onclick = loadCosts;
  $("costTemplateCreate").onclick = () => editCostTemplate();
  document.querySelectorAll("[data-config-cost]").forEach(b => b.onclick = () => editCostTemplate(Number(b.dataset.configCost)));
}

function editCostTemplate(id) {
  costState.config = id ? structuredClone(costState.data.templates.find(t => t.id === id)) : {version: 0, name: "", categories: []};
  renderCostTemplateForm();
  $("costDialog").showModal();
}

function readCostTemplate() {
  const t = costState.config;
  return {...t, name: $("costTemplateName").value.trim(), categories: [...document.querySelectorAll("[data-config-category]")].map((section, i) => ({
    id: t.categories[i]?.id, name: section.querySelector('[data-category-field="name"]').value, currency: section.querySelector('[data-category-field="currency"]').value,
    types: [...section.querySelectorAll("[data-config-type]")].map((row, j) => ({id: t.categories[i]?.types[j]?.id, name: row.querySelector('[data-type-field="name"]').value, calculation_mode: row.querySelector('[data-type-field="mode"]').value}))
  }))};
}

function renderCostTemplateForm() {
  const t = costState.config;
  $("costDialog").innerHTML = `<form id="costConfigForm"><div class="dialog-head"><h2>${t.id ? "编辑成本模板" : "新增成本模板"}</h2><button class="button" type="button" id="costClose">关闭</button></div><div class="cost-form-error" id="costFormError" role="alert" hidden></div><label class="field">模板名称<input id="costTemplateName" value="${esc(t.name)}" required maxlength="200"></label>
  ${t.categories.map((c, i) => `<section class="cost-config-category" data-config-category="${i}"><div class="cost-category-header"><label class="field">费用分类<input data-category-field="name" value="${esc(c.name)}" required></label><label class="field">币种<select data-category-field="currency" required>${costCurrencyOptions(c.currency)}</select></label><div class="cost-actions"><button class="button small" type="button" data-category-move="${i}" data-delta="-1" ${i === 0 ? "disabled" : ""}>上移</button><button class="button small" type="button" data-category-move="${i}" data-delta="1" ${i === t.categories.length - 1 ? "disabled" : ""}>下移</button><button class="button small" type="button" data-category-remove="${i}">删除分类</button></div></div><div class="cost-scroll"><table><thead><tr><th>费用名称</th><th>计算方式</th><th>操作</th></tr></thead><tbody>${c.types.map((kind, j) => `<tr data-config-type="${j}"><td><input data-type-field="name" value="${esc(kind.name)}" required></td><td><select data-type-field="mode"><option value="direct_amount" ${kind.calculation_mode === "direct_amount" ? "selected" : ""}>直接金额</option><option value="quantity_x_unit" ${kind.calculation_mode === "quantity_x_unit" ? "selected" : ""}>数量 × 单价</option></select></td><td><div class="cost-actions"><button class="button small" type="button" data-type-move="${i},${j}" data-delta="-1" ${j === 0 ? "disabled" : ""}>上移</button><button class="button small" type="button" data-type-move="${i},${j}" data-delta="1" ${j === c.types.length - 1 ? "disabled" : ""}>下移</button><button class="text-button" type="button" data-type-remove="${i},${j}">删除</button></div></td></tr>`).join("")}</tbody></table></div><button class="button small" type="button" data-type-add="${i}">新增费用类型</button></section>`).join("")}
  <button class="button" type="button" id="costCategoryAdd">新增费用分类</button><div class="cost-footnote">已有历史费用的类型不能删除，也不能修改其币种或计算方式。</div><div class="cost-dialog-actions">${t.id ? '<button class="button danger" type="button" id="costTemplateDelete">删除模板</button>' : ""}<button class="button primary" type="submit">保存模板</button></div></form>`;
  const update = fn => { costState.config = readCostTemplate(); fn(costState.config); renderCostTemplateForm(); };
  $("costClose").onclick = () => $("costDialog").close();
  $("costCategoryAdd").onclick = () => update(v => v.categories.push({name: "", currency: "", types: []}));
  document.querySelectorAll("[data-category-remove]").forEach(b => b.onclick = () => update(v => v.categories.splice(Number(b.dataset.categoryRemove), 1)));
  document.querySelectorAll("[data-category-move]").forEach(b => b.onclick = () => update(v => { const i = Number(b.dataset.categoryMove), j = i + Number(b.dataset.delta); [v.categories[i], v.categories[j]] = [v.categories[j], v.categories[i]]; }));
  document.querySelectorAll("[data-type-add]").forEach(b => b.onclick = () => update(v => v.categories[Number(b.dataset.typeAdd)].types.push({name: "", calculation_mode: "direct_amount"})));
  document.querySelectorAll("[data-type-remove]").forEach(b => b.onclick = () => update(v => { const [i,j] = b.dataset.typeRemove.split(",").map(Number); v.categories[i].types.splice(j, 1); }));
  document.querySelectorAll("[data-type-move]").forEach(b => b.onclick = () => update(v => { const [i,j] = b.dataset.typeMove.split(",").map(Number), k = j + Number(b.dataset.delta), list = v.categories[i].types; [list[j],list[k]] = [list[k],list[j]]; }));
  $("costConfigForm").onsubmit = async e => { e.preventDefault(); await costFormAction(async () => { await api("costs/templates", "PUT", readCostTemplate()); $("costDialog").close(); await loadCosts(); toast("成本模板已保存"); }); };
  if (t.id) $("costTemplateDelete").onclick = async () => { if (!confirm(`删除成本模板 ${t.name}？`)) return; await costFormAction(async () => { await api("costs/templates/remove", "POST", {id: t.id, version: t.version}); $("costDialog").close(); await loadCosts(); toast("成本模板已删除"); }); };
}
