import json
import unittest

from hwchecklib.plan import KINDS, PlanError, load_plan, parse_plan, signature

from .fakes import EXAMPLE_PLAN, scratch_dir


def check(**over):
    c = {"id": "speakers", "title": "speakers", "kind": "speakers",
         "instructions": "play the tones", "expect": "you hear them"}
    c.update(over)
    return c


class PlanParsing(unittest.TestCase):
    def test_example_plan_is_valid(self):
        plan = load_plan(EXAMPLE_PLAN)
        kinds = [c["kind"] for c in plan["checks"]]
        self.assertEqual(kinds[-1], "hibernate")
        self.assertEqual(set(kinds), set(KINDS))  # the example uses every kind
        self.assertEqual(len({c["id"] for c in plan["checks"]}), len(kinds))

    def test_minimal_check_defaults(self):
        plan = parse_plan({"checks": [{"id": "wifi", "title": "wifi", "kind": "manual"}]})
        self.assertEqual(plan["checks"][0],
                         {"id": "wifi", "title": "wifi", "kind": "manual", "instructions": "", "expect": ""})
        self.assertIsNone(plan["machine"])

    def test_machine_and_model_override(self):
        plan = parse_plan({"machine": "testbook", "model": "test model", "checks": [check()]})
        self.assertEqual((plan["machine"], plan["model"]), ("testbook", "test model"))

    def test_signature_is_ids_and_kinds(self):
        plan = parse_plan({"checks": [check(), check(id="cam", kind="camera")]})
        self.assertEqual(signature(plan), [["speakers", "speakers"], ["cam", "camera"]])


class PlanRefusals(unittest.TestCase):
    def refused(self, obj, *fragments):
        with self.assertRaises(PlanError) as ctx:
            parse_plan(obj)
        text = "\n".join(ctx.exception.problems)
        for fragment in fragments:
            self.assertIn(fragment, text)
        return ctx.exception.problems

    def test_not_an_object(self):
        self.refused([check()], "must be a json object")

    def test_checks_missing_or_empty(self):
        self.refused({}, "\"checks\" must be a non-empty list")
        self.refused({"checks": []}, "\"checks\" must be a non-empty list")
        self.refused({"checks": "speakers"}, "\"checks\" must be a non-empty list")

    def test_check_not_an_object(self):
        self.refused({"checks": ["speakers"]}, "check 1 must be an object")

    def test_missing_fields(self):
        self.refused({"checks": [{"id": "x"}]}, "missing \"title\"", "missing \"kind\"")
        self.refused({"checks": [{"title": "x", "kind": "manual"}]}, "missing \"id\"")

    def test_bad_id(self):
        self.refused({"checks": [check(id="Speakers!")]}, "\"id\" must be lowercase")
        self.refused({"checks": [check(id="")]}, "\"id\" must be lowercase")

    def test_duplicate_id(self):
        self.refused({"checks": [check(), check()]}, "id \"speakers\" is used twice")

    def test_unknown_kind(self):
        self.refused({"checks": [check(kind="smell")]}, "unknown kind \"smell\"")

    def test_unknown_keys(self):
        self.refused({"checks": [check(instruction="typo")]}, "unknown key \"instruction\"")
        self.refused({"checks": [check()], "check": []}, "unknown plan key \"check\"")

    def test_wrong_types(self):
        self.refused({"checks": [check(title=3)]}, "\"title\" must be a string")
        self.refused({"checks": [check(title="  ")]}, "\"title\" is empty")
        self.refused({"checks": [check()], "machine": 7}, "\"machine\" must be a non-empty string")

    def test_hibernate_must_be_last_and_single(self):
        hib = check(id="hibernate", kind="hibernate", title="hibernate")
        self.refused({"checks": [hib, check()]}, "hibernate check must be the last")
        self.refused({"checks": [hib, dict(hib, id="hib2")]}, "only one hibernate check")

    def test_every_problem_is_reported(self):
        problems = self.refused({"checks": [check(kind="smell"), check(id="Bad")]})
        self.assertGreaterEqual(len(problems), 2)

    def test_bad_files(self):
        d = scratch_dir(self)
        with self.assertRaises(PlanError) as ctx:
            load_plan(d / "absent.json")
        self.assertIn("can't read", str(ctx.exception))
        bad = d / "bad.json"
        bad.write_text("{\"checks\": [", encoding="utf-8")
        with self.assertRaises(PlanError) as ctx:
            load_plan(bad)
        self.assertIn("not valid json", str(ctx.exception))
        good = d / "good.json"
        good.write_text(json.dumps({"checks": [check()]}), encoding="utf-8")
        self.assertEqual(load_plan(good)["checks"][0]["id"], "speakers")


if __name__ == "__main__":
    unittest.main()
