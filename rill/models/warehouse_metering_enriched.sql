-- Operator-authored Rill join model (Sprint 6′ M2 example, D432). Rill owns this model graph and
-- executes the join; agent-bi checks its form, hashes it with its upstream models and runs it verbatim.
SELECT
  CAST(m."usage_day" AS DATE) AS "usage_day",
  m."warehouse_name",
  w."size" AS "warehouse_size",
  w."cost_center",
  m."credits_used_compute",
  m."credits_used_cloud_services",
  m."credits_used"
FROM warehouse_metering_source AS m
LEFT JOIN warehouses_source AS w
  ON w."name" = m."warehouse_name"
