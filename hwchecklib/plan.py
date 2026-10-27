"""the check plan: a json file listing the checks to run, in order."""

from __future__ import annotations

import json
import re
from pathlib import Path

# kind -> where its test runs:
#   page       the browser runs the test (sound, camera, touch, ...)
#   live       the server reads the system and pushes what it reads to the page
#   hibernate  the hibernate round trip; always the last check
#   manual     instructions only; the person looks and answers
KINDS = {
    "speakers": "page",
    "headphones": "page",
    "microphone": "page",
    "camera": "page",
    "touch": "page",
    "pen": "page",
    "keys": "page",
    "rotation": "live",
    "tablet-mode": "live",
    "lid": "live",
    "power": "live",
    "external-display": "live",
    "bluetooth": "live",
    "hibernate": "hibernate",
    "manual": "manual",
}

PLAN_KEYS = {"checks", "machine", "model"}
CHECK_KEYS = {"id", "title", "kind", "instructions", "expect"}
REQUIRED_CHECK_KEYS = ("id", "title", "kind")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
MAX_CHECKS = 100
MAX_LEN = {"title": 120, "instructions": 2000, "expect": 1000, "machine": 120, "model": 200}


class PlanError(ValueError):
    """the plan can't be used; .problems lists every reason, in plain words."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def where(kind: str) -> str:
    return KINDS[kind]


def parse_plan(obj) -> dict:
    """validate a decoded plan and return it normalised; raise PlanError if it is bad."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        raise PlanError(["the plan must be a json object with a \"checks\" list"])
    for key in sorted(set(obj) - PLAN_KEYS):
        problems.append(f"unknown plan key \"{key}\"")
    plan: dict = {"machine": None, "model": None, "checks": []}
    for key in ("machine", "model"):
        value = obj.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append(f"\"{key}\" must be a non-empty string")
        elif len(value) > MAX_LEN[key]:
            problems.append(f"\"{key}\" is longer than {MAX_LEN[key]} characters")
        else:
            plan[key] = value.strip()

    checks = obj.get("checks")
    if not isinstance(checks, list) or not checks:
        problems.append("\"checks\" must be a non-empty list")
        raise PlanError(problems)
    if len(checks) > MAX_CHECKS:
        problems.append(f"too many checks ({len(checks)}; at most {MAX_CHECKS})")

    seen_ids: set[str] = set()
    for n, check in enumerate(checks, start=1):
        label = f"check {n}"
        if not isinstance(check, dict):
            problems.append(f"{label} must be an object")
            continue
        if isinstance(check.get("id"), str):
            label = f"check {n} (\"{check['id']}\")"
        for key in sorted(set(check) - CHECK_KEYS):
            problems.append(f"{label}: unknown key \"{key}\"")
        for key in REQUIRED_CHECK_KEYS:
            if key not in check:
                problems.append(f"{label}: missing \"{key}\"")
        out = {}
        cid = check.get("id")
        if "id" in check:
            if not isinstance(cid, str) or not ID_RE.match(cid):
                problems.append(f"{label}: \"id\" must be lowercase letters, digits, - or _ (at most 48)")
            elif cid in seen_ids:
                problems.append(f"{label}: id \"{cid}\" is used twice")
            else:
                seen_ids.add(cid)
        out["id"] = cid
        kind = check.get("kind")
        if "kind" in check and kind not in KINDS:
            problems.append(f"{label}: unknown kind \"{kind}\" (known: {', '.join(KINDS)})")
        out["kind"] = kind
        for key in ("title", "instructions", "expect"):
            value = check.get(key, "")
            if not isinstance(value, str):
                problems.append(f"{label}: \"{key}\" must be a string")
                value = ""
            elif len(value) > MAX_LEN[key]:
                problems.append(f"{label}: \"{key}\" is longer than {MAX_LEN[key]} characters")
            out[key] = value.strip()
        if "title" in check and isinstance(check.get("title"), str) and not out["title"]:
            problems.append(f"{label}: \"title\" is empty")
        plan["checks"].append(out)

    kinds = [c.get("kind") for c in plan["checks"]]
    hib = [i for i, k in enumerate(kinds) if k == "hibernate"]
    if len(hib) > 1:
        problems.append("only one hibernate check is allowed")
    if hib and hib[-1] != len(kinds) - 1:
        problems.append("the hibernate check must be the last check")
    if problems:
        raise PlanError(problems)
    return plan


def load_plan(path) -> dict:
    """read and validate a plan file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanError([f"can't read {path}: {exc.strerror or exc}"]) from None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanError([f"{path} is not valid json: {exc.msg} at line {exc.lineno} column {exc.colno}"]) from None
    return parse_plan(obj)


def signature(plan: dict) -> list[list[str]]:
    """what must stay the same for a run to be resumed with this plan."""
    return [[c["id"], c["kind"]] for c in plan["checks"]]
