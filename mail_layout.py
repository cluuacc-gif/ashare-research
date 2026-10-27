"""Concise research email content, with matching plain text and safe inline HTML."""
import html
import math
import datetime as dt
from html.parser import HTMLParser
from urllib.parse import quote

VERSION = 'research-email-v8-versioned-active-holding'
STAGES = {'evening':('����','18:00 �̺�'), 'morning':('����','09:00 ��ǰ'), 'auction':('09:25�۲�','����ʱ��')}
REPO_URL = 'https://github.com/cluuacc-gif/ashare-research'


def number(value, digits=2):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '��'
    return f'{value:,.{digits}f}'


def pct(value):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '��'
    return f'{value:.1%}'


def money(value):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '��'
    if abs(value)>=100000000: return f'{value/100000000:.2f}��'
    return f'{value/10000:,.0f}��'


def _phrases(value, limit=3):
    if isinstance(value, str): return value
    if not isinstance(value, (list, tuple)): return ''
    return '��'.join(str(item) for item in value[:limit] if isinstance(item, (str, int, float)))


def _outlook_current(payload, value, schema, base, target):
    """A report must not present a different session or future input as its own."""
    if not isinstance(value, dict) or value.get('schema') != schema: return False
    if value.get('base_date') != base or value.get('target_date') != target: return False
    try:
        own = dt.datetime.fromisoformat(value['information_cutoff'])
        report = dt.datetime.fromisoformat(payload['information_cutoff'])
        return own.utcoffset() is not None and report.utcoffset() is not None and own <= report
    except (KeyError, TypeError, ValueError):
        return False


def _sector_section(payload, value, base, target):
    current = _outlook_current(payload, value, 'sector-outlook-v1', base, target)
    rows = value.get('rankings') if current and value.get('status') == 'ready' else []
    rows = rows if isinstance(rows, list) else []
    rendered = []
    risks = []
    for row in rows[:5]:
        if not isinstance(row, dict): continue
        rendered.append([str(row.get('rank', len(rendered)+1)), str(row.get('sector') or '��'),
                         number(row.get('score'), 1),
                         number(row.get('member_count'), 0)+'ֻ��'+pct(row.get('coverage')),
                         _phrases(row.get('drivers'), 2) or '�ȴ��������'])
        risk = _phrases(row.get('risks'), 1)
        if risk: risks.append(str(row.get('sector') or '���')+'��'+risk)
    if rendered:
        sources=value.get('sources') if isinstance(value.get('sources'), dict) else {}
        coverage=value.get('coverage') if isinstance(value.get('coverage'), dict) else {}
        classification={'sina_industry':'������ҵ����', 'exchange_industry':'֤ȯ������ҵ�ֶ�'}.get(value.get('classification'), '��Ӧ����ҵ����')
        note = (f"Ԥ�� {target} ��Ա��θ����г���ǿ�����򣻻������� {base}����֪��Ϣ���� {value.get('information_cutoff')}��"
                f"�������{classification}������{number(coverage.get('covered_sectors'), 0)}����顢{number(coverage.get('covered_stocks'), 0)}ֻ��Ʊ��"
                f"��ҵ�ɷ��������ʱ��{sources.get('membership_latest_observed_at') or 'δ��¼'}��"
                '����������ʽ������򣬲������Ǹ��ʻ���ʤ�ʣ�ͬһ���Ҳ���ܸ�������µ���'
                '��ʷʱ��ɷ�֤�ݲ��㣬��ǰ�Ӷ�������һ�����տ�ʼ���ټ��顣')
        if sources.get('membership_effective_after_price_base'):
            note += ' ���β��û���������֮�󲹳����ҵ�ɷ֣�ֻ������ǰ��ֹʱ���֪����Ϣ��'
        if risks: note += ' ���գ�'+'��'.join(risks[:3])+'��'
    else:
        note = (f'Ŀ�꽻���� {target or "��"}������û�����������ں���Ϣ��ֹƥ��ĳ�ְ��֤�ݣ��ݲ��г�����ǿ��������'
                '����ѵ����Ƿ���ֱ�ӳ�������Ԥ�⡣')
    return {'title':'���հ��ǿ��չ��', 'columns':['����','���','ǿ����','����������','��Ҫ����'],
            'rows':rendered, 'note':note}


