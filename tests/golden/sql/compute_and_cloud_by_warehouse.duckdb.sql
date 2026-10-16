SELECT
  "warehouse_name" AS "warehouse_name",
  SUM("credits_used_cloud_services") AS "total_credits_used_cloud_services",
  SUM("credits_used_compute") AS "total_credits_used_compute"
FROM "fixture"."main"."warehouse_metering_history"
WHERE 1=1
GROUP BY "warehouse_name"
ORDER BY "total_credits_used_cloud_services" DESC, "total_credits_used_compute" DESC, "warehouse_name" ASC
LIMIT 1000