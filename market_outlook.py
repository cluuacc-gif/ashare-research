#!/usr/bin/env python3
"""Freeze sector/opening research from a verified snapshot; never send or trade."""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import zipfile
import urllib.parse
from contextlib import closing

import collector as c
import pipeline_state as state
from trade_calendar import context

POINTER = 'state/market-outlook-current.json'
ASSETS = ('outlook.json', 'report.html', 'source_manifest.json')
EXTRA_ASSETS = ('fund_flow_evidence.zip','price_action_evidence.zip','probability_evidence.zip','pa_trade_evidence.zip',
                'nextday_evidence.zip','active_trade_evidence.zip')


def read_pointer(repo):
    try:
        raw = json.loads(state.run('gh', 'api', f'repos/{repo}/contents/{POINTER}?ref={state.BRANCH}'))
        return json.loads(base64.b64decode(raw['content'])), raw['sha']
    except subprocess.CalledProcessError as exc:
        if 'HTTP 404' in (exc.stderr or ''):
            return None, None
        raise


def compact_pointer(manifest):
    """Archive large frozen constituents as hashed assets, outside Contents API."""
    value = copy.deepcopy(manifest)
    value['schema'] = 'market-outlook-pointer-v1'
    value['outlook']['sector_outlook'].pop('frozen_members', None)
    value['outlook']['sector_outlook']['frozen_members_archived'] = True
    # Pack large arrays before the legacy small-row cleanup so their exact
    # identities, timestamps, stock flows and source hashes survive unchanged.
    from fund_flow_pointer import pack_members
    pack_members(value['outlook'].get('fund_flow'))
    for row in (value['outlook'].get('fund_flow') or {}).get('sectors',[]):
        row.pop('field_evidence',None)
        for member in row.get('members',[]):
            for key in ('source_url','raw_sha256','flow_date_source_url','flow_date_raw_sha256'):
                member.pop(key,None)
        if row.get('history'):
            row['history_archived']=True
            row.pop('history',None)
    for key,details in [('sector_forward_tracking','resolved'),('stock_strength_forward_tracking','results')]:
        if key in value['outlook']:
            value['outlook'][key].pop(details,None)
            value['outlook']['forward_ledgers_archived']=True
    selection=value['outlook'].get('sector_stock_selection') or {}
    for key in ('primary','reserves','watchlist','actual_fund_flow'):
        selection.pop(key,None)
    if isinstance(selection.get('rejected_candidates'),list):
        selection['rejected_candidates']=selection['rejected_candidates'][:10]
    (value['outlook'].get('opening_strength_plan') or {}).pop('independent_technical_watchlist',None)
    study=value['outlook'].get('price_action_research') or {}
    for key in ('protocol','selection_rules','current_watchlist'):
        study.pop(key,None)
    for metric in (study.get('diagnostic') or {}).values():
        metric.pop('realized_equity_curve',None)
    if value['outlook'].get('probability_research'):
        from probability_report import compact
        value['outlook']['probability_research']=compact(value['outlook']['probability_research'])
    if value['outlook'].get('pa_trade_research'):
        from pa_trade_report import compact
        value['outlook']['pa_trade_research']=compact(value['outlook']['pa_trade_research'])
    if value['outlook'].get('nextday_research'):
        from nextday_report import compact
        value['outlook']['nextday_research']=compact(value['outlook']['nextday_research'])
        value['outlook']['forward_ledgers_archived']=True
    if value['outlook'].get('active_trade_system'):
        from active_trade_report import compact
        value['outlook']['active_trade_system']=compact(value['outlook']['active_trade_system'])
        value['outlook']['forward_ledgers_archived']=True
    raw = json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')
    if len(raw) > 800_000:
        raise ValueError('compact outlook pointer exceeds readback size limit')
    return value


