"""
Shared state manager — bot writes, dashboard reads
"""

import json
import math
import os
from datetime import datetime
from typing import Any, Dict


STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'state.json')


def _clean(obj):
    """Replace NaN/Inf with None so the JSON file is always valid."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
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
        json.dump(_clean(data), f, default=str)
    os.replace(tmp, STATE_FILE)


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'status': 'starting', 'last_update': None}
