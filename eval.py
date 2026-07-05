#!/usr/bin/env python3
"""Cross-model eval harness: GLM-5.2 vs GPT-5.5 vs Claude Fable 5.

Runs every task in tasks/ against every model in models.json, N trials each.
Each run writes its own timestamped set of files — results/results-<ts>.jsonl,
results/summary-<ts>.md and results/report-<ts>.html — so the reports reflect
exactly one run and never blend stale trials from an earlier prompt or checker
across runs.

All models run through this same harness with the same prompts and the same
checkers, so scores are directly comparable — unlike vendor-reported
benchmark numbers produced on different scaffolds.

Usage:
    python3 eval.py                          # all tasks x all models, 1 trial
    python3 eval.py --trials 3               # 3 trials per (task, model)
    python3 eval.py --models fable-5,glm-5.2 --tasks coding-csv-dedupe
    python3 eval.py --dry-run                # list what would run

Layout (one file, five layers):
    providers  — (cfg, prompt, tools) -> ModelResponse via one pinned OpenRouter call
    checkers   — (spec, text, tool_calls) -> (passed, detail), registered by @checker
    rubric     — optional cross-judged LLM scoring
    reports    — per-run results/summary-<ts>.md + results/report-<ts>.html
    runner     — CLI, selection, and the task x model x trial loop

WARNING: tasks with a "python_tests" checker EXECUTE model-generated code
locally. Run this harness on an isolated machine (see README).
"""
import argparse
import concurrent.futures
import hashlib
import html
import itertools
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results"


# ---------------------------------------------------------------- providers
#
# One provider path: every model is called through call_openrouter, a single
# pinned OpenRouter chat/completions stream. build_request / reduce_stream /
# map_refusal are pure helpers (no I/O) so the routing pin, sampling gating,
# TTFT clock and refusal mapping are all unit-testable without spending a token.

