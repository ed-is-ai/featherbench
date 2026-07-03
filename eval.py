#!/usr/bin/env python3
"""Cross-model eval harness: GLM-5.2 vs GPT-5.5 vs Claude Fable 5.

Runs every task in tasks/ against every model in models.json, N trials each,
records per-trial results to results/results.jsonl and writes a summary table
to results/summary.md.

All models run through this same harness with the same prompts and the same
checkers, so scores are directly comparable — unlike vendor-reported
benchmark numbers produced on different scaffolds.

Usage:
    python3 eval.py                          # all tasks x all models, 1 trial
    python3 eval.py --trials 3               # 3 trials per (task, model)
    python3 eval.py --models fable-5,glm-5.2 --tasks csv-dedupe
    python3 eval.py --dry-run                # list what would run

Layout (one file, five layers):
    providers  — (cfg, prompt, tools) -> ModelResponse, registered by @provider
    checkers   — (spec, text, tool_calls) -> (passed, detail), registered by @checker
    rubric     — optional cross-judged LLM scoring
    reports    — results/summary.md + results/report.html
    runner     — CLI, selection, and the task x model x trial loop

WARNING: tasks with a "python_tests" checker EXECUTE model-generated code
locally. Run this harness on an isolated machine (see README).
"""
import argparse
import html
import itertools
import json
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
# A provider is a function (cfg, prompt, tools) -> ModelResponse, registered
# under models.json's (provider, api) pair. Adding a provider family is one
# decorated function; nothing downstream changes.

@dataclass
class ModelResponse:
    """The normalized result every provider returns. Checkers, the rubric and
    the reports only ever see this shape, never a raw SDK response."""
    text: str = ""
    refusal: bool = False
    refusal_category: str = None
    stop_reason: str = None
    tool_calls: list = field(default_factory=list)
    input_tokens: int = None
    output_tokens: int = None
    latency_s: float = None  # stamped by call_model


PROVIDERS = {}


def provider(name, api=None):
    """Register a provider function under (provider, api). The api=None entry
    doubles as the fallback for configs whose "api" has no exact match."""
    def register(fn):
        PROVIDERS[(name, api)] = fn
        return fn
    return register


def call_model(name, cfg, prompt, tools=None):
    """Dispatch to the registered provider and stamp wall-clock latency."""
    fn = PROVIDERS.get((cfg["provider"], cfg.get("api"))) or PROVIDERS.get((cfg["provider"], None))
    if fn is None:
        raise ValueError(f"unknown provider {cfg['provider']!r} for model {name}")
    t0 = time.monotonic()
    resp = fn(cfg, prompt, tools)
    resp.latency_s = time.monotonic() - t0
    return resp


