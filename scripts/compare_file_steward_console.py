"""Paired unchanged-source Console runs; optional mock-only diagnostic is separate."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

BASELINE = "5c5d313273807db0686f0ea5d7f388d846762ded"
SLICE = "742d90bd9c37c8b610b3168d5cf9684783cf1d54"
SOURCE = Path(__file__).resolve().parents[1]
PATHS = ("tests/test_homeplane_console.py", "homeplane/static/app.js", "pyproject.toml")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_at(checkpoint, name):
    return subprocess.check_output(["git", "show", f"{checkpoint}:{name}"], cwd=SOURCE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    sources = {
        key: {name: source_at(checkpoint, name) for name in PATHS}
        for key, checkpoint in (("baseline", BASELINE), ("candidate", SLICE))
    }
    assert sources["baseline"][PATHS[0]] == sources["candidate"][PATHS[0]], "Original test drift"
    report = {
        "baseline_checkpoint": BASELINE,
        "candidate_checkpoint": SLICE,
        "python": sys.executable,
        "python_version": sys.version,
        "node_version": subprocess.check_output(["node", "--version"]).decode().strip(),
        "test_source_unchanged": True,
        "source_sha256": {
            key: {name: sha(value) for name, value in files.items()} for key, files in sources.items()
        },
        "mock_diagnostic": "Only temporary test document gets body.dataset={}; production app unchanged",
        "runs": [],
    }
    with tempfile.TemporaryDirectory(prefix="friday-console-paired-") as work:
        for mode in ("raw", "body_mock_diagnostic"):
            for key in ("baseline", "candidate"):
                run_id = f"{key}-{mode}"
                root = Path(work) / run_id
                files = dict(sources[key])
                changes = 0
                if mode == "body_mock_diagnostic":
                    text = files[PATHS[0]].decode()
                    text, changes = re.subn(r"document\s*:\s*\{", "document:{body:{dataset:{}},", text)
                    assert changes == 7
                    files[PATHS[0]] = text.encode()
                for name, value in files.items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(value)
                junit = args.output.resolve() / f"{run_id}.xml"
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    PATHS[0],
                    "-p",
                    "no:cacheprovider",
                    "--tb=line",
                    f"--junitxml={junit}",
                ]
                result = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True)
                output = result.stdout + result.stderr
                (args.output / f"{run_id}.txt").write_text(output)
                tree = ET.parse(junit)
                cases = []
                for case in tree.findall(".//testcase"):
                    failure = case.find("failure")
                    message = failure.get("message", "") if failure is not None else ""
                    cases.append(
                        {
                            "name": case.get("name"),
                            "outcome": "failed" if failure is not None else "passed",
                            "failure_message": message.replace(str(root), "<SOURCE>"),
                        }
                    )
                report["runs"].append(
                    {
                        "id": run_id,
                        "exit_code": result.returncode,
                        "command": [
                            part.replace(str(args.output.resolve()), "<EVIDENCE>") for part in command
                        ],
                        "temporary_test_edits": changes,
                        "test_sha256": sha(files[PATHS[0]]),
                        "app_sha256": sha(files[PATHS[1]]),
                        "cases": cases,
                    }
                )
    for mode in ("raw", "body_mock_diagnostic"):
        pairs = [run for run in report["runs"] if run["id"].endswith("-" + mode)]
        report[mode + "_case_outcomes_equal"] = [(c["name"], c["outcome"]) for c in pairs[0]["cases"]] == [
            (c["name"], c["outcome"]) for c in pairs[1]["cases"]
        ]
        report[mode + "_failure_messages_equal"] = pairs[0]["cases"] == pairs[1]["cases"]
    (args.output / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "runs": [
                    {
                        "id": r["id"],
                        "exit_code": r["exit_code"],
                        "passed": sum(c["outcome"] == "passed" for c in r["cases"]),
                        "failed": sum(c["outcome"] == "failed" for c in r["cases"]),
                    }
                    for r in report["runs"]
                ],
                "raw_outcomes_equal": report["raw_case_outcomes_equal"],
                "diagnostic_outcomes_equal": report["body_mock_diagnostic_case_outcomes_equal"],
            }
        )
    )


if __name__ == "__main__":
    main()
