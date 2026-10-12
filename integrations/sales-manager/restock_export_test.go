package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

func TestRestockSnapshotQuantityStatusShopsAndPrivacy(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("isolated TEST_DATABASE_URL required")
	}
	ctx := context.Background()
	store, err := openStore(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	// Dedicated isolated database, unique shop keys, no production fallback.
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	sku := "restock-test-" + suffix
	_, err = store.db.Exec(`INSERT INTO canonical_skus(warehouse_sku,product_name) VALUES($1,'Test Hanger')`, sku)
	if err != nil {
		t.Fatal(err)
	}
	defer store.db.Exec(`DELETE FROM normalized_orders WHERE source_order_no LIKE $1`, "restock-"+suffix+"-%")
	loc, _ := time.LoadLocation("Asia/Shanghai")
	today := time.Now().In(loc)
	day := time.Date(today.Year(), today.Month(), today.Day(), 0, 0, 0, 0, loc)
	type sample struct {
		shop, status, raw string
		eligible          bool
		at                time.Time
		units, warehouse  int
	}
	samples := []sample{
		{"panda-homes", "pending_shipping", `{"parentOrderMap":{"parentShippingTime":1234567890},"customer":"private"}`, true, day, 3, 6},
		{"panda-buy", "pending_shipping", `{"parentOrderMap":{"parentShippingTime":"invalid"}}`, true, day.Add(time.Hour), 2, 2},
		{"future-" + suffix, "delivered", `{}`, true, day.Add(2 * time.Hour), 4, 4},
		{"panda-homes", "refunded", `{}`, true, day, 100, 100},
		{"panda-homes", "cancelled", `{}`, true, day, 100, 100},
		{"panda-homes", "shipped", `{}`, false, day, 100, 100},
		{"panda-homes", "shipped", `{}`, true, day.Add(-time.Second), 100, 100},
		{"panda-homes", "shipped", `{}`, true, day.AddDate(0, 0, 1), 100, 100},
	}
	for i, x := range samples {
		var id int64
		err = store.db.QueryRow(`INSERT INTO normalized_orders(platform,shop_key,source_order_no,normalized_status,occurred_at,occurred_at_source,sales_eligible,raw_payload) VALUES('temu',$1,$2,$3,$4,'first_seen',$5,$6) RETURNING id`, x.shop, fmt.Sprintf("restock-%s-%d", suffix, i), x.status, x.at, x.eligible, x.raw).Scan(&id)
		if err != nil {
			t.Fatal(err)
		}
		_, err = store.db.Exec(`INSERT INTO normalized_order_lines(order_id,source_line_key,platform_sku,warehouse_sku,quantity,warehouse_quantity,conversion_factor,variant_name,warehouse_code) VALUES($1,'line','platform-private',$2,$3,$4,$5,'Black / 20pack','NY')`, id, sku, x.units, x.warehouse, float64(x.warehouse)/float64(x.units))
		if err != nil {
			t.Fatal(err)
		}
	}
	server := &APIServer{store: store, timezone: "Asia/Shanghai", logger: slog.Default()}
	response := httptest.NewRecorder()
	server.restockSnapshot(response, httptest.NewRequest("GET", "/api/restock/snapshot?days=1", nil))
	if response.Code != 200 {
		t.Fatalf("response %d: %s", response.Code, response.Body.String())
	}
	var envelope struct {
		Success bool
		Data    struct {
			Version int
			Points  []struct {
				SKU, Shop, Status, Warehouse, Region string
				Units                                float64
				WarehouseUnits                       float64 `json:"warehouse_units"`
			}
			Shops []struct{ Key string }
		}
	}
	if err = json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	var units, warehouses float64
	seen := map[string]string{}
	for _, p := range envelope.Data.Points {
		if p.SKU == sku {
			units += p.Units
			warehouses += p.WarehouseUnits
			seen[p.Shop] = p.Status
			if p.Warehouse != "NY" || p.Region != "" {
				t.Fatalf("phase2 dimensions %+v", p)
			}
		}
	}
	if envelope.Data.Version != 2 || units != 9 || warehouses != 12 {
		t.Fatalf("version=%d units=%v warehouse=%v", envelope.Data.Version, units, warehouses)
	}
	if seen["home"] != "shipped" || seen["buy"] != "pending" || seen["temu.future-"+suffix] != "completed" {
		t.Fatalf("states: %+v", seen)
	}
	var raw map[string]any
	json.Unmarshal(response.Body.Bytes(), &raw)
	for _, p := range raw["data"].(map[string]any)["points"].([]any) {
		v := p.(map[string]any)
		if _, ok := v["source_order_no"]; ok {
			t.Fatal("order identity leaked")
		}
		if _, ok := v["raw_payload"]; ok {
			t.Fatal("customer data leaked")
		}
	}
	bad := httptest.NewRecorder()
	server.restockSnapshot(bad, httptest.NewRequest("GET", "/api/restock/snapshot?days=1461", nil))
	if bad.Code != 400 {
		t.Fatalf("invalid days %d", bad.Code)
	}
}

