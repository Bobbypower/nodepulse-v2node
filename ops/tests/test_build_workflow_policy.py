"""Contract for manually triggered, build-only hosted automation."""

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8"))


def test_only_manual_dispatch_triggers_a_build():
    doc = workflow()
    # PyYAML follows YAML 1.1 and interprets the unquoted GitHub key "on" as True.
    events = doc.get("on", doc.get(True))
    assert isinstance(events, dict)
    assert set(events) == {"workflow_dispatch"}


def test_hosted_jobs_do_not_run_test_suites():
    for job in workflow()["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert not re.search(r"\bgo\s+test\b|\bpytest\b|\bunittest\b", run), step.get("name")


def test_build_and_versioned_artifacts_remain_available():
    doc = workflow()
    build = doc["jobs"]["build"]
    assert {item["arch"] for item in build["strategy"]["matrix"]["include"]} == {"amd64", "arm64"}
    steps = {step.get("name"): step for step in build["steps"]}
    assert "go build" in steps["Build"]["run"]
    assert "go mod edit -replace" in steps["Build"]["run"]
    assert "upstream_sha" in steps["Build"]["run"] and "patch_sha" in steps["Build"]["run"]
    assert steps["Upload artifact"]["uses"].startswith("actions/upload-artifact@")
    release_steps = {step.get("name"): step for step in doc["jobs"]["release"]["steps"]}
    assert "SHA256SUMS" in release_steps["Add installer asset"]["run"]
    assert "--check-published" in release_steps["Guard immutable bootstrap source"]["run"]
    assert "dist/bootstrap-control.sha256" in release_steps["Publish immutable bootstrap source"]["with"]["files"]
