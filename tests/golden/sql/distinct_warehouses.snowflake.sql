SELECT
  COUNT(DISTINCT "warehouse_name") AS "distinct_warehouse_name"
FROM "FIXTURE"."MAIN"."warehouse_metering_history"
WHERE 1=1
ORDER BY "distinct_warehouse_name" DESC
LIMIT 1000