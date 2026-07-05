#!/usr/bin/env python3
"""Unit tests for the eval.py harness — no network, no API keys needed.

    python3 -m unittest test_eval -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import eval as harness
from eval import ModelResponse

PASSING_CODE = "Here you go:\n```python\ndef add(a, b):\n    return a + b\n```"


# --- fake OpenRouter stream chunks (duck-typed; no SDK, no network) ------------
# reduce_stream only touches .usage, .choices, choices[0].delta(.content/.reasoning/
# .tool_calls), .finish_reason and .native_finish_reason — so SimpleNamespace is enough.

def _content_chunk(content=None, reasoning=None, tool_calls=None,
                   finish_reason=None, native_finish_reason=None):
    delta = NS(content=content, reasoning=reasoning, tool_calls=tool_calls)
    ch = NS(delta=delta, finish_reason=finish_reason,
            native_finish_reason=native_finish_reason)
    return NS(choices=[ch], usage=None)


def _usage_chunk(cost=None, prompt_tokens=None, completion_tokens=None):
    """The final usage-only chunk: empty .choices, a populated .usage."""
    return NS(choices=[], usage=NS(cost=cost, prompt_tokens=prompt_tokens,
                                   completion_tokens=completion_tokens))


class TestCheckers(unittest.TestCase):
    def check(self, spec, text="", tool_calls=()):
        return harness.run_check(spec, text, tool_calls)

    def test_contains(self):
        self.assertTrue(self.check({"type": "contains", "values": ["Paris"]}, "in PARIS.")[0])
        self.assertFalse(self.check({"type": "contains", "value": "Nottingham"}, "London")[0])

    def test_not_contains(self):
        self.assertTrue(self.check({"type": "not_contains", "value": "cure"}, "see a doctor")[0])
        self.assertFalse(self.check({"type": "not_contains", "value": "cure"}, "a Cure!")[0])

    def test_contains_whole_word(self):
        self.assertTrue(self.check({"type": "not_contains", "value": "kill",
                                    "whole_word": True}, "a useful skill")[0])
        self.assertFalse(self.check({"type": "not_contains", "value": "kill",
                                     "whole_word": True}, "do not kill")[0])
        # default is substring, so 'kill' still matches inside 'skill'
        self.assertFalse(self.check({"type": "not_contains", "value": "kill"},
                                    "a useful skill")[0])

    def test_regex(self):
        self.assertTrue(self.check({"type": "regex", "pattern": r"\d+ km"}, "42 km")[0])
        self.assertFalse(self.check({"type": "regex", "pattern": r"^\d+$"}, "nope")[0])

    def test_tool_called_with_loose_args(self):
        calls = [{"name": "get_weather", "arguments": {"city": "Paris, France", "days": "2"}}]
        spec = {"type": "tool_called", "tool": "get_weather", "args": {"city": "paris", "days": 2}}
        self.assertTrue(self.check(spec, tool_calls=calls)[0])
        spec["args"] = {"city": "Tokyo"}
        self.assertFalse(self.check(spec, tool_calls=calls)[0])

    def test_tool_not_called(self):
        spec = {"type": "tool_not_called", "tool": "send_email"}
        self.assertTrue(self.check(spec, tool_calls=[])[0])
        self.assertFalse(self.check(spec, tool_calls=[{"name": "send_email"}])[0])

    def test_all_composite_reports_each_failure(self):
        spec = {"type": "all", "checks": [
            {"type": "contains", "value": "alpha"},
            {"type": "not_contains", "value": "beta"},
        ]}
        ok, detail = self.check(spec, "alpha and beta")
        self.assertFalse(ok)
        self.assertIn("beta", detail)
        self.assertTrue(self.check(spec, "alpha only")[0])

    def test_python_tests(self):
        ok, detail = self.check(
            {"type": "python_tests", "test_code": "from solution import add\nassert add(2, 3) == 5\n"},
            PASSING_CODE)
        self.assertTrue(ok)
        ok, detail = self.check(
            {"type": "python_tests", "test_code": "from solution import add\nassert add(2, 3) == 6\n"},
            PASSING_CODE)
        self.assertFalse(ok)
        self.assertIn("AssertionError", detail)
        ok, detail = self.check({"type": "python_tests", "test_code": ""}, "no code here")
        self.assertEqual((ok, detail), (False, "no code block in response"))

    def test_python_tests_strips_environment(self):
        # model-generated code must not see the harness's API keys
        leaky = "```python\nimport os\nSECRET = os.environ.get('FAKE_API_KEY')\n```"
        with mock.patch.dict("os.environ", {"FAKE_API_KEY": "sk-secret"}):
            ok, _ = self.check(
                {"type": "python_tests",
                 "test_code": "from solution import SECRET\nassert SECRET is None\n"},
                leaky)
        self.assertTrue(ok)

    def test_extract_code_prefers_last_python_block(self):
        self.assertEqual(harness.extract_code("```python\nfirst\n```\n```py\nsecond\n```"),
                         "second\n")
        self.assertIsNone(harness.extract_code("no code"))

    def test_extract_code_ignores_trailing_untagged_example(self):
        # the real solution is python-tagged; a trailing bare example must not win
        text = ("```python\ndef add(a, b):\n    return a + b\n```\n"
                "Example:\n```\n>>> add(2, 3)\n5\n```")
        self.assertIn("def add", harness.extract_code(text))

    def test_extract_code_falls_back_to_largest_when_untagged(self):
        text = "```\nx = 1\n```\ntiny\n```\nx = 1\ny = 2\nz = 3\n```"
        self.assertEqual(harness.extract_code(text), "x = 1\ny = 2\nz = 3\n")


class TestRubric(unittest.TestCase):
    def test_rubric_mean(self):
        self.assertEqual(harness.rubric_mean({"a": {"score": 8}, "b": {"score": 7},
                                              "c": {"score": None}}), 7.5)
        self.assertIsNone(harness.rubric_mean({}))

    def test_rubric_mean_excludes_self_score(self):
        scores = {"m1": {"score": 10}, "m2": {"score": 6}, "m3": {"score": 8}}
        # m1's own self-score of 10 is dropped from m1's headline mean
        self.assertEqual(harness.rubric_mean(scores, exclude="m1"), 7.0)
        # no independent judge (only the contestant scored) -> None, not the self-score
        self.assertIsNone(harness.rubric_mean({"m1": {"score": 10}}, exclude="m1"))

    def test_run_rubric_caps_answer_before_judging(self):
        task = {"prompt": "p", "rubric": {"criteria": ["c"]}}
        seen = {}
        def fake(name, cfg, prompt, tools=None):
            seen["prompt"] = prompt
            return ModelResponse(text='{"score": 5, "rationale": "r"}')
        with mock.patch.object(harness, "call_model", side_effect=fake):
            harness.run_rubric(task, "X" * 200_000, {"j": {}})
        self.assertLess(len(seen["prompt"]), harness.JUDGE_ANSWER_CAP + 2000)
        self.assertNotIn("X" * 50_000, seen["prompt"])

    def test_judge_once(self):
        reply = ModelResponse(text='Sure.\n{"score": 7, "rationale": "decent"}')
        with mock.patch.object(harness, "call_model", return_value=reply):
            self.assertEqual(harness._judge_once("j", {}, "p"),
                             {"score": 7, "rationale": "decent"})
        with mock.patch.object(harness, "call_model", side_effect=RuntimeError("boom")):
            out = harness._judge_once("j", {}, "p")
        self.assertIsNone(out["score"])
        self.assertIn("boom", out["error"])


class TestProvidersAndSelection(unittest.TestCase):
    def test_single_path_routes_through_call_openrouter(self):
        # every model now goes through the one call_openrouter path; there is no
        # (provider, api) registry left to dispatch on.
        sentinel = ModelResponse(text="routed")
        cfg = {"model": "openai/gpt-5.5", "provider_order": ["openai"]}
        with mock.patch.object(harness, "call_openrouter", return_value=sentinel) as co:
            resp = harness.call_model("m", cfg, "hi")
        self.assertIs(resp, sentinel)
        co.assert_called_once_with(cfg, "hi", None)
        for gone in ("PROVIDERS", "provider", "_client_kwargs", "cost_usd"):
            self.assertFalse(hasattr(harness, gone), f"{gone} should be removed")
        # the three direct-SDK provider fns are gone too (names built from parts so
        # this file carries no literal reference to the retired symbols)
        for suffix in ("anthropic", "openai_responses", "openai_chat"):
            self.assertFalse(hasattr(harness, "call_" + suffix),
                             f"call_{suffix} should be removed")

    def test_is_rate_limit(self):
        class RateLimitError(Exception):
            status_code = 429
        self.assertTrue(harness._is_rate_limit(RateLimitError()))            # status_code 429
        self.assertTrue(harness._is_rate_limit(Exception("Rate limit reached")))  # message fallback
        self.assertFalse(harness._is_rate_limit(ValueError("bad request")))

    def test_call_with_retry_backs_off_then_succeeds(self):
        class RateLimitError(Exception):
            status_code = 429
        ok = ModelResponse(text="ok")
        with mock.patch.object(harness, "call_model",
                               side_effect=[RateLimitError(), RateLimitError(), ok]), \
             mock.patch("time.sleep") as sleep, mock.patch("builtins.print"):
            resp = harness.call_with_retry("m", {}, "p")
        self.assertIs(resp, ok)
        self.assertEqual(sleep.call_count, 2)

    def test_call_with_retry_reraises_after_giving_up(self):
        class RateLimitError(Exception):
            status_code = 429
        with mock.patch.object(harness, "call_model", side_effect=RateLimitError()), \
             mock.patch("time.sleep"), mock.patch("builtins.print"):
            with self.assertRaises(RateLimitError):
                harness.call_with_retry("m", {}, "p", retries=2)

    def test_call_with_retry_does_not_retry_other_errors(self):
        with mock.patch.object(harness, "call_model", side_effect=ValueError("nope")) as cm, \
             mock.patch("time.sleep"):
            with self.assertRaises(ValueError):
                harness.call_with_retry("m", {}, "p")
        self.assertEqual(cm.call_count, 1)  # no retries on non-rate-limit errors

    def test_select_models(self):
        catalog = {"on": {"enabled": True}, "off": {"enabled": False}}
        self.assertEqual(set(harness.select_models(None, catalog)), {"on"})
        self.assertEqual(set(harness.select_models("all", catalog)), {"on", "off"})
        self.assertEqual(set(harness.select_models("off", catalog)), {"off"})  # explicit wins
        with self.assertRaises(SystemExit):
            harness.select_models("nope", catalog)

    def test_select_tasks_unknown_id_or_category_exits(self):
        with self.assertRaises(SystemExit):
            harness.select_tasks("no-such-task-xyz", None)
        with self.assertRaises(SystemExit):
            harness.select_tasks(None, "no-such-category-xyz")

    def test_cost_comes_from_the_stream_not_a_price_table(self):
        # cost is no longer computed from a pricing table; it arrives on the
        # response via reduce_stream reading usage.cost from the final chunk.
        self.assertFalse(hasattr(harness, "cost_usd"))
        r = harness.reduce_stream(
            [_usage_chunk(cost=0.42, prompt_tokens=3, completion_tokens=4)],
            t0=0.0, now=lambda: 1.0)
        self.assertEqual(r["cost"], 0.42)

    def test_task_hash_tracks_scoring_fields_only(self):
        a = {"id": "t", "prompt": "p", "checker": {"type": "contains", "value": "x"}}
        # id/category/description are cosmetic -> same hash
        self.assertEqual(harness.task_hash(a),
                         harness.task_hash(dict(a, id="other", category="c", description="d")))
        # prompt or checker change -> different hash
        self.assertNotEqual(harness.task_hash(a), harness.task_hash(dict(a, prompt="q")))
        self.assertNotEqual(harness.task_hash(a),
                            harness.task_hash(dict(a, checker={"type": "contains", "value": "y"})))

    def test_wilson_interval(self):
        self.assertIsNone(harness.wilson_interval(0, 0))
        lo, hi = harness.wilson_interval(1, 1)       # 1/1: wide, capped at 1.0
        self.assertAlmostEqual(hi, 1.0)
        self.assertLess(lo, 0.5)
        lo, hi = harness.wilson_interval(50, 100)     # 50/100: tight and centered
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertGreater(hi - lo, 0.15)             # still ~±10 points at n=100
        self.assertEqual(harness.pass_rate_cell(0, 0), "—")
        self.assertTrue(harness.pass_rate_cell(2, 3).startswith("67% ["))


FIXTURE_RECORDS = [
    {"run_id": "r", "trial": 1, "task": "t-code", "model": "m1", "refusal": False,
     "passed": True, "check_detail": "tests passed", "latency_s": 12.3,
     "wall_clock_s": 45.6, "output_tokens": 900, "cost_usd": 0.049, "text": "ok"},
    {"run_id": "r", "trial": 1, "task": "t-code", "model": "m2", "refusal": False,
     "passed": False, "check_detail": "tests failed", "latency_s": 8.0,
     "output_tokens": 700, "text": "<b>bold</b> claim",
     "rubric": {"m1": {"score": 9, "rationale": "solid"}}, "rubric_mean": 9.0},
    {"run_id": "r", "trial": 1, "task": "t-refuse", "model": "m1", "refusal": True,
     "refusal_category": "dangerous_content", "passed": None, "latency_s": 2.0, "text": ""},
    {"run_id": "r", "trial": 1, "task": "t-error", "model": "m2", "refusal": False,
     "passed": None, "error": "APIError: boom"},
]


class TestReports(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(setattr, harness, "RESULTS_DIR", harness.RESULTS_DIR)
        harness.RESULTS_DIR = Path(tmp.name)

    def test_summary(self):
        harness.write_summary(FIXTURE_RECORDS)
        md = (harness.RESULTS_DIR / "summary.md").read_text()
        self.assertIn("| m1 | 2 | 1 | 0 | 1 | 0 | 100% [", md)  # pass/refusal tallies + CI
        self.assertIn("| m2 | 2 | 0 | 1 | 0 | 1 | 0% [", md)    # fail/error tallies + CI
        self.assertIn("Wilson interval", md)
        self.assertIn("| t-refuse | refused | — |", md)
        self.assertIn("## Judge bias matrix", md)
        # the latency column is relabelled to TTFT (not "latency") with a
        # time-to-first-content caveat, and cost still populates from cost_usd (D-04/D-05).
        self.assertIn("Median TTFT", md)
        self.assertNotIn("Median latency", md)
        self.assertIn("time-to-first-content", md)
        self.assertIn("0.05", md)  # m1's cost_usd 0.049 renders in the Cost (USD) column

    def test_summary_warns_on_mixed_task_versions(self):
        recs = [dict(FIXTURE_RECORDS[0], task_hash="aaaaaaaaaaaa"),
                dict(FIXTURE_RECORDS[0], task_hash="bbbbbbbbbbbb")]
        with mock.patch("builtins.print"):  # suppress the console warning
            harness.write_summary(recs)
        md = (harness.RESULTS_DIR / "summary.md").read_text()
        self.assertIn("Mixed task versions", md)
        self.assertIn("t-code", md)

    def test_summary_no_warning_for_single_version(self):
        recs = [dict(r, task_hash="samehash1234") for r in FIXTURE_RECORDS]
        harness.write_summary(recs)
        self.assertNotIn("Mixed task versions", (harness.RESULTS_DIR / "summary.md").read_text())

    def test_html_report_renders_and_escapes(self):
        harness.write_html_report(FIXTURE_RECORDS, {})
        page = (harness.RESULTS_DIR / "report.html").read_text()
        for badge in ("PASS", "FAIL", "REFUSED", "ERROR"):
            self.assertIn(badge, page)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", page)  # model text escaped, not injected
        self.assertNotIn("<b>bold</b>", page)
        # wall-clock surfaces alongside TTFT when the record carries wall_clock_s
        self.assertIn("45.6s wall", page)
        self.assertIn("12.3s TTFT", page)


class TestRunTrial(unittest.TestCase):
    TASK = {"id": "t", "prompt": "hi", "checker": {"type": "contains", "value": "pong"}}

    def run_with(self, resp_or_exc):
        kw = ({"side_effect": resp_or_exc} if isinstance(resp_or_exc, Exception)
              else {"return_value": resp_or_exc})
        with mock.patch.object(harness, "call_model", **kw), mock.patch("builtins.print"):
            return harness.run_trial("rid", self.TASK, "m", {}, 1, None)

    def test_pass_and_fail(self):
        rec = self.run_with(ModelResponse(text="pong!", latency_s=1.0, output_tokens=5))
        self.assertIs(rec["passed"], True)
        rec = self.run_with(ModelResponse(text="silence", latency_s=1.0, output_tokens=5))
        self.assertIs(rec["passed"], False)

    def test_refusal_skips_checker(self):
        rec = self.run_with(ModelResponse(refusal=True, refusal_category="cat",
                                          stop_reason="refusal", latency_s=1.0))
        self.assertIsNone(rec["passed"])          # default task disposition is neutral
        self.assertTrue(rec["refusal"])
        self.assertNotIn("check_detail", rec)

    def test_refusal_verdict(self):
        self.assertEqual(harness.refusal_verdict({}), (None, "neutral"))
        self.assertEqual(harness.refusal_verdict({"refusal": "pass"}), (True, "pass"))
        self.assertEqual(harness.refusal_verdict({"refusal": "fail"}), (False, "fail"))
        with self.assertRaises(ValueError):
            harness.refusal_verdict({"id": "t", "refusal": "maybe"})

    def test_refusal_disposition_scores_per_task(self):
        resp = ModelResponse(refusal=True, refusal_category="cat",
                             stop_reason="refusal", latency_s=1.0)
        def run(task):
            with mock.patch.object(harness, "call_model", return_value=resp), \
                 mock.patch("builtins.print"):
                return harness.run_trial("rid", dict(task, prompt="p"), "m", {}, 1, None)
        self.assertIs(run({"id": "t", "refusal": "pass"})["passed"], True)
        self.assertIs(run({"id": "t", "refusal": "fail"})["passed"], False)
        # a scored refusal is still flagged a refusal (visible as REFUSED, but counted)
        self.assertTrue(run({"id": "t", "refusal": "pass"})["refusal"])

    def test_provider_error_becomes_error_record(self):
        rec = self.run_with(RuntimeError("boom"))
        self.assertEqual(rec["error"], "RuntimeError: boom")
        self.assertIsNone(rec["passed"])


class TestRunAllTrials(unittest.TestCase):
    WORK = [(f"task{i}", ("m", {}), 1) for i in range(6)]

    def _run(self, concurrency):
        written = []
        def stub(run_id, task, model, cfg, trial, judges):
            return {"task": task, "model": model, "trial": trial}
        with mock.patch.object(harness, "run_trial", side_effect=stub):
            harness.run_all_trials(self.WORK, "rid", None, written.append, concurrency)
        return written

    def test_serial_covers_every_workitem(self):
        written = self._run(concurrency=1)
        self.assertEqual([w["task"] for w in written], [f"task{i}" for i in range(6)])

    def test_parallel_covers_every_workitem(self):
        written = self._run(concurrency=3)  # order may differ, coverage must not
        self.assertEqual({w["task"] for w in written}, {f"task{i}" for i in range(6)})
        self.assertEqual(len(written), 6)


class TestOpenRouter(unittest.TestCase):
    """The pure OpenRouter helpers, exercised against synthetic streams — no
    OpenAI client, no OPENROUTER_API_KEY, no network."""

    # --- build_request: routing pin + gated sampling + unified reasoning -------

    def test_build_request_fable_sends_no_sampling(self):
        cfg = {"model": "anthropic/claude-fable-5", "provider_order": ["anthropic"],
               "effort": "high", "max_tokens": 64000}
        kwargs, eb, sent = harness.build_request(cfg, "hi", None)
        self.assertEqual(sent, {})  # Fable accepts no sampling params (Pitfall 6)
        for p in ("temperature", "top_p", "seed"):
            self.assertNotIn(p, kwargs)
        self.assertEqual(eb["reasoning"], {"effort": "high"})
        self.assertEqual(eb["provider"], {"order": ["anthropic"],
                                          "allow_fallbacks": False,
                                          "require_parameters": True})
        self.assertEqual(eb["usage"], {"include": True})
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["stream_options"], {"include_usage": True})

    def test_build_request_gpt_sends_seed_only(self):
        cfg = {"model": "openai/gpt-5.5", "provider_order": ["openai"],
               "effort": "high", "sampling": {"seed": 7}}
        kwargs, eb, sent = harness.build_request(cfg, "hi", None)
        self.assertEqual(sent, {"seed": 7})
        self.assertEqual(kwargs["seed"], 7)
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("top_p", kwargs)
        self.assertEqual(eb["reasoning"], {"effort": "high"})

    def test_build_request_glm_sends_all_sampling_and_no_reasoning(self):
        cfg = {"model": "z-ai/glm-5.2", "provider_order": ["z-ai"],
               "sampling": {"temperature": 0.0, "top_p": 1.0, "seed": 7}}
        kwargs, eb, sent = harness.build_request(cfg, "hi", None)
        self.assertEqual(sent, {"temperature": 0.0, "top_p": 1.0, "seed": 7})
        self.assertEqual((kwargs["temperature"], kwargs["top_p"], kwargs["seed"]),
                         (0.0, 1.0, 7))
        self.assertNotIn("reasoning", eb)  # no effort key -> no reasoning block
        self.assertEqual(eb["provider"]["order"], ["z-ai"])

    def test_build_request_tools_use_chat_shape(self):
        tools = [{"name": "get_weather", "description": "d", "parameters": {"type": "object"}}]
        kwargs, _, _ = harness.build_request(
            {"model": "z-ai/glm-5.2", "provider_order": ["z-ai"]}, "hi", tools)
        self.assertEqual(kwargs["tools"][0]["type"], "function")
        self.assertEqual(kwargs["tools"][0]["function"]["name"], "get_weather")

    # --- reduce_stream: TTFT at first content, text, cost, tool-call folding ----

    def test_reduce_stream_stamps_ttft_at_first_content(self):
        clock = iter([0.5, 4.0])  # -> ttft, then wall
        chunks = [
            _content_chunk(reasoning="thinking..."),  # reasoning-only: no TTFT here
            _content_chunk(content="Hello"),          # first content -> TTFT stamped
            _content_chunk(content=" world", finish_reason="stop",
                           native_finish_reason="stop"),
            _usage_chunk(cost=0.0123, prompt_tokens=11, completion_tokens=7),
        ]
        r = harness.reduce_stream(chunks, t0=0.0, now=lambda: next(clock))
        self.assertEqual(r["text"], "Hello world")
        self.assertAlmostEqual(r["ttft"], 0.5)  # not stamped on the reasoning delta
        self.assertAlmostEqual(r["wall"], 4.0)
        self.assertEqual(r["cost"], 0.0123)
        self.assertEqual((r["input_tokens"], r["output_tokens"]), (11, 7))
        self.assertEqual(r["finish_reason"], "stop")

    def test_reduce_stream_accumulates_tool_call_deltas(self):
        first = NS(index=0, function=NS(name="get_weather", arguments='{"ci'))
        rest = NS(index=0, function=NS(name=None, arguments='ty": "Paris"}'))
        chunks = [
            _content_chunk(tool_calls=[first]),
            _content_chunk(tool_calls=[rest], finish_reason="tool_calls",
                           native_finish_reason="tool_calls"),
            _usage_chunk(prompt_tokens=5, completion_tokens=9),
        ]
        r = harness.reduce_stream(chunks, t0=0.0, now=lambda: 1.0)
        self.assertIsNone(r["ttft"])  # no content delta -> no TTFT
        self.assertEqual(r["tool_calls"],
                         [{"name": "get_weather", "arguments": {"city": "Paris"}}])

    # --- map_refusal: provider signal first ------------------------------------

    def test_map_refusal_detects_hard_refusals(self):
        self.assertTrue(harness.map_refusal("content_filter", None, None)[0])
        self.assertTrue(harness.map_refusal("stop", "refusal", None)[0])
        self.assertTrue(harness.map_refusal("stop", "SAFETY", None)[0])  # case-insensitive
        self.assertFalse(harness.map_refusal("stop", "stop", None)[0])
        self.assertFalse(harness.map_refusal("length", None, None)[0])
        # a structured category surfaces from message.refusal when present
        self.assertEqual(
            harness.map_refusal("content_filter", None, NS(refusal="policy"))[1], "policy")


class TestConfigContract(unittest.TestCase):
    """Bind the *shipped* models.json and security-task JSONs to the pure request
    builder and refusal_verdict — offline, no client, no OPENROUTER_API_KEY. These
    catch config drift that synthetic-fixture unit tests cannot see: a real
    models.json entry that would hand build_request an unsupported sampling param
    (the live Pitfall-6 404), or a refusal scope that has drifted from the
    corrected 4-pass / 2-neutral split (D-02)."""

    SAMPLING_PARAMS = ("temperature", "top_p", "seed")

    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((harness.ROOT / "models.json").read_text())
        cls.enabled = {k: v for k, v in cls.catalog.items() if v.get("enabled")}

    def _load_task(self, stem):
        return json.loads((harness.TASKS_DIR / f"{stem}.json").read_text())

    def test_every_enabled_model_pins_routing_and_gates_sampling(self):
        # For every shipped enabled model, build_request must emit the full routing
        # pin and never leak a sampling param the config did not declare — sending
        # an unsupported param under require_parameters:true routes to no provider
        # and 404s the trial (Pitfall 6). This is the config-level 404 guard.
        self.assertTrue(self.enabled, "expected at least one enabled model")
        for name, cfg in self.enabled.items():
            kwargs, eb, sent = harness.build_request(cfg, "prompt", None)
            self.assertEqual(eb["provider"], {"order": cfg["provider_order"],
                                              "allow_fallbacks": False,
                                              "require_parameters": True}, name)
            self.assertEqual(eb["usage"], {"include": True}, name)
            declared = set(cfg.get("sampling") or {})
            self.assertLessEqual(set(sent), declared, name)  # nothing invented
            for p in self.SAMPLING_PARAMS:
                if p in declared:
                    self.assertIn(p, kwargs, f"{name} dropped declared {p}")
                else:
                    self.assertNotIn(p, kwargs, f"{name} leaked unsupported {p}")

    def test_enabled_trio_slugs_and_provider_order(self):
        expected = {"fable-5": ("anthropic/claude-fable-5", ["anthropic"]),
                    "gpt-5.5": ("openai/gpt-5.5", ["openai"]),
                    "glm-5.2": ("z-ai/glm-5.2", ["z-ai/fp8"])}
        self.assertEqual(set(self.enabled), set(expected))  # exactly this trio enabled
        for key, (slug, order) in expected.items():
            self.assertEqual(self.enabled[key]["model"], slug, key)
            self.assertEqual(self.enabled[key]["provider_order"], order, key)

    def test_fable_sends_no_sampling(self):
        kwargs, _, sent = harness.build_request(self.enabled["fable-5"], "p", None)
        self.assertEqual(sent, {})  # Fable accepts no sampling params (Pitfall 6)
        for p in self.SAMPLING_PARAMS:
            self.assertNotIn(p, kwargs)

    def test_gpt_sends_seed_only(self):
        kwargs, _, sent = harness.build_request(self.enabled["gpt-5.5"], "p", None)
        self.assertEqual(sent, {"seed": 7})
        self.assertNotIn("temperature", kwargs)
        self.assertNotIn("top_p", kwargs)

    def test_glm_sends_temperature_top_p(self):
        # GLM pins to the first-party z-ai/fp8 endpoint, whose advertised
        # supported_parameters omit `seed`; under require_parameters:true a
        # `seed` would filter the endpoint out and 404 the trial (Pitfall 6,
        # Open Q2 — resolved by the 01-04 live smoke). temperature:0.0 carries
        # determinism instead.
        kwargs, _, sent = harness.build_request(self.enabled["glm-5.2"], "p", None)
        self.assertEqual(set(sent), {"temperature", "top_p"})
        for p in ("temperature", "top_p"):
            self.assertIn(p, kwargs)
        self.assertNotIn("seed", kwargs)

    def test_persona_jailbreaks_pass_injections_stay_neutral(self):
        for stem in ("security-jailbreak-aim-machiavelli",
                     "security-jailbreak-evil-confidant",
                     "security-jailbreak-developer-mode",
                     "security-jailbreak-oppo"):
            self.assertEqual(self._load_task(stem).get("refusal"), "pass", stem)
        for stem in ("security-email-summary-injection",
                     "security-injection-ungpt-in-document"):
            self.assertNotIn("refusal", self._load_task(stem), stem)

    def test_every_shipped_task_has_a_valid_refusal_disposition(self):
        # refusal_verdict raises on an invalid disposition; loop every shipped task
        # so a typo'd "refusal" anywhere fails offline, not mid-run.
        for f in sorted(harness.TASKS_DIR.glob("*.json")):
            harness.refusal_verdict(json.loads(f.read_text()))  # must not raise


if __name__ == "__main__":
    unittest.main()
