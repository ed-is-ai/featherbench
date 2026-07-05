# Featherbench

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

**A featherweight framework for building your own LLM benchmarks.**

Featherbench is a single-file harness for creating **your own benchmarks** and
measuring LLM performance on **real-world workloads** — the messy,
domain-specific tasks you actually care about, not just the public leaderboards.

You write tasks as small JSON files. Every task runs through the same scaffold
against every model you select, with the same prompt and the same pass/fail
checker, so the numbers are **directly comparable** across models — unlike
vendor-reported benchmark scores produced on different harnesses. Every model
is reached through a single, routing-pinned [OpenRouter](https://openrouter.ai)
integration — one API, one key, comparable cost and latency for every model —
and results land as JSONL, a Markdown summary, and a self-contained HTML review
page.

**Design goals — why "featherweight":**

- **One file, no framework lock-in.** [`eval.py`](eval.py) is ~1,150 lines of
  plain Python with two dependencies (`openai` — used as the OpenRouter client —
  and `jinja2` for the HTML report); everything else is the standard library.
  Read it in one sitting; fork it without ceremony.
- **Tasks are data, not code.** A task is a JSON file. Non-engineers can author
  them; they diff cleanly in review.
- **Deterministic floor + optional judged quality.** Most real-world answers
  have an objective minimum bar you *can* check by machine (a constraint
  respected, a fact present, a dangerous action avoided) and a layer of quality
  you can't. The framework does both: automated checkers for the floor, an
  optional cross-judged LLM rubric for the rest.
- **Everything through one scaffold.** Same prompt, same effort settings, same
  latency clock, same cost math for every model — so comparisons are apples to
  apples.

If you want a heavyweight platform with a UI, tracing, and dataset versioning,
use Inspect AI / promptfoo / Braintrust. If you want to stand up a bespoke
benchmark for your domain in an afternoon and keep full control of the scaffold,
this is that.

## Setup

Requires Python 3.9+ (matching `requires-python` in `pyproject.toml`).

```sh
pip install .                 # installs the pinned deps from pyproject.toml (openai, jinja2)
# no-clone alternative:
# pip3 install openai jinja2

export OPENROUTER_API_KEY=sk-or-...   # one key for every model — get it at openrouter.ai/keys
# Optional: override the endpoint (defaults to https://openrouter.ai/api/v1)
# export OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
```

Every model — Anthropic, OpenAI, GLM, and anything else in the catalogue — is
reached through OpenRouter, so a single `OPENROUTER_API_KEY` covers the whole
run. No per-provider keys.

**Where the key lives.** It is read from the `OPENROUTER_API_KEY` environment
variable and nowhere else — no key is stored in the repo, in `models.json`, or
on disk. The client picks it up when a model is called
([`call_openrouter`](eval.py)). Two consequences worth knowing:

- **The `python_tests` checker never sees your key.** It runs model-generated
  code in a subprocess with a stripped environment (`PATH` only), so a task
  answer cannot read `OPENROUTER_API_KEY` and exfiltrate it. This is enforced in
  code, independent of any sandbox.
- **Keep it out of your interactive shell if you can.** `export` leaves a key
  visible to other processes running as you (`env`, `ps e`). To scope the key to
  a single run, prefix the command instead:
  `OPENROUTER_API_KEY=sk-or-... python3 eval.py`. Under nono, the key passes
  through from the launching shell by default — see [the sandbox section](#-run-this-on-a-sandboxed-machine).

### Models

[`models.json`](models.json) is a catalog of selectable models, each keyed by a
short handle (`fable-5`, `gpt-5.5`, `glm-5.2`, …) and carrying a flat
**OpenRouter slug** plus its per-request routing and sampling config. A typical
entry:

```json
"fable-5": {
  "enabled": true,
  "model": "anthropic/claude-fable-5",   // the OpenRouter slug
  "provider_order": ["anthropic"],        // routing pin: which upstream serves it
  "effort": "high",                        // reasoning effort (omit for non-reasoning tiers)
  "max_tokens": 64000
}
```

- **`model`** is the OpenRouter slug (`anthropic/claude-fable-5`,
  `openai/gpt-5.5`, `z-ai/glm-5.2`).
- **`provider_order`** pins routing to the labelled upstream (e.g. `["z-ai/fp8"]`
  for GLM's first-party fp8 endpoint) — combined with `allow_fallbacks:false`
  and `require_parameters:true`, a run never scores a silent fallback or a
  quantized variant in place of the model you named.
- **`sampling`** (optional) declares only the params the pinned endpoint
  supports (`temperature` / `top_p` / `seed`); anything unsupported is left off
  so `require_parameters` doesn't reject the route.
- **`effort`** (optional) sets reasoning effort; omit it for tiers that don't
  support it.

Selection:

- **`enabled: true`** marks the default set — a bare `eval.py` run uses only
  those (out of the box: `fable-5`, `gpt-5.5`, `glm-5.2`). Flip the flag to
  change the default panel.
- **`--models a,b`** runs exactly those keys, even if disabled (an unknown key
  errors with the list of valid ones).
- **`--models all`** runs the whole catalog.

Cost comes straight from OpenRouter's per-response `usage.cost`, so the Cost
(USD) column populates for **every** model with no price table to maintain.
Before a real run, check each slug against
[OpenRouter's model list](https://openrouter.ai/models) and adjust the exact IDs
(`openai/gpt-5.5`, `z-ai/glm-5.2`, …) to what your account can route to.

## ⚠️ Run this on a sandboxed machine

Tasks with a `python_tests` checker **execute model-generated code** with a
subprocess timeout and a stripped environment (the subprocess sees `PATH`
only, so your API keys are not exposed to it) — but no filesystem or network
isolation.

An easy way to add that isolation is [nono](https://github.com/nolabs-ai/nono)
(`brew install nono`), which scopes filesystem access to the repo directory
and always blocks `~/.ssh`, `~/.aws` and shell configs:

```sh
nono run --allow . -- python3 eval.py
```

Optionally restrict the network to just the model APIs (activates nono's
proxy mode):

```sh
nono run --allow . \
  --allow-domain openrouter.ai \
  -- python3 eval.py
```

nono passes your `OPENROUTER_API_KEY` through from the launching shell, so the
harness can still authenticate; the sandbox's job is to stop model-generated
code from reaching your files or the network, not to hide the key the harness
itself needs. To avoid the key living in your interactive shell at all, prefix it
on the nono command (`OPENROUTER_API_KEY=sk-or-... nono run ... -- python3
eval.py`).

If you'd rather not use a sandbox, run the harness on an isolated machine (or
containerise the checker — see [Extension points](#extension-points)), not on a
machine with personal data or broad credentials.

## Usage

```sh
python3 eval.py --dry-run                  # see what would run
python3 eval.py                            # all tasks x the enabled models, 1 trial
python3 eval.py --trials 3                 # 3 trials each (report variance, not single runs)
python3 eval.py --categories coding,security          # run only certain task categories
python3 eval.py --tasks coding-csv-dedupe,coding-rate-limiter   # run specific tasks by id
python3 eval.py --models opus-4-8,gpt-5.5  # run specific models (even if disabled)
python3 eval.py --models all               # run the whole catalog
python3 eval.py --no-rubric                # skip LLM-judged scoring (cheaper)
python3 eval.py --concurrency 8            # run 8 trials in parallel (serial by default)
```

Outputs:

Each run writes its own timestamped set of files (`<ts>` is a UTC stamp like
`20260704T101530Z`), so editing a task's prompt or checker and re-running never
blends old trials into the new numbers — every run stands alone:

- `results/results-<ts>.jsonl` — one record per trial: full response text,
  pass/fail with checker detail, tool calls, latency, input/output tokens, cost,
  stop reason, refusals, errors, and any rubric scores. `run_id` matches the
  filename stamp and `task_hash` fingerprints the task's prompt/checker/tools.
  This is the raw dataset — build your own analysis on it.
- `results/summary-<ts>.md` — aggregate table (pass rate with a **95% Wilson
  confidence interval**, median latency, tokens, cost per model) plus a per-task
  grid and, for rubric tasks, a judge-bias matrix. The interval is the honest
  read on a binary checker over few trials: a wide bracket (e.g. `67% [21–94]`
  at three trials) means the point estimate is not yet meaningful — raise
  `--trials` (all trials of one run land in one file). Built from just this run's
  records; to combine several runs deliberately, concatenate their JSONL files
  and pass them to `write_summary()`, which still warns via `task_hash` if you
  blend more than one prompt/checker version of a task.
- `results/report-<ts>.html` — self-contained review page (no external assets, opens
  straight from disk). Every trial grouped under its task with pass/fail badges,
  refusals, rubric scores + judge rationales, tool calls, cost/latency, and the
  full response text one click away. Filter by not-passed / fails / refusals and
  search the response text — the fast path for eyeballing *why* a model failed.

The harness itself has a unit test suite (no network, no API keys — providers
are mocked): `python3 -m unittest test_eval`.

## Constructing a task

A task is one JSON file in [`tasks/`](tasks/). The full anatomy:

```json
{
  "id": "coding-my-task",
  "category": "coding",      // groups the task; drives --categories and the summary rollup
  "description": "What this probes and what the floor is (notes for humans; not sent to the model).",
  "prompt": "The exact prompt sent to every model. Embed any inputs inline.",
  "tools": [ ... ],          // optional — for function-calling tasks
  "checker": { ... },        // the automated pass/fail floor (omit for unscored)
  "rubric": { "criteria": [ ... ] }   // optional — LLM-judged quality on top of the floor
}
```

Only `id` (defaults to the filename) and `prompt` are strictly required. A task
with no `checker` is still run and recorded — useful for purely qualitative
comparison via the rubric or by reading `report.html`.

Tasks are filtered at run time with `--tasks <id,...>` (exact ids) and/or
`--categories <cat,...>` (by the `category` field); the two combine as an
intersection. The shipped categories are **coding**, **realworld**, **data**,
**security**, and **tool-use**, and every task file is named `<category>-<name>`
so they group on disk. `summary.md` includes a per-category pass-rate rollup and
`report.html` tags each task with its category — add your own categories freely.

### The recipe

1. **Write the prompt you'd actually send.** Make it realistic and
   self-contained. If the task needs a document, a table, a transcript, or a
   schema, **paste it into the prompt** rather than referencing an external
   file. That keeps every run reproducible and offline, avoids live-data drift,
   and — crucially — means *you* know the correct answers, so you can check them.
2. **Decide the checkable floor.** Ask: what is the objective minimum that
   separates a usable answer from a non-answer? A constraint respected, a
   structure present, a specific fact correct, a forbidden term absent, a
   dangerous tool not called. Express that with one or more checkers (below).
   Aim for a *floor*, not a full grade — don't try to encode taste in regex.
3. **Add a rubric for the quality a regex can't see** (optional). Realistic
   pacing, correct trade-offs, tone, completeness — see
   [LLM rubric judging](#llm-rubric-judging).
4. **Add tools** if it's a function-calling task (see [Tool use](#tool-use)).
5. **Iterate against a good and a bad sample.** Before trusting a task, confirm
   your checker passes a hand-written good answer and fails a bad one — the
   ~30-line pattern the sample tasks were validated with:

   ```python
   import json, sys; sys.path.insert(0, ".")
   from eval import run_checker
   task = json.load(open("tasks/my-task.json"))
   print(run_checker(task, {"text": "<a good answer>", "tool_calls": []}))  # -> (True, ...)
   print(run_checker(task, {"text": "<a bad answer>",  "tool_calls": []}))  # -> (False, ...)
   ```

   Then `python3 eval.py --tasks my-task --models fable-5 --trials 1` for a live
   check.

### Design principles for real-world tasks

- **Author the answer key.** Because you wrote the input document/table/schema,
  you know the right notice period, the right deposit figure, the seeded data
  defect. Check *those* specific values — that's what makes a subjective-looking
  task objectively gradeable. (See `tenancy-extraction`, `data-quality-assessment`.)
- **Floor, don't ceiling.** The checker asks "is this a real attempt that
  respects the hard constraints?" not "is this the best possible answer?". A
  vegetarian recipe checker forbids meat words; it doesn't judge whether the
  recipe is *good*. Leave the ceiling to the rubric or a human reading
  `report.html`.
- **Make failure legible.** Use a composite `all` checker with a `label` on each
  sub-check, so a fail says exactly which bar was missed.
- **Probe one capability per task.** Groundedness, constraint-following,
  debugging, tool selection, injection resistance — a task that mixes five
  things tells you nothing when it fails.
- **Include a negative control.** For "did it stay grounded / resist / not
  fabricate" tasks, seed something the model would only produce if it *failed*
  (a fact not in the document, a canary the injection asks for) and assert its
  absence with `not_contains`.

### Checker toolkit

The `checker` is a small tree of typed nodes. Composite nodes (`all`) nest
other checkers; leaf nodes test the response.

| type | fields | passes when |
|---|---|---|
| `python_tests` | `test_code`, `timeout_s` | the response's solution block (last ` ```python ` block, else the largest block) is saved as `solution.py` and `test_code` (which imports it) exits 0 |
| `regex` | `pattern`, optional `label` | pattern matches the response (add `(?i)` for case-insensitive; the whole response is searched with `re.S`) |
| `contains` | `value` or `values`, optional `whole_word` | all strings appear in the response (case-insensitive substring; `whole_word: true` requires word boundaries) |
| `not_contains` | `value` or `values`, optional `whole_word` | none of the strings appear (case-insensitive) — for constraint violations and negative controls |
| `all` | `checks` (list of sub-checkers) | every sub-check passes; the failure detail names each miss |
| `tool_called` | `tool`, optional `args` (dict) | the model called `tool` this turn (and every arg in `args` matched — substring, case-insensitive, so `Paris` matches `Paris, France`) |
| `tool_not_called` | `tool` | the model did *not* call `tool` — for destructive actions it shouldn't take |
| *(no checker)* | — | recorded but unscored (qualitative tasks) |

Choosing:

- **Code output** → `python_tests`. Cover the reported bug *and* the
  previously-working cases, so a rewrite that regresses fails.
- **A specific fact / figure / format must appear** → `regex` or `contains`
  (anchor line-oriented outputs with `(?m)^...`).
- **A constraint must be respected** → `not_contains`. These match **substrings**
  by default, so forbidding `"meat"` also trips on `"meat-free"`. Add
  `"whole_word": true` to require word boundaries (fixes `kill`/`skill`), but note
  that still treats `meat-free` as containing `meat` — when a negative control
  hinges on adjacent forms or negation, use a `regex` checker written to mean
  exactly what you intend.
- **Several bars at once** → `all` with labelled sub-checks.
- **Function calling** → `tool_called` / `tool_not_called`.
- **Quality beyond the floor** → add a `rubric` (not a checker).

### Refusals

A **hard refusal** (the provider's safety classifier stops the response;
`stop_reason` is `refusal`) short-circuits the checker — there is no answer to
score. How that counts is task-local, set by an optional top-level `"refusal"`
field:

| `"refusal"` | a refusal counts as | use for |
|---|---|---|
| `neutral` *(default)* | unscored — kept out of the pass-rate denominator | most tasks, where a refusal is neither right nor wrong |
| `pass` | a pass | a prompt the model *should* decline (some jailbreaks) |
| `fail` | a fail | a benign task it should not have ducked (over-refusal) |

Refusal handling is deliberately per-task, not a blanket rule by category: a
jailbreak that also asks a benign question (see `security-jailbreak-oppo`, whose
checker requires the octopus answer) wants the benign reply, so a full refusal
there is over-refusal, not success. A scored refusal still shows as `REFUSED` in
the report — it is counted, not relabelled. Note this applies only to *hard*
refusals; a model that declines in ordinary prose is scored by the checker like
any other answer.

### Tool use

A task can offer tools by adding a provider-neutral `tools` list; the harness
translates it to OpenRouter's chat-completions tool format (nested under a
`function` key) and normalizes the model's calls back into a `tool_calls` list
on each result, so tasks stay provider-agnostic. This is
**single-step** (Level 1–2): the harness captures the first turn's tool calls
and the `tool_called` / `tool_not_called` checkers inspect them — it does not
run a mock tool and feed the result back for a second turn. Tools are declared
inline, so runs stay deterministic with no live API.

```json
{
  "id": "weather-tool",
  "prompt": "What's the weather in Paris? Use the tool.",
  "tools": [{
    "name": "get_weather",
    "description": "Get current weather for a location.",
    "parameters": {"type": "object",
      "properties": {"location": {"type": "string"}},
      "required": ["location"]}
  }],
  "checker": {"type": "tool_called", "tool": "get_weather", "args": {"location": "Paris"}}
}
```

`tool-use-weather-basic` (right tool, right args) and `tool-use-selection-flights`
(offered a safe `search_flights` and a destructive `book_flight`, does it
search-only when told not to book?) are the shipped examples. Tool calls show in
`report.html` and `results.jsonl`.

## LLM rubric judging

A task may add a top-level `rubric` with quality criteria that go beyond the
pass/fail floor:

```json
"rubric": {"criteria": ["Costs are realistic for Lisbon", "Pacing suits a 6-year-old"]}
```

When a rubric is present, **every selected model scores every response** blind
(the judge isn't told which model wrote the answer), 1–10 against the criteria.
Records gain a `rubric` grid and a `rubric_mean`; the summary gains a *Rubric
/10* column and a **judge-bias matrix** — the mean score each judge gives each
contestant. Because every contestant also judges, self-preference shows up as a
visible number instead of hiding inside a single "neutral" judge that is
secretly one of the contestants. Each judge's one-line rationale is stored in
`results.jsonl`.

Cost: rubric tasks make one extra API call per judge per trial, using the same
model configs as generation. Skip with `--no-rubric`. Use rubrics for
open-ended deliverables (advice, plans, data models); coding tasks don't need
them — unit tests are a stronger signal.

## Extension points

The harness is meant to be forked. The common extensions and where they live:

- **A new checker type.** Write a `(spec, text, tool_calls) -> (passed, detail)`
  function and register it with `@checker("<type>")` in [`eval.py`](eval.py) —
  there is no central dispatch to edit. `run_check` looks your type up by name,
  and because `all` recurses through `run_check`, the new type immediately
  composes with the others. Natural additions: `max_words` (format limits),
  `json_schema` (validate a JSON block), `sql_result` (run the model's SQL
  against an in-memory SQLite fixture and assert the result set), `numeric_close`
  (answer within a tolerance).
- **A new model.** Add an entry to `models.json` with its OpenRouter slug and a
  `provider_order` routing pin — no code change, since every model goes through
  the one `call_openrouter` path. All models OpenRouter can reach are available
  this way. If you need to talk to something OpenRouter doesn't serve, replace
  the single `call_openrouter` function (it returns a `ModelResponse` with
  `text`, `tool_calls`, `stop_reason`, `input_tokens`, `output_tokens`,
  `cost_usd`, and `refusal`/`refusal_category` if applicable) — everything
  downstream (checkers, cost, the report, rubric judging) works unchanged because
  it only sees that object.
- **A new task field.** Fields you add to a task JSON are available on the
  `task` dict in `main()`; thread them where you need them (e.g. a per-task
  `system` prompt, a per-task `max_tokens`, a `tags` list for grouping).
- **Custom scoring or reporting.** The per-run `results-<ts>.jsonl` files are the
  source of truth — point any notebook or BI tool at one, or `cat` several
  together to analyse across runs. `write_summary(records, out_path)` and
  `write_html_report(records, tasks_by_id, out_path)` both take a plain list of
  records, so you can regenerate or restyle a report — from one run's file or a
  hand-picked set — without re-running models.
- **Sandboxing model code.** Run the whole harness under
  [nono](https://github.com/nolabs-ai/nono) (see above), or wrap the
  `python_tests` subprocess in `check_python_tests()` with your container
  runtime of choice (e.g. `docker run --rm --network=none`) for the tightest
  per-checker isolation.
- **The judge panel.** `run_rubric()` uses the selected model set as judges. Swap
  in a fixed panel, add an external judge, or change the 1–10 scale by editing
  `JUDGE_PROMPT`.

The per-trial record schema (keys in each `results-<ts>.jsonl`) is the stable contract
between the harness and your tooling: `run_id, task, task_hash, model, trial,
timestamp, text, tool_calls, passed, check_detail, refusal, refusal_category,
stop_reason, latency_s, wall_clock_s, input_tokens, output_tokens, cost_usd,
sampling_sent, rubric, rubric_mean, error`. `latency_s` is time-to-first-token
and `wall_clock_s` is the full-response wall time; `sampling_sent` records the
exact sampling params that reached the pinned endpoint.

## What ships — the sample task library

The included tasks double as worked examples of each pattern. Files are named
`<category>-<name>`, so `ls tasks/` groups them by type.

- **`coding`** — deterministic `python_tests`. Greenfield
  (`coding-csv-dedupe`, `coding-rate-limiter`, `coding-log-parse`) and debugging
  with buggy code + traceback where tests also cover the previously-working
  cases (`coding-debug-billing-date`, `coding-debug-mutable-default`,
  `coding-debug-money-split`, `coding-debug-pagination`).
- **`realworld`** — advice / constraint / groundedness, composite `all` floors:
  `realworld-recipe-veggie-weeknight`, `realworld-holiday-plan-lisbon`,
  `realworld-flight-search-honesty`, `realworld-crying-baby`,
  `realworld-honey-cough-pushback`, `realworld-date-night-nottingham`,
  `realworld-marathon-pb-plan`, `realworld-format-strict-bullets`,
  `realworld-tenancy-extraction`.
- **`security`** — jailbreak / prompt-injection resistance
  (`security-email-summary-injection`, `security-injection-ungpt-in-document`,
  the `security-jailbreak-*` set), built from promptfoo's packaged payload
  templates.
- **`tool-use`** — `tool-use-weather-basic`, `tool-use-selection-flights`.
- **`data`** — analytics / data-engineering deliverables, deterministic floor
  plus a cross-judged rubric: `data-csv-mapping-customer` (source→target field
  mapping), `data-model-from-interview` (dimensional model + requirements from a
  transcript), `data-quality-assessment` (find the seeded defects in a table),
  `data-fabric-roadmap-user-stories` (a phased user-story roadmap for a Microsoft
  Fabric build from a catalogue + mapping + requirements). The last three chain —
  the mapping and requirements feed the roadmap.

Read the preserved response text in `results.jsonl` / `report.html` for the
qualitative comparison, and run `--trials 3+` so you report variance, not
single-shot luck.

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

## License

[MIT](LICENSE) © 2026 Ed Yau.
