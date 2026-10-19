"""Bounded Chinese presentation of prospective matched T+1 observations."""
import html
import math

VERSION = 'nextday-forward-research-v1'
ARMS = ('base_0940', 'base_1440', 'confluence_0940', 'confluence_1440',
        'confluence_opening_retest', 'confluence_late_confirmation')
NAMES = {'base_0940': '原池·早盘对照', 'base_1440': '原池·尾盘对照',
         'confluence_0940': '共振子集·早盘对照', 'confluence_1440': '共振子集·尾盘对照',
         'confluence_opening_retest': '共振子集·早盘突破回测',
         'confluence_late_confirmation': '共振子集·尾盘确认'}
METRICS = ('scheduled_observations', 'reference_closed', 'distinct_entry_dates',
    'unknown', 'pending', 'excluded_filter', 'no_signal', 'price_outside_bounds',
    'reference_gross_profit_rate', 'reference_ge3_rate', 'reference_ge6_rate',
    'mean_gross_return', 'worst_gross_return',
    'reference_profit_fraction_including_unknown_as_not_won',
    'mean_D1_after_buy_return', 'mean_overnight_return', 'mean_D2_after_open_return',
    'coverage_fraction_closed', 'average_winning_return', 'average_nonwinning_return')
REASONS = {
    'base_breadth_unknown': '市场上涨家数比例未知',
    'base_return_1d_unknown': '市场当日收益未知',
    'base_return_5d_unknown': '市场近5日收益未知',
    'base_breadth_below_0.5_or_invalid': '市场上涨家数不足一半',
    'base_return_1d_negative': '市场当日走弱', 'base_return_5d_negative': '市场近5日走弱',
    'verified_frozen_sector_membership_unavailable': '缺少冻结且可核验的同源行业成分',
    'sector_valid_members_below_5': '板块可核验成分不足5只',
    'sector_history_coverage_below_0.65_or_unknown': '板块历史覆盖不足65%或未知',
    'sector_phase_not_strengthening': '板块尚未进入相对走强阶段',
    'sector_relative_return_5d_not_positive_or_unknown': '板块近5日未强于市场或未知',
    'sector_breadth_below_0.55': '板块上涨家数不足55%',
    'same_vendor_actual_flow_not_positive': '同源板块资金未呈正流入',
    'stock_minus_board_return_5d_not_positive_or_unknown': '个股近5日未强于板块或未知',
    'stock_not_top_30_percent_of_full_board_history': '未进入整个板块近5日涨幅前30%',
}


def _finite(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and abs(value) <= 1e12)
    except OverflowError:
        return False


def _scalar(value, limit=80):
    if value is None or isinstance(value, bool): return value
    if _finite(value): return value
    return value[:limit].encode('utf-8')[:limit].decode('utf-8', errors='ignore') if isinstance(value, str) else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _strings(value, count=3, limit=48):
    return [_scalar(item, 64 if item in REASONS else limit)
            for item in _list(value)[:count] if isinstance(item, str)]


def _counts(raw, fields):
    return {key: raw.get(key) if _finite(raw.get(key)) else 0 for key in fields}


def _summary(raw):
    raw = _mapping(raw)
    result = {key: raw.get(key) if _finite(raw.get(key)) else None for key in METRICS}
    for key in ('scheduled_observations', 'reference_closed', 'distinct_entry_dates',
                'unknown', 'pending', 'excluded_filter', 'no_signal', 'price_outside_bounds'):
        if result[key] is None: result[key] = 0
    interval = (_mapping(raw.get('bootstrap_interval'))
                if result['reference_closed'] >= 40 and result['distinct_entry_dates'] >= 20 else {})
    result['bootstrap_interval'] = {}
    for key in ('gross_mean', 'reference_profit_rate', 'moving5_gross_mean', 'moving5_reference_profit_rate'):
        pair = interval.get(key)
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and all(_finite(value) for value in pair):
            result['bootstrap_interval'][key] = list(pair)
    result.update(formal_trade_ready=False, actual_win_rate=None,
                  execution_win_rate_80_validated=False)
    return result