@dataclass
class ModelResponse:
    """The normalized result the provider returns. Checkers, the rubric and
    the reports only ever see this shape, never a raw SDK response."""
    text: str = ""
    refusal: bool = False
    refusal_category: str = None
    stop_reason: str = None
    tool_calls: list = field(default_factory=list)
    input_tokens: int = None
    output_tokens: int = None
    latency_s: float = None  # TTFT: time to first *content* token (D-04)
    cost_usd: float = None  # USD actually charged, read from usage.cost
    wall_clock_s: float = None  # full stream duration (thinking + content)
    sampling_sent: dict = field(default_factory=dict)  # sampling params actually sent


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_request(cfg, prompt, tools=None):
    """PURE: return (kwargs, extra_body, sampling_sent) for one pinned OpenRouter
    call — no I/O, so the routing pin and sampling gating are unit-testable.

    Every request is pinned to the labeled provider (provider.order +
    allow_fallbacks:false + require_parameters:true) so no silent fallback or
    quantized endpoint scores a different model than the one labeled (D-08).
    Only the sampling params listed in cfg["sampling"] are sent: with
    require_parameters:true, sending a param the model's provider does not support
    routes to no provider and 404s the trial (Pitfall 6), so gating is mandatory.
    """
    extra_body = {"provider": {"order": cfg["provider_order"],
                               "allow_fallbacks": False,
                               "require_parameters": True},
                  "usage": {"include": True}}
    if cfg.get("effort"):
        # Unified reasoning control — the nested reasoning:{effort}, not a
        # top-level reasoning_effort (a silent no-op for Claude over OpenRouter,
        # Pitfall 5). Omitted entirely for models with no effort key.
        extra_body["reasoning"] = {"effort": cfg["effort"]}
    sampling_sent = {p: cfg["sampling"][p]
                     for p in ("temperature", "top_p", "seed")
                     if p in (cfg.get("sampling") or {})}
    kwargs = dict(model=cfg["model"], messages=[{"role": "user", "content": prompt}],
                  stream=True, stream_options={"include_usage": True},
                  max_tokens=cfg.get("max_tokens", 64000), **sampling_sent)
    if tools:
        # Chat/completions nests the schema under a "function" key.
        kwargs["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t["parameters"]}} for t in tools]
    return kwargs, extra_body, sampling_sent


def reduce_stream(chunks, t0, now=time.monotonic):
    """PURE: fold an OpenRouter chat stream into the fields ModelResponse needs.

    Returns a dict — text, ttft, wall, input_tokens, output_tokens, cost,
    finish_reason, native_finish_reason, tool_calls. TTFT is stamped at the first
    non-empty content delta (reasoning/thinking deltas are ignored, D-04); wall is
    stamped after the final chunk; cost and tokens come from the final usage-only
    chunk (empty .choices). `now` is injectable so the clock is deterministic
    under test.
    """
    parts = []
    ttft = usage = fr = nfr = None
    tool_frags = {}  # index -> {"name": str|None, "arguments": [str, ...]}
    for chunk in chunks:
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:  # final usage-only chunk carries no choices
            continue
        ch = chunk.choices[0]
        delta = ch.delta
        if getattr(delta, "content", None):
            if ttft is None:
                ttft = now() - t0  # first *content* token, not a reasoning delta
            parts.append(delta.content)
        for tc in (getattr(delta, "tool_calls", None) or []):
            slot = tool_frags.setdefault(getattr(tc, "index", 0),
                                         {"name": None, "arguments": []})
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"].append(fn.arguments)
        if ch.finish_reason:
            fr = ch.finish_reason
            nfr = getattr(ch, "native_finish_reason", None)
    tool_calls = [{"name": f["name"], "arguments": _json_args("".join(f["arguments"]))}
                  for _, f in sorted(tool_frags.items()) if f["name"]]
    return {"text": "".join(parts), "ttft": ttft, "wall": now() - t0,
            "input_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "cost": getattr(usage, "cost", None),
            "finish_reason": fr, "native_finish_reason": nfr,
            "tool_calls": tool_calls}


def map_refusal(finish_reason, native_finish_reason, message):
    """PURE: map the (possibly native) finish reason to (refusal, category).

    Provider signal first (D-01): a normalized content_filter, or a native
    refusal/content_filter/safety stop, is a hard refusal. The OpenAI-compat path
    rarely carries a structured category, so category is usually None.
    """
    hard = (finish_reason == "content_filter"
            or (native_finish_reason or "").lower() in {"refusal", "content_filter", "safety"})
    category = getattr(message, "refusal", None)
    return hard, category


def call_openrouter(cfg, prompt, tools=None):
    """The single provider path: stream one pinned OpenRouter chat request and
    fold it into a ModelResponse.

    Every model — Claude, GPT, GLM — runs through here, so cost, TTFT/wall-clock,
    sampling and refusal are all measured the same way and stay comparable. The
    request is pinned (see build_request) so no fallback re-serves another model;
    a safety refusal is recorded (refusal=True), never transparently re-served.
    The API key is read only from the environment and never leaves this function.
    """
    from openai import OpenAI
    client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
    kwargs, extra_body, sampling_sent = build_request(cfg, prompt, tools)
    t0 = time.monotonic()
    stream = client.chat.completions.create(extra_body=extra_body, **kwargs)
    r = reduce_stream(stream, t0)
    refusal, category = map_refusal(r["finish_reason"], r["native_finish_reason"], None)
    return ModelResponse(
        text=r["text"], refusal=refusal, refusal_category=category,
        stop_reason=r["native_finish_reason"] or r["finish_reason"],
        tool_calls=r["tool_calls"],
        input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
        cost_usd=r["cost"], latency_s=r["ttft"], wall_clock_s=r["wall"],
        sampling_sent=sampling_sent)


def call_model(name, cfg, prompt, tools=None):
    """Route a model through the single OpenRouter path.

    Timing is owned by call_openrouter (only the stream sees the first content
    token), so — unlike the old dispatcher — nothing is stamped here.
    """
    return call_openrouter(cfg, prompt, tools)


def _is_rate_limit(exc):
    """True for rate-limit / 429 errors, without importing any provider SDK.

    The OpenAI SDK's RateLimitError carries status_code 429; we also fall back to
    the exception's type name / message so any client raising a rate-limit error
    is retried the same way."""
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    return "ratelimit" in type(exc).__name__.lower() or "rate limit" in str(exc).lower()


def call_with_retry(name, cfg, prompt, tools=None, retries=4, base_delay=2.0):
    """call_model with exponential backoff on rate-limit errors only.

    A full-catalog x N-trials run reliably hits 429s; without this each becomes an
    error record and silently thins the dataset. Non-rate-limit errors (bad
    request, auth, provider down) are re-raised immediately — retrying them just
    wastes time. Latency is measured per attempt inside call_model, so the
    recorded latency_s reflects the successful call, not the waits.
    """
    for attempt in range(retries + 1):
        try:
            return call_model(name, cfg, prompt, tools)
        except Exception as exc:
            if attempt >= retries or not _is_rate_limit(exc):
                raise
            wait = base_delay * 2 ** attempt
            print(f"   rate-limited ({type(exc).__name__}); "
                  f"retry {attempt + 1}/{retries} in {wait:.0f}s", flush=True)
            time.sleep(wait)


def _json_args(raw):
    """Tool-call arguments arrive as a JSON string; tolerate malformed ones."""
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}


# ---------------------------------------------------------------- checkers
#
# A checker is a function (spec, text, tool_calls) -> (passed, detail),
# registered under the "type" string used in task JSON. Adding a checker type
# is one decorated function.

CHECKERS = {}


def checker(ctype):
    def register(fn):
        CHECKERS[ctype] = fn
        return fn
    return register


def run_checker(task, resp):
    """Return (passed, detail). passed is None when the task has no checker.

    `resp` is a ModelResponse, so checkers can see both the answer text and
    the normalized tool_calls (for tool-use tasks).
    """
    spec = task.get("checker")
    if not spec:
        return None, "no checker"
    return run_check(spec, resp.text or "", resp.tool_calls or [])


def run_check(spec, text, tool_calls=()):
    fn = CHECKERS.get(spec["type"])
    if fn is None:
        raise ValueError(f"unknown checker type {spec['type']!r}")
    return fn(spec, text, tool_calls)


@checker("all")
def check_all(spec, text, tool_calls):
    """Composite: every sub-check must pass. Failure detail names each miss."""
    results = [run_check(sub, text, tool_calls) for sub in spec["checks"]]
    failures = [detail for ok, detail in results if not ok]
    return (not failures), ("; ".join(failures) if failures else "ok")


@checker("tool_called")
def check_tool_called(spec, text, tool_calls):
    """PASS if some tool call matches `tool` (and every arg in `args`, if given)."""
    name, want = spec["tool"], spec.get("args")
    for call in tool_calls:
        if call.get("name") != name:
            continue
        got = call.get("arguments") or {}
        if want is None or all(_arg_match(got.get(k), v) for k, v in want.items()):
            return True, f"called {name}" + (f" with {want}" if want else "")
    return False, f"no matching call to {name}({spec.get('args') or ''})"


