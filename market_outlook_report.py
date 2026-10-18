"""Standalone, offline Chinese sector outlook and conditional opening watchlist."""
from __future__ import annotations
import html
import math
from datetime import datetime, timedelta, timezone


def e(value):
    return html.escape(str(value), quote=True)


def num(value, digits=1):
    return f'{value:,.{digits}f}' if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else '—'


def pct(value):
    return num(value*100,2)+'%' if isinstance(value,(int,float)) and math.isfinite(value) else '—'


def local_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M 北京时间')
    except (TypeError,ValueError):
        return '—'


def items(values):
    if isinstance(values,str):
        return e(values)
    return '；'.join(e(v) for v in (values or [])) or '—'


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
        return num(number/1e8,2)+'亿' if isinstance(number,(int,float)) else '—'
    for row in data[:8]:
        rows.append(['<strong>'+e(row.get('name','—'))+'</strong>',yi(row.get('main_net_inflow_yuan')),
                     num(row.get('main_net_ratio_pct'),2)+'%',yi(row.get('main_net_3d_yuan')),
                     yi(row.get('main_net_5d_yuan')),e(phases.get(row.get('code')) or '待核对同源成分走势')])
    if not rows:
        rows=[['本期缺少及时、匹配的真实资金流','—','—','—','—','不使用成交量或涨幅冒充资金流']]
    board_rows=[]
    for board in selection.get('sector_board',[]):
        if board.get('eligible_for_stock_selection') is not True:continue
        board_rows.append([e(board.get('sector','—')),e(board.get('phase_label','—')),num(board.get('score')),
                           '资金＋走势共振' if board.get('flow_integrated') else '仅走势证据',
                           num(board.get('member_count'),0)+'只'])
        if len(board_rows)>=6:break
    coverage=flow.get('coverage') or {}
    result='<section><h2>板块资金流与潜在转强</h2><p>先看真实主力净额，再核对多日变化与板块扩散。今日回流、持续流入和价格早期转强分别观察，净流入不自动等于明日上涨。</p>'
    result+=table(['板块','今日净额','净占比','3日净额','5日净额','结构阶段'],rows)
    result+='<p class="note">资金日期 '+e(flow.get('base_trade_date') or value.get('base_trade_date','—'))+'；本期可用'+num(coverage.get('valid_sector_count'),0)+'个行业。资金是东方财富主力净额统计，可能含B股；股票观察只筛A股。它不是机构账户持仓，也不是全市场现金净流入。<a href="https://data.eastmoney.com/bkzj/hy.html">查看供应商口径</a>。</p>'
    if board_rows:
        result+='<h2 style="margin-top:24px">增强板块中的选股范围</h2>'+table(['板块','阶段','共振分','证据','匹配成分'],board_rows)
    result+='<p class="note">资金行业与价格行业只有通过同源成分证券代码核对才合并。没有匹配行业、资金证据或价格行为的股票，不补入主要观察名单。早期转强属于待验证假设，不能提前知道将要上涨。</p></section>'
    return result