def compact(study):
    """Keep all displayed fields, dropping raw ledgers and unbounded history."""
    if not isinstance(study, dict) or study.get('schema') != VERSION:
        return {}
    result = {'schema': VERSION, 'formal_trade_ready': False, 'actual_win_rate': None,
        'execution_win_rate_80_validated': False, 'orders_submitted': False,
        'prospective_only': True, 'automatic_policy_activation': False,
        'full_evidence_archived_separately': True}
    for key in ('generated_at', 'updated_at', 'base_trade_date', 'target_trade_date',
                'protocol_sha256', 'input_database_sha256'):
        if key in study: result[key] = _scalar(study[key], 80)
    result['tracked_dates'] = study.get('tracked_dates') if _finite(study.get('tracked_dates')) else 0
    raw = _mapping(study.get('current_cohort'))
    current = {key: _scalar(raw[key], 80) for key in ('schema', 'base_trade_date',
        'target_trade_date', 'D2_trade_date', 'generated_at', 'cohort_id', 'cohort_sha256',
        'protocol_sha256', 'status', 'reason') if key in raw}
    current.update(forward_eligible=raw.get('forward_eligible') is True,
        formal_trade_ready=False, actual_win_rate=None, execution_win_rate_80_validated=False)
    current['coverage'] = _counts(_mapping(raw.get('coverage')), ('candidate_pool_count',
        'valid_pool_count', 'base_cohort_count', 'market_gate_passed_count',
        'sector_gate_passed_count', 'leadership_gate_passed_count', 'confluence_count'))
    market = _mapping(raw.get('market_gate'))
    current['market_gate'] = {key: market.get(key) if _finite(market.get(key)) else None
                             for key in ('base_breadth', 'base_return_1d', 'base_return_5d')}
    current['market_gate'].update(known=market.get('known') is True, eligible=market.get('eligible') is True)
    current['base_candidates'] = []
    for row in _list(raw.get('base_candidates'))[:12]:
        if not isinstance(row, dict): continue
        kept = {key: row[key] if _finite(row[key]) else None for key in ('base_close', 'frozen_entry_floor',
            'frozen_entry_cap', 'frozen_quantity_shares', 'relative_return_5d',
            'leadership_percentile', 'leadership_rank', 'leadership_member_count',
            'leadership_top_count') if key in row}
        if 'membership_snapshot_sha256' in row:
            kept['membership_snapshot_sha256'] = _scalar(row['membership_snapshot_sha256'], 64)
        kept['symbol'] = _scalar(row.get('symbol'), 16)
        kept['name'] = _scalar(row.get('name'), 32)
        identity = _mapping(row.get('sector_identity'))
        kept['sector_identity'] = {key: _scalar(identity[key], 32) for key in
                                   ('classification', 'sector_code', 'sector') if key in identity}
        kept.update(confluence_eligible=row.get('confluence_eligible') is True,
            flow_known=row.get('flow_known') is True, flow_credit=row.get('flow_credit') is True,
            reasons=_strings(row.get('reasons')), formal_trade_ready=False,
            actual_win_rate=None, execution_win_rate_80_validated=False)
        current['base_candidates'].append(kept)
    result['current_cohort'] = current
    summary = _mapping(study.get('summary'))
    result['summary'] = {arm: _summary(summary.get(arm)) for arm in ARMS}
    pairs = _mapping(study.get('paired_timing'))
    result['paired_timing'] = {}
    for key in ('base', 'confluence'):
        pair = _mapping(pairs.get(key))
        result['paired_timing'][key] = {field: pair.get(field) if _finite(pair.get(field)) else None
            for field in ('both_closed', 'unmatched_or_pending', 'mean_return_1440_minus_0940',
                          'profit_fraction_1440_minus_0940')}
        result['paired_timing'][key].update(conditional_on_both_reference_entries_and_exits=True,
            is_population_causal_effect=False, selects_policy=False)
    return result


