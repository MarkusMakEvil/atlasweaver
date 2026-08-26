from __future__ import annotations

from pathlib import Path
from importlib import resources

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL_ROOT = ROOT / "skills/using-project-knowledge-graphs"
PACKAGED_SKILL_ROOT = resources.files("project_knowledge").joinpath(
    "resources/skills/using-project-knowledge-graphs"
)
SKILL_ROOTS = (SOURCE_SKILL_ROOT, PACKAGED_SKILL_ROOT)


def skill_text(skill_root) -> str:
    return skill_root.joinpath("SKILL.md").read_text(encoding="utf-8")


def frontmatter(skill_root) -> dict[str, object]:
    text = skill_text(skill_root)
    assert text.startswith("---\n")
    _, block, _ = text.split("---", 2)
    value = yaml.safe_load(block)
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_skill_routes_graph_work_without_mutation_authority(skill_root) -> None:
    text = skill_text(skill_root)
    lowered = text.lower()

    assert "project-knowledge preflight" in text
    assert "$graphify" in text
    assert "exact approval" in lowered
    assert "never commit or push implicitly" in lowered
    assert "read-only" in lowered


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_description_is_trigger_only_and_discriminating(skill_root) -> None:
    metadata = frontmatter(skill_root)
    description = metadata["description"]

    assert metadata["name"] == "using-project-knowledge-graphs"
    assert isinstance(description, str)
    assert description.startswith("Use when ")
    assert len(description) < 500
    assert "substantive" in description
    assert "workflow" not in description.lower()


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_skill_routes_details_progressively(skill_root) -> None:
    text = skill_text(skill_root)

    assert "references/workflow.md" in text
    assert "Read `references/workflow.md`" in text
    assert skill_root.joinpath("references/workflow.md").is_file()


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_skill_preserves_provenance_freshness_and_checkpoint_contract(skill_root) -> None:
    text = skill_text(skill_root).lower()

    assert "graphify-extracted" in text
    assert "graphify-inferred" in text
    assert "source-verified" in text
    assert "stale" in text
    assert all(
        term in text
        for term in ("architecture", "schema", "public interface", "module boundary")
    )
    assert "trivial" in text


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_skill_keeps_generated_and_human_notes_isolated(skill_root) -> None:
    combined = (
        skill_text(skill_root)
        + "\n"
        + skill_root.joinpath("references/workflow.md").read_text(encoding="utf-8")
    ).lower()

    assert "generated/" in combined
    assert "notes/" in combined
    assert "unknown" in combined
    assert "abort" in combined
    assert "global deny" in combined
    assert "non-negotiable" in combined
    for account_specific in (
        "makevil" + "way",
        "markus" + "makevil",
        "threads " + "post",
        "telegram " + "post",
    ):
        assert account_specific not in combined


@pytest.mark.parametrize("skill_root", SKILL_ROOTS)
def test_skill_documents_receipt_adapter_secret_and_integrity_contracts(skill_root) -> None:
    combined = skill_text(skill_root) + "\n" + skill_root.joinpath(
        "references/workflow.md"
    ).read_text(encoding="utf-8")
    lowered = combined.lower()

    assert "project-knowledge scan-secrets" in combined
    assert "--receipt" in combined
    assert "project-knowledge adapt" in combined
    assert "--raw-candidate" in combined
    assert "--staged-input" in combined
    assert "impact_analysis_trusted" in combined
    assert "graph_integrity_degraded" in combined
    assert "non-bypassable" in lowered