func TestRestockSnapshotSeparatesSoldPackFromWarehouseDemand(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("isolated TEST_DATABASE_URL required")
	}
	store, err := openStore(context.Background(), dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	suffix := fmt.Sprintf("%d", time.Now().UnixNano())
	small, large := "restock-50-"+suffix, "restock-100-"+suffix
	defer store.db.Exec(`DELETE FROM normalized_orders WHERE source_order_no LIKE $1`, "packs-"+suffix+"-%")
	for _, sku := range []string{small, large} {
		if _, err = store.db.Exec(`INSERT INTO canonical_skus(warehouse_sku,product_name) VALUES($1,'Pink Hanger')`, sku); err != nil {
			t.Fatal(err)
		}
	}
	samples := []struct {
		platform, shop, sold, warehouse string
		quantity, factor                int
	}{
		{"temu", "panda-homes", small, small, 561, 1},
		{"temu", "panda-homes", large, small, 64, 2},
		{"temu", "panda-buy", large, large, 10, 1},
		{"shein", "beauty-hangers-home", "opaque-alias-" + suffix, small, 5, 1},
	}
	for i, x := range samples {
		var id int64
		err = store.db.QueryRow(`INSERT INTO normalized_orders(platform,shop_key,source_order_no,normalized_status,occurred_at,occurred_at_source,sales_eligible) VALUES($1,$2,$3,'shipped',now(),'platform_order_time',true) RETURNING id`, x.platform, x.shop, fmt.Sprintf("packs-%s-%d", suffix, i)).Scan(&id)
		if err != nil {
			t.Fatal(err)
		}
		_, err = store.db.Exec(`INSERT INTO normalized_order_lines(order_id,source_line_key,platform_sku,warehouse_sku,quantity,warehouse_quantity,conversion_factor) VALUES($1,'line',$2,$3,$4,$5,$6)`, id, x.sold, x.warehouse, x.quantity, x.quantity*x.factor, x.factor)
		if err != nil {
			t.Fatal(err)
		}
	}
	server := &APIServer{store: store, timezone: "Asia/Shanghai", logger: slog.Default()}
	response := httptest.NewRecorder()
	server.restockSnapshot(response, httptest.NewRequest("GET", "/api/restock/snapshot?days=1&version=3", nil))
	if response.Code != 200 {
		t.Fatalf("response %d: %s", response.Code, response.Body.String())
	}
	var result struct {
		Data struct {
			Version int
			Points  []struct {
				SKU, Shop      string
				SalesSKU       string `json:"sales_sku"`
				Units          float64
				WarehouseUnits float64 `json:"warehouse_units"`
				Factor         float64 `json:"conversion_factor"`
			}
		}
	}
	if err = json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	sold, demand := map[string]float64{}, map[string]float64{}
	count := 0
	for _, p := range result.Data.Points {
		if p.SKU != small && p.SKU != large {
			continue
		}
		count++
		if p.Units*p.Factor != p.WarehouseUnits {
			t.Fatalf("invalid conversion %+v", p)
		}
		sold[p.Shop+":"+p.SalesSKU] += p.Units
		demand[p.Shop+":"+p.SKU] += p.WarehouseUnits
	}
	if result.Data.Version != 3 || count != 4 || sold["home:"+small] != 561 || sold["home:"+large] != 64 || demand["home:"+small] != 689 || demand["home:"+large] != 0 || sold["shein:"+small] != 5 || sold["buy:"+large] != 10 {
		t.Fatalf("version=%d points=%d sales=%v demand=%v", result.Data.Version, count, sold, demand)
	}
	invalid := httptest.NewRecorder()
	server.restockSnapshot(invalid, httptest.NewRequest("GET", "/api/restock/snapshot?version=4", nil))
	if invalid.Code != 400 {
		t.Fatalf("invalid version accepted: %d", invalid.Code)
	}
}

func TestRestockSnapshotReportsSyncFailureWithoutExposingRawError(t *testing.T) {
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("isolated TEST_DATABASE_URL required")
	}
	ctx := context.Background()
	store, err := openStore(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	id, err := store.beginSync(ctx, "incremental")
	if err != nil {
		t.Fatal(err)
	}
	defer store.db.Exec(`DELETE FROM sync_runs WHERE id=$1`, id)
	if err = store.finishSync(ctx, id, "failed", SyncCounts{}, fmt.Errorf("private diagnostic")); err != nil {
		t.Fatal(err)
	}
	response := httptest.NewRecorder()
	server := &APIServer{store: store, timezone: "Asia/Shanghai", logger: slog.Default()}
	server.restockSnapshot(response, httptest.NewRequest("GET", "/api/restock/snapshot?days=1", nil))
	var envelope struct {
		Data struct {
			Sync struct {
				Status string `json:"status"`
			} `json:"sync"`
		} `json:"data"`
	}
	if err = json.Unmarshal(response.Body.Bytes(), &envelope); err != nil {
		t.Fatal(err)
	}
	if response.Code != 200 || envelope.Data.Sync.Status != "failed" {
		t.Fatalf("sync health missing: %s", response.Body.String())
	}
	var decoded map[string]any
	json.Unmarshal(response.Body.Bytes(), &decoded)
	encoded, _ := json.Marshal(decoded["data"].(map[string]any)["sync"])
	if string(encoded) == "null" {
		t.Fatal("missing sync state")
	}
	for _, b := range []string{"private diagnostic", "error_message"} {
		if bytes.Contains(encoded, []byte(b)) {
			t.Fatal("private error leaked")
		}
	}
}