def _arg_match(got, want):
    """Loose match: `want` normalized is a substring of `got` normalized.

    Tolerates 'Paris' matching a model that answered 'Paris, France', and
    coerces numbers/enums to strings so 2 matches "2".
    """
    return str(want).strip().lower() in str(got).strip().lower()


@checker("tool_not_called")
def check_tool_not_called(spec, text, tool_calls):
    name = spec["tool"]
    hit = [c for c in tool_calls if c.get("name") == name]
    return (not hit), ("ok" if not hit else f"called forbidden tool {name}")


@checker("contains")
def check_contains(spec, text, tool_calls):
    missing = [v for v in _wanted_values(spec) if not _value_present(spec, v, text)]
    return (not missing), (f"missing: {missing!r}" if missing else "ok")


@checker("not_contains")
def check_not_contains(spec, text, tool_calls):
    found = [v for v in _wanted_values(spec) if _value_present(spec, v, text)]
    return (not found), (f"forbidden term present: {found!r}" if found else "ok")


def _wanted_values(spec):
    return spec.get("values") or [spec["value"]]


def _value_present(spec, value, text):
    """Whether `value` occurs in `text`. Case-insensitive substring by default;
    "whole_word": true requires word boundaries (\\bvalue\\b), so 'kill' no longer
    matches 'skill'. Word boundaries treat punctuation as a break, so 'meat' still
    matches 'meat-free' — reach for a `regex` checker when you need that precision."""
    if spec.get("whole_word"):
        return re.search(r"\b" + re.escape(value) + r"\b", text, re.I) is not None
    return value.lower() in text.lower()


@checker("regex")
def check_regex(spec, text, tool_calls):
    ok = re.search(spec["pattern"], text, re.S) is not None
    label = spec.get("label", spec["pattern"])
    return ok, ("matched" if ok else f"no match: {label}")


@checker("python_tests")
def check_python_tests(spec, text, tool_calls):
    code = extract_code(text)
    if code is None:
        return False, "no code block in response"
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "solution.py").write_text(code)
        (Path(d) / "run_tests.py").write_text(spec["test_code"])
        try:
            proc = subprocess.run(
                [sys.executable, "run_tests.py"],
                cwd=d, capture_output=True, text=True,
                timeout=spec.get("timeout_s", 30),
                # model-generated code must not see API keys etc.
                env={"PATH": os.environ.get("PATH", "")},
            )
        except subprocess.TimeoutExpired:
            return False, "tests timed out"
        if proc.returncode == 0:
            return True, "tests passed"
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return False, f"tests failed: {tail}"


CODE_BLOCK_RE = re.compile(r"```(\w+)?\n(.*?)```", re.S)


def extract_code(text):
    """Pick the code block most likely to be the solution.

    Prefer the last ```python/```py block — models label their final answer and
    put it last. Only if nothing is python-tagged do we fall back to the largest
    block, which dodges the common failure where an answer *ends* with a short
    untagged example-usage or sample-output fence that the old "last block wins"
    rule would have run as the solution. Returns None if there are no blocks.
    """
    blocks = CODE_BLOCK_RE.findall(text)  # [(lang, body), ...]
    if not blocks:
        return None
    python = [body for lang, body in blocks if (lang or "").lower() in ("python", "py")]
    if python:
        return python[-1]
    return max((body for _, body in blocks), key=len)


# ---------------------------------------------------------------- llm rubric

JUDGE_PROMPT = """You are scoring a model's answer against a rubric. You do not know \
which model wrote it. Be strict, and use the full 1-10 scale — a 5 is a mediocre \
answer, not a bad one.

<task>
{task_prompt}
</task>

<answer>
{answer}
</answer>

<rubric>
{criteria}
</rubric>

Score the answer 1-10 against the rubric (10 = excellent on every criterion, \
1 = fails almost all of them). Reply with ONLY a JSON object, no other text:
{{"score": <integer 1-10>, "rationale": "<one sentence>"}}"""


JUDGE_ANSWER_CAP = 40000  # chars (~8k tokens); rubric answers are prose — this only trims runaways


def run_rubric(task, answer, judges):
    """Every judge model scores the answer blind. Returns {judge: {score, rationale}}.

    Using all three contestants as judges makes judge bias measurable (the
    summary prints a judge x contestant matrix) instead of hidden behind a
    single 'neutral' judge that is actually one of the contestants.

    The answer is capped at JUDGE_ANSWER_CAP before judging: a runaway response
    would otherwise be sent in full to every judge (the record's 200k cap is
    applied only afterwards), multiplying one bad answer into N expensive calls.
    """
    answer = answer or ""
    if len(answer) > JUDGE_ANSWER_CAP:
        answer = answer[:JUDGE_ANSWER_CAP] + "\n…[answer truncated for judging]"
    criteria = "\n".join("- " + c for c in task["rubric"]["criteria"])
    prompt = JUDGE_PROMPT.format(task_prompt=task["prompt"], answer=answer, criteria=criteria)
    return {name: _judge_once(name, cfg, prompt) for name, cfg in judges.items()}


def _judge_once(judge_name, cfg, prompt):
    try:
        reply = call_with_retry(judge_name, cfg, prompt).text or ""
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return {"score": None, "error": "no JSON in judge reply"}
        data = json.loads(m.group(0))
        score = data.get("score")
        return {"score": int(score) if score is not None else None,
                "rationale": str(data.get("rationale", ""))[:300]}
    except Exception as e:
        return {"score": None, "error": f"{type(e).__name__}: {e}"[:200]}


