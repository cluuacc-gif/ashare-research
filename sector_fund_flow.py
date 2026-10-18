"""Freeze first-party Eastmoney industry fund-flow observations for forward research.

The vendor's main-order flow is not a measure of all cash entering equities.  No
OHLC-derived money-flow estimate is substituted when the real feed is unavailable.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
import urllib.parse
import urllib.request

SCHEMA = 'sector-fund-flow-v1'
CLASSIFICATION = 'eastmoney_industry'
TZ = timezone(timedelta(hours=8))
SOURCE_URL = 'https://data.eastmoney.com/bkzj/hy.html'
DEFINITION_URL = 'https://data.eastmoney.com/newstatic/js/bkzj/list.js'
PROXY_URL = 'https://data.eastmoney.com/dataapi/bkzj/getbkzj'
INDUSTRY_FILTER = 'm:90+s:4'
FIELDS = {
    'f62': 'main_net_inflow_yuan', 'f184': 'main_net_ratio_pct',
    'f267': 'main_net_3d_yuan', 'f268': 'main_net_3d_ratio_pct',
    'f164': 'main_net_5d_yuan', 'f165': 'main_net_5d_ratio_pct',
    'f174': 'main_net_10d_yuan', 'f175': 'main_net_10d_ratio_pct',
    'f3': 'return_1d_pct', 'f109': 'return_5d_pct',
    'f160': 'return_10d_pct', 'f66': 'super_large_net_inflow_yuan',
    'f72': 'large_net_inflow_yuan', 'f6': 'amount_yuan',
    'f124': 'source_updated_at',
}
PERCENT_FIELDS = {'f184', 'f268', 'f165', 'f175', 'f3', 'f109', 'f160'}
HISTORY_FIELDS = ('trade_date', 'main_net_inflow_yuan', 'small_net_inflow_yuan',
                  'medium_net_inflow_yuan', 'large_net_inflow_yuan',
                  'super_large_net_inflow_yuan', 'main_net_ratio_pct',
                  'small_net_ratio_pct', 'medium_net_ratio_pct',
                  'large_net_ratio_pct', 'super_large_net_ratio_pct',
                  'close', 'return_1d_pct')
MAX_RESPONSE = 4 * 1024 * 1024
NOTES = [
    '资金流是东方财富按成交单规模与买卖方向分类的主力净额，并非全部市场资金净流入，也不能证明机构真实持仓。',
    '净额、净占比和价格行为分别保留；量价指标不冒充资金流。',
    '当前行业分类独立于新浪行业；只用东方财富板块代码与其当期成员证券代码关联，不按名称拼接。',
    '行业资金口径保留供应商原始全量成分，可能包含B股；个股候选明确只筛A股，不将行业全额称为纯A股资金。',
    '今日、3日、5日、10日是供应商观察值；本次获得的历史数据仅用于当前向前观察，不能倒填过去回测。',
    '增强分数用于观察排序，未经前瞻校准，不是上涨概率或交易胜率。',
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def _stamp(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('timestamp must include timezone')
    return result.astimezone(TZ)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                               sort_keys=True, allow_nan=False) + '\n', encoding='utf-8')


def _request(url: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; AShareResearch/1.0)',
        'Referer': SOURCE_URL, 'Accept': 'application/json,text/plain,*/*',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('response exceeds size bound')
    return raw, _now()


def _fetch(url: str, filename: str, output: Path, timeout: float) -> dict:
    """Persist response bytes before parsing, including malformed vendor responses."""
    try:
        raw, observed_at = _request(url, timeout)
        destination = output / 'raw' / filename
        destination.write_bytes(raw)
        result = {'source_url': url, 'observed_at': observed_at,
                  'raw_file': 'raw/' + filename, 'raw_sha256': _sha(raw),
                  'raw_bytes': len(raw), 'status': 'received'}
        try:
            payload = json.loads(raw.decode('utf-8-sig'))
            if not isinstance(payload, dict) or payload.get('rc') != 0:
                raise ValueError('vendor response has nonzero rc or invalid root')
            result['payload'] = payload
        except (UnicodeError, ValueError, TypeError) as error:
            result.update(status='invalid', error=str(error)[:200])
        return result
    except Exception as error:
        # Errors may include request URLs, which are all public, unauthenticated URLs.
        return {'source_url': url, 'observed_at': _now(), 'status': 'unavailable',
                'error': (type(error).__name__ + ': ' + str(error))[:300]}


def _proxy_url(key: str, code: str) -> str:
    return PROXY_URL + '?' + urllib.parse.urlencode({'key': key, 'code': code})


def _diff(payload: dict) -> tuple[list[dict], int]:
    data = payload.get('data')
    if not isinstance(data, dict):
        raise ValueError('vendor data object missing')
    rows, total = data.get('diff'), data.get('total')
    if (not isinstance(rows, list) or not isinstance(total, int)
            or isinstance(total, bool) or not 0 < total <= 1500
            or len(rows) > total):
        raise ValueError('invalid vendor row count')
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('invalid vendor row')
    return rows, total


def _decode_proxy_value(field: str, value: Any) -> Any:
    number = _number(value)
    if number is None:
        return None
    if field == 'f124':
        try:
            return datetime.fromtimestamp(number, TZ).isoformat(timespec='seconds')
        except (ValueError, OverflowError, OSError):
            return None
    # The same-origin proxy returns raw fixed-point values (no fltt=2).
    # Its integer 190 matches clist fltt=2's 1.90 percentage points.
    return number / 100 if field in PERCENT_FIELDS else number


def _merge_fields(responses: dict[str, dict], base: str, cutoff: str) -> tuple[list[dict], dict, list[str]]:
    known_at = _stamp(cutoff)
    merged, totals, field_complete, problems = {}, {}, {}, []
    for field, response in responses.items():
        if response.get('status') != 'received' or 'payload' not in response:
            problems.append('field_unavailable:' + field)
            continue
        if _stamp(response['observed_at']) > known_at:
            problems.append('observation_after_cutoff:' + field)
            continue
        try:
            rows, total = _diff(response['payload'])
        except ValueError:
            problems.append('field_invalid:' + field)
            continue
        totals[field], seen = total, set()
        for row in rows:
            code = row.get('f12')
            if (not isinstance(code, str) or not re.fullmatch(r'BK\d{4}', code)
                    or code in seen):
                problems.append('field_duplicate_or_invalid_code:' + field)
                continue
            seen.add(code)
            item = merged.setdefault(code, {'code': code, 'name': row.get('f14', ''),
                    'classification': CLASSIFICATION, 'field_evidence': {},
                    'members': [], 'member_count': 0, 'vendor_member_total': None,
                    'membership_status': 'not_collected', 'history': [],
                    'history_status': 'not_collected'})
            if item['name'] != row.get('f14'):
                problems.append('conflicting_vendor_name:' + code)
                item['name_conflict'] = True
                continue
            item[FIELDS[field]] = _decode_proxy_value(field, row.get(field))
            item['field_evidence'][FIELDS[field]] = {
                key: response[key] for key in ('source_url', 'observed_at', 'raw_sha256')}
        field_complete[field] = len(seen) == total
    valid = []
    for item in merged.values():
        for name in FIELDS.values():
            item.setdefault(name, None)
        try:
            updated = _stamp(item['source_updated_at'])
        except (ValueError, TypeError):
            continue
        if (updated.date().isoformat() != base or updated > known_at
                or updated.timetz().replace(tzinfo=None) < time(15)
                or item.get('name_conflict') or not item.get('name')
                or item['main_net_inflow_yuan'] is None):
            continue
        ratio = item['main_net_ratio_pct']
        if ratio is not None and abs(ratio) > 100:
            problems.append('invalid_net_ratio:' + item['code'])
            continue
        values = [evidence['observed_at'] for evidence in item['field_evidence'].values()]
        item.update(trade_date=base, observed_at=max(values, key=_stamp),
                    source_url=f'https://data.eastmoney.com/bkzj/{item["code"]}.html')
        # Vendor money-net components can have small integer rounding differences.
        a, b, main = (item[name] for name in
                      ('super_large_net_inflow_yuan', 'large_net_inflow_yuan', 'main_net_inflow_yuan'))
        item['components_reconciled'] = (None if a is None or b is None else
            abs((a + b) - main) <= max(10, abs(main) * .00001))
        if item['components_reconciled'] is False:
            problems.append('main_components_conflict:' + item['code'])
            continue
        valid.append(item)
    total = totals.get('f62', 0)
    coverage = {'vendor_sector_total': total, 'received_sector_count': len(merged),
                'valid_sector_count': len(valid),
                'complete': bool(total and len(valid) == total and field_complete.get('f62')
                                 and field_complete.get('f124')),
                'field_complete': field_complete, 'history_boards': 0, 'membership_boards': 0}
    return valid, coverage, sorted(set(problems))


def _pct(value: float | None, values: list[float]) -> float:
    if value is None or not values:
        return .0
    return (sum(other < value for other in values) + .5 * sum(other == value for other in values)) / len(values)


def _rank(sectors: list[dict]) -> list[dict]:
    """A fixed transparent cross-sectional heuristic, never a calibrated probability."""
    for item in sectors:
        three, five = item['main_net_3d_yuan'], item['main_net_5d_yuan']
        acceleration = None if three is None or five is None else three / 3 - (five - three) / 2
        scale = None if three is None or five is None else max(abs(three), abs(five - three), 1)
        item['flow_acceleration_yuan_per_day'] = acceleration
        item['flow_acceleration_normalized'] = None if acceleration is None else acceleration / scale
        positive = item['main_net_inflow_yuan'] > 0
        emerging = positive and acceleration is not None and acceleration > 0
        item['flow_state'] = ('emerging_inflow' if emerging else
                              'persistent_inflow' if positive and five is not None and five > 0 else
                              'one_day_inflow' if positive else 'net_outflow')
    distributions = {name: [item[name] for item in sectors if item[name] is not None]
                     for name in ('main_net_inflow_yuan', 'main_net_ratio_pct', 'flow_acceleration_normalized')}
    for item in sectors:
        item['flow_enhancement_score'] = round(100 * (
            .25 * _pct(item['main_net_inflow_yuan'], distributions['main_net_inflow_yuan'])
            + .40 * _pct(item['main_net_ratio_pct'], distributions['main_net_ratio_pct'])
            + .35 * _pct(item['flow_acceleration_normalized'], distributions['flow_acceleration_normalized'])), 4)
        item['score_is_probability'] = False
        item['not_overextended_today'] = (None if item['return_1d_pct'] is None else item['return_1d_pct'] <= 3)
    ranked = sorted(sectors, key=lambda x: (x['main_net_inflow_yuan'] > 0,
                    x['flow_enhancement_score'], x['main_net_inflow_yuan'], x['code']), reverse=True)
    for rank, item in enumerate(ranked, 1):
        item['rank'] = rank
    return ranked


def _symbol(code: Any, market: Any) -> str | None:
    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
        return None
    if code.startswith(('4', '8', '92')):
        return code + '.BJ'
    if market == 1 and code.startswith('6'):
        return code + '.SH'
    if market == 0 and code.startswith(('0', '3')):
        return code + '.SZ'
    return None


def _members(response: dict, cutoff: str, dates_response: dict | None = None,
             base: str | None = None) -> tuple[list[dict], int | None, str]:
    if response.get('status') != 'received' or _stamp(response['observed_at']) > _stamp(cutoff):
        return [], None, 'unavailable'
    try:
        rows, total = _diff(response['payload'])
    except (ValueError, KeyError):
        return [], None, 'unavailable'
    source_dates = {}
    if (dates_response and dates_response.get('status') == 'received'
            and _stamp(dates_response['observed_at']) <= _stamp(cutoff)):
        try:
            dates_rows, dates_total = _diff(dates_response['payload'])
            for row in dates_rows:
                symbol = _symbol(row.get('f12'), row.get('f13'))
                if symbol and symbol not in source_dates:
                    source_dates[symbol] = _decode_proxy_value('f124', row.get('f124'))
        except (ValueError, KeyError):
            pass
    members, seen, known_excluded, invalid = [], set(), 0, 0
    for row in rows:
        symbol = _symbol(row.get('f12'), row.get('f13'))
        if symbol is None:
            code, market = row.get('f12'), row.get('f13')
            if (isinstance(code, str) and re.fullmatch(r'\d{6}', code)
                    and ((market == 1 and code.startswith('900'))
                         or (market == 0 and code.startswith('200')))):
                known_excluded += 1
            else:
                invalid += 1
            continue
        if symbol in seen or not row.get('f14'):
            invalid += 1
            continue
        seen.add(symbol)
        member = {'symbol': symbol, 'name': row['f14'],
            # Membership was learned now. Do not backdate its point-in-time validity.
            'effective_date': _stamp(response['observed_at']).date().isoformat(),
            'observed_at': response['observed_at'], 'source_url': response['source_url'],
            'raw_sha256': response['raw_sha256'], 'main_net_inflow_yuan': None,
            'flow_trade_date': None, 'flow_observed_at': None,
            'source_updated_at': None, 'flow_status': 'date_unverified'}
        updated = source_dates.get(symbol)
        if updated and base:
            stamp = _stamp(updated)
            if (stamp.date().isoformat() == base and stamp <= _stamp(cutoff)
                    and stamp.timetz().replace(tzinfo=None) >= time(15)):
                amount = _number(row.get('f62'))
                if amount is not None:
                    member.update(main_net_inflow_yuan=amount, flow_trade_date=base,
                        source_updated_at=updated, flow_status='verified_current_date',
                        flow_observed_at=max(response['observed_at'], dates_response['observed_at'], key=_stamp),
                        flow_date_source_url=dates_response['source_url'],
                        flow_date_raw_sha256=dates_response['raw_sha256'])
        members.append(member)
    complete = (len(rows) == total and len(members) + known_excluded == total and not invalid)
    return members, total, ('complete' if complete else 'partial')


def _history(response: dict, base: str, cutoff: str, limit: int) -> list[dict]:
    if response.get('status') != 'received' or _stamp(response['observed_at']) > _stamp(cutoff):
        return []
    data = response.get('payload', {}).get('data')
    if not isinstance(data, dict) or not isinstance(data.get('klines'), list):
        return []
    result = {}
    for raw in data['klines']:
        fields = raw.split(',') if isinstance(raw, str) else []
        if len(fields) < len(HISTORY_FIELDS):
            continue
        try:
            day = date.fromisoformat(fields[0]).isoformat()
            values = [float(value) for value in fields[1:len(HISTORY_FIELDS)]]
        except (ValueError, TypeError):
            continue
        if (day > base or day in result or any(not math.isfinite(value) for value in values)):
            continue
        item = dict(zip(HISTORY_FIELDS, [day] + values))
        item.update(observed_at=response['observed_at'], source_url=response['source_url'],
                    raw_sha256=response['raw_sha256'], historical_live_availability_verified=False)
        result[day] = item
    return [result[day] for day in sorted(result)[-limit:]]


def _public_evidence(response: dict) -> dict:
    return {key: value for key, value in response.items() if key != 'payload'}


def collect(output_dir, base_date: str, cutoff: str | None = None,
            max_history: int = 20, max_members: int = 12, timeout: float = 6) -> dict:
    """Collect real flows and immutable evidence into a previously nonexistent directory.

    A supplied cutoff is strict: a newly fetched response observed later cannot be
    made to look available earlier. Without a cutoff the collector freezes its own
    genuine post-collection information cutoff for the next forward report.
    """
    base = date.fromisoformat(base_date).isoformat()
    if cutoff is not None:
        _stamp(cutoff)
    if not 0 <= max_members <= 24 or not 0 <= max_history <= 60 or not 0 < timeout <= 20:
        raise ValueError('collection limits outside bounds')
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'raw').mkdir()
    evidence, responses = [], {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks = {pool.submit(_fetch, _proxy_url(field, INDUSTRY_FILTER),
                             'industries-' + field + '.json', output, timeout): field for field in FIELDS}
        for future in as_completed(tasks):
            field = tasks[future]
            response = future.result()
            responses[field] = response
            evidence.append(_public_evidence(response))
    preliminary_cutoff = cutoff or _now()
    sectors, coverage, blockers = _merge_fields(responses, base, preliminary_cutoff)
    sectors = _rank(sectors)
    chosen = [item for item in sectors if item['main_net_inflow_yuan'] > 0][:max_members]
    if chosen:
        with ThreadPoolExecutor(max_workers=6) as pool:
            tasks = {}
            for item in chosen:
                code = item['code']
                task = pool.submit(_fetch, _proxy_url('f62', 'b:' + code),
                                   'members-' + code + '.json', output, timeout)
                tasks[task] = item, 'members'
                task = pool.submit(_fetch, _proxy_url('f124', 'b:' + code),
                                   'member-dates-' + code + '.json', output, timeout)
                tasks[task] = item, 'member_dates'
                if max_history:
                    query = urllib.parse.urlencode({'lmt': 0, 'klt': 101, 'secid': '90.' + code,
                        'fields1': 'f1,f2,f3,f7',
                        'fields2': ','.join('f' + str(i) for i in range(51, 66)),
                        'ut': 'b2884a393a59ad64002292a3e90d46a5'})
                    task = pool.submit(_fetch, 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?' + query,
                                       'history-' + code + '.json', output, timeout)
                    tasks[task] = item, 'history'
            completed = []
            for future in as_completed(tasks):
                item, kind = tasks[future]
                response = future.result()
                evidence.append(_public_evidence(response))
                completed.append((item, kind, response))
        final_cutoff = cutoff or _now()
        member_dates = {item['code']: response for item, kind, response in completed if kind == 'member_dates'}
        for item, kind, response in completed:
            if kind == 'members':
                members, total, status = _members(response, final_cutoff, member_dates.get(item['code']), base)
                item.update(members=members, member_count=len(members),
                            vendor_member_total=total, membership_status=status,
                            membership_scope='A_share_only',
                            vendor_membership_scope='all_share_classes',
                            excluded_non_ashare_count=(total - len(members) if status == 'complete' else None))
            elif kind == 'history':
                history = _history(response, base, final_cutoff, max_history)
                item.update(history=history, history_status='available' if history else 'unavailable')
    final_cutoff = cutoff or _now()
    coverage.update(history_boards=sum(bool(item['history']) for item in sectors),
                    membership_boards=sum(item['membership_status'] == 'complete' for item in sectors))
    if not sectors:
        blockers.append('no_current_dated_vendor_money_flow')
    if sectors and not coverage['complete']:
        blockers.append('incomplete_industry_universe')
    if chosen and not coverage['membership_boards']:
        blockers.append('member_feed_unavailable')
    generated = _now()
    manifest = {'schema': SCHEMA + '-evidence', 'base_trade_date': base,
                'classification': CLASSIFICATION, 'generated_at': generated,
                'information_cutoff': final_cutoff,
                'evidence': sorted(evidence, key=lambda value: value['source_url'])}
    _write_json(output / 'evidence_manifest.json', manifest)
    observed = [response['observed_at'] for response in responses.values()]
    value = {'schema': SCHEMA, 'classification': CLASSIFICATION,
        'status': 'ready' if coverage['complete'] else 'partial' if sectors else 'unavailable',
        'base_trade_date': base, 'information_cutoff': final_cutoff,
        'observed_at': max(observed, key=_stamp) if observed else generated,
        'generated_at': generated, 'source_url': SOURCE_URL,
        'source_definition_url': DEFINITION_URL,
        'coverage': coverage, 'sectors': sectors, 'blockers': sorted(set(blockers)),
        'notes': NOTES,
        'score_method': {'name': 'fixed_cross_sectional_flow_acceleration_v1',
            'weights': {'today_net_yuan_percentile': .25, 'today_net_ratio_percentile': .40,
                        'acceleration_percentile': .35},
            'acceleration_formula': '3d_net/3 - (5d_net-3d_net)/2',
            'probability_calibrated': False, 'historical_live_availability_verified': False},
        'evidence_manifest_sha256': _sha((output / 'evidence_manifest.json').read_bytes())}
    _write_json(output / 'fund_flow.json', value)
    _write_json(output / 'checksums.json', {'schema': SCHEMA + '-checksums',
        'fund_flow.json': _sha((output / 'fund_flow.json').read_bytes()),
        'evidence_manifest.json': value['evidence_manifest_sha256']})
    return load_verified(output)


def load_verified(directory) -> dict:
    """Verify bytes, source dates and observation cutoffs; return the report payload."""
    output = Path(directory).resolve()
    checksums = json.loads((output / 'checksums.json').read_text(encoding='utf-8'))
    for name in ('fund_flow.json', 'evidence_manifest.json'):
        if _sha((output / name).read_bytes()) != checksums.get(name):
            raise ValueError('fund-flow artifact checksum mismatch: ' + name)
    value = json.loads((output / 'fund_flow.json').read_text(encoding='utf-8'))
    manifest = json.loads((output / 'evidence_manifest.json').read_text(encoding='utf-8'))
    if (value.get('schema') != SCHEMA or value.get('classification') != CLASSIFICATION
            or value.get('evidence_manifest_sha256') != checksums['evidence_manifest.json']
            or manifest.get('base_trade_date') != value.get('base_trade_date')
            or manifest.get('information_cutoff') != value.get('information_cutoff')):
        raise ValueError('fund-flow artifact identity mismatch')
    cutoff = _stamp(value['information_cutoff'])
    for item in manifest.get('evidence', []):
        if 'raw_file' not in item:
            continue
        relative = Path(item['raw_file'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'raw':
            raise ValueError('unsafe evidence path')
        path = (output / relative).resolve()
        if output not in path.parents or _sha(path.read_bytes()) != item['raw_sha256']:
            raise ValueError('raw fund-flow evidence checksum mismatch')
    for sector in value.get('sectors', []):
        if (sector.get('classification') != CLASSIFICATION or sector.get('trade_date') != value['base_trade_date']
                or _stamp(sector['observed_at']) > cutoff or _stamp(sector['source_updated_at']) > cutoff
                or _stamp(sector['source_updated_at']).date().isoformat() != value['base_trade_date']):
            raise ValueError('fund-flow date or classification mismatch')
        for member in sector.get('members', []):
            if (_stamp(member['observed_at']) > cutoff
                    or member['effective_date'] != _stamp(member['observed_at']).date().isoformat()):
                raise ValueError('membership cutoff mismatch or backdated membership')
            if member.get('flow_status') == 'verified_current_date':
                if (member.get('flow_trade_date') != value['base_trade_date']
                        or _stamp(member['flow_observed_at']) > cutoff
                        or _stamp(member['source_updated_at']) > cutoff
                        or _stamp(member['source_updated_at']).date().isoformat() != value['base_trade_date']
                        or _number(member['main_net_inflow_yuan']) is None):
                    raise ValueError('individual-stock flow cutoff mismatch')
        for row in sector.get('history', []):
            if row['trade_date'] > value['base_trade_date'] or _stamp(row['observed_at']) > cutoff:
                raise ValueError('fund-flow historical lookahead')
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cutoff')
    parser.add_argument('--max-members', type=int, default=12)
    parser.add_argument('--history-days', type=int, default=20)
    parser.add_argument('--timeout', type=float, default=6)
    arguments = parser.parse_args()
    result = collect(arguments.output, arguments.base, arguments.cutoff,
                     arguments.history_days, arguments.max_members, arguments.timeout)
    print(json.dumps({key: result[key] for key in ('schema', 'status', 'base_trade_date', 'information_cutoff', 'coverage', 'blockers')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
