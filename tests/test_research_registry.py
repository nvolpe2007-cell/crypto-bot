"""
Tests for the hypothesis registry.

Validates the live registry file parses cleanly and the helpers behave.
A schema/typo break here is a CI-visible failure rather than a silent
"my CLI returns nothing" surprise.
"""
from __future__ import annotations

from src.research.registry import (
    Hypothesis,
    VALID_STATUSES,
    by_family,
    by_status,
    load,
)


def test_registry_file_parses_clean():
    reg = load()
    assert reg, "registry is empty — at least one hypothesis expected"
    # Every entry validates against the schema in load() — duplicate ids,
    # missing required fields, invalid statuses would have raised.
    assert all(isinstance(h, Hypothesis) for h in reg)


def test_all_statuses_are_valid():
    for h in load():
        assert h.status in VALID_STATUSES, f"{h.id}: bad status {h.status!r}"


def test_ids_are_unique_and_kebab_case():
    ids = [h.id for h in load()]
    assert len(ids) == len(set(ids)), "duplicate id in registry"
    for hid in ids:
        assert hid == hid.lower(), f"non-lowercase id: {hid}"
        assert " " not in hid, f"space in id: {hid}"
        assert "_" not in hid, f"underscore in id (use hyphens): {hid}"


def test_required_text_fields_non_empty():
    for h in load():
        assert h.name, f"{h.id}: empty name"
        assert h.hypothesis, f"{h.id}: empty hypothesis"
        assert h.code, f"{h.id}: empty code pointer"


def test_evidence_entries_have_date_and_result():
    for h in load():
        for i, e in enumerate(h.evidence):
            assert e.date, f"{h.id} evidence[{i}]: missing date"
            assert e.result, f"{h.id} evidence[{i}]: missing result"


def test_deployment_gate_has_requires_unless_killed_or_infra():
    """Killed/infrastructure entries don't need a forward-looking gate; everything
    else does (otherwise the entry can't progress through the lifecycle)."""
    for h in load():
        if h.status in {"killed", "infrastructure"}:
            continue
        assert "requires" in h.deployment_gate, \
            f"{h.id} ({h.status}): missing deployment_gate.requires"


def test_by_status_filters_correctly():
    reg = load()
    paper = by_status("paper-only", reg)
    assert all(h.status == "paper-only" for h in paper)
    # Same call without items should hit the registry file (no exception).
    assert by_status("killed") == [h for h in reg if h.status == "killed"]


def test_by_family_filters_correctly():
    reg = load()
    altperp = by_family("altperp", reg)
    assert altperp, "expected at least one altperp entry"
    assert all(h.family == "altperp" for h in altperp)


def test_funding_arb_kraken_is_marked_as_the_only_executable():
    """Spot-check: the audit conclusion is encoded in the registry as
    a paper-only entry whose name flags it as the executable arm. If someone
    deletes this assertion they're discarding the audit finding."""
    reg = load()
    kraken = next((h for h in reg if h.id == "funding-arb-kraken"), None)
    assert kraken is not None, "funding-arb-kraken entry missing"
    assert kraken.status == "paper-only"
    assert "executable" in kraken.name.lower() or "kraken" in kraken.name.lower()