def validate(value, source, *, allow_legacy_active=False):
    if value.get('schema') != 'market-outlook-v1':
        raise ValueError('unknown outlook schema')
    if (value.get('snapshot_release') != source['release_tag'] or
            value.get('input_database_sha256') != source['database']['sha256'] or
            value.get('base_trade_date') != source['base_trade_date'] or
            value.get('target_trade_date') != source['next_trade_date']):
        raise ValueError('outlook belongs to a different snapshot')
    if context(value['base_trade_date'])['next_session'] != value['target_trade_date']:
        raise ValueError('target is not the next verified trading session')
    cutoff = dt.datetime.fromisoformat(value['information_cutoff'])
    if cutoff.tzinfo is None:
        raise ValueError('outlook cutoff must include timezone')
    close = dt.datetime.combine(dt.date.fromisoformat(value['base_trade_date']), dt.time(15), c.TZ)
    if cutoff < close:
        raise ValueError('closing-price inputs were not yet known')
    generated = dt.datetime.fromisoformat(value['generated_at'])
    if cutoff > generated:
        raise ValueError('information cutoff is in the future')
    if value.get('formal_trade_ready') is not False or value.get('actual_win_rate') is not None:
        raise ValueError('research output cannot claim actual trade qualification')
    if value.get('execution_win_rate_80_validated') is not False:
        raise ValueError('unvalidated execution win rate')
    if value.get('active_trade_system'):
        study=value['active_trade_system'];known=dt.datetime.fromisoformat(study['generated_at'])
        from active_trade_system import SCHEMA, PROTOCOL_SHA256, KNOWN_PROTOCOL_SHA256S
        accepted = set(KNOWN_PROTOCOL_SHA256S) if allow_legacy_active else {PROTOCOL_SHA256}
        if (study.get('schema')!=SCHEMA or study.get('protocol_sha256') not in accepted
                or study.get('base_trade_date')!=source['base_trade_date']
                or study.get('target_trade_date')!=source['next_trade_date']
                or study.get('input_database_sha256')!=source['database']['sha256']
                or study.get('system_enabled') is not True
                or study.get('signal_generation_enabled') is not True
                or study.get('mode')!='conditional_manual_execution'
                or study.get('order_execution_enabled') is not False
                or study.get('empirical_validation_passed') is not False
                or study.get('actual_win_rate') is not None
                or study.get('formal_trade_ready') is not False
                or study.get('execution_win_rate_80_validated') is not False
                or known.tzinfo is None or known>cutoff):
            raise ValueError('active rule identity, cutoff or execution claims invalid')
        from active_trade_system import compatible_policy_migration
        plan_policy=study.get('plan_policy_id') or study['protocol_sha256']
        if (plan_policy not in KNOWN_PROTOCOL_SHA256S or
                plan_policy != study['protocol_sha256'] and not compatible_policy_migration(plan_policy,study['protocol_sha256'])):
            raise ValueError('active frozen plan policy identity invalid')
        if plan_policy == PROTOCOL_SHA256:
            from active_regime import schedule
            for plan in study.get('plans',[]):
                schedule(plan)
                if plan.get('base_trade_date')!=source['base_trade_date'] or plan.get('target_trade_date')!=source['next_trade_date']:
                    raise ValueError('active holding plan belongs to a different snapshot session')
    if value.get('nextday_research'):
        study=value['nextday_research'];known=dt.datetime.fromisoformat(study['generated_at'])
        from nextday_forward import SCHEMA, PROTOCOL_SHA256
        if (study.get('schema')!=SCHEMA or study.get('protocol_sha256')!=PROTOCOL_SHA256
                or study.get('base_trade_date')!=source['base_trade_date']
                or study.get('target_trade_date')!=source['next_trade_date']
                or study.get('input_database_sha256')!=source['database']['sha256']
                or study.get('formal_trade_ready') is not False
                or study.get('execution_win_rate_80_validated') is not False
                or study.get('actual_win_rate') is not None
                or known.tzinfo is None or known>cutoff):
            raise ValueError('next-day forward research identity, cutoff or trade claims invalid')
    if value.get('pa_trade_research'):
        study=value['pa_trade_research'];known=dt.datetime.fromisoformat(study['generated_at'])
        if (study.get('schema')!='pa-context-trade-research-v1'
                or study.get('base_trade_date')!=source['base_trade_date']
                or study.get('target_trade_date')!=source['next_trade_date']
                or study.get('input_database_sha256')!=source['database']['sha256']
                or study.get('formal_trade_ready') is not False
                or study.get('execution_win_rate_80_validated') is not False
                or study.get('actual_win_rate') is not None
                or known.tzinfo is None or known>cutoff):
            raise ValueError('PA trade research identity, cutoff or trade claims invalid')
    if value.get('probability_research'):
        study=value['probability_research']
        known=dt.datetime.fromisoformat(study['generated_at'])
        if (study.get('schema')!='calibrated-tplus1-probability-v1'
                or study.get('base_trade_date')!=source['base_trade_date']
                or study.get('input_database_sha256')!=source['database']['sha256']
                or study.get('formal_trade_ready') is not False
                or study.get('execution_win_rate_80_validated') is not False
                or study.get('actual_win_rate') is not None
                or known.tzinfo is None or known>cutoff):
            raise ValueError('probability research identity, cutoff or trade claims invalid')
    for key in ('sector_outlook', 'opening_strength_plan'):
        part = value.get(key)
        if not isinstance(part, dict):
            raise ValueError('missing research section: '+key)
        for field, expected in (('base_trade_date', value['base_trade_date']),
                                ('target_trade_date', value['target_trade_date']),
                                ('information_cutoff', value['information_cutoff'])):
            if part.get(field) != expected:
                raise ValueError('research section identity differs: '+key+'/'+field)
    return value