def rubric_mean(scores, exclude=None):
    """Mean rubric score, leaving out the contestant's own self-score.

    Every contestant is also a judge, so a self-flattering model would inflate
    its own headline number. Passing `exclude=<contestant>` drops the judge whose
    name matches (the self-cell). The raw per-judge scores stay in the record, so
    the judge-bias matrix still shows self-preference. Returns None when no
    independent judge scored the answer (e.g. a single-model run judging itself).
    """
    vals = [s["score"] for judge, s in (scores or {}).items()
            if s.get("score") is not None and judge != exclude]
    return round(sum(vals) / len(vals), 2) if vals else None


# ---------------------------------------------------------------- tasks + models

def task_hash(task):
    """Short content hash of the parts of a task that affect scoring — prompt,
    checker, tools (id/category/description are cosmetic and excluded). Stamped
    into every record so the summary can warn when one task id blends results
    from more than one task version across appended runs."""
    payload = json.dumps({"prompt": task.get("prompt"), "checker": task.get("checker"),
                          "tools": task.get("tools")}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def load_tasks(task_filter):
    tasks = []
    for f in sorted(TASKS_DIR.glob("*.json")):
        task = json.loads(f.read_text())
        task["id"] = task.get("id", f.stem)
        if not task_filter or task["id"] in task_filter:
            tasks.append(task)
    return tasks


def task_categories():
    """Map task id -> category by scanning tasks/ (for the summary rollup)."""
    cats = {}
    for f in TASKS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except ValueError:
            continue
        cats[d.get("id", f.stem)] = d.get("category", "uncategorized")
    return cats


def select_models(spec, catalog):
    """--models semantics: explicit keys run even if disabled, 'all' runs the
    whole catalog, no flag runs the models marked "enabled": true."""
    if spec == "all":
        return dict(catalog)
    if spec:
        wanted = [k.strip() for k in spec.split(",") if k.strip()]
        unknown = [k for k in wanted if k not in catalog]
        if unknown:
            sys.exit("unknown model key(s): %s\navailable: %s"
                     % (", ".join(unknown), ", ".join(catalog)))
        return {k: catalog[k] for k in wanted}
    return {k: v for k, v in catalog.items() if v.get("enabled", True)}


def select_tasks(tasks_spec, categories_spec):
    """--tasks names ids, --categories names categories; they intersect. An
    unknown id or category aborts with the valid list, like --models — a typo'd
    filter shouldn't silently drop work and waste a run."""
    all_tasks = load_tasks(None)
    if tasks_spec:
        wanted = {t.strip() for t in tasks_spec.split(",") if t.strip()}
        available = {t["id"] for t in all_tasks}
        unknown = wanted - available
        if unknown:
            sys.exit("unknown task id(s): %s\navailable: %s"
                     % (", ".join(sorted(unknown)), ", ".join(sorted(available))))
        tasks = [t for t in all_tasks if t["id"] in wanted]
    else:
        tasks = all_tasks
    if categories_spec:
        wanted = {c.strip() for c in categories_spec.split(",") if c.strip()}
        available = {t.get("category", "uncategorized") for t in all_tasks}
        unknown = wanted - available
        if unknown:
            sys.exit("unknown categor(y/ies): %s\navailable: %s"
                     % (", ".join(sorted(unknown)), ", ".join(sorted(available))))
        tasks = [t for t in tasks if t.get("category") in wanted]
    return tasks


# ---------------------------------------------------------------- summary.md

def group_by(records, key):
    groups = {}
    for r in records:
        groups.setdefault(r[key], []).append(r)
    return groups


def pass_counts(rs):
    """(passed, scored) — scored excludes refusals, errors and checker-less trials."""
    passed = sum(1 for r in rs if r.get("passed") is True)
    scored = sum(1 for r in rs if r.get("passed") in (True, False))
    return passed, scored


def wilson_interval(passed, n, z=1.96):
    """95% Wilson score interval for a binomial pass rate, as (lo, hi) fractions.

    Binary checkers over a handful of trials carry huge uncertainty — at n=3 the
    interval spans ~40 points either way, which is exactly what a reader should
    see before treating "2/3" as a real number. Returns None when n == 0.
    """
    if n == 0:
        return None
    phat = passed / n
    denom = 1 + z**2 / n
    center = phat + z**2 / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))
    return (max(0.0, (center - margin) / denom), min(1.0, (center + margin) / denom))


def pass_rate_cell(passed, scored):
    """Pass rate with its 95% Wilson interval, e.g. '67% [21–94]'. Denominator is
    Pass+Fail (refusals/errors excluded), which the Pass/Fail columns make visible."""
    if not scored:
        return "—"
    lo, hi = wilson_interval(passed, scored)
    return f"{100.0 * passed / scored:.0f}% [{100 * lo:.0f}–{100 * hi:.0f}]"


def md_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def _mixed_hash_tasks(records):
    """task id -> set of task_hashes, for ids whose records span >1 version.
    Records without a task_hash (from before hashing existed) are ignored."""
    by_task = {}
    for r in records:
        if r.get("task_hash"):
            by_task.setdefault(r["task"], set()).add(r["task_hash"])
    return {t: hs for t, hs in by_task.items() if len(hs) > 1}


