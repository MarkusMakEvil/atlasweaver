from __future__ import annotations

import copy
import json
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PINNED_ACTION = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$"
)


class WorkflowLoader(yaml.SafeLoader):
    pass


WorkflowLoader.yaml_implicit_resolvers = copy.deepcopy(
    yaml.SafeLoader.yaml_implicit_resolvers
)
for first, resolvers in list(WorkflowLoader.yaml_implicit_resolvers.items()):
    WorkflowLoader.yaml_implicit_resolvers[first] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def load_workflow(name: str) -> dict[str, object]:
    value = yaml.load(
        (WORKFLOWS / name).read_text(encoding="utf-8"), Loader=WorkflowLoader
    )
    assert isinstance(value, dict)
    return value


def action_uses(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "uses":
                assert isinstance(item, str)
                found.append(item)
            else:
                found.extend(action_uses(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(action_uses(item))
    return tuple(found)


def steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = next(iter(jobs.values()))
    assert isinstance(job, dict) and isinstance(job["steps"], list)
    return job["steps"]


def step_named(workflow: dict[str, object], name: str) -> dict[str, object]:
    return next(item for item in steps(workflow) if item.get("name") == name)


def test_check_workflow_has_closed_typed_inputs_and_read_only_permissions() -> None:
    workflow = load_workflow("atlasweaver-check.yml")
    call = workflow["on"]["workflow_call"]
    assert set(call["inputs"]) == {
        "backend",
        "deep-mode",
        "model",
        "python-version",
        "repo-root",
        "require-impact-trust",
    }
    assert set(call["secrets"]) == {"semantic_backend_token"}
    assert workflow["permissions"] == {"contents": "read"}
    assert all(
        job.get("permissions", {"contents": "read"}) == {"contents": "read"}
        for job in workflow["jobs"].values()
    )


def test_every_external_action_reference_is_a_full_commit_sha() -> None:
    for path in WORKFLOWS.glob("*.yml"):
        for uses in action_uses(load_workflow(path.name)):
            assert PINNED_ACTION.fullmatch(uses), (path, uses)


def test_ci_fetches_history_and_derives_graphify_version_from_registry() -> None:
    workflow = load_workflow("ci.yml")
    checkout = next(
        item for item in steps(workflow) if str(item.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "production_graphify_compatibility" in text
    assert "graphifyy==0.9.48" not in text


def test_check_never_executes_consumer_commands_or_uploads_graph_content() -> None:
    workflow = load_workflow("atlasweaver-check.yml")
    text = (WORKFLOWS / "atlasweaver-check.yml").read_text(encoding="utf-8")
    assert "test-command" not in text and "shell-command" not in text
    assert "python -m project_knowledge.workflow_boundary check" in text
    assert all(
        name in text
        for name in (
            "preflight.json",
            "doctor.json",
            "scan.json",
            "health.json",
            "GITHUB_STEP_SUMMARY",
        )
    )
    upload = step_named(workflow, "Upload content-free summary")
    serialized = json.dumps(upload, sort_keys=True)
    assert "graphify-out" not in serialized
    assert "atlasweaver-check-summary.json" in serialized


def test_check_scopes_semantic_secret_to_one_mode_step() -> None:
    workflow = load_workflow("atlasweaver-check.yml")
    semantic = step_named(workflow, "Semantic preflight")
    code_only = step_named(workflow, "Code-only preflight")
    admission = step_named(workflow, "Validate extraction inputs")
    assert semantic["if"] == "${{ inputs.backend != '' && inputs.model != '' }}"
    assert code_only["if"] == (
        "${{ inputs.backend == '' && inputs.model == '' && !inputs.deep-mode }}"
    )
    assert semantic["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(code_only, sort_keys=True)
    assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(admission, sort_keys=True)
    for item in steps(workflow):
        if item is semantic:
            continue
        assert "ATLASWEAVER_BACKEND_TOKEN" not in json.dumps(item, sort_keys=True)


def test_reusable_workflow_uses_hard_pinned_trusted_tool_checkout() -> None:
    workflow = load_workflow("atlasweaver-check.yml")
    tool_sha = workflow["env"]["ATLASWEAVER_TOOL_SHA"]
    assert re.fullmatch(r"[0-9a-f]{40}", tool_sha)
    text = json.dumps(workflow, sort_keys=True)
    raw = (WORKFLOWS / "atlasweaver-check.yml").read_text(encoding="utf-8")
    assert "github.workflow_sha" not in text
    assert '--project "$GITHUB_WORKSPACE/atlasweaver-tool"' in raw
    assert '--project "$GITHUB_WORKSPACE/consumer"' not in raw


def test_publish_workflow_splits_privilege_and_serializes_release_identity() -> None:
    workflow = load_workflow("atlasweaver-publish.yml")
    jobs = workflow["jobs"]
    assert set(jobs) == {"inspect", "build", "publish"}
    assert jobs["inspect"]["permissions"] == {"contents": "read"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["publish"]["permissions"] == {
        "attestations": "write",
        "contents": "write",
        "id-token": "write",
    }
    assert jobs["publish"]["runs-on"] == "ubuntu-24.04"
    assert jobs["publish"]["concurrency"] == {
        "cancel-in-progress": False,
        "group": (
            "atlasweaver-publish-${{ github.repository_id }}-"
            "${{ needs.inspect.outputs.project_uid }}-"
            "${{ needs.inspect.outputs.channel }}"
        ),
    }


def test_publish_privileged_job_attests_before_verified_upload() -> None:
    workflow = load_workflow("atlasweaver-publish.yml")
    publish = workflow["jobs"]["publish"]
    serialized = json.dumps(publish, sort_keys=True)
    assert all(term not in serialized for term in (
        "graphify extract", "project-knowledge refresh", "pytest",
        "semantic_backend_token",
    ))
    names = [item.get("name", item.get("uses", "")) for item in publish["steps"]]
    assert names.index("Attest release bundle") < names.index(
        "Verify reusable-workflow signer"
    ) < names.index("Upload verified rolling release asset")
    token_steps = {
        item["name"]
        for item in publish["steps"]
        if "GITHUB_TOKEN" in item.get("env", {})
    }
    assert token_steps == {
        "Verify reusable-workflow signer",
        "Upload verified rolling release asset",
    }


def test_publish_secret_is_confined_to_semantic_unprivileged_build() -> None:
    workflow = load_workflow("atlasweaver-publish.yml")
    hits = []
    for job_name, job in workflow["jobs"].items():
        for item in job["steps"]:
            if "ATLASWEAVER_BACKEND_TOKEN" in json.dumps(item, sort_keys=True):
                hits.append((job_name, item))
    assert len(hits) == 1
    job_name, semantic = hits[0]
    assert job_name == "build"
    assert semantic["name"] == "Refresh private graph"
    assert semantic["if"] == "${{ inputs.backend != '' && inputs.model != '' }}"
    assert semantic["env"]["ATLASWEAVER_BACKEND_TOKEN"] == (
        "${{ secrets.semantic_backend_token }}"
    )