def _opening_section(payload, value, base, target):
    current = _outlook_current(payload, value, 'opening-strength-research-v1', base, target)
    rows = value.get('watchlist') if current else []
    rows = rows if isinstance(rows, list) else []
    rendered = []
    cancellations = []
    for row in rows[:5]:
        if not isinstance(row, dict): continue
        name=str(row.get('name') or '��')+('�����۲죩' if row.get('is_reserve_only') else '')
        rendered.append([str(row.get('symbol') or '��'), name,
                         str(row.get('sector') or 'δƥ��'), number(row.get('close')),
                         _phrases(row.get('entry_conditions'), 3) or '�ȴ���������ȷ��'])
        cancel = row.get('cancel_conditions')
        for condition in cancel if isinstance(cancel, list) else []:
            if isinstance(condition, str) and condition not in cancellations: cancellations.append(condition)
    confirmed = current and bool(value.get('live_signal'))
    if rendered:
        note = (f'�۲� {target} ���̿���תǿ�Ĺ�Ʊ���������� {base}����֪��Ϣ���� {value.get("information_cutoff")}��'
                '�˱�Ϊ�����۲�أ�ǰ���̷��������ڿ��������źš�')
        note += ('��ȡ������ʱЧҪ��Ŀ���ȷ�����룬������ֻ����ɽ�������' if confirmed else
                 '��ǰû��ͨ������ļ�ʱ����ȷ�ϣ������ɿ�ִ������ָ�')
        if cancellations: note += ' ȡ��������'+'��'.join(cancellations[:3])+'��'
        if any(isinstance(row,dict) and row.get('is_reserve_only') for row in rows[:5]):
            note += '���3���̶��ʽ������4��5�����۲죬���Զ���λ��'
        note += '��1������󣬵�2�찴Ԥ���˳������о�������3%��6%��Ŀ�꣬��δ֤ʵʤ�ʸ���80%��'
    else:
        note = (f'Ŀ�꽻���� {target or "��"}������û�����������ںͽ�ֹʱ��ƥ��Ŀ��̹۲�أ�'
                'ȱ�����ʡ��ɺ���Ŀ��̱���ʱ����������ָ�')
    return {'title':'����תǿ�����۲�', 'columns':['����','����','���','ǰ����','��Ҫȷ�ϵ�����'],
            'rows':rendered, 'note':note}


def _flow_section(payload, value, selection, base):
    from research_context import verified_flow
    flow=verified_flow(value,base,payload.get('information_cutoff'))
    phases={row.get('sector_code'):row.get('phase_label') for row in selection.get('sector_board',[])}
    rows=[]
    for row in sorted(flow.get('sectors',[]),key=lambda x:x.get('main_net_inflow_yuan') or 0,reverse=True)[:5]:
        rows.append([str(row.get('name') or '��'),money(row.get('main_net_inflow_yuan')),
                     number(row.get('main_net_ratio_pct'))+'%',money(row.get('main_net_3d_yuan')),
                     money(row.get('main_net_5d_yuan')),str(phases.get(row.get('code')) or '���˶�����')])
    note=('�������� '+str(base)+'�����ջ����ͳ�������ֱ��жϣ��ʽ���Ʋ�����δ�����ǡ�'
          '������������Ϊ��Ӧ�̳ɽ�����ͳ�ƣ���ҵ���ܺ�B�ɣ������ڻ����˻�ʵ�����֣��۲��ƱֻɸA�ɡ�'
          'ֻ��ͬԴ�ɷִ���ƥ����ܽ�������ѡ�ɣ�������ҵ����Ӳƴ��ȱ����ʵ�ʽ����������۹��������')
    if not rows:note+=' ����ȱ�����ں���֪ʱ��һ�µ��ʽ�����������ȱ��'
    return {'title':'����ʽ�����Ǳ��תǿ','columns':['���','���վ���','��ռ��','3�վ���','5�վ���','�׶�'],
            'rows':rows,'note':note}


def _math_section(study):
    labels={'unfiltered_price_action_ranking_baseline':'���������׼','selected_policy':'ѡ�����о�����',
            'fixed_compression_breakout':'������ͻ��','fixed_trend_pullback':'�����ز�','fixed_higher_lows':'�ߵ͵�̧��'}
    rows=[]
    for key,label in labels.items():
        metric=(study.get('diagnostic') or {}).get(key) or {}
        if metric.get('n_attempts'):
            rows.append([label,number(metric.get('n_attempts'),0)+'��'+number(metric.get('n_unresolved'),0),
                         pct(metric.get('win_rate')),pct(metric.get('ge3_rate')),
                         pct(metric.get('mean_closed_gross_return')),pct(metric.get('strength3_rate'))])
    return {'title':'�۸���Ϊ��T+1��ʷ���','columns':['����','�볡��δ���','ëӯ��','�ﵽ3%','ƽ���ѱպ�����','����3���ո�'],
        'rows':rows,'note':'ATR����������������ͻ�ƻزȺ͸ߵ͵�ṹ����Ԥ����ѧ������㣬�������Ǹ��ʡ�'
        '��������ԭ5��10Ԫ�ص���ʷ���գ��°�5��100ԪԤ���о��ؼ��ʽ����д�ǰ����顣����ʷ�����ڿ��������߲ο��ɽ���֤����ִ�гɽ����Ҹ����δʹ����ʵ��ʷ�ʽ�����ʱ����ҵ�ɷ֣����ܴ��������ʽ������ʤ�ʡ�'
        'D1��D2�˳�������2��3��ǿ�ȵ����˶ԣ����ݴ��ӳ��ֲ֡���ǰ���۷ѡ�Ĭ��3%Ŀ���2%���ղο������տɳ������ռۣ�6%ֻ��������ǿĿ�ꡣ'}


