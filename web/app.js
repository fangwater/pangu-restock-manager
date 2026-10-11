"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const num = (v) =>
  v == null
    ? "—"
    : Number(v).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const pct = (v) => `${Number(v || 0).toFixed(2)}%`;
const colors = [
  "#8778e3",
  "#e8b76a",
  "#dc8fa5",
  "#7cbdac",
  "#8da7d1",
  "#b695cd",
];
const statuses = {
  draft: "草稿",
  confirmed: "已确认",
  purchasing: "采购中",
  completed: "已完成",
};
const state = {
  data: null,
  view: "overview",
  query: { preset: "last30" },
  page: 1,
  size: 20,
  selected: new Set(),
  sort: "total",
  desc: true,
  charts: [],
  drawerSku: null,
  loading: false,
  classification: null,
  classCategory: "all",
  classSearch: "",
  classPage: 1,
  classSelected: new Set(),
};
function icons() {
  window.lucide?.createIcons();
}
function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 4500);
}
async function api(path, method = "GET", body) {
  const r = await fetch(`api/${path}`, {
    method,
    headers: body
      ? { "Content-Type": "application/json", "X-Pangu-Request": "restock" }
      : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const result = await r.json();
  if (!r.ok || !result.success) throw new Error(result.error || "请求失败");
  return result.data;
}
function chosen(id) {
  const el = $(id);
  return el.tagName === "SELECT"
    ? Array.from(el.selectedOptions).map((o) => o.value)
    : Array.from(el.querySelectorAll("input:checked")).map((o) => o.value);
}
function readQuery() {
  const q = {
    preset: $("preset").value,
    shops: chosen("shopSelect").join(","),
    statuses: chosen("statusSelect").join(","),
    category: $("categorySelect").value,
  };
  if ($("startDate").value || $("endDate").value) {
    q.start = $("startDate").value;
    q.end = $("endDate").value;
  }
  return q;
}
function exportFilters() {
  return { ...state.data.filters, q: $("search").value.trim() };
}
async function load(query = state.query) {
  if (state.loading) return;
  state.loading = true;
  $("queryButton").disabled = true;
  try {
    const data = await api(`dashboard?${new URLSearchParams(query)}`);
    state.data = data;
    if (state.view === "classification") state.classification = null;
    state.query = { ...data.filters };
    state.page = 1;
    state.selected.clear();
    $("loadError").hidden = true;
    $("filterPending").textContent = "";
    render();
  } catch (e) {
    $("loadError").textContent = e.message;
    $("loadError").hidden = false;
    toast(e.message, true);
  } finally {
    state.loading = false;
    $("queryButton").disabled = false;
  }
}
function syncControls() {
  const d = state.data,
    q = d.filters;
  $("startDate").value = q.start;
  $("endDate").value = q.end;
  $("shopOptions").innerHTML =
    `<label><input type="checkbox" value="all">全部店铺</label>` +
    d.shops
      .map(
        (s) =>
          `<label><input type="checkbox" value="${esc(s.key)}">${esc(s.name)}</label>`,
      )
      .join("");
  const all = q.shops.length === d.shops.length;
  $("shopSelect")
    .querySelectorAll("input")
    .forEach(
      (i) => (i.checked = all ? i.value === "all" : q.shops.includes(i.value)),
    );
  $("categorySelect").innerHTML =
    '<option value="all">全部类目</option>' +
    d.categories
      .map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`)
      .join("");
  $("categorySelect").value = q.category;
  $("statusSelect")
    .querySelectorAll("input")
    .forEach((i) => (i.checked = q.statuses.includes(i.value)));
  $("dateRange").textContent = `${q.start} — ${q.end} · ${q.days}天`;
  dropdownLabels();
}
function dropdownLabels() {
  const shops = chosen("shopSelect"),
    status = chosen("statusSelect");
  $("shopSummary").textContent = shops.includes("all")
    ? "全部店铺"
    : shops.length === 1
      ? state.data.shops.find((s) => s.key === shops[0])?.name || shops[0]
      : `${shops.length}个店铺`;
  $("statusSummary").textContent =
    status.length === 3
      ? "全部有效状态"
      : status.length === 1
        ? state.data.statuses[status[0]]
        : `${status.length}个状态`;
}
function render() {
  const d = state.data;
  if (!d) return;
  syncControls();
  const titles = {
    overview: "备货总览",
    plans: "备货计划",
    catalog: "产品资料",
    classification: "SKU 分类",
    rules: "备货规则",
    sources: "数据连接",
  };
  $("pageTitle").innerHTML =
    esc(titles[state.view]) + '<span class="title-dot">.</span>';
  $("breadcrumbTitle").textContent = titles[state.view];
  document
    .querySelectorAll("[data-view]")
    .forEach((b) =>
      b.classList.toggle("active", b.dataset.view === state.view),
    );
  const secondary = ["sources", "rules", "classification"].includes(state.view);
  $("periodToolbar").hidden = secondary;
  $("categoryGrid").hidden = secondary;
  $("statsGrid").hidden = secondary || state.view === "catalog";
  $("chartsGrid").hidden = state.view !== "overview";
  $("skuPanel").hidden = secondary;
  $("sourcesPage").hidden = !secondary;
  $("totalSales").innerHTML = `${num(d.summary.total)}<small>件</small>`;
  $("activeSkus").textContent = num(d.summary.active_skus);
  $("suggestedTotal").innerHTML =
    `${num(d.summary.suggested)}<small>仓库单位</small>`;
  $("plannedTotal").textContent = num(d.summary.planned);
  $("salesFoot").textContent = "订单件数 · 不含取消/退款";
  $("skuFoot").textContent =
    `${d.summary.sku_count} 个 SKU · ${d.summary.unclassified} 个待分类`;
  $("suggestionFoot").textContent =
    `${d.summary.urgent_skus} 个需优先备货 · 按箱规取整`;
  $("planFoot").textContent = `${d.summary.locked} 个已锁定`;
  $("planNavCount").textContent = d.rows.filter(
    (r) => r.plan && r.plan.status !== "completed",
  ).length;
  $("categoryGrid").innerHTML = d.categories
    .map(
      (c) =>
        `<button class="category-card ${d.filters.category === c.code ? "active" : ""}" data-category="${esc(c.code)}"><span class="category-top"><span class="category-name"><span class="category-icon">${categoryIcon(c.icon)}</span>${esc(c.name)}</span><span>${pct(c.share)}</span></span><strong>${num(c.total)} <small>件</small></strong><div class="track"><b style="width:${c.share}%"></b></div><small>${c.sku_count} 个有销量 SKU</small></button>`,
    )
    .join("");
  $("categoryGrid")
    .querySelectorAll("button")
    .forEach(
      (b) =>
        (b.onclick = () =>
          load({
            ...state.query,
            category:
              state.data.filters.category === b.dataset.category
                ? "all"
                : b.dataset.category,
          })),
    );
  $("connection").className =
    `connection ${d.sources.some((s) => s.status === "error") ? "error" : "ok"}`;
  $("connection").innerHTML =
    `<span></span>${d.sync.running ? "同步中" : d.sources.some((s) => s.status === "error") ? "部分连接异常" : "数据已连接"}`;
  $("syncStamp").textContent =
    `最近销量同步 ${formatTime(d.snapshots.sales)} · 每 5 分钟更新`;
  $("syncButton").disabled = d.sync.running;
  $("syncButton").classList.toggle("spinning", d.sync.running);
  $("methodNote").innerHTML =
    (d.warnings.length
      ? `<div class="warning-stack">${d.warnings.map(esc).join("<br>")}</div>`
      : "") + "销量：订单件数 · 采购/库存：仓库SKU单位 · 库存范围：全仓";
  if (state.view === "classification") {
    if (state.classification) renderClassification();
    else loadClassification();
  } else if (state.view === "rules") renderRules();
  else if (state.view === "sources") renderSources();
  else renderTable();
  if (state.view === "overview") renderCharts();
  icons();
}
function formatTime(t) {
  return t
    ? new Date(t).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "尚未同步";
}
function filteredRows() {
  let rows = state.data.rows;
  const q = $("search").value.trim().toLowerCase();
  if (q)
    rows = rows.filter((r) =>
      [r.sku, r.name, r.color, r.spec].join(" ").toLowerCase().includes(q),
    );
  const stock = $("stockFilter").value;
  if (stock === "urgent")
    rows = rows.filter((r) => ["out", "low"].includes(r.stock_status));
  if (stock === "active") rows = rows.filter((r) => r.total > 0);
  if (stock === "unplanned") rows = rows.filter((r) => !r.plan);
  if (state.view === "plans") rows = rows.filter((r) => r.plan);
  return [...rows].sort((a, b) => {
    const av = a[state.sort] ?? -1,
      bv = b[state.sort] ?? -1;
    return (
      (typeof av === "string" ? av.localeCompare(bv) : av - bv) *
        (state.desc ? -1 : 1) || a.sku.localeCompare(b.sku)
    );
  });
}
function sortHeader(label, key) {
  return `<button data-sort="${key}">${label}${state.sort === key ? (state.desc ? " ↓" : " ↑") : " ↕"}</button>`;
}
function renderTable() {
  const catalog = state.view === "catalog";
  $("tableTitle").textContent = catalog
    ? "SKU 产品主数据"
    : state.view === "plans"
      ? "已保存备货计划"
      : "SKU 销量与备货";
  $("bulkButton").hidden = catalog;
  $("recomputeButton").hidden = catalog;
  $("stockFilter").hidden = catalog;
  $("shareMode").hidden = catalog;
  const shops = state.data.filters.shops;
  const all = filteredRows();
  const pages = Math.max(1, Math.ceil(all.length / state.size));
  state.page = Math.min(state.page, pages);
  const rows = all.slice(
    (state.page - 1) * state.size,
    state.page * state.size,
  );
  const shareKey = $("shareMode").value;
  $("tableHead").innerHTML = catalog
    ? "<tr><th>SKU / 商品</th><th>一级类目</th><th>颜色</th><th>规格</th><th>箱规</th><th>覆盖天数</th><th>资料状态</th><th>操作</th></tr>"
    : `<tr><th><input class="table-check" type="checkbox" id="selectPage" aria-label="选中本页未建计划 SKU"></th><th>${sortHeader("SKU / 商品", "sku")}</th><th>类目 / 颜色 / 规格</th>${shops.map((k) => `<th>${esc(state.data.shops.find((s) => s.key === k).name)}销量</th>`).join("")}<th>${sortHeader("总销量", "total")}</th><th>${sortHeader(shareKey === "category_share" ? "类目内占比" : "全局占比", shareKey)}</th><th>${sortHeader("可售库存", "available")}</th><th>${sortHeader("建议备货", "suggested")}</th><th>计划数量 / 状态</th><th>操作</th></tr>`;
  $("tableBody").innerHTML = rows.length
    ? rows
        .map((r) =>
          catalog
            ? `<tr><td class="sku-cell"><strong class="sku-name">${esc(r.sku)}</strong><div class="row-sub">${esc(r.name)}</div></td><td>${esc(r.category_name)}</td><td>${esc(r.color) || "—"}</td><td>${esc(r.spec) || "—"}</td><td>${num(r.product.box_size)}${r.product.box_size ? "" : " · 继承"}</td><td>${num(r.product.target_days)}${r.product.target_days ? " 天" : " · 继承"}</td><td><span class="badge ${r.product.confirmed ? "ok" : "warn"}">${r.product.confirmed ? "已确认" : "自动识别"}</span></td><td><button class="button small" data-catalog="${esc(r.sku)}">维护资料</button></td></tr>`
            : `<tr><td><input type="checkbox" class="table-check row-select" data-sku="${esc(r.sku)}" ${state.selected.has(r.sku) ? "checked" : ""} ${r.plan || r.suggested == null || r.suggested <= 0 ? "disabled" : ""} aria-label="选择 ${esc(r.sku)}"></td><td class="sku-cell"><strong class="sku-name">${esc(r.sku)}</strong><div class="row-sub">${esc(r.name)}</div></td><td>${esc(r.category_name)}<div class="row-sub">${esc(r.color) || "—"} · ${esc(r.spec) || "—"}</div></td>${shops.map((k) => `<td>${num(r.sales[k])}<div class="row-sub">${pct(r.shares[k])} / SKU</div></td>`).join("")}<td><strong>${num(r.total)}</strong><div class="row-sub">类目排名 #${r.rank}</div></td><td class="share-cell"><b>${pct(r[shareKey])}</b><div class="track"><span style="width:${r[shareKey]}%"></span></div><span class="row-sub">${shareKey === "category_share" ? "全局" : "类目"} ${pct(r[shareKey === "category_share" ? "global_share" : "category_share"])}</span></td><td>${num(r.available)}<div class="row-sub">${r.coverage == null ? (r.has_inventory ? "全仓合计" : "未映射库存") : `约 ${num(r.coverage)} 天`}</div></td><td title="${esc(r.suggestion_reason)}"><strong>${num(r.suggested)}</strong><div class="row-sub">${r.suggested == null ? "待补齐数据" : `箱规 ${r.basis.box_size} · ${num((r.suggested || 0) / r.basis.box_size)} 箱`}</div></td><td>${r.plan ? `<strong>${num(r.plan.quantity)}</strong><div class="row-sub">${statuses[r.plan.status]} · ${r.plan.locked ? "已锁定" : "未锁定"}</div>` : '<span class="row-sub">尚未保存</span>'}</td><td><div class="row-actions"><button class="button small tinted" data-plan="${esc(r.sku)}">${r.plan ? "查看计划" : "备货"}</button><button class="button small" data-catalog="${esc(r.sku)}" aria-label="维护 ${esc(r.sku)} 资料"><i data-lucide="tags"></i></button></div></td></tr>`,
        )
        .join("")
    : `<tr><td colspan="${8 + shops.length}"><div class="empty-state"><i data-lucide="search-x"></i><strong>${state.view === "plans" ? "暂无备货计划" : "没有符合条件的数据"}</strong></div></td></tr>`;
  $("tableCount").textContent = `共 ${all.length} 个 SKU · 第 ${state.page} 页`;
  $("pageInfo").textContent = `${state.page} / ${pages}`;
  $("prevPage").disabled = state.page === 1;
  $("nextPage").disabled = state.page >= pages;
  $("tableHead")
    .querySelectorAll("[data-sort]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          state.desc = state.sort === b.dataset.sort ? !state.desc : true;
          state.sort = b.dataset.sort;
          renderTable();
        }),
    );
  $("tableBody")
    .querySelectorAll("[data-plan]")
    .forEach((b) => (b.onclick = () => openPlan(b.dataset.plan)));
  $("tableBody")
    .querySelectorAll("[data-catalog]")
    .forEach((b) => (b.onclick = () => openCatalog(b.dataset.catalog)));
  $("tableBody")
    .querySelectorAll(".row-select")
    .forEach(
      (c) =>
        (c.onchange = () => {
          c.checked
            ? state.selected.add(c.dataset.sku)
            : state.selected.delete(c.dataset.sku);
          updateSelection();
        }),
    );
  if ($("selectPage"))
    $("selectPage").onchange = (e) => {
      $("tableBody")
        .querySelectorAll(".row-select:not(:disabled)")
        .forEach((c) => {
          c.checked = e.target.checked;
          c.checked
            ? state.selected.add(c.dataset.sku)
            : state.selected.delete(c.dataset.sku);
        });
      updateSelection();
    };
  updateSelection();
  icons();
}
function updateSelection() {
  $("bulkButton").disabled = !state.selected.size;
  $("bulkButton").querySelector("span").textContent = state.selected.size
    ? `采用建议 (${state.selected.size})`
    : "采用建议";
}
function renderCharts() {
  state.charts.forEach((c) => c.destroy());
  state.charts = [];
  if (!window.Chart) return;
  const d = state.data,
    shops = d.filters.shops;
  const labels = shops.map((k) => d.shops.find((s) => s.key === k).name);
  $("chartsGrid").querySelector(".chart-legend").innerHTML = labels
    .map(
      (n, i) =>
        `<span><b style="background:${colors[i % colors.length]}"></b>${esc(n)}</span>`,
    )
    .join("");
  const opts = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    animation: false,
  };
  state.charts.push(
    new Chart($("salesChart"), {
      type: "line",
      data: {
        labels: d.series.map((p) => p.date.slice(5)),
        datasets: shops.map((k, i) => ({
          label: labels[i],
          data: d.series.map((p) => p[k]),
          borderColor: colors[i % colors.length],
          backgroundColor: colors[i % colors.length],
          tension: 0.3,
          pointRadius: 0,
          borderWidth: 2,
        })),
      },
      options: {
        ...opts,
        scales: {
          x: {
            grid: { display: false },
            ticks: { maxTicksLimit: 8, font: { size: 9 }, color: "#aaa4b6" },
          },
          y: {
            beginAtZero: true,
            ticks: { font: { size: 9 }, color: "#aaa4b6" },
            grid: { color: "#f1eef6" },
          },
        },
      },
    }),
  );
  state.charts.push(
    new Chart($("shareChart"), {
      type: "doughnut",
      data: {
        labels,
        datasets: [
          {
            data: shops.map((k) => d.summary.shops[k]),
            backgroundColor: shops.map((k, i) => colors[i % colors.length]),
            borderWidth: 3,
            borderColor: "#fff",
          },
        ],
      },
      options: { ...opts, cutout: "78%" },
    }),
  );
  document.querySelector(".donut-center strong").textContent = shops.length;
  $("splitLegend").innerHTML = shops
    .map(
      (k, i) =>
        `<div class="split-row"><div class="split-row-top"><span><b style="background:${colors[i % colors.length]}"></b>${esc(labels[i])}</span><strong>${pct(d.summary.shares[k])}</strong></div><small>${num(d.summary.shops[k])} 件</small></div>`,
    )
    .join("");
}
function closeDrawer() {
  $("drawer").hidden = true;
  $("drawerBackdrop").hidden = true;
  state.drawerSku = null;
}
function drawerHeader(title, copy) {
  return `<div class="drawer-head"><div><span class="eyebrow">PURCHASE DECISION</span><h2 id="drawerTitle">${esc(title)}</h2><p>${esc(copy)}</p></div><button class="icon-button" type="button" id="closeDrawer" aria-label="关闭"><i data-lucide="x"></i></button></div>`;
}
function showDrawer(html) {
  $("drawer").innerHTML = html;
  $("drawer").hidden = false;
  $("drawerBackdrop").hidden = false;
  $("closeDrawer").onclick = closeDrawer;
  icons();
  $("drawer").querySelector("input,select,button")?.focus();
}
function basisHtml(b, filters) {
  const facts = [
    ["统计订单件数", num(b.order_sales)],
    ["仓库换算需求", num(b.warehouse_sales)],
    ["日均订单件数", num(b.order_daily_rate)],
    ["日均仓库需求", num(b.daily_rate)],
    ["覆盖天数", `${num(b.target_days)} 天`],
    ["安全系数", pct((b.safety_factor || 0) * 100)],
    ["全仓可售库存", num(b.available)],
    ["抵扣在途", num(b.in_transit_deducted)],
    ["箱规", `${num(b.box_size)} / 箱`],
    ["换算系数", num(b.conversion_ratio)],
  ];
  return `<div class="basis-grid">${facts.map(([k, v]) => `<div><span>${k}</span><strong>${v}</strong></div>`).join("")}</div><div class="basis-copy">${esc(filters?.start || "未记录")} — ${esc(filters?.end || "")}<br>日均仓库需求 × 覆盖天数 × (1 + 安全系数) − 可售库存 − 计入在途，向上取整至箱规倍数。<br>计算时间：${esc(formatTime(b.calculated_at))}</div>`;
}
function openPlan(sku) {
  const r = state.data.rows.find((x) => x.sku === sku),
    p = r.plan;
  state.drawerSku = sku;
  showDrawer(
    drawerHeader(sku, r.name) +
      `<form id="planForm"><div class="drawer-body"><section class="drawer-section"><h3>当前自动建议 ${num(r.suggested)} · 仓库 SKU 单位</h3>${r.suggestion_reason ? `<p class="catalog-tip">${esc(r.suggestion_reason)}</p>` : ""}${basisHtml(r.basis, state.data.filters)}</section>${p ? `<details class="drawer-section"><summary>${p.locked ? "已锁定" : "已保存"}计划的原始计算依据</summary>${p.basis?.daily_rate != null ? basisHtml(p.basis, p.basis_filter) : '<p class="catalog-tip">旧计划未记录计算依据，数量已保留。</p>'}</details>` : ""}<section class="drawer-section"><h3>采购数量与店铺分配</h3><label class="field">备货数量（仓库 SKU 单位）<input type="number" name="quantity" min="0" max="10000000" step="1" required value="${p?.quantity ?? r.suggested ?? 0}"></label><button class="text-button" type="button" id="useSuggested" ${r.suggested == null ? "disabled" : ""}>采用最新建议 ${num(r.suggested)}</button><div class="form-grid"><label class="field">计划状态<select name="status">${Object.entries(
        statuses,
      )
        .map(
          ([k, v]) =>
            `<option value="${k}" ${p?.status === k ? "selected" : ""}>${v}</option>`,
        )
        .join(
          "",
        )}</select></label><label class="field">分配方式<select name="mode"><option value="sales">按本次销量占比</option><option value="manual" ${p?.mode === "manual" ? "selected" : ""}>手动设置比例</option></select></label></div><div id="manualRatios" class="ratio-grid">${state.data.shops.map((s) => `<label class="field">${esc(s.name)} %<input type="number" min="0" max="100" step="0.01" data-weight="${esc(s.key)}" value="${p?.mode === "manual" ? p.weights[s.key] || 0 : r.shares[s.key] || 0}"></label>`).join("")}</div><div id="allocationPreview" class="allocation-preview"></div><p class="allocation-hint">店铺分配按整数最大余数法计算，分配之和等于采购数量。按销量保存时固定当次比例。</p><label class="check-field"><input name="locked" type="checkbox" ${p?.locked !== false ? "checked" : ""}><span>锁定计划数量<small>锁定值不被重算覆盖；未锁定草稿可通过「重算未锁定」更新。</small></span></label><label class="field">采购备注<textarea name="note" maxlength="1000">${esc(p?.note || "")}</textarea></label></section><div id="planError" class="form-error"></div><section id="planHistory"></section></div><div class="drawer-footer"><button class="button" type="button" id="cancelPlan">取消</button><button class="button primary" type="submit">保存计划</button></div></form>`,
  );
  const f = $("planForm");
  function preview() {
    const qty = Number(f.elements.quantity.value);
    const weights =
      f.elements.mode.value === "manual"
        ? Object.fromEntries(
            Array.from(f.querySelectorAll("[data-weight]")).map((i) => [
              i.dataset.weight,
              Number(i.value),
            ]),
          )
        : r.sales;
    const total = Object.values(weights).reduce((a, b) => a + b, 0);
    const exact = state.data.shops.map((s) =>
        total ? (qty * (weights[s.key] || 0)) / total : 0,
      ),
      vals = exact.map(Math.floor);
    let left = qty - vals.reduce((a, b) => a + b, 0);
    [...vals.keys()]
      .sort((a, b) => exact[b] - vals[b] - (exact[a] - vals[a]) || a - b)
      .forEach((i) => {
        if (left > 0) {
          vals[i]++;
          left--;
        }
      });
    $("manualRatios").hidden = f.elements.mode.value !== "manual";
    $("allocationPreview").innerHTML = state.data.shops
      .map(
        (s, i) =>
          `<div><span>${esc(s.name)}</span><strong>${num(vals[i])}</strong></div>`,
      )
      .join("");
  }
  f.oninput = preview;
  f.onchange = preview;
  preview();
  $("cancelPlan").onclick = closeDrawer;
  $("useSuggested").onclick = () => {
    f.elements.quantity.value = r.suggested;
    preview();
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    const btn = f.querySelector("[type=submit]");
    btn.disabled = true;
    try {
      const weights = Object.fromEntries(
        Array.from(f.querySelectorAll("[data-weight]")).map((i) => [
          i.dataset.weight,
          Number(i.value),
        ]),
      );
      await api("plans", "PUT", {
        sku,
        quantity: Number(f.elements.quantity.value),
        version: p?.version || 0,
        status: f.elements.status.value,
        mode: f.elements.mode.value,
        weights,
        note: f.elements.note.value,
        locked: f.elements.locked.checked,
        filters: state.data.filters,
      });
      closeDrawer();
      await load();
      toast("计划已保存");
    } catch (e) {
      $("planError").textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  };
  api(`sku?${new URLSearchParams({ ...state.query, sku })}`)
    .then((result) => {
      if (state.drawerSku !== sku || !$("planHistory")) return;
      $("planHistory").innerHTML = result.history.length
        ? "<h3>最近维护记录</h3>" +
          result.history
            .map(
              (h) =>
                `<div class="history-item"><strong>${esc({ create: "创建计划", update: "更新计划", catalog: "维护产品资料" }[h.action] || "维护记录")}${h.after.quantity != null ? ` · ${num(h.after.quantity)}` : ""}</strong><span>${esc(formatTime(h.created_at))}</span></div>`,
            )
            .join("")
        : "";
    })
    .catch(() => {});
}
function optionalNumber(input) {
  return input.value === "" ? null : Number(input.value);
}
function openCatalog(sku) {
  const r = state.data.rows.find((x) => x.sku === sku),
    p = r.product;
  state.drawerSku = sku;
  showDrawer(
    drawerHeader("维护产品资料", sku) +
      `<form id="catalogForm"><div class="drawer-body"><label class="field">商品名称<input name="name" maxlength="500" value="${esc(r.name)}"></label><label class="field">一级类目<select name="category">${state.data.categories.map((c) => `<option value="${esc(c.code)}" ${c.code === r.category ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label><div class="form-grid"><label class="field">颜色<input name="color" maxlength="500" value="${esc(r.color)}"></label><label class="field">规格<input name="spec" maxlength="500" value="${esc(r.spec)}"></label></div><label class="field">箱规（留空继承规则）<input name="box_size" type="number" step="1" min="1" max="100000" value="${p.box_size ?? ""}"></label><div class="form-grid"><label class="field">SKU 覆盖天数（可留空）<input name="target_days" type="number" step="1" min="1" max="365" value="${p.target_days ?? ""}"></label><label class="field">SKU 安全缓冲 %（可留空）<input name="safety" type="number" step="0.01" min="0" max="300" value="${p.safety_factor == null ? "" : p.safety_factor * 100}"></label></div><div class="form-error" id="catalogError"></div></div><div class="drawer-footer"><button class="button primary" type="submit">保存并确认资料</button></div></form>`,
  );
  const f = $("catalogForm");
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      await api("catalog", "PUT", {
        sku,
        version: p.version || 0,
        name: f.elements.name.value,
        category: f.elements.category.value,
        color: f.elements.color.value,
        spec: f.elements.spec.value,
        box_size: optionalNumber(f.elements.box_size),
        target_days: optionalNumber(f.elements.target_days),
        safety_factor:
          f.elements.safety.value === ""
            ? null
            : Number(f.elements.safety.value) / 100,
      });
      closeDrawer();
      await load();
      toast("产品资料已保存");
    } catch (e) {
      $("catalogError").textContent = e.message;
    }
  };
}
function ruleFields(rule) {
  return `<div class="form-grid"><label class="field">覆盖天数<input name="target_days" type="number" min="1" max="365" step="1" required value="${rule.target_days}"></label><label class="field">安全缓冲 %<input name="safety" type="number" min="0" max="300" step="0.01" required value="${rule.safety_factor * 100}"></label></div><label class="field">默认箱规（仓库单位 / 箱）<input name="box_size" type="number" min="1" max="100000" step="1" required value="${rule.box_size}"></label><label class="check-field"><input name="include_in_transit" type="checkbox" ${rule.include_in_transit ? "checked" : ""}><span>抵扣在途库存<small>仅在确认在途能够覆盖采购周期时启用。</small></span></label>`;
}
function dialog(title, copy, content, onSubmit) {
  $("settingsDialog").innerHTML =
    `<form id="dialogForm"><div class="dialog-head"><h2>${esc(title)}</h2><button class="icon-button" type="button" id="closeSettings" aria-label="关闭"><i data-lucide="x"></i></button></div>${copy ? `<p class="dialog-copy">${esc(copy)}</p>` : ""}${content}<div class="form-error" id="dialogError"></div><button class="button primary full" type="submit">保存</button></form>`;
  $("closeSettings").onclick = () => $("settingsDialog").close();
  $("dialogForm").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("dialogForm").querySelector("[type=submit]");
    btn.disabled = true;
    try {
      await onSubmit($("dialogForm"));
      $("settingsDialog").close();
      await load();
      toast("已保存");
    } catch (e) {
      $("dialogError").textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  };
  icons();
  $("settingsDialog").showModal();
}
function rulePayload(f) {
  return {
    target_days: Number(f.elements.target_days.value),
    safety_factor: Number(f.elements.safety.value) / 100,
    box_size: Number(f.elements.box_size.value),
    include_in_transit: f.elements.include_in_transit.checked,
  };
}
function openSettings() {
  if (!state.data) return;
  dialog(
    "全局备货规则",
    "新计算立即使用这些规则；锁定计划继续保留已保存的数量。",
    ruleFields(state.data.settings) +
      `<div class="formula">建议量 = 日均仓库需求 × 覆盖天数 × (1 + 安全系数) − 可售库存 − 计入在途<br><small>最低为 0，再向上取整到箱规倍数。</small></div>`,
    (f) => api("settings", "PUT", rulePayload(f)),
  );
}
function openRule(rule) {
  const d = state.data;
  dialog(
    rule ? "编辑备货规则" : "添加备货规则",
    "单店查询应用店铺规则；全店合并应用全局及类目规则。SKU 专属设置优先。",
    `<div class="form-grid"><label class="field">类目<select name="category" ${rule ? "disabled" : ""}><option value="">全部类目</option>${d.categories.map((c) => `<option value="${esc(c.code)}" ${rule?.category === c.code ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></label><label class="field">店铺<select name="shop" ${rule ? "disabled" : ""}><option value="">全部店铺</option>${d.shops.map((s) => `<option value="${esc(s.key)}" ${rule?.shop === s.key ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label></div>${ruleFields(rule || d.settings)}`,
    (f) => {
      const category = f.elements.category.value,
        shop = f.elements.shop.value;
      const existing = d.rules.find(
        (r) => r.category === category && r.shop === shop,
      );
      return api("rules", "PUT", {
        ...rulePayload(f),
        category,
        shop,
        version: rule?.version || existing?.version || 0,
      });
    },
  );
}
function renderRules() {
  const d = state.data,
    category = (c) =>
      d.categories.find((x) => x.code === c)?.name || "全部类目",
    shop = (k) => d.shops.find((x) => x.key === k)?.name || "全部店铺";
  $("sourcesPage").innerHTML =
    `<article class="panel rules-panel"><div class="sku-panel-heading" style="padding:0"><h2>全局规则</h2><button class="button" id="editGlobal">编辑</button></div><div class="rule-facts"><span>覆盖 ${d.settings.target_days}天</span><span>安全系数 ${pct(d.settings.safety_factor * 100)}</span><span>箱规 ${d.settings.box_size}</span><span>${d.settings.include_in_transit ? "抵扣在途" : "不抵扣在途"}</span></div></article><article class="panel rules-panel"><div class="sku-panel-heading" style="padding:0"><h2>类目与店铺规则</h2><button class="button primary" id="addRule">新增规则</button></div><div class="rule-list">${d.rules.length ? d.rules.map((r, i) => `<div class="rule-row"><div><strong>${esc(category(r.category))} / ${esc(shop(r.shop))}</strong><p>覆盖 ${r.target_days}天 · 安全系数 ${pct(r.safety_factor * 100)} · 箱规 ${r.box_size} · ${r.include_in_transit ? "抵扣在途" : "不抵扣在途"}</p></div><div class="rules-actions"><button class="button small" data-rule="${i}">编辑</button><button class="button small" data-remove="${i}">删除</button></div></div>`).join("") : "<p>暂无专属规则</p>"}</div></article>`;
  $("editGlobal").onclick = openSettings;
  $("addRule").onclick = () => openRule();
  $("sourcesPage")
    .querySelectorAll("[data-rule]")
    .forEach(
      (b) => (b.onclick = () => openRule(d.rules[Number(b.dataset.rule)])),
    );
  $("sourcesPage")
    .querySelectorAll("[data-remove]")
    .forEach(
      (b) =>
        (b.onclick = async () => {
          try {
            await api(
              "rules/remove",
              "POST",
              d.rules[Number(b.dataset.remove)],
            );
            await load();
            toast("规则已删除");
          } catch (e) {
            toast(e.message, true);
          }
        }),
    );
}
function renderSources() {
  const names = {
    sales: "ERP 销量中台",
    inventory: "仓库库存",
    home: "Panda Homes",
    buy: "Panda Buy",
    shein: "Shein",
  };
  $("sourcesPage").innerHTML =
    `<div class="sources-grid">${state.data.sources.map((s) => `<article class="panel source-card"><div class="source-head"><span class="source-icon"><i data-lucide="${s.source === "inventory" ? "warehouse" : "database"}"></i></span><div><h3>${esc(names[s.source] || s.source)}</h3><p>${s.status === "ok" ? "连接正常" : "拉取失败 · 保留最后成功快照"}</p></div></div><div class="source-datum"><span>最近成功</span><strong>${esc(formatTime(s.success_at))}</strong></div><div class="source-datum"><span>最近检查</span><strong>${esc(formatTime(s.checked_at))}</strong></div>${s.details.rows != null ? `<div class="source-datum"><span>聚合记录</span><strong>${num(s.details.rows)}</strong></div>` : ""}${s.details.skus != null ? `<div class="source-datum"><span>库存 SKU</span><strong>${num(s.details.skus)}</strong></div>` : ""}${s.error ? `<p class="source-error">${esc(s.error)}</p>` : ""}</article>`).join("")}</div><article class="panel source-explanation"><h2>数据与口径</h2><p>销量以订单 quantity 求和，取消与退款订单剔除；库存与采购建议以仓库 SKU 单位计算。</p><p>部分历史 Temu 订单无平台下单时间，沿用中台首次采集日期，日期归属需结合此限制解读。</p><p>同步失败保留成功快照；超过 30 分钟、所选日期不完整或 SKU 缺少库存映射时暂停相应建议。</p></article>`;
}
async function downloadExcel(kind) {
  const button = kind === "purchase" ? $("purchaseExport") : $("exportButton");
  button.disabled = true;
  try {
    let job = await api("exports", "POST", { kind, filters: exportFilters() });
    toast("正在生成 Excel…");
    const until = Date.now() + 120000;
    while (["queued", "running"].includes(job.status) && Date.now() < until) {
      await new Promise((r) => setTimeout(r, 800));
      job = await api(`exports/${job.id}`);
    }
    if (job.status !== "ready")
      throw new Error(job.error || "导出仍在处理，请稍后重试");
    const a = document.createElement("a");
    a.href = `api/exports/${job.id}/download`;
    a.download = job.filename;
    document.body.append(a);
    a.click();
    a.remove();
    toast("Excel 已生成");
  } catch (e) {
    toast(e.message, true);
  } finally {
    button.disabled = false;
  }
}
function categoryIcon(icon = "package") {
  const paths = {
    hanger:
      '<path d="M10 6a2 2 0 1 1 3.5 1.3L12 9v2"/><path d="m12 11-9 6a1 1 0 0 0 .6 2h16.8a1 1 0 0 0 .6-2Z"/>',
    hamper:
      '<path d="M4 7h16l-1.5 14h-13Z"/><path d="M8 7V5a4 4 0 0 1 8 0v2M8 11v6m4-6v6m4-6v6"/>',
    box: '<path d="m12 3 9 5-9 5-9-5Z"/><path d="M3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/>',
    layers:
      '<path d="m12 3 10 6-10 6L2 9Z"/><path d="m2 13 10 6 10-6M2 17l10 6 10-6"/>',
    package:
      '<rect x="4" y="5" width="16" height="16" rx="2"/><path d="M9 5v5h6V5M9 16h6"/>',
  };
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[icon] || paths.package}</svg>`;
}
async function loadClassification() {
  $("sourcesPage").innerHTML =
    '<div class="panel"><div class="empty-state"><div class="spinner"></div><strong>加载中</strong></div></div>';
  try {
    state.classification = await api("catalog");
    state.classSelected.clear();
    if (state.view === "classification") renderClassification();
  } catch (e) {
    $("sourcesPage").innerHTML =
      `<div class="error-banner">${esc(e.message)}</div>`;
  }
}
function classCategoryForm(category) {
  const c = category || {
    code: `category_${Date.now().toString(36)}`,
    name: "",
    icon: "package",
    sort_order: 100,
    version: 0,
    match_rules: { sku_prefixes: [], keywords: [] },
  };
  dialog(
    category ? "编辑类目" : "新增类目",
    "",
    `<input type="hidden" name="code" value="${esc(c.code)}"><label class="field">类目名称<input name="name" value="${esc(c.name)}" maxlength="60" required></label><label class="field">图标<input name="icon" type="hidden" value="${esc(c.icon)}"></label><div class="icon-choices">${["hanger", "hamper", "package", "box", "layers"].map((icon) => `<button type="button" class="icon-choice ${c.icon === icon ? "active" : ""}" data-category-icon="${icon}" aria-label="${{ hanger: "衣架图标", hamper: "收纳筐图标", package: "商品图标", box: "包装图标", layers: "层级图标" }[icon]}" aria-pressed="${c.icon === icon}">${categoryIcon(icon)}</button>`).join("")}</div><label class="field">排序<input name="sort_order" type="number" min="0" max="10000" step="1" value="${c.sort_order}" required></label>${c.code === "uncategorized" ? "" : `<div class="form-grid"><label class="field">SKU 前缀（每行一项）<textarea name="sku_prefixes" rows="4">${esc((c.match_rules.sku_prefixes || []).join("\n"))}</textarea></label><label class="field">商品关键词（每行一项）<textarea name="keywords" rows="4">${esc((c.match_rules.keywords || []).join("\n"))}</textarea></label></div><div class="rule-impact">规则变更会更新未确认的自动分类，人工确认的 SKU 保持原分类。</div>`}`,
    (f) =>
      api("categories", "PUT", {
        code: c.code,
        name: f.elements.name.value,
        icon: f.elements.icon.value,
        sort_order: Number(f.elements.sort_order.value),
        version: c.version,
        match_rules: {
          sku_prefixes:
            f.elements.sku_prefixes?.value
              .split("\n")
              .map((v) => v.trim())
              .filter(Boolean) || [],
          keywords:
            f.elements.keywords?.value
              .split("\n")
              .map((v) => v.trim())
              .filter(Boolean) || [],
        },
      }),
  );
  $("dialogForm")
    .querySelectorAll("[data-category-icon]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          $("dialogForm").elements.icon.value = b.dataset.categoryIcon;
          $("dialogForm")
            .querySelectorAll("[data-category-icon]")
            .forEach((x) => {
              x.classList.toggle("active", x === b);
              x.setAttribute("aria-pressed", String(x === b));
            });
        }),
    );
}
function renderClassification() {
  const d = state.classification;
  if (!d) return;
  if (
    !["all", ...d.categories.map((c) => c.code)].includes(state.classCategory)
  )
    state.classCategory = "all";
  const counts = Object.fromEntries(
    d.categories.map((c) => [
      c.code,
      d.items.filter((p) => p.category === c.code).length,
    ]),
  );
  const q = state.classSearch.toLowerCase();
  const items = d.items.filter(
    (p) =>
      (state.classCategory === "all" || p.category === state.classCategory) &&
      (!q ||
        [p.sku, p.name, p.color, p.spec].join(" ").toLowerCase().includes(q)),
  );
  const pages = Math.max(1, Math.ceil(items.length / 20));
  state.classPage = Math.min(state.classPage, pages);
  const rows = items.slice((state.classPage - 1) * 20, state.classPage * 20);
  $("sourcesPage").innerHTML =
    `<div class="classification-layout"><aside class="panel category-directory"><div class="directory-head"><h2>类目</h2><button class="icon-button" id="newClassCategory" aria-label="新增类目"><i data-lucide="plus"></i></button></div><button class="directory-item ${state.classCategory === "all" ? "active" : ""}" data-class-category="all"><span class="category-icon">${categoryIcon("layers")}</span><span>全部类目</span><b>${d.items.length}</b></button>${d.categories.map((c) => `<div class="directory-entry"><button class="directory-item ${state.classCategory === c.code ? "active" : ""}" data-class-category="${esc(c.code)}"><span class="category-icon" data-icon="${esc(c.icon)}">${categoryIcon(c.icon)}</span><span>${esc(c.name)}</span><b>${counts[c.code]}</b></button><button class="category-edit" data-class-edit="${esc(c.code)}" aria-label="编辑${esc(c.name)}"><i data-lucide="settings-2"></i></button></div>`).join("")}</aside><section class="panel classification-table"><div class="classification-heading"><h2>SKU 分类</h2><div class="rules-actions"><button class="button small" id="manageClassCategory">编辑类目</button><button class="button small" id="deleteClassCategory" ${state.classCategory === "all" || state.classCategory === "uncategorized" || counts[state.classCategory] ? "disabled" : ""}>删除空类目</button></div></div><div class="classification-toolbar"><div class="search-box"><i data-lucide="search"></i><input id="classSearch" value="${esc(state.classSearch)}" placeholder="SKU / 商品名称" aria-label="搜索分类 SKU"></div><div class="classification-bulk"><span id="classSelection">已选 ${state.classSelected.size}</span><select id="classTarget" aria-label="目标类目">${d.categories.map((c) => `<option value="${esc(c.code)}">${esc(c.name)}</option>`).join("")}</select><button class="button primary small" id="classBulkApply" ${state.classSelected.size ? "" : "disabled"}>批量分类</button></div></div><div class="table-scroll"><table><thead><tr><th><input type="checkbox" id="classSelectPage" aria-label="选择本页 SKU"></th><th>SKU / 商品</th><th>颜色 / 规格</th><th>所属类目</th><th>分类状态</th></tr></thead><tbody>${rows.length ? rows.map((p) => `<tr><td><input type="checkbox" class="class-row-check" data-sku="${esc(p.sku)}" ${state.classSelected.has(p.sku) ? "checked" : ""} aria-label="选择 ${esc(p.sku)}"></td><td class="sku-cell"><strong class="sku-name">${esc(p.sku)}</strong><div class="row-sub">${esc(p.name)}</div></td><td>${esc(p.color) || "—"}<div class="row-sub">${esc(p.spec) || "—"}</div></td><td><select class="inline-category" data-class-sku="${esc(p.sku)}" aria-label="${esc(p.sku)}所属类目">${d.categories.map((c) => `<option value="${esc(c.code)}" ${c.code === p.category ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></td><td><span class="badge ${p.confirmed ? "ok" : "warn"}">${p.confirmed ? "已确认" : "自动匹配"}</span></td></tr>`).join("") : '<tr><td colspan="5"><div class="empty-state"><strong>暂无 SKU</strong></div></td></tr>'}</tbody></table></div><div class="table-footer"><span>共 ${items.length}个 SKU</span><div class="pagination"><button class="icon-button" id="classPrev" ${state.classPage === 1 ? "disabled" : ""} aria-label="上一页"><i data-lucide="chevron-left"></i></button><span>${state.classPage} / ${pages}</span><button class="icon-button" id="classNext" ${state.classPage === pages ? "disabled" : ""} aria-label="下一页"><i data-lucide="chevron-right"></i></button></div></div></section></div>`;
  $("newClassCategory").onclick = () => classCategoryForm();
  $("sourcesPage")
    .querySelectorAll("[data-class-category]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          state.classCategory = b.dataset.classCategory;
          state.classPage = 1;
          renderClassification();
        }),
    );
  $("sourcesPage")
    .querySelectorAll("[data-class-edit]")
    .forEach(
      (b) =>
        (b.onclick = () =>
          classCategoryForm(
            d.categories.find((c) => c.code === b.dataset.classEdit),
          )),
    );
  $("manageClassCategory").onclick = () =>
    state.classCategory === "all"
      ? classCategoryForm()
      : classCategoryForm(
          d.categories.find((c) => c.code === state.classCategory),
        );
  $("deleteClassCategory").onclick = () => {
    const c = d.categories.find((c) => c.code === state.classCategory);
    dialog("删除类目", "", `<p class="dialog-copy">${esc(c.name)}</p>`, () =>
      api("categories/remove", "POST", { code: c.code, version: c.version }),
    );
  };
  $("classSearch").oninput = (e) => {
    const pos = e.target.selectionStart;
    state.classSearch = e.target.value;
    state.classPage = 1;
    renderClassification();
    $("classSearch").focus();
    $("classSearch").setSelectionRange(pos, pos);
  };
  const updateSelection = () => {
    $("classSelection").textContent = `已选 ${state.classSelected.size}`;
    $("classBulkApply").disabled = !state.classSelected.size;
  };
  $("sourcesPage")
    .querySelectorAll(".class-row-check")
    .forEach(
      (i) =>
        (i.onchange = () => {
          i.checked
            ? state.classSelected.add(i.dataset.sku)
            : state.classSelected.delete(i.dataset.sku);
          updateSelection();
        }),
    );
  $("classSelectPage").onchange = (e) => {
    $("sourcesPage")
      .querySelectorAll(".class-row-check")
      .forEach((i) => {
        i.checked = e.target.checked;
        i.checked
          ? state.classSelected.add(i.dataset.sku)
          : state.classSelected.delete(i.dataset.sku);
      });
    updateSelection();
  };
  async function classify(skus, category) {
    const payload = {
      category,
      items: skus.map((sku) => ({
        sku,
        version: d.items.find((p) => p.sku === sku).version,
      })),
    };
    await api("catalog/classify", "POST", payload);
    await load();
    toast(`已分类 ${skus.length}个 SKU`);
  }
  $("classBulkApply").onclick = async () => {
    const btn = $("classBulkApply");
    btn.disabled = true;
    try {
      await classify([...state.classSelected], $("classTarget").value);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  };
  $("sourcesPage")
    .querySelectorAll("[data-class-sku]")
    .forEach(
      (i) =>
        (i.onchange = async () => {
          i.disabled = true;
          try {
            await classify([i.dataset.classSku], i.value);
          } catch (e) {
            toast(e.message, true);
            renderClassification();
          }
        }),
    );
  $("classPrev").onclick = () => {
    state.classPage--;
    renderClassification();
  };
  $("classNext").onclick = () => {
    state.classPage++;
    renderClassification();
  };
  icons();
}

document.querySelectorAll("[data-view]").forEach(
  (b) =>
    (b.onclick = () => {
      state.view = b.dataset.view;
      if (state.view === "classification") state.classification = null;
      state.page = 1;
      state.selected.clear();
      $("sidebar").classList.remove("open");
      render();
    }),
);
$("menu").onclick = () => $("sidebar").classList.toggle("open");
$("drawerBackdrop").onclick = closeDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDrawer();
  if (
    e.key === "/" &&
    !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
  ) {
    e.preventDefault();
    $("search").focus();
  }
});
$("settingsButton").onclick = openSettings;
$("sourceLink").onclick = () => {
  state.view = "sources";
  render();
};
$("queryButton").onclick = () => {
  document.querySelectorAll(".check-dropdown").forEach((d) => (d.open = false));
  load(readQuery());
};
$("resetQuery").onclick = () => {
  $("preset").value = "last30";
  $("search").value = "";
  $("stockFilter").value = "all";
  load({ preset: "last30" });
};
$("preset").onchange = () => {
  $("startDate").value = "";
  $("endDate").value = "";
  $("filterPending").textContent = "未查询";
};
["startDate", "endDate", "categorySelect"].forEach(
  (id) =>
    ($(id).onchange = () => {
      if (["startDate", "endDate"].includes(id)) $("preset").value = "custom";
      $("filterPending").textContent = "未查询";
    }),
);
$("shopSelect").onchange = (e) => {
  if (e.target.value === "all" && e.target.checked)
    $("shopSelect")
      .querySelectorAll('input:not([value="all"])')
      .forEach((i) => (i.checked = false));
  else if (e.target.value !== "all")
    $("shopSelect").querySelector('[value="all"]').checked = false;
  dropdownLabels();
  $("filterPending").textContent = "未查询";
};
$("statusSelect").onchange = () => {
  dropdownLabels();
  $("filterPending").textContent = "未查询";
};
document.addEventListener("click", (e) =>
  document.querySelectorAll(".check-dropdown[open]").forEach((d) => {
    if (!d.contains(e.target)) d.open = false;
  }),
);
$("search").oninput = () => {
  state.page = 1;
  renderTable();
};
$("stockFilter").onchange = () => {
  state.page = 1;
  renderTable();
};
$("shareMode").onchange = renderTable;
$("sort").onchange = () => {
  state.sort = { sales: "total", suggested: "suggested", sku: "sku" }[
    $("sort").value
  ];
  state.desc = state.sort !== "sku";
  renderTable();
};
$("prevPage").onclick = () => {
  state.page--;
  renderTable();
};
$("nextPage").onclick = () => {
  state.page++;
  renderTable();
};
$("exportButton").onclick = () => downloadExcel("report");
$("purchaseExport").onclick = () => downloadExcel("purchase");
$("bulkButton").onclick = async () => {
  try {
    const rows = state.data.rows.filter((r) => state.selected.has(r.sku));
    await api("plans/bulk", "POST", {
      filters: state.data.filters,
      items: rows.map((r) => ({ sku: r.sku, version: 0 })),
    });
    await load();
    toast(`已保存并锁定 ${rows.length} 个备货计划`);
  } catch (e) {
    toast(e.message, true);
  }
};
$("recomputeButton").onclick = async () => {
  try {
    const r = await api("plans/recompute", "POST", {
      filters: state.data.filters,
    });
    await load();
    toast(
      `已更新 ${r.updated} 个未锁定草稿，保留 ${r.skipped_locked} 个锁定计划`,
    );
  } catch (e) {
    toast(e.message, true);
  }
};
$("syncButton").onclick = async () => {
  try {
    await api("sync", "POST", {});
    $("syncButton").disabled = true;
    toast("正在同步最新销量与库存");
  } catch (e) {
    toast(e.message, true);
  }
};
let wasRunning = false;
setInterval(async () => {
  try {
    const r = await api("status");
    $("syncButton").disabled = r.sync.running;
    $("syncButton").classList.toggle("spinning", r.sync.running);
    if (
      wasRunning &&
      !r.sync.running &&
      !state.drawerSku &&
      !$("settingsDialog").open
    )
      await load();
    wasRunning = r.sync.running;
  } catch {}
}, 5000);
icons();
load();
