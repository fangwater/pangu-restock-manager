BEGIN READ ONLY;
SET LOCAL statement_timeout='30s';
SELECT jsonb_build_object(
  'templates', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_cost_templates t),
  'categories', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY sort_order,id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_cost_categories t),
  'types', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY sort_order,id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_cost_types t),
  'tickets', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_tickets t),
  'lines', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_ticket_lines t),
  'costs', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY id),'[]'::jsonb) FROM shein_beauty_hangers_home.inventory_cost_entries t)
);
COMMIT;
