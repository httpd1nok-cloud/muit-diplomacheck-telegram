from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = ROOT / 'config' / 'muit_rules.json'

@lru_cache(maxsize=1)
def get_rules() -> dict:
    with RULES_FILE.open(encoding='utf-8') as f:
        return json.load(f)


def rubric_view() -> dict:
    rules = get_rules()
    return {
        'version': rules['version_date'],
        'source': rules['document'],
        'rubric': rules['rubric'],
        'possible_total': sum(x['max_points'] for x in rules['rubric']),
        'note': rules['grading_note'],
    }
