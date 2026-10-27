"""Standalone, offline Chinese sector outlook and conditional opening watchlist."""
from __future__ import annotations
import html
import math
from datetime import datetime, timedelta, timezone


def e(value):
    return html.escape(str(value), quote=True)


def num(value, digits=1):
    return f'{value:,.{digits}f}' if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else '��'


def pct(value):
    return num(value*100,2)+'%' if isinstance(value,(int,float)) and math.isfinite(value) else '��'


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M ����ʱ��')
    except (TypeError,ValueError):
        return '��'


def items(values):
    if isinstance(values,str):
        return e(values)
    return '��'.join(e(v) for v in (values or [])) or '��'


def table(headers, rows):
    head=''.join('<th>'+e(h)+'</th>' for h in headers)
    body=''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)
    return '<div class="scroll"><table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table></div>'


def flow_section(value):
    flow=value.get('fund_flow') or {}
    selection=value.get('sector_stock_selection') or {}
    phases={row.get('sector_code'):row.get('phase_label') for row in selection.get('sector_board',[])}
    rows=[]
    data=flow.get('sectors',[]) if flow.get('status') in ('ready','partial') else []
    data=sorted(data,key=lambda row:row.get('main_net_inflow_yuan') or 0,reverse=True)
    def yi(number):
        return num(number/1e8,2)+'��' if isinstance(number,(int,float)) else '��'
    for row in data[:8]:
        rows.append(['<strong>'+e(row.get('name','��'))+'</strong>',yi(row.get('main_net_inflow_yuan')),
                     num(row.get('main_net_ratio_pct'),2)+'%',yi(row.get('main_net_3d_yuan')),
                     yi(row.get('main_net_5d_yuan')),e(phases.get(row.get('code')) or '���˶�ͬԴ�ɷ�����')])
    if not rows:
        rows=[['����ȱ�ټ�ʱ��ƥ�����ʵ�ʽ���','��','��','��','��','��ʹ�óɽ������Ƿ�ð���ʽ���']]
    board_rows=[]
    for board in selection.get('sector_board',[]):
        if board.get('eligible_for_stock_selection') is not True:continue
        board_rows.append([e(board.get('sector','��')),e(board.get('phase_label','��')),num(board.get('score')),
                           '�ʽ����ƹ���' if board.get('flow_integrated') else '������֤��',
                           num(board.get('member_count'),0)+'ֻ'])
        if len(board_rows)>=6:break
    coverage=flow.get('coverage') or {}
    result='<section><h2>����ʽ�����Ǳ��תǿ</h2><p>�ȿ���ʵ��������ٺ˶Զ��ձ仯������ɢ�����ջ�������������ͼ۸�����תǿ�ֱ�۲죬�����벻�Զ������������ǡ�</p>'
    result+=table(['���','���վ���','��ռ��','3�վ���','5�վ���','�ṹ�׶�'],rows)
    result+='<p class="note">�ʽ����� '+e(flow.get('base_trade_date') or value.get('base_trade_date','��'))+'�����ڿ���'+num(coverage.get('valid_sector_count'),0)+'����ҵ���ʽ��Ƕ����Ƹ���������ͳ�ƣ����ܺ�B�ɣ���Ʊ�۲�ֻɸA�ɡ������ǻ����˻��ֲ֣�Ҳ����ȫ�г��ֽ����롣<a href="https://data.eastmoney.com/bkzj/hy.html">�鿴��Ӧ�̿ھ�</a>��</p>'
    if board_rows:
        result+='<h2 style="margin-top:24px">��ǿ����е�ѡ�ɷ�Χ</h2>'+table(['���','�׶�','�����','֤��','ƥ��ɷ�'],board_rows)
    result+='<p class="note">�ʽ���ҵ��۸���ҵֻ��ͨ��ͬԴ�ɷ�֤ȯ����˶Բźϲ���û��ƥ����ҵ���ʽ�֤�ݻ�۸���Ϊ�Ĺ�Ʊ����������Ҫ�۲�����������תǿ���ڴ���֤���裬������ǰ֪����Ҫ���ǡ�</p></section>'
    return result


