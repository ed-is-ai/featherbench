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

WARNING: tasks with a "python_tests" checker EXECUTE model-generated code
locally. Run this harness on an isolated machine (see README).
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TASKS_DIR = ROOT / "tasks"
RESULTS_DIR = ROOT / "results"


# ---------------------------------------------------------------- providers

def call_anthropic(cfg, prompt):
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
    t0 = time.monotonic()
    with client.messages.stream(
        model=cfg["model"],
        max_tokens=cfg.get("max_tokens", 64000),
        output_config={"effort": cfg.get("effort", "high")},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        msg = stream.get_final_message()
    latency = time.monotonic() - t0

    if msg.stop_reason == "refusal":
        category = msg.stop_details.category if msg.stop_details else None
        return {
            "text": "", "refusal": True, "refusal_category": category,
            "stop_reason": "refusal", "latency_s": latency,
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        }

    text = "".join(b.text for b in msg.content if b.type == "text")
    return {
        "text": text, "refusal": False, "stop_reason": msg.stop_reason,
        "latency_s": latency,
        "input_tokens": msg.usage.input_tokens,
        "output_tokens": msg.usage.output_tokens,
    }


def call_openai_responses(cfg, prompt):
    """GPT-5.x via the OpenAI Responses API."""
    from openai import OpenAI
    client = OpenAI()
    t0 = time.monotonic()
    r = client.responses.create(
        model=cfg["model"],
        input=prompt,
        reasoning={"effort": cfg.get("reasoning_effort", "high")},
    )
    latency = time.monotonic() - t0
    return {
        "text": r.output_text, "refusal": False, "stop_reason": r.status,
        "latency_s": latency,
        "input_tokens": r.usage.input_tokens,
        "output_tokens": r.usage.output_tokens,
    }


def call_openai_chat(cfg, prompt):
    """Any OpenAI-compatible chat/completions endpoint (used for GLM-5.2)."""
    from openai import OpenAI
    kwargs = {}
    if "api_key_env" in cfg:
        key = os.environ.get(cfg["api_key_env"])
        if not key:
            raise RuntimeError("environment variable %s is not set" % cfg["api_key_env"])
        kwargs["api_key"] = key
    base_url = os.environ.get(cfg.get("base_url_env", ""), "") or cfg.get("default_base_url")
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    t0 = time.monotonic()
    r = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "user", "content": prompt}],
    )
    latency = time.monotonic() - t0
    return {
        "text": r.choices[0].message.content or "",
        "refusal": False,
        "stop_reason": r.choices[0].finish_reason,
        "latency_s": latency,
        "input_tokens": r.usage.prompt_tokens,
        "output_tokens": r.usage.completion_tokens,
    }


def call_model(name, cfg, prompt):
    provider = cfg["provider"]
    if provider == "anthropic":
        return call_anthropic(cfg, prompt)
    if provider == "openai" and cfg.get("api") == "responses":
        return call_openai_responses(cfg, prompt)
    if provider in ("openai", "openai_compatible"):
        return call_openai_chat(cfg, prompt)
    raise ValueError("unknown provider %r for model %s" % (provider, name))


# ---------------------------------------------------------------- checkers

CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\n(.*?)```", re.S)


def extract_code(text):
    """Return the last fenced code block (models put final code last)."""
    blocks = CODE_BLOCK_RE.findall(text)
    return blocks[-1] if blocks else None


def run_checker(task, text):
    """Return (passed, detail). passed is None when the task has no checker."""
    checker = task.get("checker")
    if not checker:
        return None, "no checker"
    return _check(checker, text)


def _check(checker, text):
    ctype = checker["type"]

    if ctype == "all":
        # Composite: every sub-check must pass. Failure detail names each miss.
        results = [_check(sub, text) for sub in checker["checks"]]
        failures = [detail for ok, detail in results if not ok]
        return (not failures), ("; ".join(failures) if failures else "ok")

    if ctype == "contains":
        values = checker.get("values") or [checker["value"]]
        haystack = text.lower()
        missing = [v for v in values if v.lower() not in haystack]
        return (not missing), ("missing: %r" % missing if missing else "ok")

    if ctype == "not_contains":
        values = checker.get("values") or [checker["value"]]
        haystack = text.lower()
        found = [v for v in values if v.lower() in haystack]
        return (not found), ("forbidden term present: %r" % found if found else "ok")

    if ctype == "regex":
        ok = re.search(checker["pattern"], text, re.S) is not None
        label = checker.get("label", checker["pattern"])
        return ok, ("matched" if ok else "no match: %s" % label)

    if ctype == "python_tests":
        code = extract_code(text)
        if code is None:
            return False, "no code block in response"
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "solution.py").write_text(code)
            (Path(d) / "run_tests.py").write_text(checker["test_code"])
            try:
                proc = subprocess.run(
                    [sys.executable, "run_tests.py"],
                    cwd=d, capture_output=True, text=True,
                    timeout=checker.get("timeout_s", 30),
                )
            except subprocess.TimeoutExpired:
                return False, "tests timed out"
            if proc.returncode == 0:
                return True, "tests passed"
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
            return False, "tests failed: %s" % tail

    raise ValueError("unknown checker type %r" % ctype)


# ---------------------------------------------------------------- run + report

def cost_usd(cfg, input_tokens, output_tokens):
    p = cfg.get("pricing_per_mtok") or {}
    if p.get("input") is None or p.get("output") is None:
        return None
    return input_tokens / 1e6 * p["input"] + output_tokens / 1e6 * p["output"]


def load_tasks(task_filter):
    tasks = []
    for f in sorted(TASKS_DIR.glob("*.json")):
        task = json.loads(f.read_text())
        task["id"] = task.get("id", f.stem)
        if not task_filter or task["id"] in task_filter:
            tasks.append(task)
    return tasks


def write_summary(records, models):
    """Aggregate all records (including past runs) into results/summary.md."""
    by_model = {}
    for r in records:
        by_model.setdefault(r["model"], []).append(r)

    lines = ["# Eval summary", "",
             "Generated %s. All models run through the same harness — scores are cross-comparable." %
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "",
             "| Model | Trials | Pass | Fail | Refusals | Errors | Pass rate | Median latency (s) | Total out-tokens | Cost (USD) |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for model in sorted(by_model):
        rs = by_model[model]
        passed = sum(1 for r in rs if r.get("passed") is True)
        failed = sum(1 for r in rs if r.get("passed") is False)
        refusals = sum(1 for r in rs if r.get("refusal"))
        errors = sum(1 for r in rs if r.get("error"))
        scored = passed + failed
        rate = "%.0f%%" % (100.0 * passed / scored) if scored else "—"
        lats = [r["latency_s"] for r in rs if r.get("latency_s") is not None]
        med = "%.1f" % statistics.median(lats) if lats else "—"
        out_tok = sum(r.get("output_tokens") or 0 for r in rs)
        costs = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        cost = "%.2f" % sum(costs) if costs else "n/a"
        lines.append("| %s | %d | %d | %d | %d | %d | %s | %s | %s | %s |" %
                     (model, len(rs), passed, failed, refusals, errors, rate, med, out_tok, cost))

    lines += ["", "## Per task", "",
              "| Task | " + " | ".join(sorted(by_model)) + " |",
              "|---|" + "---|" * len(by_model)]
    task_ids = sorted({r["task"] for r in records})
    for tid in task_ids:
        row = ["| %s " % tid]
        for model in sorted(by_model):
            rs = [r for r in by_model[model] if r["task"] == tid]
            passed = sum(1 for r in rs if r.get("passed") is True)
            scored = sum(1 for r in rs if r.get("passed") in (True, False))
            cell = "%d/%d" % (passed, scored) if scored else ("refused" if any(r.get("refusal") for r in rs) else "—")
            row.append("| %s " % cell)
        lines.append("".join(row) + "|")

    (RESULTS_DIR / "summary.md").write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", help="comma-separated model keys (default: all in models.json)")
    ap.add_argument("--tasks", help="comma-separated task ids (default: all in tasks/)")
    ap.add_argument("--trials", type=int, default=1, help="trials per (task, model)")
    ap.add_argument("--dry-run", action="store_true", help="list what would run, then exit")
    args = ap.parse_args()

    all_models = json.loads((ROOT / "models.json").read_text())
    model_filter = set(args.models.split(",")) if args.models else None
    models = {k: v for k, v in all_models.items() if not model_filter or k in model_filter}
    tasks = load_tasks(set(args.tasks.split(",")) if args.tasks else None)
    if not models or not tasks:
        sys.exit("nothing to run: %d models, %d tasks selected" % (len(models), len(tasks)))

    print("Running %d task(s) x %d model(s) x %d trial(s)" % (len(tasks), len(models), args.trials))
    if args.dry_run:
        for t in tasks:
            print("  task:", t["id"])
        for m in models:
            print("  model:", m)
        return

    RESULTS_DIR.mkdir(exist_ok=True)
    results_file = RESULTS_DIR / "results.jsonl"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with results_file.open("a") as out:
        for task in tasks:
            for model_name, cfg in models.items():
                for trial in range(args.trials):
                    label = "%s / %s / trial %d" % (task["id"], model_name, trial + 1)
                    print("-> " + label, flush=True)
                    record = {
                        "run_id": run_id, "task": task["id"], "model": model_name,
                        "trial": trial + 1,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    try:
                        resp = call_model(model_name, cfg, task["prompt"])
                        record.update(resp)
                        if resp.get("refusal"):
                            record["passed"] = None
                            print("   REFUSED (%s)" % resp.get("refusal_category"))
                        else:
                            passed, detail = run_checker(task, resp["text"])
                            record["passed"] = passed
                            record["check_detail"] = detail
                            print("   %s  (%.1fs, %s out-tokens)" % (
                                {True: "PASS", False: "FAIL", None: "DONE"}[passed],
                                resp["latency_s"], resp.get("output_tokens")))
                        record["cost_usd"] = cost_usd(
                            cfg, record.get("input_tokens") or 0, record.get("output_tokens") or 0)
                        # keep full text for later inspection, but cap runaway outputs
                        record["text"] = (record.get("text") or "")[:200000]
                    except Exception as e:  # record the failure, keep the run going
                        record["error"] = "%s: %s" % (type(e).__name__, e)
                        record["passed"] = None
                        print("   ERROR %s" % record["error"])
                    out.write(json.dumps(record) + "\n")
                    out.flush()

    all_records = [json.loads(line) for line in results_file.read_text().splitlines() if line.strip()]
    write_summary(all_records, models)
    print("\nWrote %s and %s" % (results_file, RESULTS_DIR / "summary.md"))


if __name__ == "__main__":
    main()