def write_summary(records, out_path=None):
    """Aggregate one run's records into a summary markdown file.

    out_path defaults to results/summary.md; the runner passes a per-run
    results/summary-<ts>.md so runs never overwrite each other. The staleness
    guard below still fires if a caller hands in records that span task
    versions (e.g. several run files concatenated by hand)."""
    out_path = out_path or (RESULTS_DIR / "summary.md")
    by_model = group_by(records, "model")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Eval summary", "",
             f"Generated {stamp}. All models run through the same harness — scores are cross-comparable.",
             ""]
    stale = _mixed_hash_tasks(records)
    if stale:
        lines += ["> ⚠️ **Mixed task versions.** These task ids have records from more "
                  "than one prompt/checker version in `results.jsonl`, so their aggregates "
                  "blend different tasks: **" + ", ".join(sorted(stale)) + "**. Delete "
                  "`results.jsonl` or re-run only the changed tasks to compare cleanly.", ""]
        for t in sorted(stale):
            print(f"WARNING: task {t!r} has records from {len(stale[t])} different "
                  "prompt/checker versions in results.jsonl — the summary blends them")
    lines += _overall_section(by_model)
    lines += _per_task_section(records, by_model)
    lines += _category_section(records, by_model)
    lines += _bias_section(records)
    out_path.write_text("\n".join(lines) + "\n")


def _overall_section(by_model):
    rows = []
    for model in sorted(by_model):
        rs = by_model[model]
        passed, scored = pass_counts(rs)
        rubs = [r["rubric_mean"] for r in rs if r.get("rubric_mean") is not None]
        lats = [r["latency_s"] for r in rs if r.get("latency_s") is not None]
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        rows.append([
            model, len(rs), passed, scored - passed,
            sum(1 for r in rs if r.get("refusal")),
            sum(1 for r in rs if r.get("error")),
            pass_rate_cell(passed, scored),
            f"{sum(rubs) / len(rubs):.1f}" if rubs else "—",
            f"{statistics.median(lats):.1f}" if lats else "—",
            sum(r.get("output_tokens") or 0 for r in rs),
            f"{sum(costs):.2f}" if costs else "n/a",
        ])
    header = ["Model", "Trials", "Pass", "Fail", "Refusals", "Errors", "Pass rate (95% CI)",
              "Rubric /10", "Median TTFT (s)", "Total out-tokens", "Cost (USD)"]
    return md_table(header, rows) + [
        "", "Pass rate is over Pass+Fail (refusals and errors excluded); the "
        "bracket is the 95% Wilson interval. Wide intervals mean too few trials "
        "to conclude anything — raise `--trials`.",
        "", "Median TTFT is time-to-first-content-token (reasoning/thinking deltas "
        "are excluded, so a long-thinking model is not penalised for latency it "
        "spends reasoning); total wall-clock per trial is recorded on each record "
        "and shown in the HTML report."]


def _per_task_section(records, by_model):
    models = sorted(by_model)
    rows = []
    for tid in sorted({r["task"] for r in records}):
        cells = [tid]
        for model in models:
            rs = [r for r in by_model[model] if r["task"] == tid]
            passed, scored = pass_counts(rs)
            cells.append(f"{passed}/{scored}" if scored
                         else ("refused" if any(r.get("refusal") for r in rs) else "—"))
        rows.append(cells)
    return ["", "## Per task", ""] + md_table(["Task"] + models, rows)


def _category_section(records, by_model):
    """Pass rate rolled up by task category (tool-use / realworld / coding / ...)."""
    cats = task_categories()
    models = sorted(by_model)
    names = sorted({cats.get(r["task"], "uncategorized") for r in records})
    if not names:
        return []
    rows = []
    for cat in names:
        cells = [cat]
        for model in models:
            rs = [r for r in by_model[model] if cats.get(r["task"], "uncategorized") == cat]
            passed, scored = pass_counts(rs)
            cells.append(f"{passed}/{scored} ({100.0 * passed / scored:.0f}%)" if scored else "—")
        rows.append(cells)
    return ["", "## By category", ""] + md_table(["Category"] + models, rows)


def _bias_section(records):
    """Judge x contestant matrix — makes self-preference visible instead of hidden."""
    cells = {}  # (judge, contestant) -> [scores]
    for r in records:
        for judge, s in (r.get("rubric") or {}).items():
            if s.get("score") is not None:
                cells.setdefault((judge, r["model"]), []).append(s["score"])
    if not cells:
        return []
    judges = sorted({j for j, _ in cells})
    contestants = sorted({c for _, c in cells})
    rows = []
    for judge in judges:
        vals_by_c = [cells.get((judge, c)) for c in contestants]
        rows.append([judge] + [f"{sum(v) / len(v):.1f}" if v else "—" for v in vals_by_c])
    return ["", "## Judge bias matrix (mean rubric score given)", "",
            "Rows are judges, columns are the models being scored. A judge scoring",
            "its own row-column cell notably higher than other judges score that",
            "column suggests self-preference.", ""] + \
        md_table(["Judge \\ Scored"] + contestants, rows)


# ---------------------------------------------------------------- html report

REPORT_CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --card:#f6f6f7; --border:#e2e2e5;
        --pass:#137333; --passbg:#e6f4ea; --fail:#c5221f; --failbg:#fce8e6;
        --refuse:#b06000; --refusebg:#fef3e0; --err:#7c3aed; --errbg:#f0e9fc;
        --accent:#1a73e8; --code:#f0f0f2; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16171a; --fg:#e6e6e8; --muted:#9a9aa2; --card:#1e2024; --border:#31333a;
          --pass:#81c995; --passbg:#1e3226; --fail:#f28b82; --failbg:#3a2221;
          --refuse:#fcc46b; --refusebg:#3a2e18; --err:#c5a3ff; --errbg:#2b2440;
          --accent:#8ab4f8; --code:#101114; } }
