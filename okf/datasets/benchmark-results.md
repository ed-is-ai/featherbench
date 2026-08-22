---
type: Dataset
title: Consolidated Featherbench benchmark results
description: Valid model-task-trial records consolidated from published Featherbench source runs.
resource: ../../results/summary.json
tags: [benchmarking, llm, evaluation, results]
status: stable
generated:
  by: results/consolidate.py
  at: 2026-08-22T20:48:36Z
sources:
  - id: consolidated-results
    resource: ../../results/summary.json
    title: Featherbench consolidated benchmark results
    author: featherbench
    last_modified: 2026-08-22
---

# Consolidated Featherbench benchmark results

`results/summary.json` contains 644 valid trial records from eight source runs.
Each record is re-scored against the current task definition, while preserving the
original verdict in `passed_recorded` for auditability.

## Grain

One row represents one `(run_id, task, model, trial)` observation. A later
timestamp supersedes a duplicate observation with the same scoring key inside a
source file.

## Related concepts

- [Trial record schema](../tables/benchmark-trial-records.md)
- [Pass rate metric](../metrics/pass-rate.md)
- [Median TTFT metric](../metrics/median-ttft.md)
- [Consolidation policy](../policies/consolidation.md)