def _preserve_first_published_active_decision(active,book,target):
    """Keep the first decision, including an old-policy empty decision, immutable."""
    frozen=next((item for item in book['frozen_plan_dates']
        if item['target_trade_date']==target),None)
    if frozen is not None:
        frozen_policy=book['frozen_date_policy_ids'][target]
        if frozen_policy != active.get('protocol_sha256'):
            active['upgrade_preview']={'selection_rule_version':active['selection_rule_version'],
                'status':'research_preview_only_first_published_decision_preserved',
                'plans':copy.deepcopy(active['plans']), 'eligible_for_execution':False}
            active['selection_upgrade_pending_for_frozen_target']=True
            active['selection_upgrade_effective']='next_unfrozen_target_session'
        active['plans']=copy.deepcopy(frozen['plans'])
        active['plan_policy_id']=frozen_policy
        active['coverage']['conditional_plan_count']=len(active['plans'])
        active['first_published_plan_generated_at']=frozen['generated_at']
        active['status']='conditional_plans_ready' if active['plans'] else 'cash_no_qualifying_setup'
        active['current_plan_preserved_from_first_publication']=True
    return frozen


def generate(store, cutoff=None, fund_flow_dir=None, price_action_dir=None, probability_dir=None,pa_trade_dir=None,
             active_context=None):
    root = Path(store).resolve()
    restored = json.loads((root/'restore.json').read_text(encoding='utf-8'))
    source = restored['manifest']
    if not restored.get('verified') or not source.get('preparation_ready'):
        raise ValueError('verified prepared snapshot required')
    database = root/source['database']['name']
    if Path(source['database']['name']).name != source['database']['name']:
        raise ValueError('invalid database asset name')
    state.verify_file(database, source['database'])
    flow = None
    if fund_flow_dir:
        from sector_fund_flow import load_verified
        flow=load_verified(fund_flow_dir)
    generated = c.stamp()
    if cutoff:
        explicit = dt.datetime.fromisoformat(cutoff.replace('Z','+00:00'))
        if explicit.tzinfo is None:
            raise ValueError('explicit cutoff must include timezone')
        cutoff = explicit.astimezone(c.TZ).isoformat()
    else:
        cutoff = generated
    # A standalone weekend preview uses its real preparation time, never a
    # fabricated Friday 18:00 cutoff. Mail retains its own scheduled cutoff.
    from sector_forecast import generate as sectors
    from opening_strength import build_plan
    from research_context import verified_flow
    from sector_stock_selection import generate as select_sector_stocks
    flow=verified_flow(flow,source['base_trade_date'],cutoff)
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA application_id').fetchone()[0] != c.APP_ID:
            raise ValueError('database identity mismatch')
        outlook = sectors(db, source['base_trade_date'], source['next_trade_date'], cutoff)
    plan = build_plan(database, source['base_trade_date'], source['next_trade_date'], cutoff,
                      sector_forecast=outlook, input_sha256=source['database']['sha256'],include_pool=True,
                      price_min=5,price_max=100)
    selection=select_sector_stocks(database,source['base_trade_date'],source['next_trade_date'],cutoff,
                                   outlook,plan,fund_flow=flow)
    from nextday_cohort import build as freeze_cohort
    from nextday_forward import initialize
    cohort=freeze_cohort(database,source['base_trade_date'],source['next_trade_date'],cutoff,
        outlook,plan,flow,input_sha256=source['database']['sha256'])
    nextday=initialize(cohort,source['base_trade_date'],source['next_trade_date'],
        source['database']['sha256'],cutoff)
    from active_trade_system import build as active_build, PROTOCOL_SHA256 as ACTIVE_SHA
    from active_trade_book import roll, evaluate as evaluate_active, account as active_account
    active_context=active_context or {}
    previous=(active_context.get('prior_value') or {}).get('active_trade_system')
    previous={} if previous is None else previous
    if not isinstance(previous,dict):
        raise ValueError('previous active system must be an object or absent')
    if previous and active_context.get('prior_publication_verified') is not True:
        raise ValueError('existing active plans cannot be replaced while their publication is unverified')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        book=roll(previous,bool(previous) and active_context.get('prior_publication_verified') is True,
            active_context.get('evidence',{}),db,cutoff,ACTIVE_SHA)
    active=active_build(database,source['base_trade_date'],source['next_trade_date'],cutoff,
        outlook,plan,flow,input_sha256=source['database']['sha256'],
        available_cash=book['account']['available_cash_cny'],
        occupied_symbols=book['occupied_symbols'],occupied_sectors=book['occupied_sectors'],
        occupied_routes=book.get('occupied_routes',()))
    # A verified first publication owns the day's allocation. A rerun cannot
    # replace its prices, quantities, or empty decision with a different list.
    frozen=_preserve_first_published_active_decision(active,book,source['next_trade_date'])
    active['reference_book']=book
    if frozen is not None:
        active['reference_account']=book['account']
    else:
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            provisional=[evaluate_active(item,{},db,cutoff) for item in active['plans']]
        active['reference_account']=active_account(book['records']+provisional)
        active['reference_account']['current_plan_reservation_is_conditional']=True
    from active_trade_book import v3_validation_stats
    active['v3_validation_stats']=v3_validation_stats(book['records']+([] if frozen is not None else provisional))
    plan.pop('candidate_pool',None)
    # The new primary list requires known sector membership and confluence.
    plan['independent_technical_watchlist']=plan['watchlist']
    plan['watchlist']=selection.get('watchlist',[])
    plan['selection_method']='sector_first_confluence_v1'
    plan['status']='sector_aligned_conditional_research' if plan['watchlist'] else 'no_sector_confluence'
    value = dict(schema='market-outlook-v1', generated_at=generated, information_cutoff=cutoff,
                 snapshot_release=source['release_tag'], input_database_sha256=source['database']['sha256'],
                 base_trade_date=source['base_trade_date'], target_trade_date=source['next_trade_date'],
                 sector_outlook=outlook, opening_strength_plan=plan, fund_flow=flow,
                 sector_stock_selection=selection, nextday_research=nextday,active_trade_system=active,
                 formal_trade_ready=False,
                 actual_win_rate=None, execution_win_rate_80_validated=False, costs_included=False,
                 smtp_sent=False, orders_submitted=False)
    if price_action_dir:
        study=json.loads((Path(price_action_dir)/'summary.json').read_text(encoding='utf-8'))
        if (study.get('input_database_sha256')!=source['database']['sha256'] or
                study.get('base_trade_date')!=source['base_trade_date'] or study.get('formal_trade_ready') is not False):
            raise ValueError('price-action study differs from verified input or claims trading admission')
        value['price_action_research']=study
    if probability_dir:
        study=json.loads((Path(probability_dir)/'summary.json').read_text(encoding='utf-8'))
        known=dt.datetime.fromisoformat(study['generated_at'])
        if (study.get('schema')!='calibrated-tplus1-probability-v1' or
                study.get('input_database_sha256')!=source['database']['sha256'] or
                study.get('base_trade_date')!=source['base_trade_date'] or
                study.get('formal_trade_ready') is not False or
                study.get('execution_win_rate_80_validated') is not False or
                study.get('actual_win_rate') is not None or known.tzinfo is None or
                known>dt.datetime.fromisoformat(cutoff)):
            raise ValueError('probability research identity, cutoff or trade claims invalid')
        value['probability_research']=study
    if pa_trade_dir:
        value['pa_trade_research']=json.loads((Path(pa_trade_dir)/'summary.json').read_text(encoding='utf-8'))
    validate(value, source)
    return value, source


