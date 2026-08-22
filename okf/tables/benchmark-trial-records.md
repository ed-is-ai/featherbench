---
type: JSON Table
title: Benchmark trial records
description: One model response and its scoring metadata for a task trial.
resource: ../../results/summary.json#/records
tags: [benchmarking, trials, scoring, provenance]
status: stable
---

# Schema

| Field | Type | Description |
|---|---|---|
| `run_id` | string | Identifier shared by all trials in one harness run. |
| `task` | string | Task identifier from `tasks/`. |
| `model` | string | Featherbench model handle from `models.json`. |
| `trial` | integer | One-based trial number. |
| `text` | string | Model response text. |
| `passed` | boolean/null | Final verdict after current-checker re-scoring and any active override. |
| `passed_recorded` | boolean/null | Verdict written by the original source run. |
| `check_detail` | string | Checker outcome or reason. |
| `latency_s` | number/null | Time to first content token; null for tool-only responses. |
| `wall_clock_s` | number/null | Full streamed-response duration. |
| `input_tokens` | integer/null | Provider-reported input token count. |
| `output_tokens` | integer/null | Provider-reported output token count. |
| `cost_usd` | number/null | Provider-reported answer cost in USD. |
| `rubric_mean` | number/null | Mean blind judge score for rubric tasks. |
| `source_file` | string | Source JSONL file used during consolidation. |
| `run_date` | date | Date assigned to the source run. |

## Relationships

Each record belongs to [consolidated benchmark results](../datasets/benchmark-results.md).
Its `passed` field feeds [pass rate](../metrics/pass-rate.md), while `latency_s`
feeds [median TTFT](../metrics/median-ttft.md).
