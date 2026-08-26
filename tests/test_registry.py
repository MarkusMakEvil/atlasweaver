from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from project_knowledge.models import FeatureIntent
from project_knowledge.registry import (
    DOCUMENTED_REGISTRY_ERROR_CODES,
    RegistryError,
    RegistryQueryRequest,
    capture_registry_snapshot,
    query_registry,
    registry_key,
    registry_status,
    registry_sync,
)
from project_knowledge.lifecycle import RefreshOptions, refresh_project
from tests.support import DEMO_UID, manifest_v1, manifest_v2, write_manifest_v2
from tests.test_lifecycle import (
    Official0948FixtureRunner,
    _fixture_graphify,
    _source_repository,
)


def enabled_manifest():
    return manifest_v2(features=FeatureIntent(registry="enabled"))


class GlobalFixtureRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, **options):
        call = tuple(argv)
        self.calls.append(call)
        graph_path = Path(call[3])
        key = call[5]
        source = json.loads(graph_path.read_text(encoding="utf-8"))
        node_ids = {item["id"] for item in source["nodes"]}
        nodes = []
        for item in source["nodes"]:
            copied = dict(item)
            copied["id"] = f"{key}::{item['id']}"
            nodes.append(copied)
        links = []
        for item in source["links"]:
            copied = dict(item)
            for field in ("source", "target"):
                if copied[field] in node_ids:
                    copied[field] = f"{key}::{copied[field]}"
            links.append(copied)
        home = Path(options["env"]["HOME"])
        root = home / ".graphify"
        root.mkdir(parents=True, exist_ok=True)
        (root / "global-graph.json").write_text(
            json.dumps(
                {
                    "directed": False,
                    "multigraph": False,
                    "graph": {},
                    "nodes": nodes,
                    "links": links,
                }
            ),
            encoding="utf-8",
        )
        (root / "global-manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "repos": {
                        key: {
                            "added_at": "2026-08-26T00:00:00+00:00",
                            "source_path": str(graph_path.resolve()),
                            "node_count": len(nodes),
                            "edge_count": len(links),
                            "source_hash": sha256(graph_path.read_bytes()).hexdigest()[:16],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return CompletedProcess(call, 0, "", "")


def owned_registry_repository(tmp_path: Path):
    repo, original = _source_repository(tmp_path)
    selected = replace(original, features=FeatureIntent(registry="enabled"))
    write_manifest_v2(
        repo,
        include_roots=selected.include_roots,
        features=selected.features,
    )
    executable = _fixture_graphify(tmp_path)
    refresh_project(
        repo,
        selected,
        RefreshOptions(None, None, False, True),
        runner=Official0948FixtureRunner(executable.resolve()),
        ambient={},
        graphify_binary=executable,
    )
    return repo, selected, executable


def test_registry_error_allowlist_and_uid_namespace_are_closed() -> None:
    assert DOCUMENTED_REGISTRY_ERROR_CODES == frozenset(
        {
            "registry_disabled",
            "registry_busy",
            "registry_unmanaged_state",
            "registry_recovery_required",
            "registry_source_changed",
            "manifest_migration_required",
            "registry_snapshot_missing",
            "registry_snapshot_stale",
            "registry_snapshot_mismatch",
            "registry_snapshot_too_large",
            "registry_snapshot_busy",
        }
    )
    assert registry_key(manifest_v2()) == f"atlasweaver/{DEMO_UID}"

    with pytest.raises(RegistryError) as raised:
        registry_key(manifest_v1())
    assert raised.value.code == "manifest_migration_required"


def test_disabled_status_is_read_only_and_creates_no_user_state(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_manifest_v2(repo)
    user_root = tmp_path / "user"

    result = registry_status(repo, manifest_v2(), user_root=user_root)

    assert result.status == "disabled"
    assert result.issues == ()
    assert not user_root.exists()


def test_enabled_status_without_managed_state_is_missing_and_noncreating(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_manifest_v2(repo, features=FeatureIntent(registry="enabled"))
    user_root = tmp_path / "user"

    result = registry_status(repo, enabled_manifest(), user_root=user_root)

    assert result.status == "missing"
    assert result.issues == ()
    assert not user_root.exists()


def test_status_rejects_supplied_manifest_drift_before_reading_registry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    write_manifest_v2(repo, features=FeatureIntent(registry="enabled"))

    with pytest.raises(Exception):
        registry_status(
            repo,
            replace(enabled_manifest(), display_name="Different"),
            user_root=tmp_path / "user",
        )


def test_sync_writes_uid_namespace_without_mutating_the_project_graph(
    tmp_path: Path,
) -> None:
    repo, selected, executable = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    before = tuple(
        (path.relative_to(repo / "graphify-out").as_posix(), path.read_bytes())
        for path in sorted((repo / "graphify-out").iterdir())
        if path.is_file()
    )

    result = registry_sync(
        repo,
        selected,
        user_root=user_root,
        runner=GlobalFixtureRunner(),
        graphify_binary=executable,
    )

    document = json.loads(
        (user_root / "atlasweaver/registry.json").read_text(encoding="utf-8")
    )
    assert result.status == "synced"
    assert result.key == f"atlasweaver/{DEMO_UID}"
    assert list(document["entries"]) == [result.key]
    assert registry_status(repo, selected, user_root=user_root).status == "current"
    assert before == tuple(
        (path.relative_to(repo / "graphify-out").as_posix(), path.read_bytes())
        for path in sorted((repo / "graphify-out").iterdir())
        if path.is_file()
    )


def test_unchanged_sync_reuses_the_exact_snapshot_and_generation(
    tmp_path: Path,
) -> None:
    repo, selected, executable = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    first = registry_sync(
        repo,
        selected,
        user_root=user_root,
        runner=GlobalFixtureRunner(),
        graphify_binary=executable,
    )

    second = registry_sync(
        repo,
        selected,
        user_root=user_root,
        runner=GlobalFixtureRunner(),
        graphify_binary=executable,
    )

    assert second.status == "unchanged"
    assert second.generation == first.generation == 1
    assert second.snapshot_digest == first.snapshot_digest


def test_source_change_makes_status_stale_without_registry_mutation(
    tmp_path: Path,
) -> None:
    repo, selected, executable = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    registry_sync(
        repo,
        selected,
        user_root=user_root,
        runner=GlobalFixtureRunner(),
        graphify_binary=executable,
    )
    before = tuple(
        (path.relative_to(user_root).as_posix(), path.read_bytes())
        for path in sorted(user_root.rglob("*"))
        if path.is_file()
    )
    (repo / "fixture.py").write_text("changed\n", encoding="utf-8")

    assert registry_status(repo, selected, user_root=user_root).status == "stale"
    assert before == tuple(
        (path.relative_to(user_root).as_posix(), path.read_bytes())
        for path in sorted(user_root.rglob("*"))
        if path.is_file()
    )


def test_capture_and_query_registry_use_only_validated_snapshot_bytes(
    tmp_path: Path,
) -> None:
    repo, selected, executable = owned_registry_repository(tmp_path)
    user_root = tmp_path / "user"
    synced = registry_sync(
        repo,
        selected,
        user_root=user_root,
        runner=GlobalFixtureRunner(),
        graphify_binary=executable,
    )

    snapshot = capture_registry_snapshot(
        (DEMO_UID,),
        require_graphify_projection=True,
        user_root=user_root,
    )
    envelope = query_registry(
        snapshot,
        RegistryQueryRequest(command="query", term="fixture", limit=20),
    )

    assert snapshot.generation == synced.generation
    assert snapshot.entries[0].snapshot_digest == synced.snapshot_digest
    assert snapshot.entries[0].project_uid == DEMO_UID
    assert envelope.command == "query"
    assert envelope.trust == "navigation"