def forward_tracking(value, source, store, repo, prepared_minutes=None):
    """Score the earliest frozen forecast per target date after its actual close."""
    if prepared_minutes is not None:
        prior,prior_value=prepared_minutes['prior'],prepared_minutes['prior_value']
    else:
        prior, _ = read_pointer(repo)
        prior_value=(prior or {}).get('outlook') or {}
        if prior_value.get('forward_ledgers_archived'):
            prior_value=archived_outlook(repo,prior['release_tag'],prior['files']['outlook.json'])
    previous = prior_value.get('sector_forward_tracking') or {}
    resolved = list(previous.get('resolved', []))
    pending = list(previous.get('pending', []))
    known_targets = {row['target_date'] for row in resolved + pending}
    old_forecast = ((prior or {}).get('outlook') or {}).get('sector_outlook') or {}
    if (prior and prior.get('forward_eligible') is True and old_forecast.get('status') == 'ready'
            and old_forecast.get('target_date') not in known_targets):
        pending.append({'target_date':old_forecast['target_date'], 'forecast_id':old_forecast['forecast_id'],
                        'release_tag':prior['release_tag'], 'asset':prior['files']['outlook.json']})
    remaining = []
    cutoff = dt.datetime.fromisoformat(value['information_cutoff'])
    database = Path(store).resolve()/source['database']['name']
    from sector_forecast import evaluate
    for item in pending:
        due = dt.datetime.combine(dt.date.fromisoformat(item['target_date']),dt.time(15),c.TZ)
        if cutoff < due or value['base_trade_date'] < item['target_date']:
            remaining.append(item)
            continue
        with tempfile.TemporaryDirectory(prefix='sector-forward-') as temporary:
            state.run('gh','release','download',item['release_tag'],'--repo',repo,
                      '--pattern','outlook.json','--dir',temporary)
            path = Path(temporary)/'outlook.json'
            state.verify_file(path,item['asset'])
            frozen = json.loads(path.read_text(encoding='utf-8'))['sector_outlook']
            if frozen['forecast_id'] != item['forecast_id'] or frozen['target_date'] != item['target_date']:
                raise ValueError('archived sector forecast differs from its original identity')
            result = evaluate(database,frozen,value['information_cutoff'])
        if result['status'] != 'resolved' or result.get('forward_eligible') is not True:
            remaining.append({**item,'last_status':result['status'],
                              'last_missing_stocks':result.get('missing_stocks')})
            continue
        keys = ('forecast_id','target_date','status','forward_eligible','top3_evaluated_sectors',
                'top3_relative_strength_hits','top3_absolute_up_hits','top3_top_quartile_hits')
        resolved.append({key:result.get(key) for key in keys})
    denominator = sum(row['top3_evaluated_sectors'] for row in resolved)
    value['sector_forward_tracking'] = {
        'schema':'sector-forward-ledger-v1', 'earliest_forecast_per_target_session':True,
        'resolved':resolved, 'pending':remaining, 'resolved_dates':len(resolved),
        'resolved_sector_predictions':denominator,
        'relative_strength_hit_rate':sum(row['top3_relative_strength_hits'] for row in resolved)/denominator if denominator else None,
        'absolute_up_rate':sum(row['top3_absolute_up_hits'] for row in resolved)/denominator if denominator else None,
        'top_quartile_hit_rate':sum(row['top3_top_quartile_hits'] for row in resolved)/denominator if denominator else None,
        'gross_trade_win_rate':None,
    }
    value['tracking_parent_release'] = prior['release_tag'] if prior else None
    stock_forward_tracking(value,source,store,repo,prior,prior_value)
    if value.get('nextday_research'):
        from nextday_forward import advance
        database=Path(store).resolve()/source['database']['name']
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
            value['nextday_research']=advance(value['nextday_research'],prior_value.get('nextday_research'),
                bool(prior and prior.get('forward_eligible')
                     and (prepared_minutes or {}).get('prior_publication_verified')),
                (prepared_minutes or {}).get('evidence',{}),db,value['information_cutoff'])
    return value


