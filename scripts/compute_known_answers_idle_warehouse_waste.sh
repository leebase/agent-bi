#!/usr/bin/env bash
# Sprint 0 task 6: the eight known answers on idle-warehouse-waste.sqlite (SQL exactly as listed in sprint-plan.md).
set -euo pipefail
cd "$(dirname "$0")/.."
DB=tests/fixtures/idle-warehouse-waste.sqlite
OUT=tests/fixtures/known-answers/idle_warehouse_waste
BY="sprint-0 implementer"
T=warehouse_metering_history
ka() { python3 scripts/compute_known_answer.py --sqlite "$DB" --name "$1" --by "$BY" --out "$OUT/$1.json" --sql "$2"; }
ka total_credits_all "SELECT SUM(credits_used) AS total_credits_used FROM $T"
ka credits_by_warehouse "SELECT warehouse_name, SUM(credits_used) AS total_credits_used FROM $T GROUP BY warehouse_name ORDER BY total_credits_used DESC, warehouse_name ASC"
ka credits_by_day "SELECT date(usage_day) AS time_bucket, SUM(credits_used) AS total_credits_used FROM $T GROUP BY date(usage_day) ORDER BY time_bucket ASC"
ka row_count_all "SELECT COUNT(*) AS row_count FROM $T"
ka avg_compute_by_warehouse "SELECT warehouse_name, AVG(credits_used_compute) AS avg_credits_used_compute FROM $T GROUP BY warehouse_name ORDER BY avg_credits_used_compute DESC, warehouse_name ASC"
ka compute_and_cloud_by_warehouse "SELECT warehouse_name, SUM(credits_used_cloud_services) AS total_credits_used_cloud_services, SUM(credits_used_compute) AS total_credits_used_compute FROM $T GROUP BY warehouse_name ORDER BY total_credits_used_cloud_services DESC, total_credits_used_compute DESC, warehouse_name ASC"
ka averages_by_day "SELECT date(usage_day) AS time_bucket, AVG(credits_used) AS avg_credits_used, AVG(credits_used_cloud_services) AS avg_credits_used_cloud_services FROM $T GROUP BY date(usage_day) ORDER BY time_bucket ASC"
ka distinct_warehouses "SELECT COUNT(DISTINCT warehouse_name) AS distinct_warehouse_name FROM $T"
