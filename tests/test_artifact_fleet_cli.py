from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from project_knowledge.cli_distribution import (
    add_distribution_commands,
    dispatch_distribution,
)
from project_knowledge.fleet import FleetProjectOutcome
from tests.test_fleet import _valid_workspace


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    subparsers = value.add_subparsers(dest="command", required=True)
    add_distribution_commands(subparsers)
    return value


@pytest.mark.parametrize(
    "arguments",
    [
        ("query", "term", "--limit", "0"),
        ("query", "term", "--limit", "101"),
        ("path", "source", "target", "--max-depth", "0"),
        ("path", "source", "target", "--max-depth", "33"),
        ("explain", "node", "--depth", "0"),
        ("explain", "node", "--depth", "3"),
        ("affected", "node", "--depth", "-1"),
        ("affected", "node", "--depth", "9"),
    ],
)
def test_fleet_query_parser_rejects_values_outside_public_caps(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(
            ("fleet", "query", "--workspace", "fleet.yaml", *arguments)
        )


def test_fleet_query_parser_rejects_more_than_sixteen_relations() -> None:
    arguments = [
        "fleet",
        "query",
        "--workspace",
        "fleet.yaml",
        "affected",
        "node",
    ]
    for index in range(17):
        arguments.extend(("--relation", f"relation-{index}"))
    with pytest.raises(SystemExit):
        parser().parse_args(arguments)


def test_fleet_refresh_promoted_but_stale_uses_global_exit_three(
    tmp_path: Path,
) -> None:
    workspace = _valid_workspace(tmp_path)
    arguments = parser().parse_args(
        (
            "fleet",
            "refresh",
            "--workspace",
            str(workspace),
            "--code-only",
            "--json",
        )
    )

    response = dispatch_distribution(
        arguments,
        read_secret=lambda name: pytest.fail(f"unexpected secret read: {name}"),
        fleet_runner=lambda admission, operation, request: FleetProjectOutcome(
            "promoted_but_stale",
            {
                "status": "promoted_but_stale",
                "project_id": admission.project.id,
            },
        ),
    )

    assert response.exit_code == 3
    assert response.document["status"] == "promoted_but_stale"
    assert response.document["command"] == "fleet refresh"
