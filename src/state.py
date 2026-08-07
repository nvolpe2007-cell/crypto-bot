"""
Shared state manager — bot writes, dashboard reads
"""

import json
import math
import os
from datetime import datetime
from typing import Any, Dict


STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'state.json')


def sanitize_for_json(obj):
    """Recursively replace NaN/Inf with None so JSON serialization is always
    valid and round-trips cleanly. json.dumps's default allow_nan=True does
    NOT raise on these — it silently emits non-standard NaN/Infinity literals
    that json.load parses straight back into float('nan'), and since NaN
    comparisons are always False in Python, a NaN that reaches a persisted
    position/price field can silently disable stop-loss/take-profit checks
    on it forever, surviving every subsequent restart. Shared by every
    standalone paper-arm script's _save_state(), not just write_state() below."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def write_state(data: Dict[str, Any]):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    try:
        existing = read_state()
        # Preserve keys written by lower-frequency tasks so the 2s main-loop
        # write doesn't clobber them. The funding-arb arm P&L is rewritten only
        # every ~65s by _merge_funding_state; without preserving it here the
        # dashboard would show blank/stale arb P&L ~97% of the time.
        for key in ('funding_opportunities', 'sentiment',
                    'funding_arb', 'funding_arb_majors', 'funding_arb_kraken'):
            if key not in data and key in existing:
                data[key] = existing[key]
    except Exception:
        pass
    data['last_update'] = datetime.utcnow().isoformat()
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(sanitize_for_json(data), f, default=str)
    os.replace(tmp, STATE_FILE)


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'status': 'starting', 'last_update': None}
