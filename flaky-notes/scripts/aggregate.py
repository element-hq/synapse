#!/usr/bin/env python3
"""Aggregate failed complement/trial job logs into flake frequency tables.

Reads:
  data/runs.jsonl, data/jobs/*.json, data/joblogs/<job_id>.log
Writes:
  data/summary.json          — machine-readable aggregation
  data/report.md             — human-readable report
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"

# ---------- load runs ----------
runs = {}
with open(DATA / "runs.jsonl") as f:
    for line in f:
        r = json.loads(line)
        runs[r["databaseId"]] = r

# ---------- load jobs ----------
jobs = []  # all jobs of interest, all attempts
for p in (DATA / "jobs").glob("*.json"):
    with open(p) as f:
        for line in f:
            if line.strip():
                jobs.append(json.loads(line))

def job_kind(name: str):
    if name.startswith("complement / complement"):
        return "complement"
    if name.startswith("trial"):
        return "trial"
    return None

# retry analysis: same (run_id, name), failed at attempt N, succeeded at attempt >N
by_run_name = defaultdict(list)
for j in jobs:
    if job_kind(j["name"]):
        by_run_name[(j["run_id"], j["name"])].append(j)

flake_confirmed_jobs = set()   # job ids that failed but a later attempt of same job passed
for (run_id, name), js in by_run_name.items():
    js.sort(key=lambda j: j["run_attempt"])
    for i, j in enumerate(js):
        if j["conclusion"] == "failure" and any(
            k["conclusion"] == "success" for k in js[i + 1:]
        ):
            flake_confirmed_jobs.add(j["id"])

# ---------- parse logs ----------
# GH log lines: "2026-08-13T10:13:43.1234567Z <content>"
TS_RE = re.compile(r"^\S+Z ?")
# compact jq view: "FAIL TestFoo/sub_test 12.34s"
GOFAIL_RE = re.compile(r"^FAIL (Test\S+) ([0-9.]+)s$")
# trial: "tests.foo.Bar.test_x ... [FAIL]" possibly with "[ERROR]"
TRIAL_RE = re.compile(r"^(tests\.\S+) \.\.\. \[(FAIL|ERROR)\]")
TRIAL_RES2 = re.compile(r"^\[(FAIL|ERROR)\]$")

def parse_log(path: Path, kind: str):
    """Return (failed_tests, markers) for one job log."""
    failed = set()
    markers = set()
    trial_pending_names = []
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return failed, {"unreadable"}
    for raw in text.splitlines():
        line = TS_RE.sub("", raw)
        if kind == "complement":
            m = GOFAIL_RE.match(line)
            if m:
                failed.add(m.group(1))
                continue
            if "Sanity check Complement image" in line and "##[group]" in raw:
                pass
            if "no space left on device" in line:
                markers.add("disk-full")
            if "Cannot connect to the Docker daemon" in line:
                markers.add("docker-daemon")
            if "The self-hosted runner" in line and "lost communication" in line:
                markers.add("runner-lost")
            if "The operation was canceled" in line:
                markers.add("canceled")
            if line.startswith("FAIL") and "TestSynapseVersion" in line:
                markers.add("sanity-check-failed")
            if "error building image" in line.lower() or "failed to solve" in line:
                markers.add("image-build-failed")
            if re.search(r"panic: ", line):
                markers.add("go-panic")
        elif kind == "trial":
            m = TRIAL_RE.match(line)
            if m:
                failed.add(m.group(1))
                continue
            # multi-line format:
            #   tests.foo.Bar
            #     test_x ...                                       [FAIL]
            m2 = re.match(r"^(tests\.[\w.]+)$", line)
            if m2:
                trial_pending_names = [m2.group(1)]
                continue
            m3 = re.match(r"^\s+(test_\w+) \.\.\..*\[(FAIL|ERROR)\]", line)
            if m3 and trial_pending_names:
                failed.add(f"{trial_pending_names[0]}.{m3.group(1)}")
                continue
            if "The operation was canceled" in line:
                markers.add("canceled")
    if not failed:
        markers.add("no-test-failures-parsed")
    return failed, markers

# ---------- aggregate ----------
per_test = defaultdict(lambda: {
    "count": 0, "jobs": [], "arrangements": Counter(),
    "develop_push": 0, "flake_confirmed": 0,
    "branches": set(), "days": set(),
})
job_class = Counter()
failed_jobs_meta = []

for j in jobs:
    kind = job_kind(j["name"])
    if not kind or j["conclusion"] != "failure":
        continue
    run = runs.get(j["run_id"], {})
    logp = DATA / "joblogs" / f"{j['id']}.log"
    if not logp.exists():
        job_class[f"{kind}:log-missing"] += 1
        continue
    failed, markers = parse_log(logp, kind)
    is_develop = run.get("headBranch") == "develop" and run.get("event") == "push"
    is_confirmed = j["id"] in flake_confirmed_jobs
    failed_jobs_meta.append({
        "job_id": j["id"], "run_id": j["run_id"], "attempt": j["run_attempt"],
        "name": j["name"], "kind": kind, "branch": run.get("headBranch"),
        "event": run.get("event"), "created": run.get("createdAt"),
        "develop_push": is_develop, "flake_confirmed": is_confirmed,
        "failed_tests": sorted(failed), "markers": sorted(markers),
    })
    if failed and len(failed) > 50:
        # whole-run explosion (OOM, DB loss, import error): not per-test flakes
        job_class[f"{kind}:catastrophic"] += 1
        failed_jobs_meta[-1]["markers"].append("catastrophic")
        continue
    if failed:
        job_class[f"{kind}:test-failures"] += 1
    else:
        job_class[f"{kind}:infra-or-unparsed"] += 1
    arrangement = j["name"].split("(")[-1].rstrip(")") if "(" in j["name"] else ""
    for t in failed:
        e = per_test[(kind, t)]
        e["count"] += 1
        e["jobs"].append(j["id"])
        e["arrangements"][arrangement] += 1
        if is_develop:
            e["develop_push"] += 1
        if is_confirmed:
            e["flake_confirmed"] += 1
        e["branches"].add(run.get("headBranch") or "?")
        e["days"].add((run.get("createdAt") or "?")[:10])

# roll subtests up to their root test too
root_counts = defaultdict(lambda: Counter())
for (kind, t), e in per_test.items():
    root = t.split("/")[0] if kind == "complement" else t
    root_counts[(kind, root)]["count"] += e["count"]

# ---------- write ----------
summary = {
    "n_runs": len(runs),
    "n_jobs_seen": len(jobs),
    "job_class": dict(job_class),
    "n_flake_confirmed_jobs": len(flake_confirmed_jobs),
    "per_test": {
        f"{kind}:{t}": {
            **e,
            "arrangements": dict(e["arrangements"]),
            "branches": sorted(e["branches"]),
            "days": sorted(e["days"]),
        }
        for (kind, t), e in sorted(per_test.items(), key=lambda kv: -kv[1]["count"])
    },
    "failed_jobs": failed_jobs_meta,
}
with open(DATA / "summary.json", "w") as f:
    json.dump(summary, f, indent=1)

# markdown report
lines = ["# Flake mining report", ""]
lines.append(f"- Runs analyzed: {len(runs)}")
lines.append(f"- Failed-job log classification: {dict(job_class)}")
lines.append(f"- Jobs confirmed flaky via retry-then-pass: {len(flake_confirmed_jobs)}")
lines.append("")
for kind in ("complement", "trial"):
    lines.append(f"## {kind}: most frequent failing tests\n")
    lines.append("| count | develop-push | retry-confirmed | branches | days | span | arrangements | test |")
    lines.append("|---|---|---|---|---|---|---|---|")
    items = [(t, e) for (k, t), e in per_test.items() if k == kind]
    items.sort(key=lambda kv: -kv[1]["count"])
    for t, e in items[:60]:
        arr = ", ".join(f"{a}×{c}" for a, c in e["arrangements"].most_common())
        days = sorted(e["days"])
        span = f"{days[0][5:]}–{days[-1][5:]}" if days else ""
        lines.append(
            f"| {e['count']} | {e['develop_push']} | {e['flake_confirmed']} "
            f"| {len(e['branches'])} | {len(days)} | {span} | {arr} | `{t}` |"
        )
    lines.append("")
with open(DATA / "report.md", "w") as f:
    f.write("\n".join(lines))

print(f"Wrote {DATA/'summary.json'} and {DATA/'report.md'}")
