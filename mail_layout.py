"""Concise research email content, with matching plain text and safe inline HTML."""
import html
import math
import datetime as dt
from urllib.parse import quote

VERSION = 'research-email-v4-gross-swing-target'
STAGES = {'evening':('晚报','18:00 盘后'), 'morning':('晨报','09:00 盘前'), 'auction':('09:25观察','竞价时段')}
REPO_URL = 'https://github.com/cluuacc-gif/ashare-research'


def number(value, digits=2):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '—'
    return f'{value:,.{digits}f}'


def pct(value):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '—'
    return f'{value:.1%}'


def money(value):
    if not isinstance(value,(int,float)) or not math.isfinite(value): return '—'
    if abs(value)>=100000000: return f'{value/100000000:.2f}亿'
    return f'{value/10000:,.0f}万'


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
    mode='预览' if payload.get('preview') else ('迟到回顾' if payload.get('late') else clock)
    subtitle=f"{payload['target_date']} · {mode}"
    subject=f"A股{title}｜{payload['target_date']}｜{'预览' if payload.get('preview') else ('迟到回顾' if payload.get('late') else '模型观察')}"
    band=research.get('price_band_5_10') or []
    amounts={r['symbol']:r.get('amount') for r in band}
    rows=[]
    for rank,r in enumerate(predictions.get('rows',[])[:10],1):
        rows.append([str(r.get('rank',rank)),r['symbol'],r.get('name') or '—',number(r.get('close')),
                     pct(r.get('P1')),pct(r.get('P2')),money(amounts.get(r['symbol']))])
    ready=bool(research.get('model_ready') and rows)
    summary=f"基于 {base or '—'} 收盘价量，观察 {target or '—'} 收盘价格事件；模型覆盖 {predictions.get('prediction_count',0):,} 只。"
    warnings=[]
    if not ready: warnings.append('模型预测尚未就绪，本期只展示已入库行情观察。')
    if not research.get('formal_trade_ready'): warnings.append('交易资格待核验：官方ST／停牌状态、复权、限价及风险事件数据仍有缺口。')
    if stage=='auction' and not payload.get('auction_verified'):
        warnings.append('本期09:25观察沿用前收盘模型；当前竞价输入未核验，暂不提供竞价挂单价格。')
    if payload.get('late') and not payload.get('preview'):
        warnings.append('本期为迟到回顾，不计入事前预测成绩。')
    stats=[('预测交易日',str(target or '—')),('基础行情日',str(base or '—')),
           ('当日报价',number(inventory.get('base_quotes'),0)+'只'),
           ('模型状态','训练与预测已就绪' if ready else '等待模型'),
           ('交易资格','已通过' if research.get('formal_trade_ready') else '待核验'),
           ('5～10元观察',f'{len(band):,}只')]
    sections=[]
    gross = research.get('gross_swing_strategy') or {}
    sections.append({'title':'隔日毛收益策略', 'columns':['验收项目','当前状态'],
        'rows':[['目标','毛盈利胜率严格高于80%，争取每笔3%～6%'],
                ['统计口径','不扣交易费用；盈利比例、赚到3%和6%的比例分别统计'],
                ['买入思路','优先普通走势和可买入机会；高开超限取消，不追封死涨停'],
                ['实际成交胜率','尚无通过审核的真实成交样本'],
                ['正式交易候选','空仓，等待前向成交与数据证据'],
                ['新策略研究','已生成研究版本' if gross.get('schema') == 'gross-swing-research-v2' else '等待与本期行情匹配的研究结果']],
        'note':'第1天买入、第2天按预定目标或退出规则研究卖出，不要求买入当天上涨。至少200笔、60个入场日期，且日期分组置信下界高于80%、毛期望为正，才进入目标验收。全部入场交易与未解决退出保留；日线触价不代表实际成交。'})
    diagnostic = gross.get('diagnostic') or {}
    shown = diagnostic.get('selected_policy' if (gross.get('policy') or {}).get('active') else 'fixed_reference') or {}
    if shown:
        sections.append({'title':'本期隔日策略历史诊断', 'columns':['入场尝试','已闭合','未解决','毛盈利比例','赚到3%','赚到6%'],
            'rows':[[number(shown.get('n_attempts'),0),number(shown.get('n_closed'),0),number(shown.get('n_unresolved'),0),
                     pct(shown.get('win_rate')),pct(shown.get('ge3_rate')),pct(shown.get('ge6_rate'))]],
            'note':'旧历史已用于开发，本表是研究诊断。全部已入场交易均计入，不按买入后的涨跌删除亏损；达到目标价仍须核验能否成交。'})
    if rows:
        sections.append({'title':'模型观察 Top10','columns':['排名','代码','名称','收盘','P1 上涨','P2 ≥5%','成交额'],
            'rows':rows,'note':'按P2排序。P1＝下一交易日收盘高于基础日收盘；P2＝收盘涨幅≥5%。两者均为原始未复权价格事件概率，不等于买入后的毛盈利胜率或赚到3%的比例。'})
    elif band:
        sections.append({'title':'成交额前排观察','columns':['代码','名称','收盘','成交额','ST核验'],
            'rows':[[r['symbol'],r.get('name') or '—',number(r.get('close')),money(r.get('amount')),
                     '未知' if r.get('is_st') is None else ('是' if r['is_st'] else '否')] for r in band[:10]],
            'note':'价格范围与成交额为描述性观察；交易资格尚待核验。'})
    endpoints=validation.get('endpoints') or {}
    if endpoints:
        t=validation.get('split',{}).get('test',{})
        sections.append({'title':'历史时间外验证','columns':['价格事件','样本数','Top10事件率','模型Brier','基准Brier'],
            'rows':[[label,number(m.get('n'),0),pct(m.get('top10_event_frequency')),number(m.get('brier'),5),
                     number(m.get('calibration_block_constant_baseline_brier'),5)] for label,m in endpoints.items()],
            'note':f"测试覆盖 {t.get('independent_dates','—')} 个日期（{t.get('first_input_date','—')}至{t.get('last_input_date','—')}）。Brier越低越好；基准为校准期常数概率。价格事件率不能代替成交胜率；当前按不扣费毛收益口径单独研究。"})
    if stage=='evening':
        us=payload.get('us_overnight_linkage') or {}
        source='缓存行情' if us.get('source')=='cached' else '公开行情'
        def market_time(value):
            try: return dt.datetime.fromisoformat(value).strftime('%m-%d %H:%M')
            except (TypeError,ValueError): return '未知'
        sections.append({'title':'海外市场背景','columns':['指数／标的','最新价格','日涨跌','报价时间'],
            'rows':[[r.get('label') or r.get('symbol') or '—',number(r.get('last')),pct(r.get('change_1d')),market_time(r.get('quoted_at'))] for r in us.get('quotes',[])[:5]],
            'note':(source+'；报价时间为北京时间。仅作背景，不进入当前价量模型。') if us.get('quotes') else '本次未取得可用海外行情，模型价量预测已单独计算。'})
    else:
        news=payload.get('public_news_until_0925') if stage=='auction' else payload.get('public_news_until_0900')
        news=news or []
        def news_time(row):
            try: return dt.datetime.fromisoformat(row.get('published_at') or row.get('time')).timestamp()
            except (AttributeError,TypeError,ValueError): return float('-inf')
        news=sorted(news,key=news_time,reverse=True)
        sections.append({'title':'已入库公开消息','columns':['发布时间','消息'],
            'rows':[[r.get('published_at') or r.get('time') or '—',r.get('title') or '—'] for r in news[:5] if isinstance(r,dict)],
            'note':f"按发布时间与入库时间双重截断至 {payload.get('information_cutoff','—')}；不进入当前价量模型。" if news else '本期没有符合截止时间的已入库消息，不代表市场没有消息。'})
    links=[]
    for label,tag in [('行情版本',payload.get('snapshot_release')),('模型版本',research.get('model_release'))]:
        if tag: links.append((label,REPO_URL+'/releases/tag/'+quote(str(tag),safe='')))
    if validation.get('github_run_id'):
        links.append(('运行记录',REPO_URL+'/actions/runs/'+quote(str(validation['github_run_id']),safe='')))
    return {'title':'A股'+title,'subtitle':subtitle,'subject':subject,'summary':summary,'stats':stats,
            'warnings':warnings,'sections':sections,'links':links,'ready':ready,
            'generated_at':payload.get('generated_at') or '—', 'cutoff':payload.get('information_cutoff') or '—',
            'footer':'研究观察，不自动下单。当前研究按不扣费毛收益统计，80%目标与3%～6%收益目标尚未验证；触价和模型概率不能当作成交保证。'}


