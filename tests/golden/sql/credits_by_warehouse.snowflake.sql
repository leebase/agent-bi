SELECT
  "warehouse_name" AS "warehouse_name",
  SUM("credits_used") AS "total_credits_used"
FROM "FIXTURE"."MAIN"."warehouse_metering_history"
WHERE 1=1
GROUP BY "warehouse_name"
ORDER BY "total_credits_used" DESC, "warehouse_name" ASC
LIMIT 1000