def mathematical_section(value):
    study=value.get('price_action_research') or {}
    diagnostic=study.get('diagnostic') or {}
    labels={'unfiltered_price_action_ranking_baseline':'同条件价量排序基准',
            'selected_policy':'选择期确定的研究规则','fixed_compression_breakout':'收缩后突破',
            'fixed_trend_pullback':'趋势内缩量回踩','fixed_higher_lows':'高低点抬升'}
    rows=[]
    for name,label in labels.items():
        metric=diagnostic.get(name) or {}
        if not metric.get('n_attempts'):continue
        rows.append([label,num(metric.get('n_attempts'),0)+'／'+num(metric.get('n_unresolved'),0),num(metric.get('independent_entry_dates'),0),
                     pct(metric.get('win_rate')),pct(metric.get('ge3_rate')),
                     pct(metric.get('mean_closed_gross_return')),pct(metric.get('strength3_rate'))])
    result='<section><h2>数学与价格行为检验</h2><p>把突破、回踩和高低点抬升写成可复算规则：ATR衡量波动，趋势效率衡量单向移动占比，收盘位置与上影线区分承接和回落。它们用于建立假设，历史检验负责判断是否值得继续。</p>'
    if rows:
        result+=table(['研究规则','入场／未解决','日期数','毛盈利比例','赚到3%','平均已闭合收益','连续3日收高'],rows)
    else:
        result+='<p class="muted">本期尚无与行情版本匹配的价格行为诊断，保留空缺。</p>'
    policy=study.get('policy') or {}
    status='存在通过选择期条件的研究规则，正式交易仍未准入。' if policy.get('active') else '本期没有启用通过验证的价格行为交易规则。'
    result+='<p class="note">'+e(status)+' 本表是原5～10元池的历史对照；新版5～100元预算研究池及资金共振需另作前向检验。旧历史已用于开发，本表不是新增样本外验证；日线开盘和触价是参考成交情景，无法证明个人订单成交。历史诊断没有资金流或当时行业成分证据，不能当作整套板块资金策略的胜率。</p>'
    risks=[]
    for name,label in labels.items():
        metric=diagnostic.get(name) or {}
        if not metric.get('n_attempts'):continue
        ci=(metric.get('date_block_bootstrap') or {}).get('win_rate_ci95') or []
        risks.append([label,pct(metric.get('mean_win_return')),pct(metric.get('mean_loss_return')),num(metric.get('profit_factor'),2),(pct(ci[0])+'～'+pct(ci[1])) if len(ci)==2 else '—',pct(metric.get('realized_closed_trade_max_drawdown'))])
    if risks:
        result+='<details><summary>各规则的盈亏、区间与已闭合风险</summary>'+table(['规则','平均盈利','平均亏损','盈利因子','盈利比例95%区间','已闭合回撤'],risks)+'<p class="note">按入场日期分组抽样；区间反映旧诊断样本的不确定性，不能证明未来有效。回撤未包含未知头寸盯市，不等于完整实盘最大回撤。</p></details>'
    result+='<p><b>T+1主目标：</b>D1买入、D2退出，默认研究3%目标与2%风险参考；D1不止损卖出，D2跳空可能越过风险价。6%单列观察，不能为了等第3天上涨而自动延长持仓。</p><p><b>持续走强辅助目标：</b>分别跟踪D1、D2、D3逐日收高；缺少D3行情不删除已知的D2结果，也不把3日上涨率当成T+1盈利胜率。</p>'
    result+='<p class="note">期望收益＝盈利比例×平均盈利＋亏损比例×平均亏损。80%胜率仍可能因少数大亏而亏钱，因此同时检查盈亏比、正期望和按日期分组的不确定区间。海外资料仅提供可量化的研究思路：<a href="https://www.nber.org/papers/w7613">Lo、Mamaysky、Wang（2000）</a>，<a href="https://www.cmegroup.com/education/courses/trading-and-analysis/support-and-resistance">CME支撑与阻力课程</a>；不据此推定A股短线有效。</p></section>'
    return result


def selection_funnel(value):
    selection=value.get('sector_stock_selection') or {}
    coverage=selection.get('coverage') or {}
    result='<p class="note">预算研究池为5～100元，每个名额最多1万元、100股整数手；价格上限与数量提前冻结。筛选过程：'+num(coverage.get('opening_candidate_pool'),0)+'只价量候选 → '+num(coverage.get('within_sector_candidates'),0)+'只匹配增强板块 → '+num(coverage.get('primary_count'),0)+'只主要观察、'+num(coverage.get('reserve_count'),0)+'只储备观察。原5～10元池保留作历史对照；扩大价格范围不放宽资金和价格行为要求。</p>'
    names={'price_action_quality_below_threshold':'价格行为评分不足','no_predefined_price_action_structure':'未出现预定价格行为结构','candidate_history_or_features_unverified':'历史或输入未核验'}
    rows=[]
    for row in selection.get('rejected_candidates',[])[:10]:
        reason=row.get('reason') or '尚未同时满足条件'
        rows.append([e(row.get('symbol','—')),e(row.get('name','—')),e(row.get('sector','—')),e(names.get(reason,'尚未同时满足预定条件'))])
    if rows:result+='<details><summary>查看已排除的板块内候选</summary>'+table(['代码','名称','板块','首个排除原因'],rows)+'</details>'
    return result


