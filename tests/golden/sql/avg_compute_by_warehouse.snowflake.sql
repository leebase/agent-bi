SELECT
  "warehouse_name" AS "warehouse_name",
  AVG("credits_used_compute") AS "avg_credits_used_compute"
FROM "FIXTURE"."MAIN"."warehouse_metering_history"
WHERE 1=1
GROUP BY "warehouse_name"
ORDER BY "avg_credits_used_compute" DESC, "warehouse_name" ASC
LIMIT 1000