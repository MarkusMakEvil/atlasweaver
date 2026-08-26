from __future__ import annotations

import argparse

import pytest

from project_knowledge.cli_distribution import add_distribution_commands


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    subparsers = value.add_subparsers(dest="command", required=True)
    add_distribution_commands(subparsers)
    return value


def test_distribution_parser_exposes_only_closed_public_operations() -> None:
    root = parser()
    root_action = next(
        action for action in root._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(root_action.choices) == {
        "artifact", "pull", "install-agent", "uninstall-agent", "fleet"
    }
    artifact = root_action.choices["artifact"]
    artifact_action = next(
        action for action in artifact._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(artifact_action.choices) == {"pack", "install"}
    fleet = root_action.choices["fleet"]
    fleet_action = next(
        action for action in fleet._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert set(fleet_action.choices) == {
        "doctor", "health", "pull", "refresh", "registry-sync", "query"
    }
    assert "publish" not in root_action.choices


@pytest.mark.parametrize(
    "arguments",
    [
        ["artifact", "pack", "--repo", ".", "--output", "graph.zip"],
        ["artifact", "install", "--repo", ".", "--bundle", "graph.zip"],
        ["pull", "--repo", "."],
        ["install-agent", "--platform", "codex"],
        ["uninstall-agent", "--platform", "agents"],
        ["fleet", "health", "--workspace", "fleet.yaml"],
        [
            "fleet", "query", "--workspace", "fleet.yaml",
            "affected", "node", "--depth", "2", "--relation", "calls",
        ],
    ],
)
def test_distribution_parser_accepts_documented_shapes(arguments: list[str]) -> None:
    parser().parse_args(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["artifact", "publish"],
        ["pull", "--repo", ".", "--gh-binary", "/tmp/gh"],
        ["fleet", "refresh", "--workspace", "fleet.yaml", "--graphify-binary", "x"],
    ],
)
def test_distribution_parser_rejects_publish_and_binary_overrides(
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(arguments)
