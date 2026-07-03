# Model eval harness — GLM-5.2 vs GPT-5.5 vs Claude Fable 5

Runs the same tasks, with the same prompts and the same pass/fail checkers,
against all three models. Because everything goes through one harness, the
resulting numbers are directly comparable — unlike vendor-reported benchmark
scores produced on different scaffolds (the SWE-bench Pro caveat from the
blog cluster applies to published numbers, not to results from this harness).

## Setup

```sh
pip3 install anthropic openai

export ANTHROPIC_API_KEY=sk-ant-...   # or `ant auth login` — the SDK picks up the profile
export OPENAI_API_KEY=sk-...
export GLM_API_KEY=...
# GLM endpoint defaults to https://api.z.ai/api/paas/v4/ (Z.ai international).
# For mainland bigmodel.cn: export GLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

Check the model IDs in `models.json` against each provider's current catalog
before a real run (particularly `gpt-5.5` and `glm-5.2` — adjust to the exact
IDs your accounts expose). Pricing for cost columns is per million tokens in
`models.json`; Fable 5 is pre-filled ($10/$50), fill in the other two.

## ⚠️ Run this on the sandboxed machine

Tasks with a `python_tests` checker **execute model-generated code** with a
subprocess timeout but no other isolation. That is exactly the risk profile
of the microvm/sandbox blog post — run the harness on the isolated coding
machine, not on a machine with personal data or broad credentials.

## Usage

```sh
python3 eval.py --dry-run                  # see what would run
python3 eval.py                            # all tasks x all models, 1 trial
python3 eval.py --trials 3                 # 3 trials each (report variance, not single runs)
python3 eval.py --models fable-5,glm-5.2 --tasks csv-dedupe,rate-limiter
```

Outputs:

- `results/results.jsonl` — one record per trial: full response text, pass/fail
  with checker detail, latency, input/output tokens, cost, stop reason,
  refusals, errors. Appends across runs; `run_id` groups a single invocation.
- `results/summary.md` — aggregate table (pass rate, median latency, tokens,
  cost per model) plus a per-task grid. Regenerated from the full JSONL each
  run. Delete `results/results.jsonl` to start a fresh dataset.

## Adding tasks

One JSON file per task in `tasks/`:

```json
{
  "id": "my-task",
  "prompt": "…ask for final code in a single Python code block…",
  "checker": { ... }
}
```

Checker types:

| type | fields | passes when |
|---|---|---|
| `python_tests` | `test_code`, `timeout_s` | the last code block in the response is saved as `solution.py` and `test_code` (which imports it) exits 0 |
| `regex` | `pattern`, optional `label` | pattern matches the response (add `(?i)` for case-insensitive) |
| `contains` | `value` or `values` | all strings appear in the response (case-insensitive) |
| `not_contains` | `value` or `values` | none of the strings appear (case-insensitive) — for constraint violations |
| `all` | `checks` (list of sub-checkers) | every sub-check passes; failure detail names each miss |
| *(omitted)* | — | recorded but unscored (for qualitative tasks) |

Seven sample tasks are included, in two groups:

- **Coding** (`csv-dedupe`, `rate-limiter`, `log-parse`) — deterministic
  `python_tests` checkers; pass means the code ran and met the spec.
- **Real-life** (`recipe-veggie-weeknight`, `holiday-plan-lisbon`,
  `flight-search-honesty`, `crying-baby`) — composite `all` checkers that
  enforce an *objectively checkable minimum bar*: constraint compliance
  (a vegetarian recipe with no meat hiding in the stock), structure
  (five day-headings, a budget total), honesty (disclosing that live flight
  prices aren't accessible instead of fabricating bookable flights), and
  safety content (an 11pm crying-baby answer must mention checking for
  fever, name a red-flag symptom, and give an escalation route — soothing
  tips alone fail). These bars are floors, not full quality judgments —
  read the preserved response text in `results.jsonl` for the qualitative
  comparison, which is where the interesting differences usually are.

The coding tasks are warm-up calibration tasks — for the blog post, replace
them with the real engineering tasks, and run `--trials 3` or more so the
write-up can report variance rather than single-shot results.

## Methodology notes (for the write-up)

- **Fable 5 refusals are recorded, not hidden.** Production Fable code should
  opt into server-side fallbacks (a refusal gets transparently re-served by
  Opus 4.8), but the harness deliberately omits them — a fallback would score
  Opus output as Fable. A refusal shows up in the results as its own outcome
  with the classifier category.
- **Effort/reasoning settings are pinned in `models.json`** (Fable
  `effort: high`, GPT-5.5 `reasoning_effort: high`) — state them in the post,
  since they materially affect both quality and cost.
- **Latency** is wall-clock for the full response (all models are called
  identically, Fable via streaming to avoid HTTP timeouts on long turns).
- Checkers are binary and automated; no human judging in the loop.
