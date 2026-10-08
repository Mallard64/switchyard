"""Unit tests for the noise rule, the hard checks, the step-swap plans and the LLM hash cache.

  python -m unittest discover tests        # no API key, no network, no repo data written
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llm  # noqa: E402
import stepswap as S  # noqa: E402
import support_agent as A  # noqa: E402
import thin_e2e as T  # noqa: E402
from record_baseline import PRICES  # noqa: E402
from stepfinder import spec  # noqa: E402

TICKET = {"id": "x1", "gold": {"category": "damaged", "action": "replace"}}


def row(classify='{"category": "damaged", "order_id": "A1003"}',
        decide='{"action": "replace", "policy_id": "DAMAGE-02", "amount": null, "reason": "r"}',
        tone='{"verdict": "PASS", "issues": [], "final_reply": null}', error=None):
    steps = [{"step": "classify", "raw": classify}, {"step": "decide", "raw": decide},
             {"step": "draft", "raw": "Your replacement ships at no cost."}, {"step": "tone", "raw": tone}]
    return {"error": error, "steps": steps}


class NoiseRule(unittest.TestCase):
    def test_broken_needs_old_stable_and_new_failing(self):
        self.assertEqual(T.verdict(3, 3, 0, 3), "broken")
        self.assertEqual(T.verdict(2, 3, 1, 3), "broken")  # both thresholds are inclusive

    def test_old_model_noise_cannot_flag_a_ticket(self):
        self.assertEqual(T.verdict(1, 3, 0, 3), "same")    # old only passed 1/3: not a regression
        self.assertEqual(T.verdict(3, 3, 2, 3), "same")    # new still passes most runs

    def test_fixed_is_the_mirror_image(self):
        self.assertEqual(T.verdict(0, 3, 3, 3), "fixed")
        self.assertEqual(T.verdict(1, 3, 2, 3), "fixed")

    def test_other_run_counts_and_missing_runs(self):
        self.assertEqual(T.verdict(4, 5, 1, 5), "broken")
        self.assertEqual(T.verdict(4, 5, 2, 5), "same")    # 2/5 > 1/3
        self.assertEqual(T.verdict(3, 3, 0, 0), "unknown")


class HardChecks(unittest.TestCase):
    def test_good_run_passes_everything(self):
        self.assertEqual(T.run_checks(row(), TICKET), {c: True for c in T.CHECKS})

    def test_invalid_json(self):
        self.assertFalse(T.run_checks(row(decide="I'd replace it."), TICKET)["valid_json"])

    def test_code_fenced_json_still_parses(self):
        fenced = '```json\n{"verdict": "PASS", "issues": [], "final_reply": null}\n```'
        self.assertTrue(T.run_checks(row(tone=fenced), TICKET)["valid_json"])

    def test_missing_required_field(self):
        c = T.run_checks(row(decide='{"action": "replace", "amount": null, "reason": "r"}'), TICKET)
        self.assertTrue(c["valid_json"])
        self.assertFalse(c["required_fields"])

    def test_label_mismatch(self):
        c = T.run_checks(row(decide='{"action": "deny", "policy_id": "P", "amount": null, "reason": "r"}'), TICKET)
        self.assertEqual((c["required_fields"], c["label_match"]), (True, False))

    def test_errored_run_fails_all(self):
        self.assertEqual(set(T.run_checks(row(error="HTTP 500"), TICKET).values()), {False})


class StepSwap(unittest.TestCase):
    def setUp(self):
        self.old, self.new = spec("gpt-4", {"temperature": 0.0}), spec("gpt-5.6-sol", {})
        self.plans = S.swap_plans(self.old, self.new)

    def test_rescue_is_new_everywhere_except_step_k(self):
        for k, step in enumerate(A.STEPS):
            label, plan, first_live, side = self.plans[step]["rescue"]
            self.assertEqual({s: plan[s]["model"] for s in A.STEPS},
                             {s: "gpt-4" if s == step else "gpt-5.6-sol" for s in A.STEPS})
            self.assertEqual(plan[step]["params"], {"temperature": 0.0})  # old model keeps its own sampling
            self.assertEqual((first_live, side), (k, "new"))           # upstream replays from the new run
            self.assertEqual(label, f"gpt-5.6-sol__{step}-gpt-4")       # same cache file as stepfinder.py

    def test_break_is_old_everywhere_except_step_k(self):
        for k, step in enumerate(A.STEPS):
            label, plan, first_live, side = self.plans[step]["break"]
            self.assertEqual({s: plan[s]["model"] for s in A.STEPS},
                             {s: "gpt-5.6-sol" if s == step else "gpt-4" for s in A.STEPS})
            self.assertEqual((first_live, side), (k, "old"))
            self.assertEqual(label, f"gpt-4__{step}-gpt-5.6-sol")

    def test_repaired_threshold(self):
        self.assertTrue(S.repaired(2, 3))
        self.assertFalse(S.repaired(1, 3))
        self.assertFalse(S.repaired(0, 0))

    def test_summary_sentences(self):
        tickets = [{"necessary": ["decide"], "sufficient": ["decide"]},
                   {"necessary": ["decide"], "sufficient": []},
                   {"necessary": [], "sufficient": []}]
        summary, sentences = S.step_summary(tickets)
        self.assertEqual(summary["decide"], {"necessary": 2, "sufficient": 1})
        self.assertEqual(summary["classify"], {"necessary": 0, "sufficient": 0})
        self.assertEqual(sentences, ["step decide causes 67% of failures (2/3; 1 confirmed by break)",
                                     "no single step explains 1/3 failure(s)"])

    def test_no_plant_removes_exactly_the_planted_line(self):
        step, line = A.PLANTED
        clean = A.unplanted_prompts()[step]
        self.assertIn(line, A.PROMPTS[step])
        self.assertEqual(clean, [x for x in A.PROMPTS[step] if x != line])
        self.assertEqual(len(clean), len(A.PROMPTS[step]) - 1)


class HashCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (llm.CACHE, llm._cache, llm._spent, llm.SPEND_CAP_USD)
        llm.CACHE, llm._cache, llm._spent = Path(self.tmp.name) / "calls.jsonl", None, 0.0
        self.calls = 0

    def tearDown(self):
        llm.CACHE, llm._cache, llm._spent, llm.SPEND_CAP_USD = self.saved
        self.tmp.cleanup()

    def fake(self, model, params, messages, api_base):
        self.calls += 1
        msg = SimpleNamespace(content=f"answer {self.calls}")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], model=model,
                               usage={"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
                               system_fingerprint=None)

    def test_same_call_same_run_is_free(self):
        msgs = [{"role": "user", "content": "hi"}]
        llm.set_run(1)
        a = llm.call_llm("decide", "gpt-5.6-terra", msgs, {}, completion=self.fake)
        b = llm.call_llm("decide", "gpt-5.6-terra", msgs, {}, completion=self.fake)
        self.assertEqual((self.calls, a["cached"], b["cached"], a["raw"], b["raw"]), (1, False, True, "answer 1", "answer 1"))
        row = json.loads(llm.CACHE.read_text().splitlines()[0])
        self.assertEqual((row["step"], row["run"]), ("decide", 1))
        pin, pout = PRICES["gpt-5.6-terra"]
        self.assertAlmostEqual(row["cost_usd"], 1000 / 1e6 * pin + 100 / 1e6 * pout)

    def test_each_run_is_a_separate_sample(self):
        msgs = [{"role": "user", "content": "hi"}]
        for run in (1, 2):
            llm.set_run(run)
            llm.call_llm("decide", "gpt-5.6-terra", msgs, {}, completion=self.fake)
        self.assertEqual(self.calls, 2)

    def test_spend_cap_blocks_uncached_calls_only(self):
        msgs = [{"role": "user", "content": "hi"}]
        llm.set_run(1)
        llm.call_llm("decide", "gpt-4", msgs, {"temperature": 0.0}, completion=self.fake)
        llm.SPEND_CAP_USD = 0.01  # the call above cost $0.036 at gpt-4's $30/$60
        self.assertTrue(llm.call_llm("decide", "gpt-4", msgs, {"temperature": 0.0}, completion=self.fake)["cached"])
        llm.set_run(2)
        with self.assertRaisesRegex(RuntimeError, "spend cap"):
            llm.call_llm("decide", "gpt-4", msgs, {"temperature": 0.0}, completion=self.fake)
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
