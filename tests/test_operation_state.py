from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_knowledge.operation_state import (
    CoverageMetrics,
    FailureRecord,
    OperationStateError,
    SuccessfulOperation,
    load_operation_state,
    record_success,
    record_failure,
    render_ci_summary,
)
from project_knowledge.locking import (
    capture_lifecycle_repository,
    open_repository_access,
    repository_lifecycle_lock,
)


def _success() -> SuccessfulOperation:
    return SuccessfulOperation(
        operation="artifact_install", duration_ms=125, safe_file_count=7,
        coverage=CoverageMetrics(6, 1, 3), source_digest="1" * 64,
        projection_digest="2" * 64, graph_digest="3" * 64,
        generation_digest="4" * 64, build_epoch=7, artifact_channel=None,
    )


def test_state_is_closed_and_content_free(tmp_path: Path) -> None:
    record_success(tmp_path, _success())
    path = tmp_path / ".project-knowledge/state.json"
    payload = path.read_text(encoding="utf-8")
    assert set(json.loads(payload)) == {
        "last_failure", "last_success", "schema_version"
    }
    assert str(tmp_path) not in payload
    assert all(
        term not in payload
        for term in ("src/app.py", "query", "fingerprint", "API_KEY")
    )
    assert load_operation_state(tmp_path).last_success == _success()


def test_artifact_success_requires_complete_installed_identity() -> None:
    with pytest.raises(OperationStateError, match="state_invalid"):
        SuccessfulOperation(
            operation="artifact_pack",
            duration_ms=1,
            safe_file_count=1,
            coverage=CoverageMetrics(1, 0, 0),
            source_digest="1" * 64,
            projection_digest="2" * 64,
            graph_digest=None,
            generation_digest=None,
            build_epoch=None,
            artifact_channel=None,
        )


def test_read_only_load_and_render_create_nothing(tmp_path: Path) -> None:
    assert load_operation_state(tmp_path).last_success is None
    machine, markdown = render_ci_summary([
        {"project_id": "demo", "status": "healthy"}
    ])
    assert json.loads(machine)["results"] == [
        {"project_id": "demo", "status": "healthy"}
    ]
    assert "demo" in markdown
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key", ["path", "source_file", "query", "environment", "fingerprint", "message"])
def test_summary_rejects_content_bearing_keys(key: str) -> None:
    with pytest.raises(OperationStateError, match="summary_invalid"):
        render_ci_summary([{"status": "failed", key: "private"}])


def test_state_rejects_duplicate_keys(tmp_path: Path) -> None:
    state = tmp_path / ".project-knowledge"
    state.mkdir()
    (state / "state.json").write_text(
        '{"schema_version":1,"schema_version":1,"last_success":null,"last_failure":null}\n',
        encoding="utf-8",
    )
    with pytest.raises(OperationStateError, match="state_invalid"):
        load_operation_state(tmp_path)


def test_writer_authorities_are_mutually_exclusive_and_access_requires_lease(
    tmp_path: Path,
) -> None:
    with open_repository_access(tmp_path) as repository:
        with pytest.raises(OperationStateError, match="state_authority_invalid"):
            record_success(tmp_path, _success(), repository_access=repository)

    with repository_lifecycle_lock(tmp_path):
        with capture_lifecycle_repository(tmp_path) as repository:
            with pytest.raises(OperationStateError, match="state_authority_invalid"):
                record_success(
                    tmp_path,
                    _success(),
                    repository_access=repository,
                    expected_repository_identity=repository.identity,
                )
            record_success(tmp_path, _success(), repository_access=repository)

    assert load_operation_state(tmp_path).last_success == _success()


def test_interrupted_replace_preserves_previous_state(tmp_path: Path) -> None:
    class FaultingFS:
        def checkpoint(self, operation: str) -> None:
            if operation == "replace":
                raise OSError("injected")

    record_success(tmp_path, _success())
    before = (tmp_path / ".project-knowledge/state.json").read_bytes()

    with pytest.raises(OperationStateError, match="state_write_failed"):
        record_failure(
            tmp_path,
            FailureRecord("artifact_install", "bundle_invalid"),
            fs=FaultingFS(),
        )

    assert (tmp_path / ".project-knowledge/state.json").read_bytes() == before


def test_state_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    state = tmp_path / ".project-knowledge"
    state.mkdir()
    document = {
        "schema_version": 1,
        "last_failure": None,
        "last_success": {
            **json.loads(json.dumps(_success(), default=lambda item: item.__dict__)),
            "private_path": "/private/source.py",
        },
    }
    (state / "state.json").write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OperationStateError, match="state_invalid"):
        load_operation_state(tmp_path)