def render_text(payload):
    c=content(payload)
    lines=[c['title'],c['subtitle'],'',c['summary'],'']
    lines += [key+'：'+value for key,value in c['stats']]
    lines += ['']+[value for value in c['warnings']]+['']
    for section in c['sections']:
        lines += [section['title'],'']
        if section['rows']:
            lines += [' | '.join(section['columns'])]+[' | '.join(row) for row in section['rows']]
        lines += [section['note'],'']
    lines += ['信息截止：'+c['cutoff'],'生成时间：'+c['generated_at'],c['footer'],'']
    lines += [label+'：'+url for label,url in c['links']]
    return '\n'.join(lines)+'\n'


def render_html(payload):
    c=content(payload); esc=lambda v:html.escape(str(v),quote=True)
    # Table layout and inline CSS work in Outlook; media queries improve phone layout.
    chunks=['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<style>@media(max-width:600px){.pad{padding:20px 12px!important}.data td,.data th{padding:9px 4px!important;font-size:11px!important}.heading{font-size:25px!important}}</style>',
        '</head><body style="margin:0;background:#edf2f7;color:#1c2b3d;font-family:Arial,Microsoft YaHei,PingFang SC,sans-serif">',
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 8px">',
        '<table role="presentation" width="680" cellspacing="0" cellpadding="0" style="width:100%;max-width:680px;background:#ffffff;border-collapse:collapse">',
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
        chunks += ['<tr><td class="pad" style="padding:16px 28px">',
            f'<h2 style="margin:0 0 12px;font-size:18px;color:#102b46">{esc(section["title"])}</h2>']
        if section['rows']:
            chunks.append('<table class="data" width="100%" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr>')
            chunks += [f'<th scope="col" style="text-align:left;padding:10px 7px;background:#edf3f8;color:#5b6f83;font-weight:normal;border-bottom:1px solid #d8e2eb">{esc(v)}</th>' for v in section['columns']]
            chunks.append('</tr></thead><tbody>')
            for i,row in enumerate(section['rows']):
                background='#f0fbf9' if section['title']=='模型观察 Top10' and i<3 else ('#fafcff' if i%2 else '#ffffff')
                chunks.append(f'<tr style="background:{background}">')
                chunks += [f'<td style="padding:11px 7px;line-height:1.5;border-bottom:1px solid #e7edf3;word-break:break-word">{esc(v)}</td>' for v in row]
                chunks.append('</tr>')
            chunks.append('</tbody></table>')
        chunks += [f'<p style="margin:10px 0 0;font-size:11px;color:#6c7c90;line-height:1.8">{esc(section["note"])}</p>','</td></tr>']
    chunks += ['<tr><td class="pad" style="padding:22px 28px;background:#f6f9fc;color:#728399;font-size:11px;line-height:1.8">',
        f'<div>信息截止 {esc(c["cutoff"])}<br>生成时间 {esc(c["generated_at"])}</div>',
        f'<p style="margin:10px 0">{esc(c["footer"])}</p>']
    chunks += [f'<a href="{esc(url)}" style="color:#27776f;text-decoration:none;margin-right:14px">{esc(label)}</a>' for label,url in c['links']]
    chunks.append('</td></tr></table></td></tr></table></body></html>')
    return ''.join(chunks)
