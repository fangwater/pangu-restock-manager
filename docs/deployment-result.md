# 部署与验证

2026-10-09 已上线：https://pangutech.online/pangu-restock/

- HK 项目目录：`/home/fanghaizhou/pangu-restock-manager`
- 服务：`pangu-restock-manager.service`（开机自启），`127.0.0.1:18110`
- 本地数据库：`data/restock.sqlite3`，计划及修改记录保存在 HK。
- HK Nginx `/pangu-restock/` → 本地服务；pangutech HTTPS `/pangu-restock/` → HK Nginx。
- HK 现有销售管理增加只读 `/api/restock/snapshot`；已在部署前检查其源码与测试构建一致，并保留原二进制。
- 新项目不在 pangutech 主机运行；该主机只新增 Nginx 转发。

首次生产快照近 30 天范围为 2026-09-10 至 2026-10-09（北京时间，含今天）：HOME 5,824 件、BUY 6,501 件、希音 739 件，合计 13,064 件；41 个有销量 SKU。该快照仅记录验证时的数据，线上随接口同步更新。

验证：

- 11 项 Python 测试通过：日期边界、三店汇总、整数分配总量守恒、零销量比例校验、并发版本冲突、批量事务回滚、计划持久化、修改记录、失败同步保留快照、过期数据暂停建议、在途库存规则。
- 销售管理 `go test ./...` 通过，构建成功。
- 隔离浏览器测试通过：切换天数/店铺、手动比例、保存与重载、批量创建、规则设置、数据连接和移动端。
- 生产 HTTPS 页面、健康接口、近 30 天 API、CSV 均返回 200；五个数据连接状态正常。
- 生产浏览器无页面错误或失败请求；检查真实 SKU 抽屉和五个来源卡。
- 390px 移动端无页面横向溢出；宽表在其容器中滚动。
- 生产错误请求验证返回预期 400/403/404；没有创建测试备货计划。
- 两端 Nginx 配置校验通过；systemd 服务 active/running，仅监听 HK 回环地址。

生产截图：`overview.png`、`sku-plan.png`、`mobile.png`。

2026-10-11 复核：页面、健康接口、近 30 天销量及库存同步正常，11 项 Python 测试通过。修复了 HOME/BUY 数据连接卡的店铺选择：通过 Temu manager 要求的 `X-Temu-Shop` 请求头分别读取源同步状态。销量汇总接口本身已按店铺拆分，此修复仅涉及连接卡元数据。

数据限制：TEMU 沿用销售管理的首次采集时间，历史回补可能影响日归属；推断 SKU 映射会在页面标示。销量使用现有标准订单库的状态和仓库换算系数。源数据缺明细、库存/销量快照过期和接口失败均显示在页面。

## 2026-10-11 一期升级

新版已部署至同一香港目录和 HTTPS 入口，systemd 服务 active/running。中台沿用现有 PM2 的单实例，二进制 SHA256 `bf9cdf8a30a9da11a67cc53271d9db1cdde18d863ee836cf85ca016028d86e06`；接口改动已 commit/push 至 `fangwater/pangu-sales-manager` main，提交 `13fa609`。

增加自定义日期/7/30/90 天/季度、多店铺及状态筛选、类目总览下钻、件数占比、可维护产品资料、类目/店铺规则、SKU 箱规、20% 安全缓冲、计划锁定和未锁定草稿重算、异步 Excel 报表及采购建议单。沿用团队入口，预留分仓模型。

本次将订单件数与仓库换算量分开：占比统计 `quantity`；采购需求统计 `warehouse_quantity`。此前初版销量使用仓库换算量，数值不能直接与新版订单件数比较。2026-09-12 至 2026-10-11 的一次生产验证快照为 HOME 5,415、BUY 6,767、Shein 782，合计 12,964 件；衣架 12,894、脏衣篓 55、待分类 15。数据持续同步，这些数值仅表示验证时刻。

生产数据库升级前通过 SQLite backup API 备份至 `/home/fanghaizhou/pangu-restock-manager/backups/v2-20261011/restock.sqlite3`，同目录保留旧代码和前端；在副本中验证迁移不改变已有计划。本次升级时计划数为 0，生产验证未写入测试计划。中台旧代码/二进制备份在 `/home/fanghaizhou/restock-v2-test/sales-backup-20261011/`。若需整体回退，应同时回退中台快照版本和备货代码/数据库，防止数量口径混用。

HK Nginx 仅在备货 location 中补充 gzip，配置校验通过并平滑 reload；90 天 JSON 报表从约 210 KB 压缩至约 16 KB，公网实测约 1.2 秒；本地 30/90/366 天查询约 0.07–0.08 秒。原配置备份为 `/home/fanghaizhou/restock-v2-test/nginx-restock.before-gzip`。

详细验收见 `acceptance-v2.md`。截图 `v2-production-overview.png`、`v2-production-plan.png`、`v2-production-mobile.png`；实际采购建议 Excel `v2-production-purchase.xlsx`。

最后视觉复核：长商品名与 SKU 在真实 table cell 内上下排列，SKU 列保留 230px 可读宽度；桌面及 390px 移动端复核通过。

## 2026-10-11 分类配置与筛选调整

已上线 SKU 分类管理和紧凑筛选栏；引导性副文案移除，并在工作区与项目 AGENTS.md 中记录长期 ERP 文案规范。衣架、脏衣篓为一次性初始化配置，名称、图标、排序及匹配规则由页面维护，支持逐项/批量确认归类。

23 项 Python 测试在本地及香港 Python 3.10 环境通过。隔离浏览器验证配置 CRUD、批量分类、自动匹配与人工确认保护、计划锁定及 Excel 兼容性；公网页面复核筛选、60 个 SKU 分页、规则编辑、搜索及 390px 移动端，无 JavaScript 错误。五个数据连接均正常，服务 active，生产未写入测试分类或计划。

升级前备份：`/home/fanghaizhou/pangu-restock-manager/backups/classification-20261011-100929/`。在数据库副本中校验计划、SKU 资料、类目原字段、备货规则和设置均保留，生产数据库完整性检查通过。项目已配置 `git@github.com:fangwater/pangu-restock-manager.git` 的 main 分支；运行数据、环境文件、备份和业务导出不入库。
