#!/usr/bin/env python3
"""Cohort base rates — what the meme market actually does, before any hypothesis.

WHAT THIS IS
------------
A DESCRIPTIVE report over `data/meme_cohort/`. It measures what happened to the
captured birth cohort and nothing else: how many died, how fast, how much was
ever tradeable, how far median and mean diverge, and how much of the reported
activity survives a unique-wallet sanity check.

There is NO signal here, no ranking, no prediction, and no recommendation. This
is the step that must come BEFORE a hypothesis, not a substitute for one. Its
purpose is to establish the base rate a hypothesis would have to beat -- and,
just as often, to show that the thing being hypothesised about is not tradeable
in the first place.

WHY THE ORDER MATTERS
---------------------
Every candidate edge this repo has killed died because a plausible story was
tested before the base rate was known. Once you know that (say) most pools never
clear a liquidity floor and the median outcome is negative net of cost, most
"strategies" are revealed as restatements of the base rate with extra steps.

READING IT HONESTLY
-------------------
* MORTALITY is computed only over pools old enough to have reached the horizon.
  A pool born an hour ago cannot inform the 24h death rate and is excluded from
  that row rather than counted as a survivor -- counting it would drag every
  death rate toward zero as the collector runs.
* MEDIAN vs MEAN is reported side by side deliberately. On fat-tailed data they
  disagree violently, and which one you quote silently decides your conclusion.
* Returns here are GROSS. They ignore round-trip cost entirely, so they are an
  upper bound on what was achievable, not an estimate of it. `callout_scorecard`
  is where net lives.
* Anything with n below the stated floor is labelled as such inline.

USAGE
-----
    python scripts/cohort_base_rates.py
    python scripts/cohort_base_rates.py --telegram
    python scripts/cohort_base_rates.py --json data/base_rates.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "meme_cohort"

# Below this, a rate is noise. Stated inline rather than left to the reader.
MIN_N = 30

# Horizons for the survival curve, in hours.
HORIZONS_H = (1.0, 6.0, 24.0, 72.0, 168.0)

# The liquidity floor screener_risk uses. Reported as "what fraction was ever
# tradeable at all", which is usually the headline finding.
LIQ_FLOOR = 50_000.0


def _say(line: str = "") -> None:
    print(line.encode("ascii", "replace").decode("ascii"))


def _f(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if (x != x or x in (float("inf"), float("-inf"))) else x


def _pctile(sorted_vals, q):
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(len(sorted_vals) * q)))
    return sorted_vals[i]


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _usd(v, dp=0):
    return "n/a" if v is None else "$" + format(v, ",.%df" % dp)


def _pct(v, dp=1):
    return "n/a" if v is None else format(v, "+.%df" % dp) + "%"


def load() -> tuple[dict, dict]:
    """(registry_pools, {pool: [snapshots in time order]})"""
    reg_path = DATA_DIR / "registry.json"
    if not reg_path.exists():
        raise SystemExit("No cohort at %s -- has meme_cohort.py run?" % DATA_DIR)
    with reg_path.open("r", encoding="utf-8") as fh:
        pools = (json.load(fh) or {}).get("pools") or {}

    series: dict[str, list] = {}
    for path in sorted(glob.glob(str(DATA_DIR / "snapshots-*.jsonl"))):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                p = row.get("pool")
                if p:
                    series.setdefault(p, []).append(row)
    for rows in series.values():
        rows.sort(key=lambda r: r.get("ts_ms") or 0)
    return pools, series


def mortality(pools: dict, now_ms: int) -> list[dict]:
    """Death rate at each horizon, over pools old enough to have reached it.

    A pool that has not yet lived `h` hours cannot inform the `h`-hour death
    rate. Including it as a survivor would bias every rate toward zero and make
    the market look progressively safer the longer the collector runs.
    """
    out = []
    for h in HORIZONS_H:
        eligible = dead = drained = 0
        for node in pools.values():
            born = node.get("created_ms") or node.get("first_seen_ms")
            if not born:
                continue
            age_h = (now_ms - float(born)) / 3_600_000.0
            if age_h < h:
                continue
            eligible += 1
            status = node.get("status")
            if status == "gone":
                dead += 1
            elif status == "drained":
                drained += 1
        out.append({
            "horizon_h": h, "eligible": eligible, "gone": dead,
            "drained": drained,
            "dead_pct": (100.0 * dead / eligible) if eligible else None,
            "dead_or_drained_pct": (100.0 * (dead + drained) / eligible)
                                   if eligible else None,
        })
    return out


def report(pools: dict, series: dict, now_ms: int) -> dict:
    spans = [n.get("first_seen_ms") for n in pools.values() if n.get("first_seen_ms")]
    first = min(spans) if spans else now_ms
    hours = max(1e-9, (now_ms - first) / 3_600_000.0)

    latest = {p: rows[-1] for p, rows in series.items() if rows}
    liq = sorted(x for x in (_f(r.get("reserve_usd")) for r in latest.values())
                 if x is not None)
    tradeable = sum(1 for x in liq if x >= LIQ_FLOOR)

    ratios = sorted(
        r["buys_h24"] / r["buyers_h24"]
        for r in latest.values()
        if _f(r.get("buys_h24")) and _f(r.get("buyers_h24"))
    )

    # Gross return, first observation to last, per pool with >=2 observations.
    rets = []
    for rows in series.values():
        if len(rows) < 2:
            continue
        p0, p1 = _f(rows[0].get("price_usd")), _f(rows[-1].get("price_usd"))
        if p0 and p0 > 0 and p1 is not None:
            rets.append((p1 / p0 - 1.0) * 100.0)
    rets.sort()

    return {
        "generated_ms": now_ms,
        "pools": len(pools),
        "collection_hours": hours,
        "launch_rate_per_min": len(pools) / (hours * 60.0),
        "snapshots": sum(len(v) for v in series.values()),
        "mortality": mortality(pools, now_ms),
        "liquidity": {
            "n": len(liq),
            "median": _median(liq), "p90": _pctile(liq, 0.90),
            "max": liq[-1] if liq else None,
            "above_floor": tradeable,
            "above_floor_pct": (100.0 * tradeable / len(liq)) if liq else None,
        },
        "wash": {
            "n": len(ratios), "median": _median(ratios),
            "p90": _pctile(ratios, 0.90),
            "over_10x": sum(1 for x in ratios if x > 10),
            "over_10x_pct": (100.0 * sum(1 for x in ratios if x > 10) / len(ratios))
                            if ratios else None,
        },
        "returns_gross": {
            "n": len(rets), "median": _median(rets),
            "mean": (sum(rets) / len(rets)) if rets else None,
            "p10": _pctile(rets, 0.10), "p90": _pctile(rets, 0.90),
            "best": rets[-1] if rets else None, "worst": rets[0] if rets else None,
        },
    }


def render(rep: dict) -> str:
    L = []
    L.append("COHORT BASE RATES -- descriptive only, no signals")
    L.append("=" * 64)
    L.append("  pools captured   : %d over %.1f days (%.1f/min)"
             % (rep["pools"], rep["collection_hours"] / 24.0,
                rep["launch_rate_per_min"]))
    L.append("  snapshots        : %d" % rep["snapshots"])
    L.append("")

    L.append("MORTALITY -- of pools old enough to have reached each horizon")
    L.append("  %-9s %9s %8s %9s %12s" % ("HORIZON", "ELIGIBLE", "GONE",
                                          "DRAINED", "DEAD+DRAINED"))
    for m in rep["mortality"]:
        L.append("  %-9s %9d %8d %9d %11s"
                 % ("%gh" % m["horizon_h"], m["eligible"], m["gone"],
                    m["drained"],
                    "n/a" if m["dead_or_drained_pct"] is None
                    else "%.1f%%" % m["dead_or_drained_pct"]))
    thin = [m for m in rep["mortality"] if 0 < m["eligible"] < MIN_N]
    if thin:
        L.append("  (%s below the n=%d floor -- not yet meaningful)"
                 % (", ".join("%gh" % m["horizon_h"] for m in thin), MIN_N))
    none_yet = [m for m in rep["mortality"] if m["eligible"] == 0]
    if none_yet:
        L.append("  (%s: nothing has lived that long yet)"
                 % ", ".join("%gh" % m["horizon_h"] for m in none_yet))
    L.append("")

    lq = rep["liquidity"]
    L.append("TRADEABILITY -- latest observation per pool (n=%d)" % lq["n"])
    L.append("  liquidity median %s   p90 %s   max %s"
             % (_usd(lq["median"]), _usd(lq["p90"]), _usd(lq["max"])))
    L.append("  clears the %s floor: %d (%s)"
             % (_usd(LIQ_FLOOR), lq["above_floor"],
                "n/a" if lq["above_floor_pct"] is None
                else "%.1f%%" % lq["above_floor_pct"]))
    L.append("  ^ this is the headline: everything below the floor is")
    L.append("    untradeable before direction is even a question.")
    L.append("")

    w = rep["wash"]
    L.append("WASH PROXY -- trades per unique wallet, 24h (n=%d)" % w["n"])
    L.append("  median %s   p90 %s   above 10x: %d (%s)"
             % ("n/a" if w["median"] is None else "%.1f" % w["median"],
                "n/a" if w["p90"] is None else "%.1f" % w["p90"],
                w["over_10x"],
                "n/a" if w["over_10x_pct"] is None else "%.1f%%" % w["over_10x_pct"]))
    L.append("")

    r = rep["returns_gross"]
    L.append("GROSS RETURN, first to last observation (n=%d)" % r["n"])
    L.append("  median %s     mean %s" % (_pct(r["median"]), _pct(r["mean"])))
    L.append("  p10 %s   p90 %s   worst %s   best %s"
             % (_pct(r["p10"]), _pct(r["p90"]), _pct(r["worst"]), _pct(r["best"])))
    if r["median"] is not None and r["mean"] is not None:
        gap = r["mean"] - r["median"]
        L.append("  mean-median gap: %.1f pts -- %s"
                 % (gap, "fat right tail: a few outliers carry the average, so "
                         "the typical outcome is the median, not the mean"
                    if gap > 5 else
                    "left-skewed: the average is dragged down by disasters"
                    if gap < -5 else "roughly symmetric"))
    L.append("  GROSS: ignores round-trip cost entirely. An upper bound on what")
    L.append("  was achievable, not an estimate of it.")
    if r["n"] < MIN_N:
        L.append("  n=%d is below the %d floor -- a story, not a statistic."
                 % (r["n"], MIN_N))
    L.append("")
    L.append("No signal, ranking, or recommendation is produced or implied.")
    L.append("This establishes the base rate a hypothesis would have to beat.")
    return "\n".join(L)


def telegram(rep: dict) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    from src.notifications import TelegramNotifier

    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        _say("ERROR: Telegram not configured.")
        return 1

    lq, r, w = rep["liquidity"], rep["returns_gross"], rep["wash"]
    m24 = next((x for x in rep["mortality"] if x["horizon_h"] == 24.0), None)

    L = ["<b>Cohort base rates</b>  (descriptive, no signals)",
         "%d pools over %.1f days" % (rep["pools"], rep["collection_hours"] / 24.0),
         ""]
    if m24 and m24["eligible"]:
        L.append("Dead or drained by 24h: <b>%s</b>  (n=%d)"
                 % ("n/a" if m24["dead_or_drained_pct"] is None
                    else "%.0f%%" % m24["dead_or_drained_pct"], m24["eligible"]))
    L += [
        "Clears the $50k floor: <b>%s</b>"
        % ("n/a" if lq["above_floor_pct"] is None else "%.1f%%" % lq["above_floor_pct"]),
        "Median liquidity: <b>%s</b>" % _usd(lq["median"]),
        "",
        "Gross return  median <b>%s</b>  mean <b>%s</b>"
        % (_pct(r["median"]), _pct(r["mean"])),
        "Trades per wallet  median %s"
        % ("n/a" if w["median"] is None else "%.1f" % w["median"]),
    ]
    if r["n"] < MIN_N:
        L += ["", "<i>n=%d, below the %d-sample floor. Not yet a statistic.</i>"
              % (r["n"], MIN_N)]
    L += ["", "<i>Gross of cost. Establishes the base rate a hypothesis would "
              "have to beat -- it is not one.</i>"]

    ok = TelegramNotifier(tok, chat, enabled=True, async_safe=False) \
        .send_message("\n".join(L))
    _say("Telegram %s." % ("sent" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="cohort_base_rates",
        description="Descriptive base rates over the captured meme cohort. "
                    "No signals, no ranking, no recommendations.")
    ap.add_argument("--telegram", action="store_true", help="send a digest")
    ap.add_argument("--json", dest="json_path", default=None, metavar="PATH")
    args = ap.parse_args(argv)

    pools, series = load()
    if not pools:
        _say("Cohort is empty -- nothing to report.")
        return 0

    rep = report(pools, series, int(time.time() * 1000))

    if args.json_path:
        p = Path(args.json_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, sort_keys=True, default=str)
        _say("Wrote %s" % p)

    if args.telegram:
        return telegram(rep)

    _say("")
    _say(render(rep))
    _say("")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        _say("ERROR: %s: %s" % (type(exc).__name__, exc))
        sys.exit(1)
