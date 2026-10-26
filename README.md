# Pangu 备货管理

独立运行在 HK 的备货决策系统，入口 https://pangutech.online/pangu-restock/。Python 3.10+ 标准库、SQLite、本地 Apache ECharts 6.0.0 / Lucide，后端无第三方运行依赖。沿用现有团队入口，本期不新增账号角色系统。

## 一期功能

- 自定义起止日期，含起止当天（北京时间）；近 7/30/90 天、本季度、上季度；查询和重置。单次查询最多 366 天，销量快照覆盖近 730 天。
- HOME、BUY、Shein 及中台后续新增店铺，多选店铺和有效订单状态。默认包含待发货、已发货、已完成的有效订单；取消、退款和 `sales_eligible=false` 剔除。可仅选择已发货/已完成。
- ECharts 类目和店铺销量环形图、销量趋势图，图例显示真实件数及两位小数占比，点击图例或扇区下钻。零销量显示空环；占比角度不做最小值放大。
- 可配置类目总览、SKU 下钻、排名、类目内/全局占比切换、列头排序、搜索及分页。店铺销量和 SKU 内各店占比单独展示。
- SKU 分类管理：类目名称、图标、排序、SKU 前缀和商品关键词规则；搜索、按类目筛选、逐项和批量归类。类目变更更新未确认的自动分类，人工确认的归属保留。已有 SKU 或备货规则的类目禁止删除。
- 本地产品资料：类目、名称、颜色、规格、箱规、SKU 覆盖天数及安全系数，可修正自动识别结果；新增类目无需改表。
- 自动建议、人工编辑/保存/锁定、批量采用建议、重算未锁定草稿、状态流转、店铺整数分配与修改记录。
- 真正的 `.xlsx`：异步生成销量报表和采购建议单，包含筛选与口径、类目占比、SKU 销量、采购建议四张工作表。采购单保留锁定计划的原始计算依据，数量和比例为数值单元格。
- 每 5 分钟自动同步；单源失败保留成功快照，超过 30 分钟、日期窗口不完整或 SKU 缺少库存记录时暂停相应自动建议。

## 计算与单位

销量占比以销售 SKU 的 ERP 订单行 `quantity` 求和为基数，不使用订单行数、金额或仓库换算量。销售 SKU 与仓库 SKU 独立汇总；100Pcs 映射为 2 × 50Pcs 时，100Pcs 的订单销量仍归 100Pcs，换算需求归 50Pcs。类目筛选/搜索不改变全局占比分母。两位小数用最大余数法分配，完整维度合计为 100%。

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

类目配置保存在本地 SQLite `categories` 表；`config/category-presets.json` 仅用于新库初始化与旧库一次性迁移，预置衣架、脏衣篓、烤盘、待分类。页面修改后重启不覆盖配置，不重新创建已删除的空类目。匹配顺序先 SKU 前缀、后商品关键词；关键词冲突时优先最长匹配，长度相同按类目排序；规则均不匹配则归入待分类。人工归类会记录确认状态及修改历史，批量更新以事务保证全部成功或全部回滚。

HK 销售管理新增只读 `GET /api/restock/snapshot?days=730&version=3`（版本 3）。直接聚合其 ERP 数据库，输出日期、动态店铺、销售 SKU（`sales_sku`）、仓库 SKU（`sku`）、状态、订单件数、换算系数、仓库换算需求与仓库维度，不输出订单编号或客户数据。备货项目只通过此 HTTP 接口拉取聚合数据，不直接连接外部平台或中台数据库。

库存使用 XLWMS `inventory/sku-levels` 既有合并和修正语义。同步时查询 HOME/BUY/Shein manager 的连接元数据；Temu 店铺选择通过 `X-Temu-Shop`，Shein 使用 `shop_key`。

SHEIN manager 将全状态订单及商品明细持续保存到其订单库，中台读取该库，备货系统读取中台聚合快照。首次历史核验覆盖至少 730 天，并延伸到已有订单或旧全量任务的更早起点；完成后每 5 分钟同步更新，每日复查近 90 天。备货系统通过 SHEIN `/sales-sync/status` 检查回补进度和更新时间；首次回补未完成、同步失败、进度超过 30 分钟或查询早于已核验范围时暂停相关自动建议。报表查询不会实时请求平台全量订单。

Temu 销量读取独立的全状态订单快照，使用平台实际下单时间，取消单与取消明细不计销量。同步按更新时间续传，保留一小时重叠窗口，每日复查近 90 天；分页失败不推进进度并自动重试。更早历史优先使用已有订单详情，确实缺少平台时间时标记首次采集日期。数据源页显示订单同步进度及最近全量复查时间；首次回补未完成、同步失败或进度超过 30 分钟时暂停相应备货建议。类目/颜色/规格初始识别可人工确认，未知类目保留在「待分类」，不隐藏销量。缺少库存映射的 SKU 不假定为零库存。

二期已预留 `region/warehouse` 维度和 NY/ATL/HOU/LA 映射配置结构；州范围尚待确认，本期不自动分仓/调拨。

