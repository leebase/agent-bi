SELECT
  DATE_TRUNC('day', TRY_TO_DATE("usage_day")) AS "time_bucket",
  COUNT(DISTINCT "warehouse_name") AS "distinct_warehouse_name"
FROM "FIXTURE"."MAIN"."warehouse_metering_history"
WHERE 1=1
GROUP BY "time_bucket"
ORDER BY "time_bucket" ASC, "distinct_warehouse_name" DESC
LIMIT 1000