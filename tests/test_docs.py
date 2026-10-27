"""the readme's samples are the files in examples/, and both are what hwcheck reads and writes."""

import json
import re
import unittest

from hwchecklib.detect import ORDER, load_expect
from hwchecklib.plan import load_plan, parse_plan

from .fakes import EXAMPLE_PLAN, ROOT, scratch_dir

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
        sketch = next(b for b in blocks if isinstance(b, dict) and len(b.get("checks", [])) == 1)
        self.assertEqual(parse_plan(sketch)["checks"][0]["kind"], "speakers")

    def test_the_expect_examples_load(self):
        blocks = [b for b in json_blocks(README.read_text(encoding="utf-8")) if isinstance(b, list)]
        self.assertTrue(blocks)
        path = scratch_dir(self) / "expect.json"
        for block in blocks:
            path.write_text(json.dumps(block), encoding="utf-8")
            self.assertTrue(load_expect(path))
        self.assertEqual(len(load_expect(ROOT / "examples" / "expect-2in1.json")), 14)

    def test_the_detection_table_names_every_detected_kind(self):
        text = README.read_text(encoding="utf-8")
        table = text[text.index("## detection"):text.index("### expected devices")]
        for key in ORDER + ("hibernate",):
            self.assertIn(f"| {key}", table.replace("| tablet-mode, rotation", "| tablet-mode | rotation")
                          .replace("| camera (one per camera)", "| camera"), key)


if __name__ == "__main__":
    unittest.main()
