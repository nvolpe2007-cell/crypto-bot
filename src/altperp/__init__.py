"""
Mid-Cap Alt Perp Confluence Strategy (Bybit).

A short-biased funding/OI mean-reversion fade + post-liquidation flush long,
on mid-cap altcoin perpetuals. Self-contained package — does not depend on the
legacy strategy modules. Runs PAPER-only until a Bybit execution client exists
(see config.PAPER_TRADING). Reuses the project's Telegram notifier.
"""
