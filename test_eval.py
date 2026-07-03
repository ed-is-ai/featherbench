#!/usr/bin/env python3
"""Unit tests for the eval.py harness — no network, no API keys needed.

    python3 -m unittest test_eval -v
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eval as harness
from eval import ModelResponse

PASSING_CODE = "Here you go:\n```python\ndef add(a, b):\n    return a + b\n```"


class TestCheckers(unittest.TestCase):
    def check(self, spec, text="", tool_calls=()):
        return harness.run_check(spec, text, tool_calls)

    def test_contains(self):
        self.assertTrue(self.check({"type": "contains", "values": ["Paris"]}, "in PARIS.")[0])
        self.assertFalse(self.check({"type": "contains", "value": "Nottingham"}, "London")[0])

    def test_not_contains(self):
        self.assertTrue(self.check({"type": "not_contains", "value": "cure"}, "see a doctor")[0])
        self.assertFalse(self.check({"type": "not_contains", "value": "cure"}, "a Cure!")[0])

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

    def test_extract_code_takes_last_block(self):
        self.assertEqual(harness.extract_code("```python\nfirst\n```\n```py\nsecond\n```"),
                         "second\n")
        self.assertIsNone(harness.extract_code("no code"))


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
    def test_dispatch(self):
        def lookup(cfg):
            return (harness.PROVIDERS.get((cfg["provider"], cfg.get("api")))
                    or harness.PROVIDERS.get((cfg["provider"], None)))
        self.assertIs(lookup({"provider": "anthropic"}), harness.call_anthropic)
        self.assertIs(lookup({"provider": "openai", "api": "responses"}),
                      harness.call_openai_responses)
        self.assertIs(lookup({"provider": "openai", "api": "chat"}), harness.call_openai_chat)
        self.assertIs(lookup({"provider": "openai_compatible", "api": "chat"}),
                      harness.call_openai_chat)
        with self.assertRaises(ValueError):
            harness.call_model("m", {"provider": "mystery"}, "hi")

    def test_select_models(self):
        catalog = {"on": {"enabled": True}, "off": {"enabled": False}}
        self.assertEqual(set(harness.select_models(None, catalog)), {"on"})
        self.assertEqual(set(harness.select_models("all", catalog)), {"on", "off"})
        self.assertEqual(set(harness.select_models("off", catalog)), {"off"})  # explicit wins
        with self.assertRaises(SystemExit):
            harness.select_models("nope", catalog)

    def test_cost_usd(self):
        cfg = {"pricing_per_mtok": {"input": 10.0, "output": 50.0}}
        self.assertAlmostEqual(harness.cost_usd(cfg, 1_000_000, 100_000), 15.0)
        self.assertIsNone(harness.cost_usd({}, 1, 1))

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
     "output_tokens": 900, "cost_usd": 0.049, "text": "ok"},
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


if __name__ == "__main__":
    unittest.main()