def _e(value):
    return html.escape(str(value), quote=True)


def _num(value, digits=0):
    return f'{value:,.{digits}f}' if _finite(value) else '—'


def _pct(value):
    return _num(value*100, 2)+'%' if _finite(value) else '待积累'


def _table(headers, rows):
    return ('<div class="scroll"><table><thead><tr>' + ''.join('<th>'+_e(key)+'</th>' for key in headers)
        + '</tr></thead><tbody>' + ''.join('<tr>'+''.join('<td>'+str(value)+'</td>' for value in row)+'</tr>' for row in rows)
        + '</tbody></table></div>')


def _reason(value):
    return REASONS.get(value, '其他冻结条件未满足，保留观察')


def render(study):
    study = compact(study)
    if not study: return ''
    current, summary = study['current_cohort'], study['summary']
    coverage = current['coverage']
    parts = ['<section><h2>隔日盈利前向研究：同池比较早盘与尾盘</h2>',
        '<div class="callout"><strong>主目标：D1买入参考价，到D2固定14:50卖出参考价的毛收益为正。费用为零；3%与6%是卖出时实现收益的辅助统计。</strong></div>',
        '<p>先在前一交易日收盘信息中冻结最多12只影子观察样本，再在同一名单内标记市场、板块、个股共振。共振未通过就保留原样本作对照，不补选、不重新排序。每只股票是独立观察，不是账户组合，也不是12个持仓或买入推荐；原账户仍最多3个正式名额，主策略继续停用。</p>',
        '<p class="note">仅从本次开始逐日登记真实的未来样本，旧历史不补写成新验证。分钟价格由盘后取得，只描述下一分钟开盘参考事件，不是实时看到的盘口，不证明实际成交。80%以上实际胜率尚未验证；没有个人实盘胜率或明日上涨概率。</p>']
    timings = []
    for arm in ARMS:
        early = arm in ('base_0940', 'confluence_0940', 'confluence_opening_retest')
        condition = ('不加共振过滤' if arm.startswith('base_') else
                     '同一原池的共振子集' if arm in ('confluence_0940', 'confluence_1440') else
                     '共振＋开盘5分钟突破后的回测承接' if early else '共振＋尾盘价格与低点确认')
        timings.append([NAMES[arm], condition, '09:40完成观察，下一分钟开盘参考' if early else '14:40完成观察，下一分钟开盘参考',
                        'D2固定14:50退出参考', '单只影子预算≤1万元；整100股'])
    parts.append('<h3>事前登记的六组观察</h3>'+_table(['研究组', '冻结范围与条件', 'D1入场观察', 'D2统一退出', '数量口径'], timings))
    parts.append('<p>早盘与尾盘先在原池配对，再在同一共振子集配对；突破回测与共振早盘对照，尾盘确认与共振尾盘对照。所有组统一退出时点，避免把退出差异当作筛选优势；无信号、越价、缺失和待退出均单列。</p>')
    parts.append('<details><summary>共振和分钟确认的固定条件</summary><p>市场上涨家数至少一半，市场当日及近5日收益非负。板块进入相对走强阶段，近5日强于市场，上涨家数至少55%；至少5只有完整历史的同源成分，覆盖不低于65%。个股近5日强于该板块，并在整个板块成分中排前30%（小板块向上取整）。这些是新研究假设，尚未证明有效。</p>'
        '<p>同源板块资金有及时数据时必须净流入且净占比为正；只有技术分类时明确记为资金未知，不按中文行业名称移接资金。早盘先有开盘5分钟区间，09:36—09:39已完成分钟收盘至少高出区间上沿0.01元，09:40多头回测低点在上沿±前收0.2%内、收回上沿且收盘位置至少60%，中间不得收破容差。尾盘须有完整已收盘分钟序列，最新多头收盘不低于当日开盘及成交量加权分钟收盘均值，近5根最低点不低于此前5根，收盘位置至少60%；该加权均值只是近似量价参考。</p></details>')
    market = current['market_gate']
    parts.append('<p class="note">当前冻结原池 '+_num(coverage['base_cohort_count'])+'只；市场条件通过 '
        +_num(coverage['market_gate_passed_count'])+'只，板块条件通过 '+_num(coverage['sector_gate_passed_count'])
        +'只，领导性条件通过 '+_num(coverage['leadership_gate_passed_count'])+'只，完整共振 '+_num(coverage['confluence_count'])
        +'只。覆盖市场上涨家数 '+_pct(market['base_breadth'])+'，当日 '+_pct(market['base_return_1d'])
        +'，近5日 '+_pct(market['base_return_5d'])+'。冻结时间 '+_e(current.get('generated_at') or '待生成')+'。</p>')
    candidates = []
    for row in current['base_candidates']:
        identity = row['sector_identity']
        sector_text = _e(identity.get('sector') or '行业未知')
        if identity.get('sector_code'): sector_text += '<br>'+_e(identity['sector_code'])
        sector_text += '<br>'+('同源资金已核验' if row['flow_known'] else '资金未知，无资金加分')
        reasons = '；'.join(_reason(value) for value in row['reasons'])
        state = '原池＋共振子集' if row['confluence_eligible'] else '仅原池对照'
        candidates.append([_e(row.get('name') or row.get('symbol') or '—')+'<br>'+_e(row.get('symbol') or ''),
            sector_text, _num(row.get('base_close'), 2),
            _num(row.get('frozen_entry_floor'), 2)+'～'+_num(row.get('frozen_entry_cap'), 2),
            _num(row.get('frozen_quantity_shares')), _pct(row.get('relative_return_5d')),
            _num(row.get('leadership_rank'))+'／'+_num(row.get('leadership_member_count')),
            state+('<br>'+_e(reasons) if reasons else '')])
    if candidates:
        parts.append('<h3>本期影子样本：冻结名单及共振标记</h3>'+_table(
            ['样本', '板块证据', '前收', '冻结参考价格范围', '独立参考股数', '近5日超额于板块', '全板块涨幅排名', '观察归属与未通过原因'], candidates))
    else:
        parts.append('<p class="muted">当前影子样本为空，等待有完整冻结证据的新一期名单；不以备用名单补位。</p>')
    stats, decomposition, intervals = [], [], []
    for arm in ARMS:
        metric = summary[arm]
        stats.append([NAMES[arm], _num(metric['reference_closed'])+'／'+_num(metric['scheduled_observations']),
            _num(metric['distinct_entry_dates']), _num(metric['pending'])+'／'+_num(metric['unknown']),
            _pct(metric['reference_gross_profit_rate']), _pct(metric['reference_profit_fraction_including_unknown_as_not_won']),
            _pct(metric['reference_ge3_rate']), _pct(metric['reference_ge6_rate']),
            _pct(metric['mean_gross_return']), _pct(metric['worst_gross_return'])])
        decomposition.append([NAMES[arm], _pct(metric['mean_D1_after_buy_return']),
            _pct(metric['mean_overnight_return']), _pct(metric['mean_D2_after_open_return']),
            _pct(metric['average_winning_return'])+'／'+_pct(metric['average_nonwinning_return']),
            _pct(metric['coverage_fraction_closed']), _num(metric['excluded_filter'])+'／'+_num(metric['no_signal'])+'／'+_num(metric['price_outside_bounds'])])
        interval = metric['bootstrap_interval'].get('reference_profit_rate')
        if interval:
            intervals.append(NAMES[arm]+'：'+_pct(interval[0])+'～'+_pct(interval[1]))
    parts.append('<h3>真正未来样本：持续记账，当前不能据此启用策略</h3>'+_table(
        ['研究组', '已闭合／登记', '不同入场日期', '待定／未知', '已闭合参考毛盈利比例', '计入未知的保守盈利比例', '卖出赚到3%', '卖出赚到6%', '平均毛收益', '最差毛收益'], stats))
    if all(summary[arm]['reference_closed'] == 0 for arm in ARMS):
        parts.append('<p><b>新增未来已闭合样本为0，盈利比例待积累；当前没有可发布的真实未来胜率。</b></p>')
    parts.append('<p class="note">保守盈利比例把已闭合与未知合并为分母，未知作为未胜出，待定另列；同时展示只含已闭合参考事件的比例。不能把未知删除后称为实际胜率。多只股票同日的观察共享市场风险，不同日期数比股票条数更能反映新增信息。统计结果不会自动启用规则。</p>')
    pair_rows = []
    for key, label in (('base', '同一原池'), ('confluence', '同一共振子集')):
        pair = study['paired_timing'][key]
        pair_rows.append([label, _num(pair['both_closed']), _num(pair['unmatched_or_pending']),
            _pct(pair['mean_return_1440_minus_0940']), _pct(pair['profit_fraction_1440_minus_0940'])])
    parts.append('<details><summary>同股、同日配对：尾盘相对早盘的差异</summary>'+_table(
        ['配对范围', '两组均闭合', '未配对或待定', '尾盘减早盘平均收益', '尾盘减早盘盈利比例'], pair_rows)
        +'<p class="note">这里只比较两次参考入场与退出均可核验的同股同日样本；越价或缺失可能改变配对样本，不能将差异解释为全体样本的因果效果，更不据此选择启用早盘或尾盘。</p></details>')
    parts.append('<details><summary>买入后当日、隔夜与次日的收益分解及覆盖</summary><p>每笔总毛收益满足：（1＋D1买入后收益）×（1＋隔夜收益）×（1＋D2开盘后收益）－1。分项均值不能直接相加，也不能相乘后充当总收益均值；日间强势可能被隔夜跳空抵消。</p>'+_table(
        ['研究组', 'D1买入至收盘均值', 'D1收盘至D2开盘均值', 'D2开盘至退出均值', '平均盈利／非盈利', '闭合／登记覆盖', '共振排除／无信号／越价'], decomposition)+'</details>')
    if intervals:
        parts.append('<p class="note">按入场日期重采样的盈利比例近似区间：'+_e('；'.join(intervals))+'。六组比较作近似修正；区间不是未来保证。</p>')
    parts.append('<p class="note">至少40笔闭合参考观察、20个不同入场日期后才展示按日期及连续5个有观察入场日期的区块重采样近似区间，并考虑六组比较；两种区间取较宽包络。序列依赖、制度变化与未知成交仍然存在；任何区间都不能证明80%实际胜率。D1不得卖出，D2无可核验退出价格时保持未知，不利用日内最高价假设止盈。</p>')
    parts.append('<p class="note">研究依据：<a href="https://www.brookstradingcourse.com/wp-content/uploads/2017/02/BTC-Video-15D-Signs-of-Strong-and-Weak-Breakouts.pdf">Brooks强弱突破</a>、'
        '<a href="https://shop.investors.com/images/promotional/20-Rules_102808.pdf">IBD相对强度与行业观察</a>、'
        '<a href="https://lindaraschke.net/wp-content/uploads/EBook_Updated.pdf">Linda Raschke波动突破与样本外检验</a>支持形成研究问题；'
        '<a href="https://research.rug.nl/en/publications/the-overnight-return-puzzle-and-the-t1-trading-rule-in-chinese-st/">Qiao与Dam的中国T+1隔夜研究</a>提示分开观察日间与隔夜，'
        '<a href="https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf">Bailey等回测过拟合研究</a>提示防止反复挑选历史赢家。这些来源未验证本项目的数值阈值、A股隔日盈利或80%胜率。</p></section>')
    return ''.join(parts)
