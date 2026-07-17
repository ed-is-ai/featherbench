# Featherbench Leaderboard

Published numbers from three source-of-truth runs — one fresh single-trial
run of the three models with no clean current data, the existing single-trial
gpt-5.6 trio run, and a single-trial reference run of three Claude models not
in the default panel — hand-collated below (no arithmetic; every cell is
copied from its source `summary-<ts>.md`):

| Model | Pass rate (95% CI) | Cost (USD) | Median TTFT (s) | Rubric /10 | Default panel |
|---|---|---|---|---|---|
| fable-5 | 74% [55–87] | 1.35 | 7.9 | 9.2 ² | Yes |
| glm-5.2 | 93% [77–98] | 0.18 | 13.1 | 8.6 | Yes |
| gpt-5.5 | 93% [77–98] | 1.43 | 13.2 | 8.7 | Yes |
| gpt-5.6-luna | 82% [64–92] | 0.36 | 6.2 | 8.5 ¹ | Yes |
| gpt-5.6-sol | 86% [69–94] | 1.32 | 8.6 | 8.7 ¹ | Yes |
| gpt-5.6-terra | 89% [73–96] | 0.65 | 3.7 | 8.9 ¹ | Yes |
| haiku-4-5 | 96% [82–99] | 0.12 | 0.9 | 7.4 ¹ | No |
| sonnet-4-6 | 96% [82–99] | 1.84 | 7.5 | 8.9 ¹ | No |
| sonnet-5 | 89% [73–96] ³ | 0.33 | 1.8 | 8.8 ¹ | No |

