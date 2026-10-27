"""the readme's samples are the files in examples/, and both are what hwcheck reads and writes."""

import json
import re
import unittest

from hwchecklib.plan import load_plan, parse_plan

from .fakes import EXAMPLE_PLAN, ROOT

README = ROOT / "README.md"
EXAMPLE_RESULTS = ROOT / "examples" / "results-2in1.json"


def json_blocks(text):
    blocks = []
    for body in re.findall(r"```json\n(.*?)\n```", text, re.S):
        try:
            blocks.append(json.loads(body))
        except json.JSONDecodeError:
            pass  # the format sketches aren't strict json
    return blocks


class Docs(unittest.TestCase):
    def test_readme_carries_the_sample_plan_and_its_results(self):
        blocks = json_blocks(README.read_text(encoding="utf-8"))
        plan = json.loads(EXAMPLE_PLAN.read_text(encoding="utf-8"))
        results = json.loads(EXAMPLE_RESULTS.read_text(encoding="utf-8"))
        self.assertIn(plan, blocks)
        self.assertIn(results, blocks)

    def test_sample_results_match_the_sample_plan(self):
        plan = load_plan(EXAMPLE_PLAN)
        results = json.loads(EXAMPLE_RESULTS.read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in results["checks"]], [c["id"] for c in plan["checks"]])
        for c in results["checks"]:
            self.assertIn(c["answer"], ("works", "doesnt-work", "skip"))
            self.assertTrue(c["at"])
        for key in ("machine", "model", "boot_id", "checks"):
            self.assertIn(key, results)

    def test_the_plan_format_sketch_is_a_valid_plan(self):
        blocks = json_blocks(README.read_text(encoding="utf-8"))
        sketch = next(b for b in blocks if b.get("checks") and len(b["checks"]) == 1)
        self.assertEqual(parse_plan(sketch)["checks"][0]["kind"], "speakers")


if __name__ == "__main__":
    unittest.main()
