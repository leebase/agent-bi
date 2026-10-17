# Agent BI: the three-minute demo

The demo shows one thing: **Definition and Serving are different facts, and Agent BI reports
both honestly.** It uses published Agent BI (`github.com/leebase/agent-bi`, M2.x1 or later),
Rill v0.90.5, and the included `idle_warehouse_waste` example, which has nine verified
questions.

## Setup (before the audience arrives)

```
git clone https://github.com/leebase/agent-bi.git && cd agent-bi
python3 -m venv .venv && source .venv/bin/activate && python -m pip install -e .
```

In a second terminal, from the same directory:

```
rill start rill --no-open        # or ~/.rill/rill start rill --no-open
```

## The demo

**1. Verified, not governed.**

    agent-bi status --model idle_warehouse_waste

    Definition: 9 verified queries: 0 governed, 9 verified, 0 stale
    Serving:    No serving check recorded.

> "Software has independent evidence that these nine calculations are right. Nobody has said
> the business meaning is right, so nothing is governed. And nobody has checked what Rill is
> serving."

**2. A person accepts the meaning.** The presenter runs this themselves, under their own name.
Never script it for someone else.

    for p in 'tables[0].dimensions[warehouse_name]' 'tables[0].time_dimensions[usage_day]' \
      'tables[0].metrics[row_count]' 'tables[0].metrics[total_credits_used]' \
      'tables[0].metrics[avg_credits_used]' 'tables[0].metrics[total_credits_used_compute]' \
      'tables[0].metrics[avg_credits_used_compute]' 'tables[0].metrics[total_credits_used_cloud_services]' \
      'tables[0].metrics[avg_credits_used_cloud_services]' 'tables[0].metrics[distinct_warehouse_name]'; do
      agent-bi enrich --model idle_warehouse_waste --accept "$p" --by <your-name> || break
    done
    agent-bi status --model idle_warehouse_waste

    Definition: 9 verified queries: 9 governed, 0 verified, 0 stale
    Serving:    No serving check recorded.

> "Now the meaning is governed. Notice that Serving didn't change: accepting a definition
> proves nothing about what the dashboard is showing."

**3. Ask the real runtime.**

    agent-bi verify --model idle_warehouse_waste --engine rill
    agent-bi status --model idle_warehouse_waste

    Serving: During <time> UTC, through Rill Metrics SQL (CLI), as the local default user, ...
             9 of 9 verified queries returned their known answers under agent-bi-known-answer/1.
             Not covered: other read paths and caches, other users, rendering, any time outside the check.

> "Two independent facts. The meaning is governed, and when we asked Rill, it returned the
> known answers. Agent BI also says exactly what that check did not cover."

**4. Someone changes Rill behind Agent BI's back.** This is a human or an agent editing the
analytics implementation directly.

    sed -i 's/expression: SUM("credits_used")$/expression: SUM("credits_used") * 1.1/' rill/metrics/idle_warehouse_waste.yaml
    sleep 20
    agent-bi status --model idle_warehouse_waste

    Definition: 9 verified queries: 9 governed, 0 verified, 0 stale
    Serving:    During <time> UTC (20 s ago), ... 9 of 9 verified queries returned their known answers ...

> "Still governed: the business meaning didn't change. Serving still shows the last check, with
> its age. Agent BI won't claim anything it hasn't observed since."

**5. Check again.**

    agent-bi verify --model idle_warehouse_waste --engine rill
    agent-bi status --model idle_warehouse_waste

    Definition: 9 verified queries: 9 governed, 0 verified, 0 stale
    Serving:    During <time> UTC, ... 3 of 9 verified queries returned results that differ from their known answers ...
                credits_by_warehouse: row 1: expected ['FINANCE_REPORTING_WH', 2739.0], got ['FINANCE_REPORTING_WH', 3012.9]

> "A single AI trust score has to lie here. The definition is still governed. The system
> serving it is wrong. And the governed definition is exactly what lets us say Rill is wrong."

## Reset

    git checkout -- rill/metrics/idle_warehouse_waste.yaml semantic/idle_warehouse_waste.provenance.yaml
    rm -rf out/serving

Then stop the Rill terminal.

## Notes for the presenter

- Step 4's `sed` changes only the `total_credits_used` measure, so the three questions that use
  it differ and the other six still match.
- "As the local default user" is literal: Rill's local CLI runs with no per-user security.
- Don't claim anything beyond what the lines say. The receipts are dated observations of one
  route. They are not a live guarantee, and they say nothing about dashboards or caches.
