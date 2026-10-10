"""open_pr.py without the network: which changes are applied, and how they change the app repo's files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import open_pr as O  # noqa: E402

HERE = Path(__file__).resolve().parent.parent
DATA = json.loads((HERE / "reports" / "demo" / "results.json").read_text())
DEMO = DATA["demo"]
TARGET = {"prompts_dir": "prompts", "config_file": "config.yml"}
OLD = DEMO["patch"]["edits"][0]["old"]


def app_repo(tmp, tone_line=OLD):
    d = Path(tmp)
    (d / "prompts").mkdir()
    (d / "prompts" / "tone.txt").write_text(f"line one\nline two\n{tone_line}\nline four\n")
    (d / "config.yml").write_text("steps:\n  tone:\n    model: gpt-4\n    temperature: 0\n")
    return d


class Plan(unittest.TestCase):
    def test_only_accepted_or_edited_changes_are_applied(self):
        applied, skipped = O.plan([{"id": "model", "decision": "accepted"}, {"id": "line0", "decision": "rejected"}], DEMO)
        self.assertEqual(([c["id"] for c in applied], [c["id"] for c in skipped]), (["model"], ["line0"]))

    def test_undecided_changes_are_left_out(self):
        applied, skipped = O.plan([], DEMO)
        self.assertEqual((applied, [c["decision"] for c in skipped]), ([], ["pending", "pending"]))

    def test_edited_text_is_used(self):
        applied, _ = O.plan([{"id": "line0", "decision": "edited", "text": "  Keep refund timelines.  "}], DEMO)
        self.assertEqual(applied[0]["apply"], "Keep refund timelines.")

    def test_edited_model_change_needs_a_model_name(self):
        applied, _ = O.plan([{"id": "model", "decision": "edited", "text": "model: gpt-5.6-terra  temperature: default"}], DEMO)
        self.assertEqual(applied[0]["apply"], "gpt-5.6-terra")
        with self.assertRaises(ValueError):
            O.plan([{"id": "model", "decision": "edited", "text": "use the cheaper one"}], DEMO)


class Apply(unittest.TestCase):
    def test_model_swap_and_prompt_line(self):
        with tempfile.TemporaryDirectory() as t:
            d = app_repo(t)
            applied, _ = O.plan([{"id": "model", "decision": "accepted"}, {"id": "line0", "decision": "accepted"}], DEMO)
            O.apply(d, TARGET, applied)
            self.assertEqual((d / "config.yml").read_text(), "steps:\n  tone:\n    model: gpt-5.6-sol\n")
            self.assertEqual((d / "prompts" / "tone.txt").read_text().split("\n")[2], DEMO["patch"]["edits"][0]["new"])

    def test_refuses_a_line_that_changed_since_testing(self):
        with tempfile.TemporaryDirectory() as t:
            d = app_repo(t, tone_line="Someone already rewrote this line.")
            applied, _ = O.plan([{"id": "line0", "decision": "accepted"}], DEMO)
            with self.assertRaisesRegex(RuntimeError, "has changed"):
                O.apply(d, TARGET, applied)

    def test_pr_text_flags_edits_and_left_out_changes(self):
        applied, skipped = O.plan([{"id": "model", "decision": "rejected"}, {"id": "line0", "decision": "edited", "text": "X"}], DEMO)
        text = O.body(DATA, TARGET, applied, skipped)
        self.assertIn("not re-tested", text)
        self.assertIn("Not included (rejected): model", text)


if __name__ == "__main__":
    unittest.main()
