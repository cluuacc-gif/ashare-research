"""Lossless, bounded member storage in the small current-outlook pointer.

Full evidence remains in the immutable outlook.json asset.  This representation
keeps every semantic member field needed to repeat the date/taxonomy checks and
the board/stock screen without a second network request at mail preparation.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import zlib

SCHEMA = 'fund-flow-members-zlib-v1'
MIN_PACK_BYTES = 512
MAX_BOARD_MEMBERS = 1500
MAX_BOARD_BYTES = 2_000_000
MAX_TOTAL_BYTES = 16_000_000


def pack_members(flow):
    """Pack large arrays in place; never truncate a board or replace its facts."""
    total = 0
    for board in (flow or {}).get('sectors', []):
        members = board.get('members')
        if members is None:
            continue
        if not isinstance(members, list) or len(members) > MAX_BOARD_MEMBERS:
            raise ValueError('invalid fund-flow member count')
        raw = json.dumps(members, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
        total += len(raw)
        if len(raw) > MAX_BOARD_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError('fund-flow member data exceeds expansion limit')
        if len(raw) < MIN_PACK_BYTES:
            continue
        if 'members_payload' in board:
            raise ValueError('ambiguous fund-flow member representations')
        board['members_payload'] = {
            'schema': SCHEMA, 'member_count': len(members),
            'sector_code': board.get('code'),
            'base_trade_date': flow.get('base_trade_date'),
            'uncompressed_bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'zlib_base64': base64.b64encode(zlib.compress(raw, 9)).decode('ascii'),
        }
        del board['members']
    return flow


def unpack_members(flow):
    """Return a detached flow object; invalid or oversized payloads fail closed."""
    value = copy.deepcopy(flow)
    total = 0
    for board in (value or {}).get('sectors', []):
        payload = board.get('members_payload')
        if payload is None:
            continue
        if 'members' in board or not isinstance(payload, dict):
            raise ValueError('ambiguous or invalid fund-flow member payload')
        size, count = payload.get('uncompressed_bytes'), payload.get('member_count')
        encoded = payload.get('zlib_base64')
        if (payload.get('schema') != SCHEMA or type(size) is not int
                or not 0 <= size <= MAX_BOARD_BYTES or type(count) is not int
                or not 0 <= count <= MAX_BOARD_MEMBERS or not isinstance(encoded, str)
                or len(encoded) > MAX_BOARD_BYTES * 2
                or payload.get('sector_code') != board.get('code')
                or payload.get('base_trade_date') != value.get('base_trade_date')):
            raise ValueError('unsupported or oversized fund-flow member payload')
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('fund-flow member expansion exceeds total limit')
        try:
            packed = base64.b64decode(encoded, validate=True)
            inflater = zlib.decompressobj()
            raw = inflater.decompress(packed, size + 1)
            if (len(raw) != size or not inflater.eof or inflater.unused_data
                    or inflater.unconsumed_tail
                    or hashlib.sha256(raw).hexdigest() != payload.get('sha256')):
                raise ValueError('fund-flow member bytes or checksum mismatch')
            def reject_constant(_):
                raise ValueError('nonfinite fund-flow member number')
            members = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
        except (binascii.Error, UnicodeError, zlib.error) as error:
            raise ValueError('invalid compressed fund-flow members') from error
        if (not isinstance(members, list) or len(members) != count
                or any(not isinstance(member, dict) for member in members)):
            raise ValueError('fund-flow member count or row type mismatch')
        board['members'] = members
        del board['members_payload']
    return value
