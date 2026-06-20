"""
stockbot — an isolated, paper/sim-only intraday US-equities backtest project.

Self-contained (zero coupling to the crypto bot): it just happens to live in this
repo because that's the only place this environment can persist + push code. It is
a clean candidate to `git subtree split` into its own repo.

Philosophy is identical to the crypto bot: honest costs, no look-ahead, a strict
pre-registered proof bar — and the explicit stance that retail SCALPING loses, so
this builds the defensible *intraday momentum* (ORB) version instead, judged on
the data rather than asserted.
"""