* { box-sizing:border-box; }
body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:var(--bg); color:var(--fg); }
header { padding:20px 24px; border-bottom:1px solid var(--border); position:sticky; top:0;
         background:var(--bg); z-index:5; }
h1 { margin:0 0 4px; font-size:18px; }
.sub { color:var(--muted); font-size:13px; }
.controls { margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.controls button { font:inherit; padding:5px 12px; border:1px solid var(--border); border-radius:999px;
        background:var(--card); color:var(--fg); cursor:pointer; }
.controls button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
.controls input { font:inherit; padding:5px 10px; border:1px solid var(--border); border-radius:6px;
        background:var(--card); color:var(--fg); flex:1; min-width:160px; }
main { padding:16px 24px 60px; max-width:1100px; }
.task { margin:22px 0; }
.task > h2 { font-size:16px; margin:0 0 2px; }
.cat { font-size:11px; font-weight:500; padding:1px 8px; border-radius:999px; background:var(--card);
       border:1px solid var(--border); color:var(--muted); margin-left:8px; vertical-align:middle; }
.task-desc { color:var(--muted); font-size:12.5px; margin:0 0 6px; }
details.prompt { margin:6px 0 12px; }
details.prompt summary { cursor:pointer; color:var(--accent); font-size:12.5px; }
pre { background:var(--code); border:1px solid var(--border); border-radius:8px; padding:12px;
      overflow-x:auto; white-space:pre-wrap; word-wrap:break-word; font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; margin:6px 0; }
.trial { border:1px solid var(--border); border-radius:10px; padding:12px 14px; margin:10px 0; background:var(--card); }
.trial.hidden { display:none; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.badge { font-weight:600; font-size:11px; padding:2px 9px; border-radius:999px; letter-spacing:.02em; }
.b-pass { color:var(--pass); background:var(--passbg); }
.b-fail { color:var(--fail); background:var(--failbg); }
.b-refuse { color:var(--refuse); background:var(--refusebg); }
.b-err { color:var(--err); background:var(--errbg); }
.model { font-weight:600; }
.meta { color:var(--muted); font-size:12px; }
.detail { font-size:12.5px; margin-top:6px; }
.detail .k { color:var(--muted); }
.rubric { margin-top:6px; font-size:12.5px; }
.rubric .chip { display:inline-block; margin:2px 6px 2px 0; padding:1px 8px; border-radius:6px;
        background:var(--bg); border:1px solid var(--border); }
.rubric .rat { color:var(--muted); font-size:12px; margin:2px 0 0 2px; }
details.resp summary { cursor:pointer; color:var(--accent); font-size:12.5px; margin-top:6px; }
"""

REPORT_JS = """
const q=(s,r=document)=>[...r.querySelectorAll(s)];
let mode='all', term='';
function apply(){
  q('.trial').forEach(t=>{
    const st=t.dataset.status, txt=t.dataset.text;
    let ok = mode==='all' || (mode==='fail'&&st==='fail') ||
             (mode==='refuse'&&st==='refuse') || (mode==='problem'&&st!=='pass');
    if(ok && term) ok = txt.includes(term);
    t.classList.toggle('hidden', !ok);
  });
  q('.task').forEach(sec=>{
    const any=q('.trial:not(.hidden)',sec).length>0;
    sec.style.display = any ? '' : 'none';
  });
}
document.addEventListener('DOMContentLoaded',()=>{
  q('.controls button').forEach(b=>b.onclick=()=>{
    q('.controls button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); mode=b.dataset.mode; apply();
  });
  const inp=document.getElementById('search');
  inp.oninput=()=>{ term=inp.value.toLowerCase(); apply(); };
  apply();
});
"""


def write_html_report(records, tasks_by_id, out_path=None):
    """Render one run's report.html: a self-contained, filterable review page.

    Addresses the 'inspect the results file for ...' step every rubric-less task
    leans on — groups every trial under its task, with pass/fail, refusals,
    rubric scores + rationales, cost/latency, and the full response text one
    click away. No external assets, so it opens straight from disk. out_path
    defaults to results/report.html; the runner passes a per-run
    results/report-<ts>.html.
    """
    out_path = out_path or (RESULTS_DIR / "report.html")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Eval report</title><style>", REPORT_CSS, "</style></head><body>",
        "<header><h1>Eval report</h1>",
        f"<div class='sub'>Generated {stamp} &middot; {len(records)} trials across "
        f"{len({r['task'] for r in records})} tasks. "
        "All models run through the same harness.</div>",
        "<div class='controls'>",
        "<button class='on' data-mode='all'>All</button>",
        "<button data-mode='problem'>Not passed</button>",
        "<button data-mode='fail'>Fails</button>",
        "<button data-mode='refuse'>Refusals</button>",
        "<input id='search' placeholder='search response text…'>",
        "</div></header><main>",
    ]
    by_task = group_by(records, "task")
    cats = task_categories()
    for tid in sorted(by_task):
        parts += _task_html(tid, by_task[tid], tasks_by_id.get(tid, {}), cats)
    parts.append(f"</main><script>{REPORT_JS}</script></body></html>")
    out_path.write_text("".join(parts))


def _task_html(tid, rs, task, cats):
    """One <section> per task: header, prompt, then a card per trial."""
    e = html.escape
    passed, scored = pass_counts(rs)
    rate = f" &middot; {passed}/{scored} passed" if scored else ""
    cat = task.get("category") or cats.get(tid)
    chip = f"<span class='cat'>{e(cat)}</span>" if cat else ""
    parts = [f"<section class='task' data-cat='{e(cat or '')}'>",
             f"<h2>{e(tid)}{chip}<span class='meta'>{rate}</span></h2>"]
    if task.get("description"):
        parts.append(f"<p class='task-desc'>{e(task['description'])}</p>")
    if task.get("prompt"):
        parts.append("<details class='prompt'><summary>prompt</summary>"
                     f"<pre>{e(task['prompt'])}</pre></details>")
    for r in sorted(rs, key=lambda r: (r["model"], r.get("trial", 0))):
        parts += _trial_html(r)
    parts.append("</section>")
    return parts


def _status(r):
    if r.get("error"):
        return "err", "b-err", "ERROR"
    if r.get("refusal"):
        return "refuse", "b-refuse", "REFUSED"
    if r.get("passed") is True:
        return "pass", "b-pass", "PASS"
    if r.get("passed") is False:
        return "fail", "b-fail", "FAIL"
    return "done", "meta", "DONE"


def _trial_html(r):
    """One trial card; data-status/data-text drive the client-side filters."""
    e = html.escape
    status, cls, word = _status(r)
    text = r.get("text") or ""
    call_names = " ".join(c.get("name") or "" for c in (r.get("tool_calls") or []))
    search_blob = e((text + " " + (r.get("check_detail") or "") + " " + call_names).lower())

    bits = []
    if r.get("latency_s") is not None:
        bits.append(f"{r['latency_s']:.1f}s TTFT")
    if r.get("wall_clock_s") is not None:
        bits.append(f"{r['wall_clock_s']:.1f}s wall")
    if r.get("output_tokens") is not None:
        bits.append(f"{r['output_tokens']} out-tok")
    if r.get("cost_usd") is not None:
        bits.append(f"${r['cost_usd']:.4f}")
    if r.get("stop_reason"):
        bits.append(f"stop:{r['stop_reason']}")
    meta = " &middot; ".join(e(b) for b in bits)

    parts = [f"<div class='trial' data-status='{status}' data-text='{search_blob}'>",
             f"<div class='row'><span class='badge {cls}'>{word}</span>"
             f"<span class='model'>{e(r['model'])}</span>"
             f"<span class='meta'>trial {e(str(r.get('trial', 1)))} &middot; {meta}</span></div>"]

    if r.get("error"):
        parts.append(f"<div class='detail'><span class='k'>error:</span> {e(r['error'])}</div>")
    elif r.get("refusal"):
        parts.append("<div class='detail'><span class='k'>refusal category:</span> "
                     f"{e(str(r.get('refusal_category')))}</div>")
    elif r.get("check_detail"):
        parts.append(f"<div class='detail'><span class='k'>checker:</span> {e(r['check_detail'])}</div>")

    if r.get("tool_calls"):
        rendered = ", ".join(
            "{}({})".format(c.get("name"), ", ".join(
                f"{k}={v}" for k, v in (c.get("arguments") or {}).items()))
            for c in r["tool_calls"])
        parts.append(f"<div class='detail'><span class='k'>tool calls:</span> {e(rendered)}</div>")

    if r.get("rubric"):
        chips, rats = [], []
        for judge, s in r["rubric"].items():
            sc = s.get("score")
            chips.append(f"<span class='chip'>{e(judge)}: "
                         f"{e(str(sc)) if sc is not None else '&mdash;'}</span>")
            rationale = s.get("rationale") or s.get("error")
            if rationale:
                rats.append(f"<div class='rat'>{e(judge)}: {e(str(rationale))}</div>")
        mean = r.get("rubric_mean")
        head = "<span class='k'>rubric</span> %s " % (f"mean {mean:.1f}" if mean is not None else "")
        parts.append(f"<div class='rubric'>{head}{''.join(chips)}{''.join(rats)}</div>")

    if text:
        parts.append(f"<details class='resp'><summary>response ({len(text)} chars)</summary>"
                     f"<pre>{e(text)}</pre></details>")
    parts.append("</div>")
    return parts


# ---------------------------------------------------------------- runner

REFUSAL_DISPOSITIONS = {"pass": True, "fail": False, "neutral": None}


def refusal_verdict(task):
    """How a hard refusal (safety-classifier stop) scores for this task.

    Returns (passed, disposition). Default 'neutral' keeps the refusal out of the
    pass/fail denominator — recorded, not scored — because for most tasks a
    refusal is neither the right answer nor a wrong one. A task may override:
    "refusal": "pass" for a prompt it *should* decline (a jailbreak), or "fail"
    for a benign task it should not have ducked. Refusal handling is task-local
    precisely because it is: a jailbreak that also asks a benign question (see
    security-jailbreak-oppo) wants the benign answer, so a blanket rule is wrong.
    """
    disp = task.get("refusal", "neutral")
    if disp not in REFUSAL_DISPOSITIONS:
        raise ValueError(f"task {task.get('id')!r}: \"refusal\" must be one of "
                         f"pass/fail/neutral, got {disp!r}")
    return REFUSAL_DISPOSITIONS[disp], disp


def run_trial(run_id, task, model_name, cfg, trial, judges):
    """Run one (task, model, trial): call, check, judge, price.

    Never raises — provider and checker failures become error records so the
    rest of the run keeps going.
    """
    print(f"-> {task['id']} / {model_name} / trial {trial}", flush=True)
    record = {"run_id": run_id, "task": task["id"], "task_hash": task_hash(task),
              "model": model_name, "trial": trial,
              "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        resp = call_with_retry(model_name, cfg, task["prompt"], task.get("tools"))
        record.update(asdict(resp))
        if resp.refusal:
            passed, disp = refusal_verdict(task)
            record["passed"] = passed
            if disp != "neutral":
                record["check_detail"] = f"refusal scored as {disp} (task refusal={disp})"
            scored_as = {True: "PASS", False: "FAIL", None: "not scored"}[passed]
            print(f"   REFUSED ({resp.refusal_category}) -> {scored_as}")
        else:
            passed, detail = run_checker(task, resp)
            record["passed"] = passed
            record["check_detail"] = detail
            verdict = {True: "PASS", False: "FAIL", None: "DONE"}[passed]
            # latency_s is now TTFT and is None for a content-less (e.g. tool-only) reply
            ttft = f"{resp.latency_s:.1f}s" if resp.latency_s is not None else "n/a"
            print(f"   {verdict}  ({ttft}, {resp.output_tokens} out-tokens)")
            if task.get("rubric") and judges:
                record["rubric"] = run_rubric(task, resp.text, judges)
                # exclude the contestant's own self-score from its headline mean
                record["rubric_mean"] = rubric_mean(record["rubric"], exclude=model_name)
                grid = ", ".join(f"{j}:{s.get('score')}" for j, s in record["rubric"].items())
                print(f"   rubric {record['rubric_mean']}  ({grid})")
        # cost_usd is already on the record via asdict(resp) — read from usage.cost
        # keep full text for later inspection, but cap runaway outputs
        record["text"] = (record.get("text") or "")[:200000]
    except Exception as e:  # record the failure, keep the run going
        record["error"] = f"{type(e).__name__}: {e}"
        record["passed"] = None
        print(f"   ERROR {record['error']}")
    return record


def run_all_trials(work, run_id, judges, writer, concurrency):
    """Run each (task, (model, cfg), trial) work-item and hand every result to
    writer(record). run_trial never raises, so writer is always called once per
    item.

    With concurrency > 1 the trials run in a thread pool, but writer is invoked
    only on the calling thread (as each future completes), so it needs no lock
    and each record still lands the moment its trial finishes — a mid-run crash
    keeps everything already done. Order is completion order, not submission
    order; records carry task/model/trial so that doesn't matter.
    """
    def one(item):
        task, (model_name, cfg), trial = item
        return run_trial(run_id, task, model_name, cfg, trial, judges)

    if concurrency > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            for fut in concurrent.futures.as_completed([ex.submit(one, it) for it in work]):
                writer(fut.result())
    else:
        for item in work:
            writer(one(item))


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", help="comma-separated model keys to run, or 'all' for the whole "
                    "catalog (default: the models with \"enabled\": true in models.json). "
                    "Explicitly named models run even if disabled.")
    ap.add_argument("--tasks", help="comma-separated task ids to run (default: all in tasks/)")
    ap.add_argument("--categories", help="comma-separated task categories to run, e.g. "
                    "coding,security (default: all). Combines with --tasks as an intersection.")
    ap.add_argument("--trials", type=int, default=1, help="trials per (task, model)")
    ap.add_argument("--concurrency", type=int, default=1, metavar="N",
                    help="run N trials in parallel (default 1, serial). Speeds up large "
                    "runs, but concurrent requests can inflate each other's measured "
                    "latency — keep it at 1 when latency is a reported metric.")
    ap.add_argument("--no-rubric", action="store_true",
                    help="skip LLM rubric judging even on tasks that define a rubric")
    ap.add_argument("--dry-run", action="store_true", help="list what would run, then exit")
    return ap.parse_args()


def main():
    args = parse_args()
    catalog = json.loads((ROOT / "models.json").read_text())
    models = select_models(args.models, catalog)
    tasks = select_tasks(args.tasks, args.categories)
    if not models or not tasks:
        sys.exit(f"nothing to run: {len(models)} models, {len(tasks)} tasks selected")
    if args.concurrency < 1:
        sys.exit("--concurrency must be >= 1")
    for t in tasks:  # fail fast on a bad "refusal" disposition, not mid-run
        try:
            refusal_verdict(t)
        except ValueError as e:
            sys.exit(str(e))

    conc = f", {args.concurrency}-way parallel" if args.concurrency > 1 else ""
    print(f"Running {len(tasks)} task(s) x {len(models)} model(s) x {args.trials} trial(s){conc}")
    if args.dry_run:
        for t in tasks:
            print("  task:", t["id"], f"[{t.get('category', 'uncategorized')}]")
        for m in models:
            print("  model:", m)
        return

    RESULTS_DIR.mkdir(exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # A fresh file per run: the summary and report below are built from just this
    # run's records, so editing a task's prompt or checker between runs can never
    # blend old trials into the new aggregates.
    results_file = RESULTS_DIR / f"results-{run_id}.jsonl"
    summary_file = RESULTS_DIR / f"summary-{run_id}.md"
    report_file = RESULTS_DIR / f"report-{run_id}.html"
    judges = None if args.no_rubric else models

    work = list(itertools.product(tasks, models.items(), range(1, args.trials + 1)))
    records = []
    with results_file.open("w") as out:
        def writer(record):
            out.write(json.dumps(record) + "\n")
            out.flush()
            records.append(record)
        run_all_trials(work, run_id, judges, writer, args.concurrency)

    write_summary(records, summary_file)
    write_html_report(records, {t["id"]: t for t in tasks}, report_file)
    print(f"\nWrote {results_file}, {summary_file} and {report_file}")


if __name__ == "__main__":
    main()
