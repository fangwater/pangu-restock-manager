# 采购与物流成本归属

成本台账及模板由备货系统维护，财务系统通过只读接口读取。旧 SHEIN PNL 库存单数据保留为移交前档案，旧页面入口跳转到备货系统，旧库存成本 API 停止对外提供写入。

入口：`/pangu-restock/?view=costs`、`/pangu-restock/?view=cost-templates`。

## 数据与维护

新增 SQLite 表：`cost_tickets`、`cost_templates`、`cost_history`、`cost_migrations`。库存单及模板包含各自的 SKU/费用明细和分类/费用类型，金额使用十进制字符串存储及 Decimal 计算。历史费用保存自己的币种，已有记录的费用类型禁止删除或修改币种/计算方式。库存单及未使用模板支持软删除，修改采用版本号保护并记入审计表。

迁移保留原库存单、SKU 行和费用记录 ID、单号、金额、备注及原创建/更新时间。完整源导出和 SHA256 同时保存在 `cost_migrations`。首次移交以事务执行；相同导出再次执行不重复导入，不同导出禁止覆盖移交后修改。

原采购行未注明币种，原库存单没有成本生效日期：迁移不推断这两项。它们可以在新页面维护。单号年月和创建/更新时间不能替代成本生效日期。原来未填写的费用继续为空，保持与明确录入零金额的区别。

## 接口

- `GET /api/costs`：当前台账、模板、分币种汇总及迁移信息；不含各单完整明细。
- `GET /api/costs/tickets/{id}`：单据明细及最近修改记录。
- `GET /api/costs/export`：版本 1 完整成本快照，`owner=pangu-restock-manager`，供财务读取。
- `PUT /api/costs/tickets`、`PUT /api/costs/templates`：新增或修改，需传 `version`，新建为 0。
- `POST /api/costs/tickets/remove`、`POST /api/costs/templates/remove`：按 ID 与版本号软删除。
- 所有写入沿用备货系统的同源及 `X-Pangu-Request: restock` 校验。
- 财务增加 `GET /api/profit/costs`，通过 `RESTOCK_API_BASE_URL`（默认本机 18110）读取完整成本快照；上游失败返回不可用，不返回虚构的空成本。

当前接入提供成本原始台账。销售订单成本分摊、库存批次出库匹配及跨币种汇率仍需独立处理，不能直接用采购总额抵减某月销售回款。

## 迁移操作与回退

1. 用 SQLite backup API 备份当前备货库，保存原代码和财务二进制。
2. 使用 `scripts/export-legacy-costs.sql` 只读导出原数据库，先在备货库副本中验证迁移及原有数据。
3. 通过 `deploy/legacy-cost-transfer.conf` 停止旧库存成本 API 并跳转旧入口，再读取最终源导出。
4. 使用 `python3 transfer_costs.py --input <导出 JSON> --database <备货 SQLite>` 完成一次移交，核对所有原始记录及分币种金额。
5. 发布备货维护页面及财务只读接入，验证旧入口跳转、备货数据和财务快照一致。

回退时先暂停新成本写入并导出移交后的修改，再恢复备货代码/数据库、财务二进制和旧 Nginx 配置。不要在已产生新修改后直接恢复旧成本写入，避免两个来源分别积累数据。
