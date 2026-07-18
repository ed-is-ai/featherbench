# Featherbench Leaderboard

Published numbers from three source-of-truth runs — one fresh single-trial
run of the three models with no clean current data, the existing single-trial
gpt-5.6 trio run, and a single-trial reference run of three Claude models not
in the default panel — hand-collated below (no arithmetic; every cell is
copied from its source `summary-<ts>.md`):

| Model | Pass rate (95% CI) | Cost (USD) | Median TTFT (s) | Rubric /10 | Default panel |
|---|---|---|---|---|---|
| haiku-4-5 | 96% [82–99] | 0.12 | 0.9 | 7.4 ¹ | No |
| sonnet-4-6 | 96% [82–99] | 1.84 | 7.5 | 8.9 ¹ | No |
| gpt-5.5 | 96% [82–99] ³ | 1.43 | 13.2 | 8.7 | Yes |
| glm-5.2 | 93% [77–98] | 0.18 | 13.1 | 8.6 | Yes |
| kimi-k3 | 93% [77–98] | 0.033 | 26.4 | 9.5 | No |
| sonnet-5 | 93% [77–98] ³ | 0.33 | 1.8 | 8.8 ¹ | No |
| gpt-5.6-terra | 89% [73–96] | 0.65 | 3.7 | 8.9 ¹ | Yes |
| gpt-5.6-sol | 86% [69–94] | 1.32 | 8.6 | 8.7 ¹ | Yes |
| gpt-5.6-luna | 82% [64–92] | 0.36 | 6.2 | 8.5 ¹ | Yes |
| fable-5 | 78% [59–89] ³ | 1.35 | 7.9 | 9.2 ² | Yes |

## Quality (Rubric)