def _confluence_section(selection):
    rows=[]
    for board in selection.get('sector_board',[]):
        if board.get('eligible_for_stock_selection') is not True:continue
        rows.append([str(board.get('sector') or '��'),str(board.get('phase_label') or '��'),number(board.get('score'),1),
                     '�ʽ𣫼۸���' if board.get('flow_integrated') else '������֤��',number(board.get('member_count'),0)+'ֻ'])
        if len(rows)==5:break
    return {'title':'��ǿ����ڵ�ѡ�ɷ�Χ','columns':['���','�ṹ�׶�','�����','����','�ɷ���'],
        'rows':rows,'note':'������ǿ�ȡ���ɢ�����ߺ�ͬԴ�����룬��������ǿ������תǿ��'
        '����תǿ����ʾ��ǰ���ָ��ƣ�δ֤��δ�����ǣ���Ҫ��Ʊ������ƥ�䱾���ɷֺͼ۸���Ϊ��'
        '�ʽ����и���ͬ�վ��ֵ��δ֪���������۲죻�������������ʱ������������'}


class _ActivePlainText(HTMLParser):
    """Text-only rendering of the authoritative active section, without CSS."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('style', 'script'):
            self.hidden += 1
        elif not self.hidden:
            if tag == 'br':
                self.parts.append('\n')
            elif tag == 'small':
                self.parts.append('��')

    def handle_endtag(self, tag):
        if tag in ('style', 'script'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            if tag in ('th', 'td'):
                self.parts.append(' | ')
            elif tag in ('p', 'div', 'h2', 'h3', 'tr', 'table', 'section', 'summary', 'details'):
                self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line for raw in ''.join(self.parts).splitlines()
                         if (line := ' '.join(raw.split()).strip(' |��')))


def _active_sections(payload, research, base, target):
    """Lead both email formats with the independent rule system and its book."""
    from active_trade_report import compact, POLICY_NAME, VERSION as ACTIVE_VERSION
    active=compact(research.get('active_trade_system'))
    current=False
    if active and active.get('base_trade_date')==base and active.get('target_trade_date')==target:
        try:
            own=dt.datetime.fromisoformat(active['generated_at'])
            boundary=dt.datetime.fromisoformat(payload['information_cutoff'])
            current=own.utcoffset() is not None and boundary.utcoffset() is not None and own<=boundary
        except (KeyError,TypeError,ValueError):
            pass
    if not current:
        active=compact({'schema':ACTIVE_VERSION,'status':'waiting_for_valid_source',
            'base_trade_date':base,'target_trade_date':target,'plans':[]})
    if active.get('selection_rule_version') == 3:
        from active_trade_report import render
        # One authority for route, risk, dates, funnel and versioned statistics.
        # Only the email corrects the old v3 wording: archived HTML is immutable.
        authoritative = render(active).replace('����ǰ30���Ӹ�λ', '���̺���30������ɷ��Ӹ�λ')
        plain = _ActivePlainText()
        plain.feed(authoritative)
        plain.close()
        return [{'title': '�ҵĽ���ϵͳ��' + active['policy_name'],
            'columns': [], 'rows': [],
            'authoritative_html': authoritative, 'authoritative_text': plain.text(),
            'note': '�ƻ���Ϣ��ֹ��' + str(active.get('information_cutoff') or 'δ֪')
                + '���ɶ���ƻ��;ɳֲ��˳����ޱ���ԭֵ�����򱾴������ӳ���'}]
    state={'conditional_plans_ready':'�����ƻ������ɣ�D1 14:40����ȷ�ϴ��˹��˶�',
           'cash_no_qualifying_setup':'����ղ֣�û������ȫ���������¼ƻ�',
           'waiting_for_valid_source':'�ȴ���Ч�����ݣ�ϵͳ��������'}[active['status']]
    sections=[{'title':'�ҵĽ���ϵͳ��'+POLICY_NAME,'columns':['��Ŀ','���ڰ���'],
        'rows':[['����״̬','�����ã�����ʽ�˹�ִ�У�δ����ȯ��'],
                ['����״̬',state],
                ['�̶���Χ','ȫ����ѡ�����300ֻ����������롢תǿ�Ҹ���������ȣ�ȱ��ͬԴ֤������'],
                ['�볡������','D1 14:40ʹ�ô�ǰ��������ȷ�ϣ��˹��˶�ȯ���±����ڶ��᷶Χ�ڣ�����������������ִ��'],
                ['�ʽ����˳�','3��Ԫ�ο��ʽ𣻳ֲ�����볡�ϼ������ֻ����ͬ��飻D1������D2���ṹֹ��3%Ŀ�괥���˳�����ٹ̶�14:50�ο��˳�'],
                ['ʵ�ʳɽ�ʤ��','δ֪��80%����ʵ��ʤ����δ��֤']],
        'note':'�������ö����ھ��о��������ǰû��ʵʱ�����ȯ�����ӣ�δȷ�������źš�δ�ύ�������ƻ���Ҫ�˹��˶��������ӡ��ٷ�״̬���ɳɽ����ۡ�150Ԫ��2%����ѹ��Ԥ�㲻�������ʧ��֤��'}]
    plans=active['plans']
    if plans:
        sectors=[]
        for row in plans:
            identity=row.get('sector_identity') or {}
            sectors.append(identity.get('sector') or '��' if isinstance(identity,dict) else str(identity))
        sections.append({'title':'����ϵͳ���������ƻ�','columns':['��Ʊ�����','��۷�Χ','�ṹֹ��','ֹӯ����','���������ռ��'],
            'rows':[[str(row.get('name') or row['symbol'])+' '+row['symbol']+'��'+sector,
                     number(row.get('frozen_entry_floor'))+'��'+number(row.get('frozen_entry_cap')),
                     number(row.get('structural_stop')),'ʵ����ۡ�1.03',
                     number(row.get('frozen_quantity_shares'),0)+'�ɣ�'+number(row.get('frozen_reserved_cash_cny'))+'Ԫ']
                    for row,sector in zip(plans,sectors)],
            'note':'���������� '+str(active.get('base_trade_date') or '��')+'��D1 '+str(active.get('target_trade_date') or '��')
                +'��D2 '+str(active.get('D2_trade_date') or '����������')+'������ʵ����ۣ�������ǰ���̵��ɽ��ۡ����ۡ�ȱ����ȷ�����򣬲�׷�ۡ������䣻�����˳�����һ���ӿɽ��׿������ο���ͬ����ֹӯֹ�𲢴�ʱֹ�����ȡ�'})
    account=active.get('reference_account') or {}
    if account:
        reference_rate=pct(account.get('reference_profit_rate')) if account.get('reference_profit_rate') is not None else 'δ֪'
        sections.append({'title':'����ϵͳ���Ӳο��˻�','columns':['��Ŀ','�ο��˱�'],
            'rows':[['��ʼ�����ã������ʽ�',number(account.get('initial_capital_cny'))+'��'+number(account.get('available_cash_cny'))+'��'+number(account.get('reserved_cash_cny'))+'Ԫ'],
                    ['��ʵ�ֲο�ӯ������ƽ��ʤ��',number(account.get('realized_reference_pnl_cny'))+'Ԫ��'+reference_rate],
                    ['�ֲ֣���ƽ�֣�δ֪',number(account.get('open_positions'),0)+'��'+number(account.get('reference_closed'),0)+'��'+number(account.get('unknown_positions'),0)],
                    ['��ʵ�ֲ���ӯ��','������ȯ�̳ɽ���ִ�˶ԣ���ǰδ֪']],
            'note':'���Ƿ��Ӳο��ɽ��˱���������ʵ��ȯ�ֲ̳֡��ɽ���ӯ����δ֪�˳�����ռ���ʽ��������òο��˱��ھ���ʵ�ʷ�����ӯ�����ɻ�ִȷ�ϡ�'})
    return sections


def content(payload):
    research = payload.get('research') or {}
    predictions = research.get('model_predictions') or {}
    validation = research.get('model_validation') or {}
    inventory = research.get('inventory') or {}
    stage=payload['stage']; title,clock=STAGES[stage]
    target=predictions.get('target_trade_date')
    if not target:
        target=(payload.get('calendar') or {}).get('next_session') if stage=='evening' else payload.get('target_date')
    base=payload.get('base_trade_date') or research.get('as_of') or inventory.get('latest_date')
    mode='Ԥ��' if payload.get('preview') else ('�ٵ��ع�' if payload.get('late') else clock)
    subtitle=f"{payload['target_date']} �� {mode}"
    subject=f"A��{title}��{payload['target_date']}��{'Ԥ��' if payload.get('preview') else ('�ٵ��ع�' if payload.get('late') else 'ģ�͹۲�')}"
    band=research.get('price_band_5_10') or []
    amounts={r['symbol']:r.get('amount') for r in band}
    rows=[]
    for rank,r in enumerate(predictions.get('rows',[])[:10],1):
        rows.append([str(r.get('rank',rank)),r['symbol'],r.get('name') or '��',number(r.get('close')),
                     pct(r.get('P1')),pct(r.get('P2')),money(amounts.get(r['symbol']))])
    ready=bool(research.get('model_ready') and rows)
    summary=f"��������ϵͳ�����ã�ÿ�հ��̶��������ɼƻ�����ʵʤ��δ֪������ {base or '��'} ���̼��������й۲� {target or '��'} ģ�ͼ۸��¼������� {predictions.get('prediction_count',0):,} ֻ��"
    warnings=[]
    if not ready: warnings.append('ģ��Ԥ����δ����������ֻչʾ���������۲졣')
    if not research.get('formal_trade_ready'): warnings.append('����ǰ���˹����飺�ٷ�ST��ͣ��״̬����Ȩ���޼ۼ������¼���������ȱ�ڡ�ϵͳ���ò����ڳɽ���ʤ������֤��')
    if stage=='auction' and not payload.get('auction_verified'):
        warnings.append('����09:25�۲�����ǰ����ģ�ͣ���ǰ��������δ���飬�ݲ��ṩ���۹ҵ��۸�')
    if payload.get('late') and not payload.get('preview'):
        warnings.append('����Ϊ�ٵ��عˣ���������ǰԤ��ɼ���')
    stats=[('��������ϵͳ','������'),('ʵ�ʳɽ�ʤ��','δ֪'),
           ('Ԥ�⽻����',str(target or '��')),('����������',str(base or '��')),
           ('���ձ���',number(inventory.get('base_quotes'),0)+'ֻ'),
           ('ģ��״̬','ѵ����Ԥ���Ѿ���' if ready else '�ȴ�ģ��'),
           ('�˹�����״̬','�����������ٷ�״̬���˶�'),
           ('5��10Ԫ�۲�',f'{len(band):,}ֻ')]
    active_target=(payload.get('calendar') or {}).get('next_session') or target if stage=='evening' else payload.get('target_date')
    sections=_active_sections(payload,research,base,active_target)
    nextday=research.get('nextday_research') or {}
    if nextday.get('schema')=='nextday-forward-research-v1':
        cohort=nextday.get('current_cohort') or {};coverage=cohort.get('coverage') or {}
        primary=(nextday.get('summary') or {}).get('confluence_opening_retest') or {}
        sections.append({'title':'����ӯ��ǰ���о�','columns':['�۲���Ŀ','����״̬'],
            'rows':[['��Ҫ����','D1�ο����룬D2�̶�14:50�ο��˳����ֱ��¼ëӯ����׬��3%��6%�ı���'],
                    ['�г�����','����ǰ������г���ȡ�������ǿ���Ͱ�������ȳ̶�ɸѡ���ʽ���಻���'],
                    ['ͬ����Ʊ����','09:40��14:40�ֱ�۲죬�����������̻ز⡢β��ȷ�ϣ����������ǰ�̶�'],
                    ['���ڹ۲��',str(coverage.get('base_cohort_count',0))+'ֻ�о�����������'+str(coverage.get('confluence_count',0))+'ֻ���㹲������'],
                    ['���о���',str(primary.get('reference_closed',0))+'�������۸��¼������۲�'+str(primary.get('pending',0))+'��δ֪'+str(primary.get('unknown',0))]],
            'note':'�о�������������������ͬһ�˻��ֲ֡����������̺�ȡ�ã�δ��֤ʵʱ�������ٷ��޼ۻ���ʵ�ɽ��������о����ֶ������ա���������ϵͳ�����ã�ʵ��ʤ��δ֪���о����ݲ�������ʱ�����źš�'})
    pa_trade=research.get('pa_trade_research') or {}
    if pa_trade.get('schema')=='pa-context-trade-research-v1':
        active=bool((pa_trade.get('policy') or {}).get('active'))
        sections.append({'title':'�۸���Ϊ�����߼�����','columns':['����','�о�����'],
            'rows':[['�ȿ�����','��������������ͻزȣ�����ֻ�۲����ؼٵ����ջأ������ʱ����'],
                    ['�ٿ�λ��','����ǰǰ�ߡ�����߽�ͽṹ�͵�����Ϸ��ռ䡢ʧЧ�ۺ�������������'],
                    ['��λ����ѧ','�о��˻�ÿ�������1��Ԫ������150Ԫ�ο����պͶ���2%����ѹ������������'],
                    ['T+1�˳�','D1��������D1���̽ṹʧ����D2�����˳�������Ԥ���ṹ�ۡ�3%��6%Ŀ�꼰D2�����о��˳�'],
                    ['����̬�о�״̬','��ѡ����ͨ�����о���̬�����ı������õĶ���ϵͳ' if active else '���о�û��ͨ��ѡ���ڵ���̬����������ϵͳ��������']],
            'note':'�ο�������PriceAction������Ƶ������Brooksһ�����ۣ�������Ƶ��Ļ��δȡ�ã�������ֵ���ڱ���Ŀ���䡣ӯ��ƽ������ʤ�ʲ���Ԥ��ʤ�ʣ����߿��̲ο����ܴ���ͻ�ƻ�ز��ķ��ӳɽ���֤��'})
        if active:
            watch=pa_trade.get('current_watchlist') or []
            sections.append({'title':'�۸���Ϊ�о��۲�','columns':['��Ʊ','��������','�ṹʧЧ�ο�','Ŀ��','ӯ����'],
                'rows':[[row.get('name') or row['symbol'],number(row.get('entry_cap')),number(row.get('structural_stop')),
                         pct(row.get('target_pct')),number(row.get('reward_risk'))] for row in watch[:3]],
                'note':'�����⸴�˰���ʽ𡢹ٷ�״̬����ʱ�̿ڣ�δ���ӷ�������ʱ������ͻ�ƺͻز�ȷ�ϱ���δ�������ο�ʧЧ���޷���֤ʵ��ֹ��'})
    if stage == 'evening':
        sector_target=(payload.get('calendar') or {}).get('next_session') or target
        sections.append(_flow_section(payload,research.get('fund_flow') or {},research.get('sector_stock_selection') or {},base))
        sections.append(_confluence_section(research.get('sector_stock_selection') or {}))
        sections.append(_sector_section(payload, research.get('sector_outlook') or {}, base, sector_target))
    else:
        sections.append(_opening_section(payload, research.get('opening_strength') or {}, base, payload.get('target_date')))
    gross = research.get('gross_swing_strategy') or {}
    sections.append({'title':'����ë������ԣ����о���', 'columns':['������Ŀ','���о�״̬'],
        'rows':[['Ŀ��','ëӯ��ʤ���ϸ����80%����ȡÿ��3%��6%'],
                ['ͳ�ƿھ�','���۽��׷��ã�ӯ��������׬��3%��6%�ı����ֱ�ͳ��'],
                ['����˼·','������ͨ���ƺͿ�������᣻�߿�����ȡ������׷������ͣ'],
                ['ʵ�ʳɽ�ʤ��','����ͨ����˵���ʵ�ɽ�����'],
                ['���о����׺�ѡ','�ղ֣��ȴ�ǰ��ɽ�������֤�ݣ�������ϵͳ�ֿ�'],
                ['�²����о�','�������о��汾' if gross.get('schema') == 'gross-swing-research-v2' else '�ȴ��뱾������ƥ����о����']],
        'note':'���ڱ������о����տھ�����������������ϵͳ�Ƿ����á���1�����롢��2�찴Ԥ��Ŀ����˳������о���������Ҫ�����뵱�����ǡ�����200�ʡ�60���볡���ڣ������ڷ��������½����80%��ë����Ϊ�����Ž���Ŀ�����ա�ȫ���볡������δ����˳����������ߴ��۲�����ʵ�ʳɽ���'})
    diagnostic = gross.get('diagnostic') or {}
    shown = diagnostic.get('selected_policy' if (gross.get('policy') or {}).get('active') else 'fixed_reference') or {}
    if shown:
        sections.append({'title':'���ڸ��ղ�����ʷ���', 'columns':['�볡����','�ѱպ�','δ���','ëӯ������','׬��3%','׬��6%'],
            'rows':[[number(shown.get('n_attempts'),0),number(shown.get('n_closed'),0),number(shown.get('n_unresolved'),0),
                     pct(shown.get('win_rate')),pct(shown.get('ge3_rate')),pct(shown.get('ge6_rate'))]],
            'note':'����ʷ�����ڿ������������о���ϡ�ȫ�����볡���׾����룬�����������ǵ�ɾ�����𣻴ﵽĿ�����������ܷ�ɽ���'})
    if stage=='evening':
        probability=research.get('probability_research') or {}
        if probability.get('schema')=='calibrated-tplus1-probability-v1':
            diagnostic=probability.get('diagnostic') or {}
            selected=diagnostic.get('selected_policy') or {}
            baseline=diagnostic.get('same_universe_price_action_baseline') or {}
            active=bool((probability.get('policy') or {}).get('active'))
            sections.append({'title':'У׼��������ղ��Լ���','columns':['�о�����','�볡����','ëӯ������','׬��3%','ƽ���ѱպ�����'],
                'rows':[['ͬ����5��100Ԫ�����׼',number(baseline.get('n_attempts'),0),pct(baseline.get('win_rate')),pct(baseline.get('ge3_rate')),pct(baseline.get('mean_closed_gross_return'))],
                        ['ѡ����ȷ���ĸ��ʹ���' if active else '���ʹ���δͨ��ѡ����',number(selected.get('n_attempts'),0),pct(selected.get('win_rate')),pct(selected.get('ge3_rate')),pct(selected.get('mean_closed_gross_return'))]],
                'note':'�Ƚ��߼��ع顢�ݶ��������Ȩ��ϣ�ʹ�ú��ø���У׼��ģ�ͷ�����ˣ�����̶���ѡ��������'
                'ѵ����У׼��ѡ������ϰ����ڸ����������½�����������������տ鸴�ˡ�'
                '����ʷ��ϲ���������������˳ɽ�ʤ�ʣ�δͨ��ʱ���ֿղ֡�3%��6%ʹ�ò�ͬԤ������������������ǿ�����ơ�'})
        sections.append(_math_section(research.get('price_action_research') or {}))
    if rows:
        sections.append({'title':'ģ�͹۲� Top10','columns':['����','����','����','����','P1 ����','P2 ��5%','�ɽ���'],
            'rows':rows,'note':'��P2����P1����һ���������̸��ڻ��������̣�P2�������Ƿ���5%�����߾�Ϊԭʼδ��Ȩ�۸��¼����ʣ�������������ëӯ��ʤ�ʻ�׬��3%�ı�����'})
    elif band:
        sections.append({'title':'�ɽ���ǰ�Ź۲�','columns':['����','����','����','�ɽ���','ST����'],
            'rows':[[r['symbol'],r.get('name') or '��',number(r.get('close')),money(r.get('amount')),
                     'δ֪' if r.get('is_st') is None else ('��' if r['is_st'] else '��')] for r in band[:10]],
            'note':'�۸�Χ��ɽ���Ϊ�����Թ۲죻�����ʸ��д����顣'})
    endpoints=validation.get('endpoints') or {}
    if endpoints:
        t=validation.get('split',{}).get('test',{})
        sections.append({'title':'��ʷʱ������֤','columns':['�۸��¼�','������','Top10�¼���','ģ��Brier','��׼Brier'],
            'rows':[[label,number(m.get('n'),0),pct(m.get('top10_event_frequency')),number(m.get('brier'),5),
                     number(m.get('calibration_block_constant_baseline_brier'),5)] for label,m in endpoints.items()],
            'note':f"���Ը��� {t.get('independent_dates','��')} �����ڣ�{t.get('first_input_date','��')}��{t.get('last_input_date','��')}����BrierԽ��Խ�ã���׼ΪУ׼�ڳ������ʡ��۸��¼��ʲ��ܴ���ɽ�ʤ�ʣ���ǰ�����۷�ë����ھ������о���"})
    if stage=='evening':
        us=payload.get('us_overnight_linkage') or {}
        source='��������' if us.get('source')=='cached' else '��������'
        def market_time(value):
            try: return dt.datetime.fromisoformat(value).strftime('%m-%d %H:%M')
            except (TypeError,ValueError): return 'δ֪'
        sections.append({'title':'�����г�����','columns':['ָ�������','���¼۸�','���ǵ�','����ʱ��'],
            'rows':[[r.get('label') or r.get('symbol') or '��',number(r.get('last')),pct(r.get('change_1d')),market_time(r.get('quoted_at'))] for r in us.get('quotes',[])[:5]],
            'note':(source+'������ʱ��Ϊ����ʱ�䡣���������������뵱ǰ����ģ�͡�') if us.get('quotes') else '����δȡ�ÿ��ú������飬ģ�ͼ���Ԥ���ѵ������㡣'})
    else:
        news=payload.get('public_news_until_0925') if stage=='auction' else payload.get('public_news_until_0900')
        news=news or []
        def news_time(row):
            try: return dt.datetime.fromisoformat(row.get('published_at') or row.get('time')).timestamp()
            except (AttributeError,TypeError,ValueError): return float('-inf')
        news=sorted(news,key=news_time,reverse=True)
        sections.append({'title':'����⹫����Ϣ','columns':['����ʱ��','��Ϣ'],
            'rows':[[r.get('published_at') or r.get('time') or '��',r.get('title') or '��'] for r in news[:5] if isinstance(r,dict)],
            'note':f"������ʱ�������ʱ��˫�ؽض��� {payload.get('information_cutoff','��')}�������뵱ǰ����ģ�͡�" if news else '����û�з��Ͻ�ֹʱ����������Ϣ���������г�û����Ϣ��'})
    links=[]
    for label,tag in [('����汾',payload.get('snapshot_release')),('ģ�Ͱ汾',research.get('model_release'))]:
        if tag: links.append((label,REPO_URL+'/releases/tag/'+quote(str(tag),safe='')))
    if validation.get('github_run_id'):
        links.append(('���м�¼',REPO_URL+'/actions/runs/'+quote(str(validation['github_run_id']),safe='')))
    return {'title':'A��'+title,'subtitle':subtitle,'subject':subject,'summary':summary,'stats':stats,
            'warnings':warnings,'sections':sections,'links':links,'ready':ready,
            'generated_at':payload.get('generated_at') or '��', 'cutoff':payload.get('information_cutoff') or '��',
            'footer':'�о��۲죬���Զ��µ�����ǰ�о������۷�ë����ͳ�ƣ�80%Ŀ����3%��6%����Ŀ����δ��֤�����ۺ�ģ�͸��ʲ��ܵ����ɽ���֤��'}


def render_text(payload):
    c=content(payload)
    lines=[c['title'],c['subtitle'],'',c['summary'],'']
    lines += [key+'��'+value for key,value in c['stats']]
    lines += ['']+[value for value in c['warnings']]+['']
    for section in c['sections']:
        if 'authoritative_text' in section:
            lines += [section['authoritative_text'], section['note'], '']
            continue
        lines += [section['title'],'']
        if section['rows']:
            lines += [' | '.join(section['columns'])]+[' | '.join(row) for row in section['rows']]
        lines += [section['note'],'']
    lines += ['��Ϣ��ֹ��'+c['cutoff'],'����ʱ�䣺'+c['generated_at'],c['footer'],'']
    lines += [label+'��'+url for label,url in c['links']]
    return '\n'.join(lines)+'\n'


def render_html(payload):
    c=content(payload); esc=lambda v:html.escape(str(v),quote=True)
    # The wide active tables scroll inside their own container.  A fixed mail
    # frame prevents their intrinsic minimum width from widening the phone page.
    frame_style='width:100%;max-width:680px;background:#ffffff;border-collapse:collapse'
    if any('authoritative_html' in section for section in c['sections']):
        frame_style += ';table-layout:fixed'
    # Table layout and inline CSS work in Outlook; media queries improve phone layout.
    chunks=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<style>@media(max-width:600px){.pad{padding:20px 12px!important}.data td,.data th{padding:9px 4px!important;font-size:11px!important}.heading{font-size:25px!important}}</style>',
        '</head><body style="margin:0;background:#edf2f7;color:#1c2b3d;font-family:Arial,Microsoft YaHei,PingFang SC,sans-serif">',
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 8px">',
        f'<table role="presentation" width="680" cellspacing="0" cellpadding="0" style="{frame_style}">',
        '<tr><td class="pad" style="background:#102b46;padding:32px;color:#ffffff">',
        '<div style="font-size:11px;letter-spacing:2px;color:#81d5d0">A-SHARE RESEARCH</div>',
        f'<h1 class="heading" style="font-size:32px;line-height:1.3;margin:12px 0 8px">{esc(c["title"])}</h1>',
        f'<div style="font-size:14px;color:#d3deeb">{esc(c["subtitle"])}</div></td></tr>',
        '<tr><td class="pad" style="padding:28px 28px 12px">',
        f'<p style="margin:0 0 20px;font-size:15px;line-height:1.8">{esc(c["summary"])}</p>',
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border:1px solid #e2e8f0">']
    for i in range(0,len(c['stats']),2):
        chunks.append('<tr>')
        for label,value in c['stats'][i:i+2]:
            chunks.append(f'<td width="50%" style="padding:12px 14px;border-bottom:1px solid #e2e8f0;background:#f6f9fc"><div style="font-size:11px;color:#63758b">{esc(label)}</div><div style="font-size:16px;font-weight:bold;margin-top:5px">{esc(value)}</div></td>')
        chunks.append('</tr>')
    chunks.append('</table>')
    if c['warnings']:
        chunks.append('<div style="margin-top:18px;background:#fff5df;border-left:3px solid #d9a13f;padding:12px 14px;font-size:12px;color:#795723;line-height:1.8">')
        chunks += [f'<div>{esc(value)}</div>' for value in c['warnings']]
        chunks.append('</div>')
    chunks.append('</td></tr>')
    for section in c['sections']:
        if 'authoritative_html' in section:
            chunks += ['<tr><td class="pad" style="padding:0 16px">',
                section['authoritative_html'],
                f'<p style="margin:0 12px 16px;font-size:11px;color:#6c7c90;line-height:1.8">{esc(section["note"])}</p>',
                '</td></tr>']
            continue
        chunks += ['<tr><td class="pad" style="padding:16px 28px">',
            f'<h2 style="margin:0 0 12px;font-size:18px;color:#102b46">{esc(section["title"])}</h2>']
        if section['rows']:
            chunks.append('<table class="data" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>')
            chunks += [f'<th scope="col" style="text-align:left;padding:10px 7px;background:#edf3f8;color:#5b6f83;font-weight:normal;border-bottom:1px solid #d8e2eb">{esc(v)}</th>' for v in section['columns']]
            chunks.append('</tr></thead><tbody>')
            for i,row in enumerate(section['rows']):
                background='#f0fbf9' if section['title']=='ģ�͹۲� Top10' and i<3 else ('#fafcff' if i%2 else '#ffffff')
                chunks.append(f'<tr style="background:{background}">')
                chunks += [f'<td style="padding:11px 7px;line-height:1.5;border-bottom:1px solid #e7edf3;word-break:break-word">{esc(v)}</td>' for v in row]
                chunks.append('</tr>')
            chunks.append('</tbody></table>')
        chunks += [f'<p style="margin:10px 0 0;font-size:11px;color:#6c7c90;line-height:1.8">{esc(section["note"])}</p>','</td></tr>']
    chunks += ['<tr><td class="pad" style="padding:22px 28px;background:#f6f9fc;color:#728399;font-size:11px;line-height:1.8">',
        f'<div>��Ϣ��ֹ {esc(c["cutoff"])}<br>����ʱ�� {esc(c["generated_at"])}</div>',
        f'<p style="margin:10px 0">{esc(c["footer"])}</p>']
    chunks += [f'<a href="{esc(url)}" style="color:#27776f;text-decoration:none;margin-right:14px">{esc(label)}</a>' for label,url in c['links']]
    chunks.append('</td></tr></table></td></tr></table></body></html>')
    return ''.join(chunks)
