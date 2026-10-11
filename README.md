# Pangu 备货管理

独立运行在 HK 的备货决策系统，入口 https://pangutech.online/pangu-restock/。Python 3.10+ 标准库、SQLite、本地 Chart.js/Lucide，无运行时第三方依赖。沿用现有团队入口，本期不新增账号角色系统。

## 一期功能

- 自定义起止日期，含起止当天（北京时间）；近 7/30/90 天、本季度、上季度；查询和重置。单次查询最多 366 天，销量快照覆盖近 730 天。
- HOME、BUY、Shein 及中台后续新增店铺，多选店铺和有效订单状态。默认包含待发货、已发货、已完成的有效订单；取消、退款和 `sales_eligible=false` 剔除。可仅选择已发货/已完成。
- 可配置类目总览、SKU 下钻、排名、类目内/全局占比切换、列头排序、搜索及分页。店铺销量和 SKU 内各店占比单独展示。
- SKU 分类管理：类目名称、图标、排序、SKU 前缀和商品关键词规则；搜索、按类目筛选、逐项和批量归类。类目变更更新未确认的自动分类，人工确认的归属保留。已有 SKU 或备货规则的类目禁止删除。
- 本地产品资料：类目、名称、颜色、规格、箱规、SKU 覆盖天数及安全系数，可修正自动识别结果；新增类目无需改表。
- 自动建议、人工编辑/保存/锁定、批量采用建议、重算未锁定草稿、状态流转、店铺整数分配与修改记录。
- 真正的 `.xlsx`：异步生成销量报表和采购建议单，包含筛选与口径、类目占比、SKU 销量、采购建议四张工作表。采购单保留锁定计划的原始计算依据，数量和比例为数值单元格。
- 每 5 分钟自动同步；单源失败保留成功快照，超过 30 分钟、日期窗口不完整或 SKU 缺少库存记录时暂停相应自动建议。

## 计算与单位

销量占比以 ERP 订单行 `quantity` 求和为基数，不使用订单行数、金额或仓库换算量。类目筛选/搜索不改变全局占比分母。两位小数用最大余数法分配，完整维度合计为 100%。

采购和库存按仓库 SKU 单位计算，使用中台的 `warehouse_quantity` 换算需求，与订单件数分开显示。不同包装规格存在换算关系时，页面和 Excel 展示日均订单销量、日均仓库需求与换算系数。

```
日均仓库需求 = 窗口内 warehouse_quantity / 含首尾的自然日天数
基础需求 = 日均仓库需求 × 覆盖天数 × (1 + 安全系数)
原始建议 = max(0, 基础需求 - 全仓可售库存 - 可选抵扣在途)
建议量 = ceil(原始建议 / 箱规) × 箱规
```

默认覆盖 30 天、安全系数 0.2、箱规 1、不抵扣在途。优先级为全局 → 店铺 → 类目 → 类目与店铺组合 → SKU 专属设置。店铺规则仅在单店查询时应用；全店合并使用全局及类目规则。单店查看时库存仍为全仓合计，并非虚构独立店铺库存。

保存计划会保存数量、店铺权重、筛选条件和计算依据。自动同步不修改计划；「重算未锁定」仅更新当前筛选内的未锁定草稿，已锁定/已确认/采购中/已完成计划保留。人工仍可显式编辑锁定计划。旧计划升级时保留原数量、版本与备注并默认锁定。

## 数据接口与限制

类目配置保存在本地 SQLite `categories` 表；`config/category-presets.json` 仅用于新库初始化与旧库一次性迁移，预置衣架、脏衣篓、待分类。页面修改后重启不覆盖配置，不重新创建已删除的空类目。匹配顺序先 SKU 前缀、后商品关键词，同层按类目排序；规则均不匹配则归入待分类。人工归类会记录确认状态及修改历史，批量更新以事务保证全部成功或全部回滚。

HK 销售管理新增只读 `GET /api/restock/snapshot?days=730`（版本 2）。直接聚合其 ERP 数据库，输出日期、动态店铺、仓库 SKU、状态、订单件数、仓库换算需求与仓库维度，不输出订单编号或客户数据。备货项目只通过此 HTTP 接口拉取聚合数据，不直接连接外部平台或中台数据库。

库存使用 XLWMS `inventory/sku-levels` 既有合并和修正语义。同步时查询 HOME/BUY/Shein manager 的连接元数据；Temu 店铺选择通过 `X-Temu-Shop`，Shein 使用 `shop_key`。

历史 Temu 订单部分缺少平台下单时间，沿用 ERP `first_seen` 日期，历史回补可能影响时间归属；页面与导出提示此限制。历史 Temu 中台状态可能滞后，报表用已保存的 `parentShippingTime` 将有效单归为已发货，取消/退款剔除仍沿用中台口径。类目/颜色/规格初始识别可人工确认，未知类目保留在「待分类」，不隐藏销量。缺少库存映射的 SKU 不假定为零库存。

二期已预留 `region/warehouse` 维度和 NY/ATL/HOU/LA 映射配置结构；州范围尚待确认，本期不自动分仓/调拨。

## 部署与测试

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
python3 app.py
```

默认监听 `127.0.0.1:18110`，SQLite `data/restock.sqlite3`。生产服务 `pangu-restock-manager.service`，数据目录 700、数据库 600。备份使用 SQLite backup API；在线直接复制主文件会漏掉 WAL 写入。

HK Nginx `/pangu-restock/` → 本地 18110；pangutech HTTPS 入口 → HK Nginx。应用与本地数据均在 HK。中台服务沿用已有 PM2 实例，不另启动旧 systemd 中台单元。

## 本地 API

- `GET /api/health`、`GET /api/status`
- `GET /api/dashboard?start=2026-09-12&end=2026-10-11&shops=home,buy&category=hanger&statuses=pending,shipped,completed`
- `GET /api/dashboard?preset=last30`；`preset` 支持 `last7/last30/last90/current_quarter/previous_quarter`。
- `GET /api/sku?sku=...`，同报表筛选参数。
- `GET /api/catalog` 全量 SKU 分类资料；`PUT /api/catalog` 产品资料维护。
- `POST /api/catalog/classify`：`category,items:[{sku,version}]`，单次最多 500 个 SKU，确认分类并保留其他产品资料。
- `PUT /api/categories`：`code,name,icon,sort_order,match_rules:{sku_prefixes,keywords},version`；`POST /api/categories/remove`：`code,version`。
- `PUT /api/settings`；`PUT /api/rules`；`POST /api/rules/remove`
- `PUT /api/plans`：`sku,quantity,version,status,mode,weights,note,locked,filters`。
- `POST /api/plans/bulk`：`items:[{sku,version:0}],filters`，仅新计划，事务原子保存并锁定。
- `POST /api/plans/recompute`：`filters`，只重算未锁定草稿。
- `POST /api/exports`：`kind:report|purchase,filters`；`GET /api/exports/{id}` 查询状态；`GET /api/exports/{id}/download` 下载。
- `POST /api/sync`；兼容 `GET /api/export.csv`。

写接口要求 JSON、`X-Pangu-Request: restock`，验证 Origin；计划、产品和专属规则用 version 防止覆盖并发编辑。Excel 任务使用独立两线程、最多六个排队/运行任务，生成后再开放下载，后续导出时清理超过 24 小时的文件。