def mathematical_section(value):
    study=value.get('price_action_research') or {}
    diagnostic=study.get('diagnostic') or {}
    labels={'unfiltered_price_action_ranking_baseline':'ͬ�������������׼',
            'selected_policy':'ѡ����ȷ�����о�����','fixed_compression_breakout':'������ͻ��',
            'fixed_trend_pullback':'�����������ز�','fixed_higher_lows':'�ߵ͵�̧��'}
    rows=[]
    for name,label in labels.items():
        metric=diagnostic.get(name) or {}
        if not metric.get('n_attempts'):continue
        rows.append([label,num(metric.get('n_attempts'),0)+'��'+num(metric.get('n_unresolved'),0),num(metric.get('independent_entry_dates'),0),
                     pct(metric.get('win_rate')),pct(metric.get('ge3_rate')),
                     pct(metric.get('mean_closed_gross_return')),pct(metric.get('strength3_rate'))])
    result='<section><h2>��ѧ��۸���Ϊ����</h2><p>��ͻ�ơ��زȺ͸ߵ͵�̧��д�ɿɸ������ATR��������������Ч�ʺ��������ƶ�ռ�ȣ�����λ������Ӱ�����ֳнӺͻ��䡣�������ڽ������裬��ʷ���鸺���ж��Ƿ�ֵ�ü�����</p>'
    if rows:
        result+=table(['�о�����','�볡��δ���','������','ëӯ������','׬��3%','ƽ���ѱպ�����','����3���ո�'],rows)
    else:
        result+='<p class="muted">��������������汾ƥ��ļ۸���Ϊ��ϣ�������ȱ��</p>'
    policy=study.get('policy') or {}
    status='���о���̬����ͨ��ѡ���������Ĺ�������ǰ��֤�ݡ�' if policy.get('active') else '���о���̬����δͨ��ѡ���ڣ���������ϵͳ��ǰ���ƻ���'
    result+='<p class="note">'+e(status)+' ������ԭ5��10Ԫ�ص���ʷ���գ��°�5��100ԪԤ���о��ؼ��ʽ���������ǰ����顣����ʷ�����ڿ�������������������������֤�����߿��̺ʹ����ǲο��ɽ��龰���޷�֤�����˶����ɽ�����ʷ���û���ʽ�����ʱ��ҵ�ɷ�֤�ݣ����ܵ������װ���ʽ���Ե�ʤ�ʡ�</p>'
    risks=[]
    for name,label in labels.items():
        metric=diagnostic.get(name) or {}
        if not metric.get('n_attempts'):continue
        ci=(metric.get('date_block_bootstrap') or {}).get('win_rate_ci95') or []
        risks.append([label,pct(metric.get('mean_win_return')),pct(metric.get('mean_loss_return')),num(metric.get('profit_factor'),2),(pct(ci[0])+'��'+pct(ci[1])) if len(ci)==2 else '��',pct(metric.get('realized_closed_trade_max_drawdown'))])
    if risks:
        result+='<details><summary>�������ӯ�����������ѱպϷ���</summary>'+table(['����','ƽ��ӯ��','ƽ������','ӯ������','ӯ������95%����','�ѱպϻس�'],risks)+'<p class="note">���볡���ڷ�����������䷴ӳ����������Ĳ�ȷ���ԣ�����֤��δ����Ч���س�δ����δ֪ͷ�綢�У�����������ʵ�����س���</p></details>'
    legacy_scope = ('<p class="note">����Ϊ��T+1�о��ھ���ֻ������ʷ���գ���������ϵͳԤ�ȶ�������1��2���������˳����ޡ�</p>'
                    if (value.get('active_trade_system') or {}).get('selection_rule_version') == 3 else '')
    result+=legacy_scope+'<p><b>'+('��T+1�о�Ŀ�꣺' if legacy_scope else 'T+1��Ŀ�꣺')+'</b>D1���롢D2�˳���Ĭ���о�3%Ŀ����2%���ղο���D1��ֹ��������D2���տ���Խ�����ռۡ�6%���й۲죬����Ϊ�˵ȵ�3�����Ƕ��Զ��ӳ��ֲ֡�</p><p><b>������ǿ����Ŀ�꣺</b>�ֱ����D1��D2��D3�����ոߣ�ȱ��D3���鲻ɾ����֪��D2�����Ҳ����3�������ʵ���T+1ӯ��ʤ�ʡ�</p>'
    result+='<p class="note">�������棽ӯ��������ƽ��ӯ�������������ƽ������80%ʤ���Կ����������������Ǯ�����ͬʱ���ӯ���ȡ��������Ͱ����ڷ���Ĳ�ȷ�����䡣�������Ͻ��ṩ���������о�˼·��<a href="https://www.nber.org/papers/w7613">Lo��Mamaysky��Wang��2000��</a>��<a href="https://www.cmegroup.com/education/courses/trading-and-analysis/support-and-resistance">CME֧���������γ�</a>�����ݴ��ƶ�A�ɶ�����Ч��</p></section>'
    return result