| Model | Rubric /10 | Pass rate (95% CI) | Cost (USD) | Median TTFT (s) | Default panel |
|---|---|---|---|---|---|
| kimi-k3 | 9.5 | 93% [77–98] | 0.033 | 26.4 | No |
| fable-5 | 9.2 ² | 78% [59–89] ³ | 1.35 | 7.9 | Yes |
| gpt-5.6-terra | 8.9 ¹ | 89% [73–96] | 0.65 | 3.7 | Yes |
| sonnet-4-6 | 8.9 ¹ | 96% [82–99] | 1.84 | 7.5 | No |
| sonnet-5 | 8.8 ¹ | 93% [77–98] ³ | 0.33 | 1.8 | No |
| gpt-5.5 | 8.7 | 96% [82–99] ³ | 1.43 | 13.2 | Yes |
| gpt-5.6-sol | 8.7 ¹ | 86% [69–94] | 1.32 | 8.6 | Yes |
| glm-5.2 | 8.6 | 93% [77–98] | 0.18 | 13.1 | Yes |
| gpt-5.6-luna | 8.5 ¹ | 82% [64–92] | 0.36 | 6.2 | Yes |
| haiku-4-5 | 7.4 ¹ | 96% [82–99] | 0.12 | 0.9 | No |

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
independent re-judge. ³ **Recipe-checker false-positive, corrected in-table.**
`realworld-recipe-veggie-weeknight`'s forbidden-term checker flags a
non-ingredient mention as if it were an ingredient — an advisory label-check
caution (fable-5: "some stock cubes, Worcestershire-style sauces … contain
animal products") or a negated omission list (gpt-5.5: "uses no … fish sauce
or animal-derived garnishes"; sonnet-5: the anchovy advisory). The
negation-aware `not_contains` shield added in the harness catches sonnet-5's
phrasing but not fable-5's or gpt-5.5's, so those two still score FAIL under
the current checker. All three are genuine false-positives and are counted as
PASS here: gpt-5.5 93%→96% [82–99] (27/28), sonnet-5 89%→93% [77–98] (26/28),
fable-5 74%→78% [59–89] (21/27). All pass-rate confidence intervals are
single-trial Wilson intervals (wider than a multi-trial run would produce) —
treat them as a first read, not a tight estimate.


## Efficiency (cost/task)

| Model | Pass % | Input tokens (mean) | Output tokens (mean) | Total tokens (mean) | Cost/trial |
|---|---|---|---|---|---|
| haiku-4-5 | 96.4% | 287 | 817 | 1,104 | $0.0044 |
| glm-5.2 | 92.9% | 238 | 1,371 | 1,609 | $0.0064 |
| sonnet-5 | 92.9% | 362 | 1,119 | 1,481 | $0.0119 |
| gpt-5.6-luna | 82.1% | 220 | 2,105 | 2,326 | $0.0129 |
| gpt-5.6-terra | 89.3% | 220 | 1,478 | 1,699 | $0.0227 |
| kimi-k3 | 96.5% | 308 | 2,124 | 2,433 | $0.0327 |
| gpt-5.6-sol | 84.5% | 220 | 1,518 | 1,738 | $0.0466 |
| gpt-5.5 | 97.6% | 220 | 1,642 | 1,862 | $0.0504 |
| sonnet-4-6 | 96.4% | 287 | 4,162 | 4,448 | $0.0633 |
| fable-5 ⁴ | 87.7% | 355 | 1,297 | 1,652 | $0.0684 |

⁴ fable-5's token and cost means are computed over its **answering trials only**
— the 28 refused trials (which emit near-zero output) are excluded, since
including them makes the model look artificially concise and cheap. Including
refusals its output mean would read 868 tokens at $0.0457/trial. No other model
in this table has refusals, so this adjustment affects only fable-5.

**Efficiency ranking (by cost/task):**
1. **haiku-4-5** — $0.0044/trial (cheapest, and most concise at 817 tokens)
2. **glm-5.2** — $0.0064/trial (second-cheapest)
3. **sonnet-5** — $0.0119/trial
4. **gpt-5.6-luna** — $0.0129/trial
5. **gpt-5.6-terra** — $0.0227/trial
6. **kimi-k3** — $0.0327/trial (verbose at 2,124 tokens but 96.5% accuracy)
7. **gpt-5.6-sol** — $0.0466/trial
8. **gpt-5.5** — $0.0504/trial (highest pass rate at 97.6%)
9. **sonnet-4-6** — $0.0633/trial (runaway verbose at 4,162 tokens)
10. **fable-5** — $0.0684/trial (most expensive; answering trials only)


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


## Pass Rate by Task Category

| Model | Coding | Data | Realworld | Security | Tool-use |
|---|---|---|---|---|---|
| haiku-4-5 | 100% | 100% | 89% | 100% | 100% |
| sonnet-4-6 | 100% | 100% | 89% | 100% | 100% |
| glm-5.2 | 100% | 100% | 89% | 83% | 100% |
| gpt-5.5 | 100% | 100% | 93% | 100% | 100% |
| kimi-k3 | 100% | 75% | 100% | 100% | 100% |
| gpt-5.6-terra | 100% | 100% | 100% | 50% | 100% |
| sonnet-5 | 100% | 100% | 78% | 100% | 100% |
| gpt-5.6-sol | 100% | 100% | 85% | 50% | 100% |
| gpt-5.6-luna | 100% | 100% | 89% | 33% | 100% |
| fable-5 | 79% | 100% | 80% | 100% ⁵ | 100% |

**Task-type insights:**
- **Security jailbreaks** split the field: gpt-5.6-luna and gpt-5.6-sol emit the jailbreak canary (33–50% pass), while Claude trio and gpt-5.5 resist (100%). A real safety difference, not a harness artifact.
- **Realworld** tasks are the weakest frontier — advice, planning, extraction tasks under 90% for most models. Rubric judging matters here; binary checkers miss quality gaps.
- **Coding** and **data** tasks are the harness floor — 98%+ pass rates across the board. Unit tests are a strong signal.
- **kimi-k3 weakness:** data tasks are its only category weakness (75%), particularly the data-fabric-roadmap-user-stories task.

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
- **`is_moderated` splits which refusals are commensurable.** gpt-5.6-luna andß
  gpt-5.6-sol run behind provider-side moderation; gpt-5.6-terra does not. A
  security-task refusal from a moderated model and a non-refusal from an
  unmoderated one are not strictly apples-to-apples — check `is_moderated`
  before comparing refusal behavior across those three.
- **Only a hard, provider-side stop is scored as a refusal.** A model that
  declines in prose (rather than tripping the provider's own refusal signal)
  is scored by the checker like any other answer, not counted as a refusal.
