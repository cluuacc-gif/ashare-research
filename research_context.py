"""Bounded optional published context, without collecting or sending at mail time."""
import copy
import datetime as dt
import json
import os
import subprocess


def instant(value):
    try:
        parsed=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except (TypeError,ValueError):
        return None


def unavailable_flow(base, reason):
    return {'schema':'sector-fund-flow-v1','classification':'eastmoney_industry',
            'status':'unavailable','base_trade_date':base,'sectors':[],
            'blockers':[reason],'flow_is_price_volume_proxy':False}


def verified_flow(value, base, cutoff):
    """Refuse stale dates, unknown acquisition times and mixed taxonomies."""
    limit=instant(cutoff)
    if not isinstance(value,dict) or value.get('schema')!='sector-fund-flow-v1':
        return unavailable_flow(base,'资金流证据未就绪')
    if value.get('classification')!='eastmoney_industry' or value.get('base_trade_date')!=base:
        return unavailable_flow(base,'资金流日期或行业分类与本期不匹配')
    if value.get('status') not in ('ready','partial'):
        return unavailable_flow(base,'本期没有核验通过的真实资金流数据')
    for key in ('information_cutoff','observed_at'):
        known=instant(value.get(key))
        if known is None or limit is None or known>limit:
            return unavailable_flow(base,'资金流取得时间晚于报告截止或时间未知')
    rows=value.get('sectors')
    if not isinstance(rows,list) or not rows:
        return unavailable_flow(base,'资金流没有可用板块')
    for row in rows:
        if not isinstance(row,dict) or row.get('trade_date')!=base:
            return unavailable_flow(base,'板块资金流包含日期不明或过时行')
        for key in ('source_updated_at','observed_at'):
            known=instant(row.get(key))
            if known is None or known>limit:
                return unavailable_flow(base,'板块资金流包含时间未知或未来数据')
        if instant(row['source_updated_at']).astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()!=base:
            return unavailable_flow(base,'资金流供应商行情时间与基准日不符')
        if instant(row['source_updated_at'])>instant(row['observed_at']):
            return unavailable_flow(base,'资金流供应商时间晚于取得时间')
        for member in row.get('members',[]):
            known=instant(member.get('observed_at'))
            if known is None or known>limit or str(member.get('effective_date','9999'))>str(limit.date()):
                return unavailable_flow(base,'行业成分不符合报告已知时间')
            if member.get('flow_status')=='verified_current_date':
                updated=instant(member.get('source_updated_at'))
                observed=instant(member.get('flow_observed_at'))
                if (member.get('flow_trade_date')!=base or updated is None or observed is None
                        or not updated<=observed<=limit
                        or updated.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()!=base):
                    return unavailable_flow(base,'个股资金流日期或取得时间不符')
    result=copy.deepcopy(value)
    result['source_evidence_verified']=True
    result['flow_is_price_volume_proxy']=False
    return result


def published_context(snapshot, cutoff, repo=None):
    """A single compact-pointer read; SMTP preparation cannot wait indefinitely."""
    base=snapshot['base_trade_date']
    safe={'fund_flow':unavailable_flow(base,'本期未取得匹配的已发布资金流'),
          'price_action_research':{'status':'unavailable','formal_trade_ready':False},
          'probability_research':{'status':'unavailable','formal_trade_ready':False},
          'pa_trade_research':{'status':'unavailable','formal_trade_ready':False}}
    try:
        import base64
        from pipeline_state import BRANCH
        repository=repo or os.environ.get('GITHUB_REPOSITORY','cluuacc-gif/ashare-research')
        raw=subprocess.run(['gh','api',f'repos/{repository}/contents/state/market-outlook-current.json?ref={BRANCH}'],
                           check=True,capture_output=True,text=True,timeout=10).stdout
        envelope=json.loads(raw)
        value=json.loads(base64.b64decode(envelope['content']))
        if (value.get('snapshot_release')!=snapshot['release_tag'] or
                value.get('input_database_sha256')!=snapshot['database']['sha256'] or
                value.get('base_trade_date')!=base or not value.get('assets_readback_verified_at')):
            return safe
        outlook=value['outlook']
        safe['fund_flow']=verified_flow(outlook.get('fund_flow'),base,cutoff)
        study=outlook.get('price_action_research') or {}
        generated=instant(study.get('generated_at'))
        if (study.get('input_database_sha256')==snapshot['database']['sha256']
                and study.get('base_trade_date')==base and generated is not None
                and generated<=instant(cutoff) and study.get('formal_trade_ready') is False):
            safe['price_action_research']=study
        study=outlook.get('probability_research') or {}
        generated=instant(study.get('generated_at'))
        if (study.get('schema')=='calibrated-tplus1-probability-v1'
                and study.get('input_database_sha256')==snapshot['database']['sha256']
                and study.get('base_trade_date')==base and generated is not None
                and generated<=instant(cutoff) and study.get('formal_trade_ready') is False
                and study.get('execution_win_rate_80_validated') is False
                and study.get('actual_win_rate') is None):
            safe['probability_research']=study
        study=outlook.get('pa_trade_research') or {};generated=instant(study.get('generated_at'))
        if (study.get('schema')=='pa-context-trade-research-v1'
                and study.get('input_database_sha256')==snapshot['database']['sha256']
                and study.get('base_trade_date')==base and generated is not None
                and generated<=instant(cutoff) and study.get('formal_trade_ready') is False
                and study.get('execution_win_rate_80_validated') is False
                and study.get('actual_win_rate') is None):
            safe['pa_trade_research']=study
        safe['context_release']=value['release_tag']
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        pass
    return safe