def selection_funnel(value):
    selection=value.get('sector_stock_selection') or {}
    coverage=selection.get('coverage') or {}
    result='<p class="note">Ԥ���о���Ϊ5��100Ԫ��ÿ���������1��Ԫ��100�������֣��۸�������������ǰ���ᡣɸѡ���̣�'+num(coverage.get('opening_candidate_pool'),0)+'ֻ������ѡ �� '+num(coverage.get('within_sector_candidates'),0)+'ֻƥ����ǿ��� �� '+num(coverage.get('primary_count'),0)+'ֻ��Ҫ�۲졢'+num(coverage.get('reserve_count'),0)+'ֻ�����۲졣ԭ5��10Ԫ�ر�������ʷ���գ�����۸�Χ���ſ��ʽ�ͼ۸���ΪҪ��</p>'
    names={'price_action_quality_below_threshold':'�۸���Ϊ���ֲ���','no_predefined_price_action_structure':'δ����Ԥ���۸���Ϊ�ṹ','candidate_history_or_features_unverified':'��ʷ������δ����'}
    rows=[]
    for row in selection.get('rejected_candidates',[])[:10]:
        reason=row.get('reason') or '��δͬʱ��������'
        rows.append([e(row.get('symbol','��')),e(row.get('name','��')),e(row.get('sector','��')),e(names.get(reason,'��δͬʱ����Ԥ������'))])
    if rows:result+='<details><summary>�鿴���ų��İ���ں�ѡ</summary>'+table(['����','����','���','�׸��ų�ԭ��'],rows)+'</details>'
    return result


