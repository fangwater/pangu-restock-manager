package main

import (
	"database/sql"
	"net/http"
	"strconv"
	"time"
)

const restockShopSQL = `CASE WHEN o.platform='temu' AND o.shop_key='panda-homes' THEN 'home'
 WHEN o.platform='temu' AND o.shop_key='panda-buy' THEN 'buy'
 WHEN o.platform='shein' AND o.shop_key='beauty-hangers-home' THEN 'shein'
 ELSE o.platform||'.'||o.shop_key END`

const restockStatusSQL = `CASE WHEN lower(o.normalized_status) IN ('delivered','completed') THEN 'completed'
 WHEN lower(o.normalized_status)='shipped' THEN 'shipped'
 WHEN o.platform='temu' THEN CASE
 WHEN COALESCE(o.raw_payload->'parentOrderMap'->>'parentShippingTime','') ~ '^[0-9]+$'
 THEN CASE WHEN (o.raw_payload->'parentOrderMap'->>'parentShippingTime')::numeric>0 THEN 'shipped' ELSE 'pending' END
 ELSE 'pending' END ELSE 'pending' END`

// Aggregate ERP data only. No order identifiers or customer addresses leave this endpoint.
// Order quantity is the share denominator; warehouse quantity is a separate procurement unit.
func (s *APIServer) restockSnapshot(w http.ResponseWriter, r *http.Request) {
	version := 2 // Existing consumers keep their original contract until upgraded.
	salesSKU := `l.warehouse_sku`
	if value := r.URL.Query().Get("version"); value != "" && value != "2" {
		if value != "3" {
			writeJSON(w, http.StatusBadRequest, apiResponse{Success: false, Error: "version must be 2 or 3"})
			return
		}
		version = 3
		// Temu ext_code identifies the sold pack independently of fulfillment mapping.
		// Shein's opaque skuCode aliases use the existing 1:1 canonical identity;
		// a non-1:1 mapping must retain its source identity instead of merging packs.
		salesSKU = `CASE WHEN o.platform='temu' OR l.conversion_factor<>1
		 THEN COALESCE(NULLIF(BTRIM(l.platform_sku),''),l.warehouse_sku)
		 ELSE l.warehouse_sku END`
	}
	days := 730
	if value := r.URL.Query().Get("days"); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil || parsed < 1 || parsed > 1460 {
			writeJSON(w, http.StatusBadRequest, apiResponse{Success: false, Error: "days must be between 1 and 1460"})
			return
		}
		days = parsed
	}
	loc, err := time.LoadLocation(s.timezone)
	if err != nil {
		s.internalError(w, "restock timezone", err)
		return
	}
	now := time.Now().In(loc)
	end := time.Date(now.Year(), now.Month(), now.Day()+1, 0, 0, 0, 0, loc)
	start := end.AddDate(0, 0, -days)
	tx, err := s.store.db.BeginTx(r.Context(), &sql.TxOptions{ReadOnly: true, Isolation: sql.LevelRepeatableRead})
	if err != nil {
		s.internalError(w, "restock snapshot", err)
		return
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(r.Context(), `
 SELECT (o.occurred_at AT TIME ZONE $1)::date::text, `+restockShopSQL+`,
 l.warehouse_sku,MAX(COALESCE(NULLIF(l.product_name,''),c.product_name,'')),MAX(l.variant_name),
 `+restockStatusSQL+`,COALESCE(NULLIF(l.warehouse_code,''),o.warehouse_code,''),
 SUM(l.quantity)::float8,SUM(l.warehouse_quantity)::float8,
 `+salesSKU+`,l.conversion_factor::float8
 FROM normalized_orders o JOIN normalized_order_lines l ON l.order_id=o.id
 LEFT JOIN canonical_skus c ON c.warehouse_sku=l.warehouse_sku
 WHERE o.occurred_at >= $2 AND o.occurred_at < $3 AND o.sales_eligible
 AND lower(o.normalized_status) NOT IN ('cancelled','canceled','refunded','refund','cancel')
 GROUP BY 1,2,3,6,7,10,11 ORDER BY 1,2,3,6,7,10,11`, s.timezone, start, end)
	if err != nil {
		s.internalError(w, "restock sales", err)
		return
	}
	type point struct {
		Date             string  `json:"date"`
		Shop             string  `json:"shop"`
		SKU              string  `json:"sku"`
		Name             string  `json:"name"`
		Variant          string  `json:"variant"`
		Status           string  `json:"status"`
		Warehouse        string  `json:"warehouse"`
		Region           string  `json:"region"`
		Units            float64 `json:"units"`
		WarehouseUnits   float64 `json:"warehouse_units"`
		SalesSKU         string  `json:"sales_sku,omitempty"`
		ConversionFactor float64 `json:"conversion_factor,omitempty"`
	}
	points := []point{}
	for rows.Next() {
		var p point
		if err = rows.Scan(&p.Date, &p.Shop, &p.SKU, &p.Name, &p.Variant, &p.Status, &p.Warehouse, &p.Units, &p.WarehouseUnits, &p.SalesSKU, &p.ConversionFactor); err != nil {
			break
		}
		if version == 2 {
			p.SalesSKU, p.ConversionFactor = "", 0
		}
		points = append(points, p)
	}
	if err == nil {
		err = rows.Err()
	}
	rows.Close()
	if err != nil {
		s.internalError(w, "restock sales rows", err)
		return
	}
	rows, err = tx.QueryContext(r.Context(), `SELECT o.platform,o.shop_key,`+restockShopSQL+`,MAX(o.synced_at),MAX(o.source_updated_at),
 COUNT(*) FILTER(WHERE o.occurred_at_source='first_seen'),COUNT(*),
 COUNT(*) FILTER(WHERE NOT EXISTS(SELECT 1 FROM normalized_order_lines l WHERE l.order_id=o.id))
 FROM normalized_orders o WHERE o.occurred_at >= $1 AND o.occurred_at < $2 GROUP BY 1,2,3 ORDER BY 1,2`, start, end)
	if err != nil {
		s.internalError(w, "restock sources", err)
		return
	}
	sources := []map[string]any{}
	shops := []map[string]any{}
	labels := map[string]string{"home": "Panda Homes", "buy": "Panda Buy", "shein": "Shein"}
	for rows.Next() {
		var platform, shop, key string
		var synced, updated sql.NullTime
		var fallback, total, missing int64
		if err = rows.Scan(&platform, &shop, &key, &synced, &updated, &fallback, &total, &missing); err != nil {
			break
		}
		name := labels[key]
		if name == "" {
			name = platform + " / " + shop
		}
		shops = append(shops, map[string]any{"key": key, "name": name, "platform": platform, "shop_key": shop})
		sources = append(sources, map[string]any{"platform": platform, "shop_key": shop, "key": key, "synced_at": synced.Time, "source_updated_at": updated.Time, "first_seen_orders": fallback, "orders": total, "orders_without_lines": missing})
	}
	if err == nil {
		err = rows.Err()
	}
	rows.Close()
	if err != nil {
		s.internalError(w, "restock source rows", err)
		return
	}
	var inferred, unmapped int
	err = tx.QueryRowContext(r.Context(), `SELECT COUNT(*) FILTER(WHERE mapping_status='inferred'),COUNT(*) FILTER(WHERE mapping_status='unmapped') FROM sku_mappings`).Scan(&inferred, &unmapped)
	if err != nil {
		s.internalError(w, "restock mapping quality", err)
		return
	}
	var syncState map[string]any
	var syncStatus string
	var syncStarted time.Time
	var syncCompleted, syncSucceeded sql.NullTime
	err = tx.QueryRowContext(r.Context(), `SELECT status,started_at,completed_at,
 (SELECT MAX(completed_at) FROM sync_runs WHERE status='succeeded')
 FROM sync_runs ORDER BY id DESC LIMIT 1`).Scan(&syncStatus, &syncStarted, &syncCompleted, &syncSucceeded)
	if err != nil && err != sql.ErrNoRows {
		s.internalError(w, "restock sync status", err)
		return
	}
	if err == nil {
		syncState = map[string]any{"status": syncStatus, "started_at": syncStarted, "completed_at": syncCompleted.Time, "last_success_at": syncSucceeded.Time}
	}
	if err = tx.Commit(); err != nil {
		s.internalError(w, "restock commit", err)
		return
	}
	writeJSON(w, http.StatusOK, apiResponse{Success: true, Data: map[string]any{"version": version, "unit_basis": "order_quantity", "points": points, "sources": sources, "shops": shops,
		"start": start.Format("2006-01-02"), "end": end.AddDate(0, 0, -1).Format("2006-01-02"), "timezone": s.timezone, "generated_at": now, "inferred_mappings": inferred, "unmapped_mappings": unmapped, "sync": syncState}})
}