## 部署与测试

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
python3 app.py
```

默认监听 `127.0.0.1:18110`，SQLite `data/restock.sqlite3`。生产服务 `pangu-restock-manager.service`，数据目录 700、数据库 600。备份使用 SQLite backup API；在线直接复制主文件会漏掉 WAL 写入。

部署包含 `app.py`、`reporting.py`、`excel_export.py`、`config/category-presets.json` 和 `web/`；旧库会自动补齐类目图标及规则字段，保留原类目名称、产品资料和计划。配置变更通过页面维护，初始化文件不会覆盖现有配置。

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

前端图表依赖本地打包的 Apache ECharts 6.0.0（`web/vendor/echarts.min.js`），附 LICENSE/NOTICE；生产不从 CDN 拉取脚本。样式使用统一的灰白/蓝色变量，筛选控件 36px 等高；构成图表、统计汇总、分类管理、规则、抽屉及弹窗共用基础组件。

### 销售与仓库需求口径

Temu 销售 SKU 使用原始 `ext_code`；Shein 的 1:1 平台 SKU 别名使用现有标准商品映射，非 1:1 映射保留平台销售身份。采购建议、库存覆盖与自动店铺分配使用仓库换算需求。页面详情和 Excel「仓库需求明细」逐项记录来源 SKU、店铺、订单数量、换算系数及换算需求。既有锁定计划保留原始数量、分配权重和计算依据。旧版本快照等待同步版本 3，期间暂停自动建议。

### 订单 Excel 验收

`scripts/verify_order_excel.py` 只读比对平台导出的订单 Excel 与中台明细 CSV，按「订单号 + 销售 SKU」合并重复行，生成汇总、逐单核验和差异明细。Excel 不作为同步数据源。校验脚本依赖 `openpyxl`，不属于生产服务依赖。

```sh
python scripts/verify_order_excel.py --excel orders.xlsx --orders-csv erp.csv \
  --start 2026-09-12 --end 2026-10-11 --output verification.xlsx
```

CSV 必需字段见脚本说明；`statistic_time` 必须先转换为业务时区。实时取消或退款可能使系统数量与较早导出的 Excel 不同，差异必须核实，不能按 Excel 覆盖平台状态。

## FIFO 商品成本与核算配置

入口 `?view=cost-fifo`、`?view=cost-accounting`。采购和成本主数据由备货系统管理；财务读取成本与毛利计算结果。

- 核算起始日、完整期初确认、期初数量及采购 / 头程 / 物流原币金额分别维护。缺失金额不作为零；期初变更撤销确认。
- 采购单支持多次、分仓、部分入库，累计数量不超过采购量。采购版本变化须重新核对。三项成本按采购单完整数量累计分配，保留十位小数差额守恒。
- 费用类型归属和分摊规则可配置：申报金额（默认）、数量、采购金额、人工权重。完整性须逐采购单确认（含已核对的零费用）；缺少分类、金额、申报依据或确认时保留待核对。平台面单费在财务收支中计入，避免与采购台账重复。
- 汇率支持 CNY / USD / EUR / HKD，使用业务日或此前最近生效报价，反向报价取倒数，不取未来报价；可选原入库日期或出库日期，记录报价版本和凭证。
- FIFO 口径可选订单需求测算或实际出库。按仓库 + SKU 重放全部店铺、平台后再筛选；同日先入库、再出库、最后退款退回。同日退回于次日出库可用；不得用未来入库补过去缺口。
- 实际退款商品行按店铺业务键去重，关联原出库及版本，累计数量不得超过原出库。可记录未退回、仅采购回冲或全部商品成本回冲；回冲使用原出库账面金额及汇率，保留明细。
- 出库 / 退款数据覆盖按店铺、日期及凭证分别确认，零记录也须确认。仓储源缺少时区时不推定会计日期，不自动确认账期完整。
- CSV 原子导入、业务键去重、版本冲突及变更历史适用于期初、入库和核算配置；错误整批回滚。报表 CSV 包含所有筛选行、三项成本、分摊、汇率和输入 SHA256。

API：`GET /api/costs/fifo/inputs`、`PUT /api/costs/fifo/settings`、`PUT /api/costs/fifo/records`、`POST /api/costs/fifo/records/remove`、`POST /api/costs/fifo/import`；报告 `GET /api/costs/fifo`、`/export.csv`。配置 `GET /api/costs/accounting`、`PUT /api/costs/accounting/policy`、`PUT /api/costs/accounting/{rate|rule|terms|movement|refund|coverage}`、`POST /api/costs/accounting/remove`、`/import`。写操作要求版本、JSON 和 `X-Pangu-Request: restock`，检查 Origin。

仓储接入：`GET /api/costs/accounting/warehouse?start=...&end=...&kind=receipt|movement` 经财务读取只读 XLWMS 源；每次最多 5,000 流水，超限须缩小日期。显示原始时间、源覆盖、采购 / 订单候选；仅允许唯一匹配导入，实际数量来自源流水。入库时区未确认时导入无日期草稿；出库须先确认时区，夏令时歧义拒绝自动转换。`POST /api/costs/accounting/warehouse/import` 需要所选业务键及最新源 SHA256，原子导入，保留不可变源证据；重试跳过同内容，冲突不覆盖。

毛利引擎 `GET /api/costs/margin?start=...&end=...&as_of=...&shop_key=...`，导出 `/api/costs/margin/export.csv`。按订单日期选定商品队列，平台款项累计到到账截止日，实际成本仅扣一次。三项成本、退款件数、财务账单或税费覆盖未知时毛利保持空；已知部分及缺口继续可查。财务负责补录和不可变核算快照，备货不写原财务账单。

历史 50 张采购单保留原金额；“海运费用RMB”仍是 CNY。现有 XLWMS 流水仅覆盖 2026-07-04 至 2026-08-03，不能推定后续无入库或出库。生产未确认核算起始日、完整期初及源时间时区，不能生成完整历史毛利。功能已支持业务补齐，未建立会计期间锁定或总账过账。
