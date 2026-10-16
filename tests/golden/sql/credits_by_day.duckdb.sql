SELECT
  DATE_TRUNC('day', CAST("usage_day" AS DATE)) AS "time_bucket",
  SUM("credits_used") AS "total_credits_used"
FROM "fixture"."main"."warehouse_metering_history"
WHERE 1=1
GROUP BY "time_bucket"
ORDER BY "time_bucket" ASC, "total_credits_used" DESC
LIMIT 1000