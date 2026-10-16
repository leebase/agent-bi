SELECT
  SUM("credits_used") AS "total_credits_used"
FROM "fixture"."main"."warehouse_metering_history"
WHERE 1=1
ORDER BY "total_credits_used" DESC
LIMIT 1000