SELECT
  COUNT(*) AS "row_count"
FROM "FIXTURE"."MAIN"."warehouse_metering_history"
WHERE 1=1
ORDER BY "row_count" DESC
LIMIT 1000