def render(value):
    from probability_report import render as probability_section
    from pa_trade_report import render as pa_trade_section
    from nextday_report import render as nextday_section
    from active_trade_report import render as active_section
    sectors=value.get('sector_outlook') or {}
    plan=value.get('opening_strength_plan') or {}
    rankings=sectors.get('rankings') or []
    watch=plan.get('watchlist') or []
    sector_rows=[]
    for i,row in enumerate(rankings[:8],1):
        sector_rows.append([str(i),'<strong>'+e(row.get('industry') or row.get('sector') or row.get('name') or '��')+'</strong>',
                            num(row.get('score'),1),items(row.get('drivers') or row.get('reasons')),
                            items(row.get('risks') or row.get('invalidation'))])
    if not sector_rows:
        sector_rows=[['��','����û���㹻�Ŀ�����ҵ����','��',e(sectors.get('reason') or sectors.get('status') or '�ȴ�����'),'������ȱ�����þ��������']]
    watch_rows=[]
    for i,row in enumerate(watch,1):
        label=e(row.get('name') or row.get('symbol') or '��')+('�����۲죩' if row.get('is_reserve_only') else '')
        conditions=row.get('entry_conditions') or []
        cancels=row.get('cancel_conditions') or []
        rationale=row.get('rationale') or row.get('reasons') or row.get('drivers') or []
        description=items(rationale[:2] if isinstance(rationale,list) else rationale)
        if conditions: description+='<br><span class="muted">ȷ�ϣ�'+items(conditions[1:3])+'</span>'
        if cancels: description+='<br><span class="muted">������'+items(cancels[:1])+'</span>'
        watch_rows.append([str(i),'<strong>'+label+'</strong><br><span class="muted">'+e(row.get('symbol',''))+'</span>',
                           '<span style="white-space:nowrap">'+e(row.get('industry') or row.get('sector') or 'δƥ��')+'</span>',num(row.get('close') or row.get('base_close'),2),
                           num(row.get('research_score',row.get('score')),1),description])
    if not watch_rows:
        watch_rows=[['��','���ڱ���������','��','��','��','��δͬʱ������ǿ��顢�ʽ�ͼ۸���Ϊ����']]
    coverage=sectors.get('coverage') or {}
    coverage_text=' �� '.join(e(str(k))+': '+e(str(v)) for k,v in coverage.items() if not isinstance(v,(list,dict)))
    source_urls=[]
    sources=sectors.get('sources') or {}
    source_items=sources.get('membership_urls',[]) if isinstance(sources,dict) else sources
    for item in source_items:
        url=item.get('url') if isinstance(item,dict) else item
        if isinstance(url,str) and url.startswith(('https://','http://')):
            source_urls.append('<a href="'+e(url)+'">'+e(item.get('label') or item.get('name') or url)+'</a>' if isinstance(item,dict) else '<a href="'+e(url)+'">��ҵ��Դ</a>')
    tracking=value.get('sector_forward_tracking') or {}
    tracking_text=('�����'+str(tracking.get('resolved_dates',0))+'��Ԥ�⽻���յ�ǰ��˶ԣ�'
        +'�����ǿ������'+pct(tracking.get('relative_strength_hit_rate'))
        +'���������Ǳ���'+pct(tracking.get('absolute_up_rate'))+'��')
    evidence=' / '.join(source_urls[:3]) or '��Դ��¼�����ڱ��ڶ������������ҵ�����С�'
    plan_note=items(plan.get('entry_rules') or plan.get('conditions') or plan.get('notes') or [])
    if plan_note=='��':
        plan_note='���3������Ԥ�������4��5��ֻ�۲죬���Զ���λ�����ȱ��ƥ��ʱ��������������ü���������'
    holding_v3 = (value.get('active_trade_system') or {}).get('selection_rule_version') == 3
    research_title = '�����붳���������˳����о�˳��' if holding_v3 else '����������˳����о�˳��'
    review_label = '�� ������·���˳�' if holding_v3 else '�� ���ռ���'
    objective_note = ('��ϵͳ���1��2�������հ�����·���˳������������ȷ����������1�������˳������������������2�������˳����׸������ղ���ǿ���˳�����������ӳ���Ŀ�������ëӯ��ʤ�ʣ���ȡ3%��6%�����۽��׷��á�'
                      if holding_v3 else 'Ŀ��������ߵ�1�����롢��2��������ëӯ��ʤ�ʣ���ȡ3%��6%�����۽��׷��á�')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>���հ��չ���뿪�̹۲�</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f2;color:#172722;font:16px/1.7 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}main{max-width:1180px;margin:auto;padding:42px 34px 70px}.eyebrow{font-size:13px;letter-spacing:2px;color:#537367}h1{font-size:36px;line-height:1.3;margin:12px 0}h2{font-size:23px;margin:0 0 12px}p{margin:8px 0 16px}.lead{max-width:910px;font-size:18px}.muted,small{color:#66766e}.ribbon{display:flex;gap:18px;margin:26px 0}.tile{background:#fff;border:1px solid #dce3dc;border-radius:12px;padding:18px 22px;flex:1}.tile b{display:block;font-size:27px}.tile span{font-size:13px;color:#66766e}section{background:white;border:1px solid #dde4dc;border-radius:14px;margin-top:22px;padding:25px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;background:#edf2ee;color:#486150;font-size:13px}th,td{padding:13px 12px;vertical-align:top;border-bottom:1px solid #e7ece6}th:first-child{width:46px}td:nth-child(2){min-width:125px}a{color:#216f55}.callout{border-left:4px solid #a07636;background:#fbf6ec;padding:15px 19px;margin-top:20px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.step{background:#f5f7f2;padding:17px;border-radius:9px}.step b{display:block;color:#28664b}.note{font-size:13px;color:#65736b;margin-top:15px}.pill{font-size:12px;background:#e9eee7;padding:4px 9px;border-radius:20px}.provenance{font-size:12px;overflow-wrap:anywhere}details{margin-top:15px}summary{cursor:pointer;font-weight:bold}@media(max-width:650px){main{padding:25px 15px}h1{font-size:27px}.ribbon{gap:9px}.tile{padding:13px 10px}.tile b{font-size:21px}section{padding:18px 13px}.steps{grid-template-columns:1fr}table{min-width:720px}.lead{font-size:16px}}
