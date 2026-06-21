"""Tests for the curated brain knowledge base (src/brain_knowledge.py)."""

import importlib

import src.brain_knowledge as bk


def test_knowledge_nonempty_and_curated(monkeypatch):
    monkeypatch.setenv("BRAIN_KNOWLEDGE", "1")
    importlib.reload(bk)
    txt = bk.brain_knowledge()
    assert isinstance(txt, str) and len(txt) > 500
    low = txt.lower()
    # scoped to the six understanding-targets + the graveyard
    assert "cost" in low and "flat is a real" in low
    assert "graveyard" in low
    assert "t≈-8.82" in txt or "-8.82" in txt          # the disproven scalper
    assert "fantasy" in low                            # aggressive funding arb honesty
    assert "structural" in low and "predictive" in low # the edge hierarchy
    assert "correlation" in low                        # BTC-beta risk


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("BRAIN_KNOWLEDGE", "0")
    importlib.reload(bk)
    assert bk.brain_knowledge() == ""
    importlib.reload(bk)
