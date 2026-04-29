"""
Shared state manager — bot writes, dashboard reads
"""

import json
import os
from datetime import datetime
from typing import Dict, Any


STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'state.json')


def write_state(data: Dict[str, Any]):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    # Preserve fields written by other components
    try:
        existing = read_state()
        for key in ('funding_opportunities', 'sentiment'):
            if key not in data and key in existing:
                data[key] = existing[key]
    except Exception:
        pass
    data['last_update'] = datetime.utcnow().isoformat()
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, default=str)
    os.replace(tmp, STATE_FILE)


def read_state() -> Dict[str, Any]:
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'status': 'starting', 'last_update': None}
