---
type: Metric
title: Pass rate
description: Fraction of scored trial records whose final verdict is PASS.
resource: ../../results/summary.json#/records
tags: [benchmarking, quality, pass-rate]
status: stable
---

# Definition

`pass rate = count(passed == true) / count(passed in {true, false})`

Refusals, provider errors, and checker-less trials are excluded from the
denominator. `passed` is the re-scored final verdict, not the original value in
`passed_recorded`.

## Interpretation

Published leaderboard rows use a 95% Wilson interval. A one-trial-per-task run
is useful for comparison, but its uncertainty interval is wide; use three or
more trials before treating a difference as stable.

See [trial records](../tables/benchmark-trial-records.md) and the
[consolidation policy](../policies/consolidation.md).