def prepare_nextday_minutes(repo, source, directory):
    """Capture before freezing the new cutoff; SMTP never calls this path."""
    prior,_=read_pointer(repo)
    prior_value=(prior or {}).get('outlook') or {}
    if prior_value.get('forward_ledgers_archived'):
        prior_value=archived_outlook(repo,prior['release_tag'],prior['files']['outlook.json'])
    study=prior_value.get('nextday_research') or {}
    from nextday_forward import SCHEMA, PROTOCOL_SHA256
    cohorts=[]
    publication=None
    if prior and prior.get('forward_eligible'):
        publication=verify_pointer_publication(repo,prior)
    if study.get('schema')==SCHEMA and study.get('protocol_sha256')==PROTOCOL_SHA256:
        cohorts=list(study.get('tracked_cohorts',[]))
        if publication and study.get('current_cohort',{}).get('forward_eligible'):
            prior_value=copy.deepcopy(prior_value)
            prior_value['nextday_research']['current_cohort']['publication_evidence']=publication
            study=prior_value['nextday_research']
            cohorts.append(study['current_cohort'])
    # The OHLC endpoint may return only the most recent session. Entries are
    # frozen on D1 and carried in the immutable ledger; D2 does not refetch D1.
    # Earlier missing sessions stay unknown, without fabricated backfills.
    relevant=[row for row in cohorts if source['base_trade_date'] in
        (row.get('target_trade_date'),row.get('D2_trade_date'))]
    symbols=sorted({item['symbol'] for row in relevant for item in row.get('base_candidates',[])})
    active=prior_value.get('active_trade_system') or {}
    active_cohorts=list((active.get('reference_book') or {}).get('frozen_plan_dates',[]))
    if publication and active.get('system_enabled') is True:
        active_cohorts.append({'plans':active.get('plans',[])})
    active_symbols={plan['symbol'] for item in active_cohorts for plan in item.get('plans',[])
        if source['base_trade_date'] in (plan.get('target_trade_date'),plan.get('D2_trade_date'),plan.get('planned_exit_trade_date'))}
    symbols=sorted(set(symbols)|active_symbols)
    from nextday_minutes import capture
    evidence=capture(symbols,directory)
    return {'prior':prior,'prior_value':prior_value,'evidence':evidence,
            'prior_publication_verified':publication is not None}


