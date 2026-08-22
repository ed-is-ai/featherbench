---
type: Benchmark Run
title: 2026-08-22 replacement-model panel
description: Rubric-on 28-task run for GLM-5.3, Gemini-3.7 Flash, Grok 4.6, and DeepSeek V4 Pro.
resource: ../../results/summary.json
tags: [benchmarking, llm, replacement-panel, 2026-08-22]
status: stable
sources:
  - id: source-run
    resource: ../../results/results-20260822T172041Z.jsonl
    title: Replacement-model source run
    author: featherbench
    last_modified: 2026-08-22
---

# 2026-08-22 replacement-model panel

The complete rubric-on run contains 28 tasks for each of four models, with no
provider errors. The consolidated store includes all 112 records.

| Model | Pass / 28 | Rubric tasks scored |
|---|---:|---:|
| GLM-5.3 | 28 | 13 |
| DeepSeek V4 Pro | 27 | 13 |
| Grok 4.6 | 27 | 11 |
| Gemini 3.7 Flash | 26 | 13 |

The replacement models supersede GLM-5.2, Gemini-3.6 Flash, and Grok 4.5 in the
default panel. See [consolidated results](../datasets/benchmark-results.md) and
the published [leaderboard](../../ed-o-meter.md).