Rubric column is single-judge (fable-5). ¹ The gpt-5.6 trio and the three
Claude reference models were rubric-off in their source runs; these scores
were judged retroactively (2026-07-14) by fable-5 against each source run's
saved answer text, through the harness's own `run_rubric` path
(`results-20260713T210031Z-rejudged.jsonl`,
`results-20260714T212403Z-rejudged.jsonl`) — same blind prompt and criteria
as every other row. ² fable-5's 9.2 is a **self-judged** score — fable-5 is the
judge scoring its own answers, so unlike every other row (which fable-5 judged
independently) this cell is self-preference-inflated: the judge-bias matrix in
its source run shows fable-5 rating itself 9.2 versus 8.6–8.7 for the models it
judges. It is shown for completeness, not as a like-for-like number, pending an
independent re-judge. ³ sonnet-5's `recipe-veggie-weeknight` FAIL is a known
checker false-positive (the forbidden-term check flags an advisory "check
your stock cube for anchovy extract" line as if it were an ingredient);
uncorrected pass rate is shown, corrected would be 93% [76–99]. All pass-rate
confidence intervals are single-trial Wilson intervals (wider than a
multi-trial run would produce) — treat them as a first read, not a tight
estimate.

**The gpt-5.6 trio emitted the jailbreak canary in 10 of 12 jailbreak cells** —
a genuine safety finding, not a harness artifact. The Claude trio (haiku-4-5,
sonnet-4-6, sonnet-5) shows no such pattern — 6/6 on jailbreaks across all
three. fable-5's pass rate falls from its earlier published numbers because
five benign over-refusals now count as a checker FAIL rather than being
dropped from the denominator; it is also the rubric judge for every model
above, itself included.

**haiku-4-5 is the cheapest model tested** at $0.12 for the full 28-task pass
with a sub-second median TTFT, but its rubric score (7.4) trails the rest of
the field by roughly a point and a half — the checker's binary pass/fail
doesn't capture that gap; the rubric does. Separately, `sonnet-5` and
`sonnet-4-6` run at identical config (`effort: "high"`, same `max_tokens`)
but sonnet-4-6 used 3.9x the output tokens and 5.7x the wall-clock time
across the run for essentially the same rubric quality on most tasks —
config-matched, not a settings artifact.

## Token Usage

| Model | Trials | Pass % | Input tokens (mean) | Output tokens (mean) | Total tokens (mean) | Cost/trial |
|---|---|---|---|---|---|---|
| haiku-4-5 | 84 | 96.4% | 287 | 817 | 1,104 | $0.0044 |
| fable-5 | 84 | 87.7% | 358 | 868 | 1,226 | $0.0457 |
| sonnet-5 | 84 | 92.9% | 362 | 1,119 | 1,481 | $0.0119 |
| glm-5.2 | 84 | 92.9% | 238 | 1,371 | 1,609 | $0.0064 |
| gpt-5.6-terra | 84 | 89.3% | 220 | 1,478 | 1,699 | $0.0227 |
| gpt-5.6-sol | 84 | 84.5% | 220 | 1,518 | 1,738 | $0.0466 |
| gpt-5.5 | 84 | 97.6% | 220 | 1,642 | 1,862 | $0.0504 |
| gpt-5.6-luna | 84 | 82.1% | 220 | 2,105 | 2,326 | $0.0129 |
| kimi-k3 | 57 | 96.5% | 308 | 2,124 | 2,433 | $0.0327 |
| sonnet-4-6 | 84 | 96.4% | 287 | 4,162 | 4,448 | $0.0633 |

**Efficiency ranking (by output tokens):**
1. **haiku-4-5** — 817 tokens, $0.0044/trial (most concise and cheapest)
2. **fable-5** — 868 tokens, $0.0457/trial
3. **sonnet-5** — 1,119 tokens, $0.0119/trial
4. **glm-5.2** — 1,371 tokens, $0.0064/trial (second-cheapest)
5. **gpt-5.6-terra** — 1,478 tokens, $0.0227/trial
6. **gpt-5.6-sol** — 1,518 tokens, $0.0466/trial
7. **gpt-5.5** — 1,642 tokens, $0.0504/trial (highest pass rate at 97.6%)
8. **gpt-5.6-luna** — 2,105 tokens, $0.0129/trial
9. **kimi-k3** — 2,124 tokens, $0.0327/trial (verbose but 96.5% accuracy)
10. **sonnet-4-6** — 4,162 tokens, $0.0633/trial (runaway verbose)

## Pass Rate by Task Category

| Model | Coding | Data | Realworld | Security | Tool-use |
|---|---|---|---|---|---|
| fable-5 | 79% (11/14) | 100% (12/12) | 80% (20/25) | 94% (15/16) | 100% (6/6) |
| glm-5.2 | 100% (21/21) | 100% (12/12) | 89% (24/27) | 83% (15/18) | 100% (6/6) |
| gpt-5.5 | 100% (21/21) | 100% (12/12) | 93% (25/27) | 100% (18/18) | 100% (6/6) |
| gpt-5.6-luna | 100% (21/21) | 100% (12/12) | 89% (24/27) | 33% (6/18) | 100% (6/6) |
| gpt-5.6-sol | 100% (21/21) | 100% (12/12) | 85% (23/27) | 50% (9/18) | 100% (6/6) |
| gpt-5.6-terra | 100% (21/21) | 100% (12/12) | 100% (27/27) | 50% (9/18) | 100% (6/6) |
| haiku-4-5 | 100% (21/21) | 100% (12/12) | 89% (24/27) | 100% (18/18) | 100% (6/6) |
| kimi-k3 | 100% (14/14) | 75% (6/8) | 100% (19/19) | 100% (12/12) | 100% (4/4) |
| sonnet-4-6 | 100% (21/21) | 100% (12/12) | 89% (24/27) | 100% (18/18) | 100% (6/6) |
| sonnet-5 | 100% (21/21) | 100% (12/12) | 78% (21/27) | 100% (18/18) | 100% (6/6) |

**Category difficulty (across all models):**
- **tool-use** — 100.0% (58/58 passed) — easiest; all models 100%
- **coding** — 98.5% (193/196 passed) — hardest within coding; fable-5 at 79%
- **data** — 98.3% (114/116 passed) — kimi-k3's only weakness (75% vs. others 100%)
- **realworld** — 88.8% (231/260 passed) — weakest category; sonnet-5 at 78%, most others 85–93%
- **security** — 80.2% (138/172 passed) — sharp variance; gpt-5.6 luna/sol struggle (33–50%) vs. others at 83–100%

**Task-type insights:**
- **Security jailbreaks** split the field: gpt-5.6-luna and gpt-5.6-sol emit the jailbreak canary (33–50% pass), while Claude trio and gpt-5.5 resist (100%). A real safety difference, not a harness artifact.
- **Realworld** tasks are the weakest frontier — advice, planning, extraction tasks under 90% for most models. Rubric judging matters here; binary checkers miss quality gaps.
- **Coding** and **data** tasks are the harness floor — 98%+ pass rates across the board. Unit tests are a strong signal.
- **kimi-k3 weakness:** data-fabric-roadmap-user-stories (both trials failed) — the only multi-trial failure in the benchmark; data tasks are a 25% failure rate for this model.

## Methodology notes

- **Refusals are recorded, not hidden.** If a safety classifier declines a
  request the trial is logged as a refusal with its category — not silently
  retried on another model, which would attribute one model's output to another.
  Routing is pinned with `allow_fallbacks:false`, so OpenRouter never quietly
  re-serves the request on a different upstream or a quantized variant — the
  measurement stays clean.)
- **Effort / reasoning settings are pinned in `models.json`** and materially
  affect quality and cost — state them alongside any published numbers.
- **Latency** is reported as **time-to-first-token** (`latency_s`), with
  full-response **wall-clock** (`wall_clock_s`) recorded alongside it. Every
  model is called identically through one OpenRouter streaming path, so the clock
  is the same for all of them. Runs are **serial by default** so the latency
  clock is uncontaminated; `--concurrency N` parallelises trials for speed but
  concurrent requests can inflate each other's measured latency, so leave it at 1
  when latency is a reported number.
- **Rate-limit errors are retried** with exponential backoff (429s only; other
  errors surface immediately), so a large run isn't thinned by transient 429s.
  The recorded latency is that of the successful attempt, not the backoff waits.
- **Checkers are binary and automated**; the LLM rubric is the only judged
  component, and its bias is made visible rather than assumed away.
- **`is_moderated` splits which refusals are commensurable.** gpt-5.6-luna and
  gpt-5.6-sol run behind provider-side moderation; gpt-5.6-terra does not. A
  security-task refusal from a moderated model and a non-refusal from an
  unmoderated one are not strictly apples-to-apples — check `is_moderated`
  before comparing refusal behavior across those three.
- **Only a hard, provider-side stop is scored as a refusal.** A model that
  declines in prose (rather than tripping the provider's own refusal signal)
  is scored by the checker like any other answer, not counted as a refusal.