def verify_pointer_publication(repo, prior):
    """Use the GitHub pointer commit, not a pre-upload eligibility flag alone."""
    try:
        query=urllib.parse.urlencode({'sha':state.BRANCH,'path':POINTER,'per_page':1})
        raw=subprocess.run(['gh','api',f'repos/{repo}/commits?{query}'],
            check=True,capture_output=True,text=True,timeout=10).stdout
        rows=json.loads(raw)
        published=dt.datetime.fromisoformat(rows[0]['commit']['committer']['date'].replace('Z','+00:00'))
        deadline=dt.datetime.combine(dt.date.fromisoformat(prior['target_trade_date']),dt.time(9,15),c.TZ)
        current,_=read_pointer(repo)
        if (published.tzinfo is None or published>=deadline or current!=prior
                or not prior.get('assets_readback_verified_at')):
            return None
        return {'pointer_commit_sha':rows[0]['sha'],'published_at':published.isoformat(),
                'verified_before_target_deadline':True}
    except (OSError,ValueError,KeyError,IndexError,TypeError,subprocess.SubprocessError):
        return None


def archived_outlook(repo,tag,asset):
    with tempfile.TemporaryDirectory(prefix='outlook-ledger-') as temporary:
        state.run('gh','release','download',tag,'--repo',repo,'--pattern','outlook.json','--dir',temporary)
        path=Path(temporary)/'outlook.json'
        state.verify_file(path,asset)
        return json.loads(path.read_text(encoding='utf-8'))


def stock_forward_tracking(value,source,store,repo,prior,prior_value):
    """Track frozen primary names as price events; never infer real fills."""
    from stock_strength_tracking import evaluate
    old=prior_value.get('stock_strength_forward_tracking') or {}
    pending=list(old.get('pending',[]))
    results={row['target_trade_date']:row for row in old.get('results',[])}
    selection=prior_value.get('sector_stock_selection') or {}
    known={row['target_date'] for row in pending}|set(results)
    if prior and prior.get('forward_eligible') and selection.get('primary') and prior['target_trade_date'] not in known:
        pending.append({'target_date':prior['target_trade_date'],'release_tag':prior['release_tag'],
                        'asset':prior['files']['outlook.json']})
    remaining=[]
    for item in pending:
        if item['target_date']>value['base_trade_date']:
            remaining.append(item);continue
        frozen=archived_outlook(repo,item['release_tag'],item['asset'])
        if frozen['target_trade_date']!=item['target_date']:
            raise ValueError('stock forward archive target differs')
        measured=evaluate(Path(store).resolve()/source['database']['name'],frozen,value['information_cutoff'])
        compact={key:part for key,part in measured.items() if key!='rows'}
        results[item['target_date']]=compact
        if measured.get('status')!='resolved':remaining.append(item)
    rows=list(results.values())
    n2=sum(row.get('n_D2_resolved',0) for row in rows)
    n3=sum(row.get('n_strength3_resolved',0) for row in rows)
    value['stock_strength_forward_tracking']={
        'schema':'stock-strength-forward-ledger-v1','measurement':'OPEN_REFERENCE_PRICE_EVENT',
        'earliest_forecast_per_target_session':True,'results':rows,'pending':remaining,
        'n_D2_resolved':n2,'n_strength3_resolved':n3,
        'n_D2_unknown':sum(row.get('n_D2_pending',0)+row.get('n_D2_unresolved',0) for row in rows),
        'n_strength3_unknown':sum(row.get('n_strength3_pending',0)+row.get('n_strength3_unresolved',0) for row in rows),
        'D2_open_reference_profitable_rate':sum(row.get('n_D2_open_reference_profitable',0) for row in rows)/n2 if n2 else None,
        'strength3_rate':sum(row.get('n_strength3',0) for row in rows)/n3 if n3 else None,
        'actual_win_rate':None,'real_execution_win_rate':None}


