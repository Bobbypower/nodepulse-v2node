"""Test dispatch publishes pinned assets while main retains rolling releases."""
from pathlib import Path
import re

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


class UniqueWorkflowLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = set()
        for key, _ in node.value:
            value = self.construct_object(key, deep=deep)
            if value in keys:
                raise ValueError(f"Duplicate workflow mapping key: {value}")
            keys.add(value)
        return super().construct_mapping(node, deep=deep)


def release_job():
    workflow = yaml.load((ROOT / ".github/workflows/build.yml").read_text(), Loader=UniqueWorkflowLoader)
    return workflow["jobs"]["release"]


def test_duplicate_workflow_keys_are_rejected_even_when_the_values_match():
    with pytest.raises(ValueError, match="Duplicate workflow mapping key: if"):
        yaml.load("step:\n  if: main\n  if: main\n", Loader=UniqueWorkflowLoader)


def allowed(expression, context):
    """Evaluate only the comparison/disjunction subset used by these guards.

    Unknown syntax fails this contract instead of silently treating a future
    publishing condition as safe. The values come from workflow event contexts.
    """
    if expression is None:
        return True
    terms = []
    for term in expression.split("||"):
        match = re.fullmatch(r"\s*([a-zA-Z0-9_.]+)\s*(==|!=)\s*'([^']*)'\s*", term)
        assert match, f"Unreviewed release condition: {term}"
        key, operator, value = match.groups()
        assert key in context, f"Missing event context: {key}"
        equal = context[key] == value
        terms.append(equal if operator == "==" else not equal)
    return any(terms)


@pytest.mark.parametrize("ref,event,job_runs,rolling", [
    ("refs/heads/main", "push", True, True),
    ("refs/heads/main", "schedule", True, True),
    ("refs/heads/main", "workflow_dispatch", True, True),
    ("refs/heads/codex/jp4-runtime-test", "workflow_dispatch", True, False),
    ("refs/tags/review-source", "workflow_dispatch", True, False),
    ("refs/heads/codex/jp4-runtime-test", "push", False, False),
    ("refs/heads/codex/jp4-runtime-test", "schedule", False, False),
])
def test_event_scope_keeps_shared_latest_exclusive_to_main(ref, event, job_runs, rolling):
    job = release_job()
    context = {"github.ref": ref, "github.event_name": event,
               "steps.bootstrap_source.outputs.exists": "false"}
    assert allowed(job.get("if"), context) is job_runs
    steps = {step["name"]: step for step in job["steps"]}
    for name in ["Move rolling release tag", "Publish rolling release"]:
        assert (job_runs and allowed(steps[name].get("if"), context)) is rolling
    assert (job_runs and allowed(steps["Publish immutable bootstrap source"].get("if"), context)) is job_runs


def test_test_release_is_bound_to_exact_commit_and_never_marked_latest():
    job = release_job()
    step = next(step for step in job["steps"] if step["name"] == "Publish immutable bootstrap source")
    options = step["with"]
    assert options["tag_name"] == "nodepulse-${{ github.sha }}"
    assert options["target_commitish"] == "${{ github.sha }}"
    assert options["make_latest"] is False
    assert options["prerelease"] is True
    context = {"github.ref": "refs/heads/codex/jp4-runtime-test", "github.event_name": "workflow_dispatch",
               "steps.bootstrap_source.outputs.exists": "true"}
    assert not allowed(step["if"], context)  # Existing identical source is not republished.


def test_every_step_that_touches_shared_latest_is_guarded_on_main():
    matches = []
    for step in release_job()["steps"]:
        if "v2node-nodepulse-latest" not in str(step.get("run", "")) + str(step.get("with", {})):
            continue
        matches.append(step["name"])
        for event in ["push", "schedule", "workflow_dispatch"]:
            assert not allowed(step.get("if"), {"github.ref": "refs/heads/codex/test", "github.event_name": event})
    assert set(matches) == {"Move rolling release tag", "Publish rolling release"}


def test_immutable_assets_include_every_file_in_the_existing_checksum_recipe():
    step = next(step for step in release_job()["steps"] if step["name"] == "Publish immutable bootstrap source")
    files = set(step["with"]["files"].splitlines())
    assert {"dist/v2node-*", "dist/nodepulse-edge-gateway.tar.gz", "dist/install-nodepulse-v2node.sh",
            "dist/install-nodepulse-edge.sh", "dist/SHA256SUMS", "dist/bootstrap-control.tar.gz",
            "dist/bootstrap-control.sha256"} <= files
