---
name: publish-model-run
description: >-
  Run a model through eval.py and publish the result: verify the run, correct
  cost to list rate, consolidate into results/summary.json, and add the model to
  ed-o-meter.md. Use when adding a new model to the panel or re-running one.
---

# Publish a model run

End-to-end procedure for taking a model from `models.json` entry to a published
row on the leaderboard. Every step below was walked in anger for `glm-5.3-flash`
(PRs #61, #63) and `gpt-5.6-luna` (`c962bd6`); the gotchas are real.

Do the work on a branch off an **up-to-date** `main` (`git fetch origin` first —
local `main` here is often stale). Commit each phase separately. Open a PR at the
end.

---

## 1. Catalog entry (`models.json`)

If the model is new, add a key. Before committing the route, verify it against
OpenRouter:

```bash
curl -s "https://openrouter.ai/api/v1/models/<slug>/endpoints" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | python3 -m json.tool
```

- `provider_order` must be an **exact, no-fallback route** from a real endpoint
  (`allow_fallbacks:false` + `require_parameters:true` are enforced by the
  harness). Label FP8/quantized routes as such.
- Only add `effort` if the endpoint lists `reasoning_effort` in
  `supported_parameters` **and** you want it. Match siblings: the GLM line, for
  example, runs `glm-5.3-flash` at `effort: high` but deliberately leaves
  `glm-5.3` with **no effort key** — at `effort: high` the non-flash model trips
  Z.AI's content filter (`stop_reason: "sensitive"`, empty response) on security
  tasks. Test a probe call before assuming an effort level is safe.
- `sampling` only carries params the provider supports (sending an unsupported
  one 404s the trial).

Validate and commit:

```bash
python -c "import json; json.load(open('models.json')); print('valid')"
```

## 2. Run

```bash
export OPENROUTER_API_KEY=<key>
nohup python eval.py --models <key> --trials <N> > /tmp/run.log 2>&1 &
```

- **Launch from the top-level session, not a subagent** — a returning subagent's
  background process tree gets reaped. Check liveness with `lsof`, not
  `ps | grep`.
- Reasoning models with no effort cap can take 15-20 min/task (streaming up to
  `max_tokens`). A 28-task rubric-on run is 30 min to a few hours. Be patient;
  buffered stdout makes it look stalled.
- Default `--trials 1`. Use `--trials 3` when you want a tighter Wilson interval
  (see phase 6 for how the board still stays single-trial).
- `eval.py` writes `results/results-<ts>.jsonl` + `summary-<ts>.md` +
  `report-<ts>.html`. Rubric judging (fable-5 by default) runs automatically on
  tasks that define a rubric.

## 3. Verify the run

```bash
python3 - <<'EOF'
import json
F = 'results/results-<ts>.jsonl'
r = [json.loads(l) for l in open(F) if l.strip()]
print('records', len(r), '(expect tasks x trials)')
print('errors', sum(1 for x in r if x.get('error')))
print('passed', sum(1 for x in r if x.get('passed') is True), '/',
      sum(1 for x in r if x.get('passed') is not None))
for x in r:
    if x.get('stop_reason') == 'sensitive' or (x.get('passed') is not True and not (x.get('text') or '')):
        print('  BLOCKED/EMPTY:', x['task'], x['trial'], x.get('stop_reason'))
EOF
```

- `stop_reason: "sensitive"` (or empty text + 0 tokens + ~1s wall) is a
  **provider content-filter block**, not a model answer. `map_refusal` does
  **not** currently treat it as a hard refusal, so it scores as an answer-fail.
  If a config change caused it (e.g. turning on `effort`), reconsider the config
  rather than publishing the regression.
- 0 errors expected. If there are transient errors, `--rerun-errored` the file.

## 4. Cost correction to list rate

The benchmark's Cost column must be **list price**, never a temporary
promo/discount (project rule: `feedback_cost-without-temporary-promos`). OpenRouter
returns the *billed* amount in `usage.cost`, which reflects any active promo and
prompt-cache discounts.

Check the endpoint pricing and compare per-record:

```bash
python3 - <<'EOF'
import json
IN, OUT = 0.15/1e6, 0.50/1e6           # <-- LIST rate for this model, per token
r = [json.loads(l) for l in open('results/results-<ts>.jsonl') if l.strip()]
for x in r[:5]:
    it, ot, c = x['input_tokens'], x['output_tokens'], x['cost_usd']
    print(f"charged {c:.7f}  list {it*IN+ot*OUT:.7f}  ratio {c/(it*IN+ot*OUT):.3f}")
EOF
```

- Ratio ~1.0 → billed at list, no correction needed.
- Ratio ~0.5 across the board → a 50% promo is active. **Recompute** each
  record's `cost_usd` in place from token counts at the list rate.
- Ratio varying below 0.5 (multi-trial runs) → promo **plus** prompt-cache
  discounts on repeated prompts. Recompute from tokens at list — it is the
  comparable "what a run today costs" figure and the artifact of running it
  N times drops out.

```bash
cp results/results-<ts>.jsonl results/results-<ts>.jsonl.billed-bak
python3 - <<'EOF'
import json
IN, OUT = 0.15/1e6, 0.50/1e6
F = 'results/results-<ts>.jsonl'
r = [json.loads(l) for l in open(F) if l.strip()]
for x in r:
    if x['model'] == '<key>' and x.get('input_tokens') is not None:
        x['cost_usd'] = round(x['input_tokens']*IN + x['output_tokens']*OUT, 12)
open(F, 'w').write('\n'.join(json.dumps(x) for x in r) + '\n')
EOF
```

Note the correction in the `consolidate.py` comment and the `ed-o-meter.md`
footnote (see phases 5 and 7). `results/summary.json` will then carry the
list-rate value; the `.billed-bak` keeps the billed one for audit.

## 5. Consolidate into `results/summary.json`

`results/consolidate.py` is the only path — it re-scores every answer against the
current checkers and is byte-stable apart from `generated_utc`.

Add one line to `SOURCE_RUNS` with a provenance comment (effort config, trial
count, cost basis, what it supersedes):

```python
# <key>, <N> trials x 28 tasks, rubric-on (fable-5), effort:<x>. cost_usd
# recomputed from tokens at list rate ($X / $Y per 1M) — usage.cost was on
# the <promo> promo. Supersedes <old file> per rule 1.
("results-<ts>.jsonl", "<YYYY-MM-DD>"),
```

**Gotcha:** `consolidate.py` dedupes *within* a file but **not across files**. If
the model already appears in another `SOURCE_RUNS` file (e.g. a multi-model panel
run), you must either strip its rows from that file or you get duplicate records.

```bash
cp results/summary.json /tmp/summary.pre.json
python3 results/consolidate.py
python3 - <<'EOF'
import json
o = {(r['model'],r['task'],r['trial'],r.get('source_file')): r
     for r in json.load(open('/tmp/summary.pre.json'))['records']}
n = {(r['model'],r['task'],r['trial'],r.get('source_file')): r
     for r in json.load(open('results/summary.json'))['records']}
print('added', len([k for k in n if k not in o]),
      'removed', len([k for k in o if k not in n]),
      'changed', len([k for k in o if k in n and o[k] != n[k]]))
EOF
```

Expect: only the new model's records added, **0 existing records changed**.

If you corrected cost after `eval.py` already wrote the per-run
`summary-<ts>.md` / `report-<ts>.html`, regenerate them:

```bash
python3 - <<'EOF'
import eval, json
from pathlib import Path
r = [json.loads(l) for l in open('results/results-<ts>.jsonl') if l.strip()]
tasks = {t['id']: t for t in eval.select_tasks(None, None)}
eval.write_summary(r, tasks, Path('results/summary-<ts>.md'))
eval.write_html_report(r, tasks, Path('results/report-<ts>.html'))
EOF
```

## 6. Update `ed-o-meter.md`

The board is **strictly single-trial** ("No column mixes trial counts"). For a
multi-trial run, publish **only trial 1** on the board; the full aggregate lives
in `summary.json`.

Compute the trial-1 (or single-trial) slice — pass rate + Wilson CI, cost total,
median TTFT, rubric mean (denominator is **13 not 14** if the
`security-jailbreak-aim-machiavelli` judge hole is present), per-category, and
input/output token means + cost/trial for the efficiency table.

Add the row to **all four tables**, placed by each table's sort order:

1. **Pass rate** — by pass rate desc
2. **Quality (rubric)** — by rubric desc
3. **Efficiency (cost per task)** — by cost/trial asc; renumber the ranked list below it
4. **Pass rate by task category** — same order as table 1

Add a **footnote** (next number) covering: single-trial slicing + where the full
run lives, `effort` config and any sibling divergence, and the list-rate cost
basis (mirror footnote 13's wording).

Also touch:
- intro line — bump the source-run count and name the new run
- "Rubric judging notes" — add the model to the independently-judged list
- "Methodology notes" routing-pins bullet — add `<key> -> <route>`
- "Task-type insights" bullets — where the new model clears/misses a category

## 7. Regenerate `results/summary.md` (optional)

`results/summary.md` is a gitignored mechanical rollup of *all* models from
`summary.json` (tables only, no footnotes). Regenerate it with a small script
that reads `summary.json`, computes per-model pass/rubric/TTFT/cost + a
per-category table, sorts by pass rate, and writes markdown. It is **not
committed** (`.gitignore` allows only `summary.json` and `consolidate.py` under
`results/`).

## 8. Commit & PR

- Branch off fresh `origin/main` (never reuse an already-merged branch).
- One commit per phase: catalog entry, consolidation, ed-o-meter.
- `results/results-*.jsonl` are gitignored — only `models.json`,
  `results/consolidate.py`, `results/summary.json`, `ed-o-meter.md` land in the PR.
- Keep the raw run file locally: `consolidate.py` needs it to reproduce
  `summary.json`.
- PR body: run config, headline numbers, the consolidation delta (X added, 0
  changed), and the cost basis.
