from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import project_knowledge.workflow_publish as publisher


def workflow_environment(monkeypatch: pytest.MonkeyPatch, temporary: Path) -> None:
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_REPOSITORY_ID": "1234",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "5678",
        "RUNNER_TEMP": str(temporary),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_workflow_context_is_closed_and_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_environment(monkeypatch, tmp_path)
    context = publisher.WorkflowContext.from_environment()
    assert context == publisher.WorkflowContext(
        "acme/widgets", 1234, "refs/heads/main", "branch", True,
        "a" * 40, 5678,
    )

    monkeypatch.setenv("GITHUB_REF_PROTECTED", "false")
    with pytest.raises(publisher.WorkflowPublishError) as raised:
        publisher.WorkflowContext.from_environment()
    assert raised.value.code == "workflow_source_mismatch"


def test_publication_handoff_is_canonical_private_and_rejects_unknown_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_environment(monkeypatch, tmp_path)
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"bundle")
    handoff_path = tmp_path / "publication.json"
    context = publisher.WorkflowContext.from_environment()
    handoff = publisher.WorkflowPublicationHandoff.capture(
        (11, 22), "b" * 64, context, bundle
    )
    publisher.write_publication_handoff(handoff_path, handoff)
    assert handoff_path.stat().st_mode & 0o777 == 0o600
    assert publisher.load_publication_handoff(handoff_path) == handoff
    assert json.loads(handoff_path.read_text(encoding="utf-8")) == handoff.to_dict()

    document = handoff.to_dict()
    document["path"] = "/private/source"
    handoff_path.unlink()
    handoff_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(publisher.WorkflowPublishError) as raised:
        publisher.load_publication_handoff(handoff_path)
    assert raised.value.code == "workflow_source_mismatch"


@pytest.mark.parametrize(
    ("verb", "specific"),
    [
        ("prepare", ("--input", "input.zip", "--output", "output.zip")),
        ("verify-attestation", ("--bundle", "bundle.zip")),
        ("upload", ("--bundle", "bundle.zip")),
    ],
)
def test_internal_publication_parser_has_only_phase_specific_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    specific: tuple[str, ...],
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        publisher,
        f"run_workflow_{verb.replace('-', '_')}",
        lambda *args: calls.append((verb, args)),
    )
    common = (
        "--consumer-checkout", str(tmp_path / "consumer"),
        "--repo-root", ".",
        "--trusted-tool-checkout", str(tmp_path / "trusted"),
    )
    tail = (*specific, "--handoff", str(tmp_path / "handoff.json"))
    assert publisher._main([verb, *common, *tail]) == 0
    assert len(calls) == 1


def test_post_prepare_validation_precedes_token_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "must-remain-unread")
    consumer = tmp_path / "consumer"
    trusted = tmp_path / "trusted"
    consumer.mkdir()
    trusted.mkdir()
    bundle = tmp_path / "bundle.zip"
    bundle.write_bytes(b"changed")
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")

    with pytest.raises(publisher.WorkflowPublishError):
        publisher.run_workflow_verify_attestation(
            consumer, ".", trusted, bundle, handoff
        )
    assert os.environ["GITHUB_TOKEN"] == "must-remain-unread"
