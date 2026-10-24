"""Current candidate scope only; historical diagnostics and model values stay intact."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3

from security_selection import load_metadata, normalized_name, rejection_reasons

SCHEMA = 'current-non-chinext-non-st-selection-v1'
CURRENT_FIELDS = ('current_watchlist', 'setup_observations', 'formal_candidates')


@contextmanager
def _connection(database):
    if isinstance(database, sqlite3.Connection):
        yield database
    else:
        connection = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            yield connection
        finally:
            connection.close()


def filter_current_rows(rows, database, base, cutoff):
    """Use restored, cutoff-known metadata; never infer a fresh normal status."""
    source = rows if isinstance(rows, list) else []
    symbols = sorted({row['symbol'] for row in source if isinstance(row, dict)
                      and isinstance(row.get('symbol'), str)})
    metadata, error = {}, None
    if symbols:
        try:
            with _connection(database) as connection:
                metadata = load_metadata(connection, symbols, base=base, cutoff=cutoff)
        except (OSError, sqlite3.DatabaseError):
            error = 'restored_security_metadata_unavailable'
    selected, rejected = [], Counter()
    for row in source:
        if not isinstance(row, dict) or not isinstance(row.get('symbol'), str):
            rejected['invalid_current_candidate'] += 1
            continue
        symbol = row['symbol']
        info = metadata.get(symbol)
        reasons = rejection_reasons(symbol, info, cutoff, base=base)
        # A stale payload cannot override a newly known ST name, and an
        # explicitly risky payload cannot be laundered through a normal name.
        label = normalized_name(row.get('name'))
        board = normalized_name(row.get('board') or row.get('market_board'))
        if 'ST' in label or '退' in label:
            reasons.append('excluded_current_ST_or_delisting_name')
        if board in ('创业板', 'CHINEXT'):
            reasons.append('excluded_current_chinext_board')
        if any(row.get(key) in (True, 1) for key in ('is_st', 'delisting')):
            reasons.append('excluded_current_risk_flag')
        if reasons:
            rejected.update(set(reasons))
            continue
        copy = deepcopy(row)
        copy['name'] = info['name']
        selected.append(copy)
    scope = {'schema': SCHEMA, 'base_trade_date': base, 'information_cutoff': cutoff,
             'scope': 'current_candidates_only_non_ChiNext_non_ST_non_delisting',
             'metadata': 'fresh_cutoff_known_master_name_and_existing_verified_official_risk_veto',
             'official_normal_status_certified': False,
             'input_count': len(source), 'eligible_count': len(selected),
             'rejected_count': len(source) - len(selected),
             'rejection_reasons': dict(sorted(rejected.items())),
             'historical_diagnostics_recomputed': False,
             'historical_statistics_apply_to_new_scope': False,
             'model_probabilities_recomputed': False}
    if error:
        scope['reason'] = error
    return selected, scope


def filter_current_study(study, database, base, cutoff, fields=CURRENT_FIELDS):
    """Copy current lists while retaining the original historical evidence."""
    result = deepcopy(study) if isinstance(study, dict) else {}
    scopes = {}
    for field in fields:
        if field in result:
            result[field], scopes[field] = filter_current_rows(result[field], database, base, cutoff)
    result['current_selection_scope'] = {
        'schema': SCHEMA, 'base_trade_date': base, 'information_cutoff': cutoff,
        'scope': 'current_candidate_lists_only', 'lists': scopes,
        'historical_diagnostics_recomputed': False,
        'historical_statistics_apply_to_new_scope': False,
        'model_probabilities_recomputed': False}
    return result
