-- agent-bi: generated from semantic/idle_warehouse_waste.yaml — do not edit; edit the semantic model and re-run compile-rill
SELECT
  CAST("usage_day" AS DATE) AS "usage_day",
  "warehouse_name",
  "credits_used_compute",
  "credits_used_cloud_services",
  "credits_used"
FROM sqlite_scan('../tests/fixtures/idle-warehouse-waste.sqlite', 'warehouse_metering_history')
