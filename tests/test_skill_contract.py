from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills/using-project-knowledge-graphs"
SKILL = SKILL_ROOT / "SKILL.md"
WORKFLOW = SKILL_ROOT / "references/workflow.md"


def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


def frontmatter() -> dict[str, object]:
    text = skill_text()
    assert text.startswith("---\n")
    _, block, _ = text.split("---", 2)
    value = yaml.safe_load(block)
    assert isinstance(value, dict)
    return value


def test_skill_routes_graph_work_without_mutation_authority() -> None:
    text = skill_text()
    lowered = text.lower()

    assert "project-knowledge preflight" in text
    assert "$graphify" in text
    assert "exact approval" in lowered
    assert "never commit or push implicitly" in lowered
    assert "read-only" in lowered


def test_description_is_trigger_only_and_discriminating() -> None:
    metadata = frontmatter()
    description = metadata["description"]

    assert metadata["name"] == "using-project-knowledge-graphs"
    assert isinstance(description, str)
    assert description.startswith("Use when ")
    assert len(description) < 500
    assert "substantive" in description
    assert "workflow" not in description.lower()


def test_skill_routes_details_progressively() -> None:
    text = skill_text()

    assert "references/workflow.md" in text
    assert "Read `references/workflow.md`" in text
    assert WORKFLOW.is_file()


def test_skill_preserves_provenance_freshness_and_checkpoint_contract() -> None:
    text = skill_text().lower()

    assert "graphify-extracted" in text
    assert "graphify-inferred" in text
    assert "source-verified" in text
    assert "stale" in text
    assert all(
        term in text
        for term in ("architecture", "schema", "public interface", "module boundary")
    )
    assert "trivial" in text


def test_skill_keeps_generated_and_human_notes_isolated() -> None:
    combined = (skill_text() + "\n" + WORKFLOW.read_text(encoding="utf-8")).lower()

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


def test_skill_documents_receipt_adapter_secret_and_integrity_contracts() -> None:
    combined = skill_text() + "\n" + WORKFLOW.read_text(encoding="utf-8")
    lowered = combined.lower()

    assert "project-knowledge scan-secrets" in combined
    assert "--receipt" in combined
    assert "project-knowledge adapt" in combined
    assert "--raw-candidate" in combined
    assert "--staged-input" in combined
    assert "impact_analysis_trusted" in combined
    assert "graph_integrity_degraded" in combined
    assert "non-bypassable" in lowered