def render(value):
    from probability_report import render as probability_section
    from pa_trade_report import render as pa_trade_section
    sectors=value.get('sector_outlook') or {}
    plan=value.get('opening_strength_plan') or {}
    rankings=sectors.get('rankings') or []
    watch=plan.get('watchlist') or []
    sector_rows=[]
    for i,row in enumerate(rankings[:8],1):
        sector_rows.append([str(i),'<strong>'+e(row.get('industry') or row.get('sector') or row.get('name') or '—')+'</strong>',
                            num(row.get('score'),1),items(row.get('drivers') or row.get('reasons')),
                            items(row.get('risks') or row.get('invalidation'))])
    if not sector_rows:
        sector_rows=[['—','本期没有足够的可用行业输入','—',e(sectors.get('reason') or sectors.get('status') or '等待数据'),'保留空缺，不用旧名单替代']]
    watch_rows=[]
    for i,row in enumerate(watch,1):
        label=e(row.get('name') or row.get('symbol') or '—')+('（仅观察）' if row.get('is_reserve_only') else '')
        conditions=row.get('entry_conditions') or []
        cancels=row.get('cancel_conditions') or []
        rationale=row.get('rationale') or row.get('reasons') or row.get('drivers') or []
        description=items(rationale[:2] if isinstance(rationale,list) else rationale)
        if conditions: description+='<br><span class="muted">确认：'+items(conditions[1:3])+'</span>'
        if cancels: description+='<br><span class="muted">撤销：'+items(cancels[:1])+'</span>'
        watch_rows.append([str(i),'<strong>'+label+'</strong><br><span class="muted">'+e(row.get('symbol',''))+'</span>',
                           '<span style="white-space:nowrap">'+e(row.get('industry') or row.get('sector') or '未匹配')+'</span>',num(row.get('close') or row.get('base_close'),2),
                           num(row.get('research_score',row.get('score')),1),description])
    if not watch_rows:
        watch_rows=[['—','本期保留空名单','—','—','—','尚未同时满足增强板块、资金和价格行为条件']]
    coverage=sectors.get('coverage') or {}
    coverage_text=' · '.join(e(str(k))+': '+e(str(v)) for k,v in coverage.items() if not isinstance(v,(list,dict)))
    source_urls=[]
    sources=sectors.get('sources') or {}
    source_items=sources.get('membership_urls',[]) if isinstance(sources,dict) else sources
    for item in source_items:
        url=item.get('url') if isinstance(item,dict) else item
        if isinstance(url,str) and url.startswith(('https://','http://')):
            source_urls.append('<a href="'+e(url)+'">'+e(item.get('label') or item.get('name') or url)+'</a>' if isinstance(item,dict) else '<a href="'+e(url)+'">行业来源</a>')
    tracking=value.get('sector_forward_tracking') or {}
    tracking_text=('已完成'+str(tracking.get('resolved_dates',0))+'个预测交易日的前向核对；'
        +'相对走强命中率'+pct(tracking.get('relative_strength_hit_rate'))
        +'，绝对上涨比例'+pct(tracking.get('absolute_up_rate'))+'。')
    evidence=' / '.join(source_urls[:3]) or '来源记录保存在本期冻结的行情与行业资料中。'
    plan_note=items(plan.get('entry_rules') or plan.get('conditions') or plan.get('notes') or [])
    if plan_note=='—':
        plan_note='最多3个独立预算名额；第4—5名只观察，不自动补位。板块缺少匹配时，个股排序仅采用价量特征。'
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>次日板块展望与开盘观察</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f2;color:#172722;font:16px/1.7 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}main{max-width:1180px;margin:auto;padding:42px 34px 70px}.eyebrow{font-size:13px;letter-spacing:2px;color:#537367}h1{font-size:36px;line-height:1.3;margin:12px 0}h2{font-size:23px;margin:0 0 12px}p{margin:8px 0 16px}.lead{max-width:910px;font-size:18px}.muted,small{color:#66766e}.ribbon{display:flex;gap:18px;margin:26px 0}.tile{background:#fff;border:1px solid #dce3dc;border-radius:12px;padding:18px 22px;flex:1}.tile b{display:block;font-size:27px}.tile span{font-size:13px;color:#66766e}section{background:white;border:1px solid #dde4dc;border-radius:14px;margin-top:22px;padding:25px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}th{text-align:left;background:#edf2ee;color:#486150;font-size:13px}th,td{padding:13px 12px;vertical-align:top;border-bottom:1px solid #e7ece6}th:first-child{width:46px}td:nth-child(2){min-width:125px}a{color:#216f55}.callout{border-left:4px solid #a07636;background:#fbf6ec;padding:15px 19px;margin-top:20px}.steps{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.step{background:#f5f7f2;padding:17px;border-radius:9px}.step b{display:block;color:#28664b}.note{font-size:13px;color:#65736b;margin-top:15px}.pill{font-size:12px;background:#e9eee7;padding:4px 9px;border-radius:20px}.provenance{font-size:12px;overflow-wrap:anywhere}details{margin-top:15px}summary{cursor:pointer;font-weight:bold}@media(max-width:650px){main{padding:25px 15px}h1{font-size:27px}.ribbon{gap:9px}.tile{padding:13px 10px}.tile b{font-size:21px}section{padding:18px 13px}.steps{grid-template-columns:1fr}table{min-width:720px}.lead{font-size:16px}}
</style><style>th{white-space:nowrap}td:first-child{white-space:nowrap}</style></head><body><main><div class="eyebrow">A股研究 · 下一交易日观察</div><h1>先看板块，再等开盘确认</h1>
<p class="lead">优先观察有相对强度、上涨家数和成交参与支持的板块，再从可成交的普通走势股票中寻找转强机会。以下是待验证的研究排序，当前没有已确认的买入信号。</p>
<p class="muted">目标交易日 <b>'''+e(value.get('target_trade_date','—'))+'''</b>　｜　行情截至 '''+e(value.get('base_trade_date','—'))+'''收盘　｜　研究冻结于 '''+e(local_time(value.get('information_cutoff')))+'''</p>
<div class="ribbon"><div class="tile"><span>真实资金流行业</span><b>'''+str(len((value.get('fund_flow') or {}).get('sectors',[])))+'''</b></div><div class="tile"><span>技术板块覆盖</span><b>'''+str(len(rankings))+'''</b></div><div class="tile"><span>开盘条件观察股票</span><b>'''+str(len(watch))+'''</b></div><div class="tile"><span>真实成交胜率</span><b>未验证</b></div></div>
'''+pa_trade_section(value.get('pa_trade_research') or {})+probability_section(value.get('probability_research') or {})+flow_section(value)+'''<section><h2>次日板块强弱展望</h2><p>目标是找出下一交易日更可能相对走强的板块；相对走强时仍可能下跌。分数反映本期研究排序，不是上涨概率。</p>'''+table(['排名','板块','研究分','支持因素','失效与风险'],sector_rows)+'''
<p class="note">每天下午的报告新增本节，保留数据不足时的空缺。'''+e(sectors.get('methodology_note') or sectors.get('score_definition') or '')+'''</p></section>
<section><h2>开盘转强观察名单</h2><p>不要求买入当天收涨，也不以已经封死涨停的股票作为可买入机会。名单在开盘前冻结，当日需再核对价格、交易状态和及时行情。</p>'''+selection_funnel(value)+table(['序号','股票','行业','前收盘','研究分','依据与开盘条件'],watch_rows)+'''
<div class="callout"><strong>开盘前是观察计划。</strong> 09:25报告提供条件清单；实际开盘后，只有及时行情、交易状态和限价等核验齐全，才可研究是否满足转强条件。触发条件不等于实际成交。</div>
<p class="note">'''+plan_note+'''</p></section>
'''+mathematical_section(value)+'''<section><h2>买入与次日退出的研究顺序</h2><div class="steps"><div class="step"><b>① 前收盘准备</b>冻结板块和个股观察池，价格上限提前确定；高开超过上限即放弃。</div><div class="step"><b>② 开盘核验</b>核对当天行情和可交易状态。无及时数据、封板或条件不足时不确认信号。</div><div class="step"><b>③ 次日检验</b>按预定退出规则记录毛盈利、达到3%和6%的比例；亏损和未解决退出全部保留。</div></div>
<p class="note">目标仍是提高第1天买入、第2天卖出的毛盈利胜率，争取3%～6%，不扣交易费用。现有策略尚未验证80%以上胜率；本次新增信息不会自动改变这一结论。</p></section>
<section><h2>证据范围与自动运行</h2><p>板块预测与开盘计划已纳入报告准备流程。每天盘后冻结研究输出，结果按交易日留档；下一交易日结果出现前不计算命中率。</p><p>'''+e(tracking_text)+''' 缺失行情的预测保留为未解决，不计作命中；每天只以最早冻结的一版统计。</p><p>邮件时点继续沿用北京时间09:00、09:25、18:00。独立云主机尚未部署；GitHub调度仍可能排队，严格准点发送依赖此前准备的独立运行方案。</p>
<details><summary>数据与来源</summary><p class="note">'''+coverage_text+'''</p><p class="note">'''+evidence+'''</p><p class="provenance">行情版本：<a href="https://github.com/cluuacc-gif/ashare-research/releases/tag/'''+e(value.get('snapshot_release',''))+'''">'''+e(value.get('snapshot_release','—'))+'''</a><br>行情校验值：'''+e(value.get('input_database_sha256','—'))+'''</p><p class="note">行业分类仅用于其已知时间之后的研究，不回填历史成分来制造回测命中率。当前排序不包含已验证的消息催化或资金流预测。</p><p class="note">交易时段与规则参考：<a href="https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20260424_10817739.shtml">上交所2026年交易规则</a>。本次研究不发送邮件或交易指令。</p></details></section>
<p class="note">本页为选股研究与观察计划。冻结时间与行情日期分别列示，周末生成的预览不计作上一交易日下午的事前预测。</p></main></body></html>'''
