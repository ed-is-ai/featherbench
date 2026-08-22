---
type: Metric
title: Median time to first token
description: Median elapsed seconds until the first non-empty content token across a model's valid trials.
resource: ../../results/summary.json#/records
tags: [benchmarking, latency, ttft]
status: stable
---

# Definition

`median_ttft = median(latency_s where latency_s is not null)`

Reasoning-only deltas do not start the clock: TTFT is recorded only when visible
content begins. Tool-only replies have no content-token TTFT and are excluded.

## Latest replacement-model readings

The latest serial no-rubric latency run reports: GLM-5.3 27.5 s,
DeepSeek V4 Pro 39.1 s, Grok 4.6 13.6 s, and Gemini 3.7 Flash 8.5 s.

See [trial records](../tables/benchmark-trial-records.md) and the
[2026-08-22 replacement-model run](../runs/replacement-panel-20260822.md).
