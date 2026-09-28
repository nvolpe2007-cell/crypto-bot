import subprocess
import sys

import pytest

from polymarket_bot.live_execution import _build_order_preview


def test_build_order_preview_computes_notional(monkeypatch):
    monkeypatch.setattr(
        "polymarket_bot.live_execution.get_midpoint_price", lambda token_id: 0.5
    )
    preview = _build_order_preview("tok1", "BUY", price=0.5, size=10)
    assert preview["notional_usdc"] == 5.0
    assert preview["current_midpoint"] == 0.5
    assert preview["token_id"] == "tok1"


def test_live_without_env_vars_refuses(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS", raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "polymarket_bot.live_execution",
            "order",
            "--token-id",
            "tok1",
            "--side",
            "BUY",
            "--price",
            "0.5",
            "--size",
            "10",
            "--live",
        ],
        capture_output=True,
        text=True,
        cwd=__file__.rsplit("/tests/", 1)[0],
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": __file__.rsplit("/tests/", 1)[0]},
        timeout=30,
    )
    assert result.returncode == 1
    assert "POLYMARKET_PRIVATE_KEY" in result.stderr


def test_dry_run_never_prompts_for_confirmation(monkeypatch, capsys):
    monkeypatch.setattr(
        "polymarket_bot.live_execution.get_midpoint_price", lambda token_id: 0.5
    )
    from argparse import Namespace
    from polymarket_bot.live_execution import _cmd_order

    args = Namespace(token_id="tok1", side="BUY", price=0.5, size=10, live=False)
    _cmd_order(args)  # would raise if it tried to call input()
    out = capsys.readouterr().out
    assert "Dry-run only" in out
