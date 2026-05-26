"""
Order execution. PAPER mode simulates fills (slippage against us + taker fee);
LIVE mode is intentionally NOT implemented yet — there is no Bybit-or-Kraken
execution client wired for this strategy, and going live needs Kraken Futures
keys + isolated-margin setup. Until then, live calls fail safe (refuse + log)
rather than silently no-op.
"""

import logging
from dataclasses import dataclass

from . import config

logger = logging.getLogger(__name__)


@dataclass
class Fill:
    price: float        # actual fill price (after slippage)
    qty: float          # base units filled
    notional: float     # qty × fill price
    fee: float          # taker fee paid on this fill
    simulated: bool


class PaperExecutor:
    """Simulated fills. One instance per run; stateless beyond config."""

    def __init__(self, slippage_pct: float = config.PAPER_SLIPPAGE_PCT,
                 taker_fee: float = config.KRAKEN_TAKER_FEE):
        self.slippage_pct = slippage_pct
        self.taker_fee = taker_fee

    def set_isolated_leverage(self, coin: str, leverage: float):
        """Live: would POST isolated-margin + leverage before a trade. Paper: log."""
        if config.PAPER_TRADING:
            logger.debug("[PAPER] would set %s isolated %.0fx", coin, leverage)
            return True
        logger.error("[LIVE] isolated-margin/leverage not implemented — refusing")
        return False

    def _fill_price(self, side: str, price: float) -> float:
        """Slippage always against us: buys fill higher, sells fill lower."""
        slip = self.slippage_pct
        if side == "buy":
            return price * (1 + slip)
        return price * (1 - slip)

    def execute(self, coin: str, side: str, qty: float, price: float) -> Fill:
        """side: 'buy'/'sell'. Returns a Fill. Paper only for now."""
        if not config.PAPER_TRADING:
            raise NotImplementedError(
                "LIVE execution not implemented. Set ALTPERP_PAPER=1 (PAPER_TRADING) "
                "until a Kraken Futures execution client + keys are wired."
            )
        fp = self._fill_price(side, price)
        notional = qty * fp
        fee = notional * self.taker_fee
        logger.info("[PAPER FILL] %s %s qty=%.6f @ %.4f (slip from %.4f) notional=$%.2f fee=$%.4f",
                    coin, side.upper(), qty, fp, price, notional, fee)
        return Fill(price=fp, qty=qty, notional=notional, fee=fee, simulated=True)


def _selftest():
    ex = PaperExecutor(slippage_pct=0.0005, taker_fee=0.0005)
    # Sell (short entry): fills below mid
    f = ex.execute("SOLUSDT", "sell", 2.0, 100.0)
    assert abs(f.price - 99.95) < 1e-9 and abs(f.fee - (2.0 * 99.95 * 0.0005)) < 1e-9, f
    # Buy (cover): fills above mid
    f2 = ex.execute("SOLUSDT", "buy", 2.0, 100.0)
    assert abs(f2.price - 100.05) < 1e-9, f2
    # Live refused
    import types
    orig = config.PAPER_TRADING
    try:
        config.PAPER_TRADING = False
        try:
            ex.execute("SOLUSDT", "sell", 1.0, 100.0)
            assert False, "live should have raised"
        except NotImplementedError:
            pass
    finally:
        config.PAPER_TRADING = orig
    print("orders selftest OK")


if __name__ == "__main__":
    _selftest()