def verify_bundle(out, manifest):
    out = Path(out)
    extras=manifest['outlook'].get('evidence_assets',[])
    if (not isinstance(extras,list) or len(extras)!=len(set(extras))
            or any(name not in EXTRA_ASSETS for name in extras)):
        raise ValueError('invalid evidence asset set')
    assets=ASSETS+tuple(extras)
    if sorted(manifest['files']) != sorted(assets):
        raise ValueError('outlook asset set mismatch')
    for name in assets:
        if manifest['files'][name]['name'] != name:
            raise ValueError('outlook asset name mismatch')
        state.verify_file(out/name, manifest['files'][name])
    value = json.loads((out/'outlook.json').read_text(encoding='utf-8'))
    source = json.loads((out/'source_manifest.json').read_text(encoding='utf-8'))
    validate(value, source, allow_legacy_active=True)
    if manifest['outlook'] != value:
        raise ValueError('published outlook differs from frozen assets')
    from market_outlook_report import render
    if (out/'report.html').read_text(encoding='utf-8') != render(value):
        raise ValueError('HTML report differs from frozen outlook')
    return value


def publish(repo, output, run_id):
    out = Path(output).resolve()
    value = json.loads((out/'outlook.json').read_text(encoding='utf-8'))
    source = json.loads((out/'source_manifest.json').read_text(encoding='utf-8'))
    validate(value, source)
    current, _ = state.pointer(repo)
    if current['release_tag'] != source['release_tag'] or current['database']['sha256'] != value['input_database_sha256']:
        raise ValueError('market snapshot advanced; stale outlook refused')
    old, blob = read_pointer(repo)
    if value.get('tracking_parent_release') != ((old or {}).get('release_tag')):
        raise ValueError('outlook advanced after its forward ledger was prepared')
    tag = f"market-outlook-{value['base_trade_date']}-{run_id}-{os.environ.get('GITHUB_RUN_ATTEMPT','1')}"
    deadline = dt.datetime.combine(dt.date.fromisoformat(value['target_trade_date']), dt.time(9,15), c.TZ)
    if c.now()>=deadline:
        raise ValueError('prospective publication window has closed; retain the previous pointer')
    source_commit = os.environ.get('OUTLOOK_SOURCE_COMMIT') or os.environ.get('GITHUB_SHA','main')
    manifest = dict(schema='market-outlook-publication-v1', release_tag=tag,
                    github_commit=source_commit, github_run_id=str(run_id),
                    github_run_url=f'https://github.com/{repo}/actions/runs/{run_id}',
                    snapshot_release=value['snapshot_release'], base_trade_date=value['base_trade_date'],
                    target_trade_date=value['target_trade_date'], input_database_sha256=value['input_database_sha256'],
                    forward_eligible=dt.datetime.fromisoformat(value['generated_at']) < deadline and c.now() < deadline,
                    formal_trade_ready=False, actual_win_rate=None, outlook=value,
                    files={name: state.ref(out/name) for name in ASSETS+tuple(value.get('evidence_assets',[]))}, parent_pointer_blob_sha=blob)
    verify_bundle(out, manifest)
    # Fail before creating a release if its current pointer cannot be read back.
    compact_pointer(manifest)
    state.run('gh','release','create',tag,'--repo',repo,'--target',source_commit,
              '--latest=false','--title',f"Next-session sector outlook {value['target_trade_date']}",
              '--notes','Frozen research ranking and conditional opening watchlist. No orders or mail; scores are not probabilities.')
    state.run('gh','release','upload',tag,*[str(out/name) for name in manifest['files']],'--repo',repo)
    with tempfile.TemporaryDirectory(prefix='outlook-readback-') as temporary:
        state.run('gh','release','download',tag,'--repo',repo,'--dir',temporary)
        verify_bundle(temporary,manifest)
    manifest['assets_readback_verified_at'] = c.stamp()
    manifest['forward_eligible'] = manifest['forward_eligible'] and c.now()<deadline
    if not manifest['forward_eligible']:
        raise ValueError('prospective deadline crossed during asset upload or readback')
    state.write(out/'outlook_manifest.json',manifest)
    state.run('gh','release','upload',tag,str(out/'outlook_manifest.json'),'--repo',repo)
    published_pointer = compact_pointer(manifest)
    state.write(out/'outlook_pointer.json',published_pointer)
    # Refuse races with either market-data publication or another outlook writer.
    current, _ = state.pointer(repo)
    if current['release_tag'] != source['release_tag'] or current['database']['sha256'] != value['input_database_sha256']:
        raise ValueError('market data changed during publication')
    _, current_blob = read_pointer(repo)
    if blob != current_blob:
        raise ValueError('outlook pointer changed concurrently')
    if c.now()>=deadline:
        raise ValueError('prospective publication deadline crossed during asset verification')
    request = dict(message=f'Freeze sector and opening research {value["target_trade_date"]}',
                   branch=state.BRANCH, content=base64.b64encode((out/'outlook_pointer.json').read_bytes()).decode())
    if blob:
        request['sha'] = blob
    state.write(out/'pointer-request.json', request)
    state.run('gh','api','--method','PUT',f'repos/{repo}/contents/{POINTER}',
              '--input',str(out/'pointer-request.json'))
    if read_pointer(repo)[0] != published_pointer:
        raise ValueError('outlook pointer readback differs')
    return manifest


