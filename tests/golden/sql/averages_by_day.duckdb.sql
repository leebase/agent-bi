SELECT
  DATE_TRUNC('day', CAST("usage_day" AS DATE)) AS "time_bucket",
  AVG("credits_used") AS "avg_credits_used",
  AVG("credits_used_cloud_services") AS "avg_credits_used_cloud_services"
FROM "fixture"."main"."warehouse_metering_history"
WHERE 1=1
GROUP BY "time_bucket"
ORDER BY "time_bucket" ASC, "avg_credits_used" DESC, "avg_credits_used_cloud_services" DESC
LIMIT 1000