@provider("anthropic")
def call_anthropic(cfg, prompt, tools=None):
    """Claude Fable 5 via the official anthropic SDK.

    Fable specifics: thinking is always on (the `thinking` param must be
    omitted), sampling params are not accepted, and depth is controlled with
    output_config.effort. Streaming is used because hard tasks can run for
    minutes and non-streaming requests hit HTTP timeouts.

    Deliberately NO `fallbacks` parameter: in production you would opt in so a
    safety-classifier refusal is transparently re-served by Opus 4.8, but in
    an eval that would silently score another model's output as Fable's.
    Refusals are recorded as a result instead.
    """
    import anthropic
    client = anthropic.Anthropic()
    kwargs = dict(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 64000),
        messages=[{"role": "user", "content": prompt}],
    )
    # effort is GA on Fable/Opus 4.5+/Sonnet 4.6+ but errors on Haiku 4.5 and
    # older models — omit `effort` from those models' config to skip it.
    if cfg.get("effort"):
        kwargs["output_config"] = {"effort": cfg["effort"]}
    if tools:
        # Neutral {name, description, parameters} -> Anthropic tool shape.
        kwargs["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                            "input_schema": t["parameters"]} for t in tools]
    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    usage = dict(input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
    if msg.stop_reason == "refusal":
        return ModelResponse(
            refusal=True, stop_reason="refusal",
            refusal_category=msg.stop_details.category if msg.stop_details else None,
            **usage)
    return ModelResponse(
        text="".join(b.text for b in msg.content if b.type == "text"),
        # tool_use blocks carry .input already parsed into a dict.
        tool_calls=[{"name": b.name, "arguments": b.input}
                    for b in msg.content if b.type == "tool_use"],
        stop_reason=msg.stop_reason, **usage)


@provider("openai", api="responses")
def call_openai_responses(cfg, prompt, tools=None):
    """GPT-5.x via the OpenAI Responses API."""
    from openai import OpenAI
    client = OpenAI()
    kwargs = dict(model=cfg["model"], input=prompt)
    # Reasoning effort applies to reasoning models (o-series, gpt-5.x); omit it
    # from config for non-reasoning models (gpt-4.1/4o) where it would error.
    if cfg.get("reasoning_effort"):
        kwargs["reasoning"] = {"effort": cfg["reasoning_effort"]}
    if tools:
        # Responses API function tools are flat: {type, name, description, parameters}.
        kwargs["tools"] = [{"type": "function", "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t["parameters"]} for t in tools]
    r = client.responses.create(**kwargs)
    return ModelResponse(
        text=r.output_text, stop_reason=r.status,
        tool_calls=[{"name": item.name, "arguments": _json_args(item.arguments)}
                    for item in (r.output or [])
                    if getattr(item, "type", None) == "function_call"],
        input_tokens=r.usage.input_tokens, output_tokens=r.usage.output_tokens)


@provider("openai")
@provider("openai_compatible")
def call_openai_chat(cfg, prompt, tools=None):
    """Any OpenAI-compatible chat/completions endpoint (used for GLM-5.2)."""
    from openai import OpenAI
    client = OpenAI(**_client_kwargs(cfg))
    kwargs = dict(model=cfg["model"], messages=[{"role": "user", "content": prompt}])
    if tools:
        # Chat/completions nests the schema under a "function" key.
        kwargs["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t["parameters"]}} for t in tools]
    r = client.chat.completions.create(**kwargs)
    m = r.choices[0].message
    return ModelResponse(
        text=m.content or "", stop_reason=r.choices[0].finish_reason,
        tool_calls=[{"name": tc.function.name, "arguments": _json_args(tc.function.arguments)}
                    for tc in (getattr(m, "tool_calls", None) or [])],
        input_tokens=r.usage.prompt_tokens, output_tokens=r.usage.completion_tokens)


def _client_kwargs(cfg):
    """API key and base URL for OpenAI-compatible endpoints, resolved from the
    environment variables named in the model config."""
    kwargs = {}
    if "api_key_env" in cfg:
        key = os.environ.get(cfg["api_key_env"])
        if not key:
            raise RuntimeError(f"environment variable {cfg['api_key_env']} is not set")
        kwargs["api_key"] = key
    base_url = os.environ.get(cfg.get("base_url_env", ""), "") or cfg.get("default_base_url")
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


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
    haystack = text.lower()
    missing = [v for v in _wanted_values(spec) if v.lower() not in haystack]
    return (not missing), (f"missing: {missing!r}" if missing else "ok")


@checker("not_contains")
def check_not_contains(spec, text, tool_calls):
    haystack = text.lower()
    found = [v for v in _wanted_values(spec) if v.lower() in haystack]
    return (not found), (f"forbidden term present: {found!r}" if found else "ok")


def _wanted_values(spec):
    return spec.get("values") or [spec["value"]]


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


CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\n(.*?)```", re.S)


def extract_code(text):
    """Return the last fenced code block (models put final code last)."""
    blocks = CODE_BLOCK_RE.findall(text)
    return blocks[-1] if blocks else None


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


def run_rubric(task, answer, judges):
    """Every judge model scores the answer blind. Returns {judge: {score, rationale}}.

    Using all three contestants as judges makes judge bias measurable (the
    summary prints a judge x contestant matrix) instead of hidden behind a
    single 'neutral' judge that is actually one of the contestants.
    """
    criteria = "\n".join("- " + c for c in task["rubric"]["criteria"])
    prompt = JUDGE_PROMPT.format(task_prompt=task["prompt"], answer=answer, criteria=criteria)
    return {name: _judge_once(name, cfg, prompt) for name, cfg in judges.items()}


def _judge_once(judge_name, cfg, prompt):
    try:
        reply = call_model(judge_name, cfg, prompt).text or ""
        m = re.search(r"\{.*\}", reply, re.S)
        if not m:
            return {"score": None, "error": "no JSON in judge reply"}
        data = json.loads(m.group(0))
        score = data.get("score")
        return {"score": int(score) if score is not None else None,
                "rationale": str(data.get("rationale", ""))[:300]}
    except Exception as e:
        return {"score": None, "error": f"{type(e).__name__}: {e}"[:200]}


def rubric_mean(scores):
    vals = [s["score"] for s in (scores or {}).values() if s.get("score") is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


# ---------------------------------------------------------------- tasks + models

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
    """--tasks names ids, --categories names categories; they intersect."""
    tasks = load_tasks({t.strip() for t in tasks_spec.split(",")} if tasks_spec else None)
    if categories_spec:
        wanted = {c.strip() for c in categories_spec.split(",") if c.strip()}
        available = {t.get("category", "uncategorized") for t in load_tasks(None)}
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


def md_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return lines


def write_summary(records):
    """Aggregate all records (including past runs) into results/summary.md."""
    by_model = group_by(records, "model")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = ["# Eval summary", "",
             f"Generated {stamp}. All models run through the same harness — scores are cross-comparable.",
             ""]
    lines += _overall_section(by_model)
    lines += _per_task_section(records, by_model)
    lines += _category_section(records, by_model)
    lines += _bias_section(records)
    (RESULTS_DIR / "summary.md").write_text("\n".join(lines) + "\n")


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
            f"{100.0 * passed / scored:.0f}%" if scored else "—",
            f"{sum(rubs) / len(rubs):.1f}" if rubs else "—",
            f"{statistics.median(lats):.1f}" if lats else "—",
            sum(r.get("output_tokens") or 0 for r in rs),
            f"{sum(costs):.2f}" if costs else "n/a",
        ])
    return md_table(["Model", "Trials", "Pass", "Fail", "Refusals", "Errors", "Pass rate",
                     "Rubric /10", "Median latency (s)", "Total out-tokens", "Cost (USD)"], rows)


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


def write_html_report(records, tasks_by_id):
    """Render results/report.html: a self-contained, filterable review page.

    Addresses the 'inspect results.jsonl for ...' step every rubric-less task
    leans on — groups every trial under its task, with pass/fail, refusals,
    rubric scores + rationales, cost/latency, and the full response text one
    click away. No external assets, so it opens straight from disk.
    """
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
    (RESULTS_DIR / "report.html").write_text("".join(parts))


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
        bits.append(f"{r['latency_s']:.1f}s")
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

def cost_usd(cfg, input_tokens, output_tokens):
    p = cfg.get("pricing_per_mtok") or {}
    if p.get("input") is None or p.get("output") is None:
        return None
    return input_tokens / 1e6 * p["input"] + output_tokens / 1e6 * p["output"]


def run_trial(run_id, task, model_name, cfg, trial, judges):
    """Run one (task, model, trial): call, check, judge, price.

    Never raises — provider and checker failures become error records so the
    rest of the run keeps going.
    """
    print(f"-> {task['id']} / {model_name} / trial {trial}", flush=True)
    record = {"run_id": run_id, "task": task["id"], "model": model_name,
              "trial": trial, "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        resp = call_model(model_name, cfg, task["prompt"], task.get("tools"))
        record.update(asdict(resp))
        if resp.refusal:
            record["passed"] = None
            print(f"   REFUSED ({resp.refusal_category})")
        else:
            passed, detail = run_checker(task, resp)
            record["passed"] = passed
            record["check_detail"] = detail
            verdict = {True: "PASS", False: "FAIL", None: "DONE"}[passed]
            print(f"   {verdict}  ({resp.latency_s:.1f}s, {resp.output_tokens} out-tokens)")
            if task.get("rubric") and judges:
                record["rubric"] = run_rubric(task, resp.text, judges)
                record["rubric_mean"] = rubric_mean(record["rubric"])
                grid = ", ".join(f"{j}:{s.get('score')}" for j, s in record["rubric"].items())
                print(f"   rubric {record['rubric_mean']}  ({grid})")
        record["cost_usd"] = cost_usd(cfg, record.get("input_tokens") or 0,
                                      record.get("output_tokens") or 0)
        # keep full text for later inspection, but cap runaway outputs
        record["text"] = (record.get("text") or "")[:200000]
    except Exception as e:  # record the failure, keep the run going
        record["error"] = f"{type(e).__name__}: {e}"
        record["passed"] = None
        print(f"   ERROR {record['error']}")
    return record


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

    print(f"Running {len(tasks)} task(s) x {len(models)} model(s) x {args.trials} trial(s)")
    if args.dry_run:
        for t in tasks:
            print("  task:", t["id"], f"[{t.get('category', 'uncategorized')}]")
        for m in models:
            print("  model:", m)
        return

    RESULTS_DIR.mkdir(exist_ok=True)
    results_file = RESULTS_DIR / "results.jsonl"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    judges = None if args.no_rubric else models

    with results_file.open("a") as out:
        for task, (model_name, cfg), trial in itertools.product(
                tasks, models.items(), range(1, args.trials + 1)):
            record = run_trial(run_id, task, model_name, cfg, trial, judges)
            out.write(json.dumps(record) + "\n")
            out.flush()

    all_records = [json.loads(line) for line in results_file.read_text().splitlines() if line.strip()]
    write_summary(all_records)
    write_html_report(all_records, {t["id"]: t for t in tasks})
    print(f"\nWrote {results_file}, {RESULTS_DIR / 'summary.md'} and {RESULTS_DIR / 'report.html'}")


if __name__ == "__main__":
    main()