def archive_evidence(directory,destination):
    root=Path(directory).resolve()
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise ValueError('evidence archive refuses symbolic links')
            if path.is_file():
                archive.write(path,path.relative_to(root).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cutoff')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--fund-flow-dir')
    parser.add_argument('--price-action-dir')
    parser.add_argument('--probability-dir')
    parser.add_argument('--pa-trade-dir')
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research'))
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    prepared_minutes=None
    if args.publish:
        restored=json.loads((Path(args.store)/'restore.json').read_text(encoding='utf-8'))
        prepared_minutes=prepare_nextday_minutes(args.repo,restored['manifest'],out/'nextday-evidence')
    value, source = generate(args.store,args.cutoff,args.fund_flow_dir,args.price_action_dir,args.probability_dir,
                             args.pa_trade_dir,active_context=prepared_minutes)
    value['evidence_assets']=[]
    for directory,name in [(args.fund_flow_dir,EXTRA_ASSETS[0]),(args.price_action_dir,EXTRA_ASSETS[1]),
                           (args.probability_dir,EXTRA_ASSETS[2]),(args.pa_trade_dir,EXTRA_ASSETS[3])]:
        if directory:
            archive_evidence(directory,out/name)
            value['evidence_assets'].append(name)
    if args.publish:
        forward_tracking(value,source,args.store,args.repo,prepared_minutes)
    evidence=out/'nextday-evidence'
    evidence.mkdir(exist_ok=True)
    state.write(evidence/'frozen_current_cohort.json',value['nextday_research']['current_cohort'])
    state.write(evidence/'protocol.json',value['nextday_research']['protocol'])
    state.write(evidence/'forward_ledger.json',value['nextday_research'])
    archive_evidence(evidence,out/EXTRA_ASSETS[4])
    value['evidence_assets'].append(EXTRA_ASSETS[4])
    active_evidence=out/'active-trade-evidence'
    active_evidence.mkdir()
    state.write(active_evidence/'independent_rule_and_reference_book.json',value['active_trade_system'])
    archive_evidence(active_evidence,out/EXTRA_ASSETS[5])
    value['evidence_assets'].append(EXTRA_ASSETS[5])
    from market_outlook_report import render
    state.write(out/'outlook.json', value)
    state.write(out/'source_manifest.json', source)
    (out/'report.html').write_text(render(value),encoding='utf-8')
    manifest = publish(args.repo,out,os.environ.get('GITHUB_RUN_ID','local')) if args.publish else None
    print(json.dumps(dict(base=value['base_trade_date'],target=value['target_trade_date'],
                         publication=manifest['release_tag'] if manifest else None,
                         sector_status=value['sector_outlook'].get('status'),
                         smtp_sent=False,orders_submitted=False),ensure_ascii=False))


if __name__ == '__main__':
    main()