</style><style>th{white-space:nowrap}td:first-child{white-space:nowrap}</style></head><body><main><div class="eyebrow">A���о� �� ��һ�����չ۲�</div><h1>�ȿ���飬�ٵȿ���ȷ��</h1>
<p class="lead">���ȹ۲������ǿ�ȡ����Ǽ����ͳɽ�����֧�ֵİ�飬�ٴӿɳɽ�����ͨ���ƹ�Ʊ��Ѱ��תǿ���ᡣ�����Ǵ���֤���о����򣬵�ǰû����ȷ�ϵ������źš�</p>
<p class="muted">Ŀ�꽻���� <b>'''+e(value.get('target_trade_date','��'))+'''</b>������������� '''+e(value.get('base_trade_date','��'))+'''���̡������о������� '''+e(local_time(value.get('information_cutoff')))+'''</p>
<div class="ribbon"><div class="tile"><span>��ʵ�ʽ�����ҵ</span><b>'''+str(len((value.get('fund_flow') or {}).get('sectors',[])))+'''</b></div><div class="tile"><span>������鸲��</span><b>'''+str(len(rankings))+'''</b></div><div class="tile"><span>���������۲��Ʊ</span><b>'''+str(len(watch))+'''</b></div><div class="tile"><span>��ʵ�ɽ�ʤ��</span><b>δ��֤</b></div></div>
'''+active_section(value.get('active_trade_system') or {})+nextday_section(value.get('nextday_research') or {})+pa_trade_section(value.get('pa_trade_research') or {})+probability_section(value.get('probability_research') or {})+flow_section(value)+'''<section><h2>���հ��ǿ��չ��</h2><p>Ŀ�����ҳ���һ�����ո����������ǿ�İ�飻�����ǿʱ�Կ����µ���������ӳ�����о����򣬲������Ǹ��ʡ�</p>'''+table(['����','���','�о���','֧������','ʧЧ�����'],sector_rows)+'''
<p class="note">ÿ������ı����������ڣ��������ݲ���ʱ�Ŀ�ȱ��'''+e(sectors.get('methodology_note') or sectors.get('score_definition') or '')+'''</p></section>
<section><h2>����תǿ�۲�����</h2><p>��Ҫ�����뵱�����ǣ�Ҳ�����Ѿ�������ͣ�Ĺ�Ʊ��Ϊ��������ᡣ�����ڿ���ǰ���ᣬ�������ٺ˶Լ۸񡢽���״̬�ͼ�ʱ���顣</p>'''+selection_funnel(value)+table(['���','��Ʊ','��ҵ','ǰ����','�о���','�����뿪������'],watch_rows)+'''
<div class="callout"><strong>����ǰ�ǹ۲�ƻ���</strong> 09:25�����ṩ�����嵥��ʵ�ʿ��̺�ֻ�м�ʱ���顢����״̬���޼۵Ⱥ�����ȫ���ſ��о��Ƿ�����תǿ��������������������ʵ�ʳɽ���</div>
<p class="note">'''+plan_note+'''</p></section>
'''+mathematical_section(value)+'''<section><h2>'''+research_title+'''</h2><div class="steps"><div class="step"><b>�� ǰ����׼��</b>������͸��ɹ۲�أ��۸�������ǰȷ�����߿��������޼�������</div><div class="step"><b>�� ���̺���</b>�˶Ե�������Ϳɽ���״̬���޼�ʱ���ݡ�������������ʱ��ȷ���źš�</div><div class="step"><b>'''+review_label+'''</b>��Ԥ���˳������¼ëӯ�����ﵽ3%��6%�ı����������δ����˳�ȫ��������</div></div>
<p class="note">'''+objective_note+'''���в�����δ��֤80%����ʤ�ʣ�����������Ϣ�����Զ��ı���һ���ۡ�</p></section>
<section><h2>֤�ݷ�Χ���Զ�����</h2><p>���Ԥ���뿪�̼ƻ������뱨��׼�����̡�ÿ���̺󶳽��о�������������������������һ�����ս������ǰ�����������ʡ�</p><p>'''+e(tracking_text)+''' ȱʧ�����Ԥ�Ᵽ��Ϊδ��������������У�ÿ��ֻ�����綳���һ��ͳ�ơ�</p><p>�ʼ�ʱ��������ñ���ʱ��09:00��09:25��18:00��������������δ����GitHub�����Կ����Ŷӣ��ϸ�׼�㷢��������ǰ׼���Ķ������з�����</p>
<details><summary>��������Դ</summary><p class="note">'''+coverage_text+'''</p><p class="note">'''+evidence+'''</p><p class="provenance">����汾��<a href="https://github.com/cluuacc-gif/ashare-research/releases/tag/'''+e(value.get('snapshot_release',''))+'''">'''+e(value.get('snapshot_release','��'))+'''</a><br>����У��ֵ��'''+e(value.get('input_database_sha256','��'))+'''</p><p class="note">��ҵ�������������֪ʱ��֮����о�����������ʷ�ɷ�������ز������ʡ���ǰ���򲻰�������֤����Ϣ�߻����ʽ���Ԥ�⡣</p><p class="note">����ʱ�������ο���<a href="https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml">�Ͻ���2026�꽻�׹���</a>�������о��������ʼ�����ָ�</p></details></section>
<p class="note">��ҳΪѡ���о���۲�ƻ�������ʱ�����������ڷֱ���ʾ����ĩ���ɵ�Ԥ����������һ�������������ǰԤ�⡣</p></main></body></html>'''
