"""Bounded, dependency-free presentation of the frozen price-action study."""
import html
import math


VERSION = "pa-context-trade-research-v1"
SETUPS = ("breakout_followthrough", "trend_pullback_rejection", "range_failed_breakout")
DIAGNOSTICS = ("baseline_fixed_3pct_2pct", "fixed_breakout_followthrough",
               "fixed_trend_pullback_rejection", "fixed_range_failed_breakout",
               "selected_policy", "selected_setup_fixed_3pct_2pct")
METRICS = ("n_attempts", "n_closed", "n_unresolved", "n_cancelled", "independent_entry_dates",
           "win_rate", "ge3_rate", "ge6_rate", "mean_closed_gross_return",
           "mean_win_return", "mean_loss_return", "profit_factor",
           "realized_closed_trade_max_drawdown")
NAMES = {
    "breakout_followthrough": "上涨背景中的突破跟进",
    "trend_pullback_rejection": "上涨趋势的回调承接",
    "range_failed_breakout": "区间下沿的失败下破收回",
    "baseline_fixed_3pct_2pct": "同池排序基准：固定3%／2%",
    "fixed_breakout_followthrough": "突破跟进：结构风险",
    "fixed_trend_pullback_rejection": "回调承接：结构风险",
    "fixed_range_failed_breakout": "区间收回：结构风险",
    "selected_policy": "选择期确定规则：结构风险",
    "selected_setup_fixed_3pct_2pct": "同一已选形态：固定3%／2%对照",
    "bull": "上涨趋势", "bear": "下跌趋势", "range": "交易区间", "unclear": "背景不明",
}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _scalar(value, limit=120):
    if value is None or isinstance(value, bool):
        return value
    if _finite(value):
        return value
    return value[:limit] if isinstance(value, str) else None


def _metric(raw):
    raw = raw if isinstance(raw, dict) else {}
    result = {key: raw[key] if _finite(raw.get(key)) else None for key in METRICS if key in raw}
    validation = raw.get("validation") or {}
    validation = validation if isinstance(validation, dict) else {}
    keep = {}
    for key in ("date_cluster_win_ci95", "moving_block_win_ci95", "moving_block_mean_ci95"):
        interval = validation.get(key)
        if isinstance(interval, (list, tuple)) and len(interval) == 2 and all(_finite(x) for x in interval):
            keep[key] = list(interval)
    if _finite(validation.get("familywise_lower_mean_return")):
        keep["familywise_lower_mean_return"] = validation["familywise_lower_mean_return"]
    if "selection_gate" in validation:
        keep["selection_gate"] = validation["selection_gate"] is True
    blockers = validation.get("selection_blockers")
    if isinstance(blockers, list):
        keep["selection_blockers"] = [x[:96] for x in blockers[:8] if isinstance(x, str)]
    result["validation"] = keep
    return result


def compact(study):
    """Idempotent whitelisted payload; all rendered fields survive compaction."""
    if not isinstance(study, dict) or study.get("schema") != VERSION:
        return {}
    result = {"schema": VERSION, "formal_trade_ready": False, "actual_win_rate": None,
              "execution_win_rate_80_validated":False}
    for key in ("generated_at", "base_trade_date", "target_trade_date", "input_database_sha256",
                "strategy_definition_sha256", "costs_included", "validation_origin"):
        if key in study:
            result[key] = _scalar(study[key])
    raw_policy = study.get("policy") or {}
    selected = raw_policy.get("selected_setup")
    active = raw_policy.get("active") is True and selected in SETUPS
    result["policy"] = {"active": active, "selected_setup": selected if selected in SETUPS else None,
                        "scope": "research_only"}
    raw_coverage = study.get("coverage") or {}
    result["coverage"] = {key: _scalar(raw_coverage[key]) for key in
                          ("scanned", "history_candidates", "live_candidates", "eligible_plans", "current_selected")
                          if key in raw_coverage}
    raw_diagnostics = study.get("diagnostic") or {}
    result["diagnostic"] = {key: _metric(raw_diagnostics[key]) for key in DIAGNOSTICS if key in raw_diagnostics}
    result["selection"] = []
    for row in (study.get("selection") or [])[:3]:
        if isinstance(row, dict) and row.get("setup") in SETUPS:
            result["selection"].append({"setup": row["setup"], "eligible_research": row.get("eligible_research") is True,
                                        "metrics": _metric(row.get("metrics"))})
    result["current_watchlist"] = []
    if active:
        for row in (study.get("current_watchlist") or [])[:5]:
            if not isinstance(row, dict):
                continue
            fields = ("symbol", "name", "context", "setup", "entry_cap", "frozen_entry_floor",
                      "structural_stop", "target_pct", "reward_risk", "breakeven_probability",
                      "stress_breakeven_probability", "quantity_ref", "risk_budget", "pa_plan_id",
                      "generated_at","base_trade_date","target_trade_date","close","atr14","eligible",
                      "formal_trade_ready","actual_fill_verified","breakout_level")
            result["current_watchlist"].append({key: _scalar(row[key]) for key in fields if key in row})
    return result


def _e(value):
    return html.escape(str(value), quote=True)


def _name(value):
    return NAMES.get(value, str(value) if value is not None else "未选择")


def _number(value, digits=0):
    return f"{value:,.{digits}f}" if _finite(value) else "—"


def _pct(value):
    return _number(value * 100, 2) + "%" if _finite(value) else "—"


def _table(headers, rows):
    return ('<div class="scroll"><table><thead><tr>' + ''.join('<th>' + _e(x) + '</th>' for x in headers)
            + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + x + '</td>' for x in row) + '</tr>' for row in rows)
            + '</tbody></table></div>')


def render(study):
    study = compact(study)
    if not study:
        return ""
    policy = study["policy"]
    active = policy["active"]
    coverage = study["coverage"]
    parts = ['<section><h2>价格行为交易升级：先定背景，再定买卖与风险</h2>']
    if active:
        state = '选择期通过的研究规则：' + _e(_name(policy['selected_setup'])) + '。可开展前向研究；正式交易资格仍未通过。'
    else:
        state = '本轮没有启用通过证据要求的交易规则，当前保留空名单。最终诊断数字不会反过来决定规则。'
    parts.append('<div class="callout"><strong>' + state + '</strong></div>')
    parts.append('<p>这轮升级把背景、位置、信号、成交上限、结构失效和次日退出连成一份事前计划。3%与6%目标、入场上限及盈亏比联合求解，结构不允许时放弃交易。</p>')
    parts.append('<ol><li><b>看背景：</b>先检查市场宽度与流动性，再用均线斜率、趋势效率与重叠程度判断上涨趋势或交易区间。</li>'
                 '<li><b>找位置：</b>支撑、阻力、突破位和区间边界只用当时已经出现的历史K线，判断目标前方有多少空间。</li>'
                 '<li><b>等信号：</b>固定比较突破后的跟进、上涨趋势回调承接、区间下沿失败下破收回三类形态。</li>'
                 '<li><b>算交易：</b>先冻结结构止损，联合计算目标与最高买价；3%目标需盈亏比≥1.5，6%目标仅限上涨背景且盈亏比≥1.8。条件不足不下计划。</li>'
                 '<li><b>守T+1：</b>T日收盘定计划，T+1按冻结上下限观察入场，新买股份最早T+2卖出。买入日收盘已跌破结构位，或突破形态收回突破位下方，T+2开盘处理。</li>'
                 '<li><b>查结果：</b>时间顺序选择规则，随后统一比较全部固定规则；日期聚类区间、未知交易和资金占用同时保留。</li></ol>')
    parts.append('<details><summary>三类固定形态与失效位置</summary>' + _table(
        ['事前背景', '触发证据', '冻结的失效参考'], [
            ['上涨趋势', '先有强多头突破，再有下一根多头跟进且守住突破区', '两根K低点及突破位的最低值，再减0.15 ATR'],
            ['上涨趋势', '回调触及均线附近，前根偏空，本根带下影收强', '本根低点与此前5根低点的较低值，再减0.15 ATR'],
            ['此前交易区间', '下破区间下沿后收回，并在区间较低位置收强', '失败下破最低价，再减0.15 ATR'],
        ]) + '<p class="note">阈值均为本项目固定的研究假设。两根K确认不等于已经实现完整High 2二次进场状态机；日线形态也不自动提供盘中成交证据。</p></details>')
    parts.append('<p class="note">扫描' + _number(coverage.get('scanned')) + '条历史记录；历史候选'
                 + _number(coverage.get('history_candidates')) + '条；本期候选'
                 + _number(coverage.get('live_candidates')) + '只，形成合格计划'
                 + _number(coverage.get('eligible_plans')) + '份，已选规则当前观察'
                 + _number(coverage.get('current_selected')) + '只。</p>')
    rows = []
    for key, metric in study['diagnostic'].items():
        if key in ('selected_policy', 'selected_setup_fixed_3pct_2pct') and not active:
            continue
        rows.append([_e(_name(key)), _number(metric.get('n_attempts')) + '／' + _number(metric.get('n_unresolved')),
                     _number(metric.get('independent_entry_dates')), _pct(metric.get('win_rate')),
                     _pct(metric.get('ge3_rate')), _pct(metric.get('ge6_rate')), _pct(metric.get('mean_closed_gross_return'))])
    if rows:
        parts.append('<h3>历史开发诊断：统一比较，不据此挑赢家</h3>' + _table(
            ['预定规则', '入场／未知', '入场日期', '毛盈利比例', '卖出赚到3%', '卖出赚到6%', '平均已闭合毛收益'], rows))
    parts.append('<p class="note">表中盈利比例使用保守分母，未知结果保留为非胜出；撤单单列，未解决交易占用资金。3%与6%列指按该行退出规则实际获得的收益，不是盘中曾经摸到的涨幅。费用与滑点尚未计入，净收益会低于毛收益。所有旧历史均属于开发证据。</p>')
    selection = study['selection']
    if selection:
        passed = sum(row['eligible_research'] for row in selection)
        selection_rows = []
        for row in selection:
            metric = row['metrics']; validation = metric['validation']
            selection_rows.append([_e(_name(row['setup'])), _number(metric.get('n_closed')),
                                   _number(metric.get('independent_entry_dates')),
                                   _pct(validation.get('familywise_lower_mean_return')),
                                   '通过研究门槛' if row['eligible_research'] else '样本或收益证据不足'])
        parts.append('<details><summary>选择期：' + str(passed) + '／' + str(len(selection)) + '类通过</summary>'
                     + _table(['固定形态', '完整交易', '入场日期', '修正后平均收益下界', '结论'], selection_rows)
                     + '<p class="note">只有3类预定候选；按完整日期切分并剔除结果跨界样本。研究门槛同时检查样本数、日期数、未知结果及整日／连续日期块重采样收益下界，三次比较修正仍是近似方法，不能消除全部历史开发偏差。</p></details>')
    if active and study['current_watchlist']:
        watch_rows = []
        for row in study['current_watchlist']:
            watch_rows.append([_e(row.get('name') or row.get('symbol') or '—') + '<br>' + _e(row.get('symbol') or ''),
                               _e(_name(row.get('context'))),
                               _number(row.get('frozen_entry_floor'), 2) + '～' + _number(row.get('entry_cap'), 2),
                               _number(row.get('structural_stop'), 2), _pct(row.get('target_pct')),
                               _number(row.get('reward_risk'), 2), _pct(row.get('breakeven_probability')),
                               _pct(row.get('stress_breakeven_probability')), _number(row.get('quantity_ref'))])
        parts.append('<h3>当前研究计划</h3>' + _table(
            ['股票', '背景', '冻结买价范围', '结构参考位', '目标', '盈亏比', '毛盈亏平衡门槛', '跳空压力门槛', '参考股数'], watch_rows))
    selected_metric = study['diagnostic'].get('selected_policy') or {}
    ci = (selected_metric.get('validation') or {}).get('moving_block_win_ci95')
    if active and ci:
        parts.append('<p class="note">已选规则的连续日期块毛盈利比例95%区间：' + _pct(ci[0]) + '～' + _pct(ci[1]) + '。区间反映历史参考成交与样本不确定性。</p>')
    parts.append('<p><b>盈亏平衡门槛不是预测胜率。</b>仅在止盈或止损两种结果的简化假设下，毛口径公式为p=L／(G+L)，L为结构风险、G为目标收益；压力情景把L再加2%跳空。实际还存在收盘退出与其他跳空结果，须用完整交易检验期望。3万元参考账户每笔预算150元、最多1万元，按100股取整并提前冻结；这只是压力测算，隔夜跳空、跌停或无法成交可能使实际损失超过预算。</p>')
    parts.append('<p class="note">日线历史使用T+1开盘参考成交，买入当天不卖。盘中开盘区间突破／回测确认的逻辑已编写，历史分钟证据与实时接入尚未完成；数据未连接时保持等待，不把日线高低价当成可成交触发。T+2先处理买入日已失效的计划，其余按预定结构止损／目标或收盘退出；同日先后不明采用保守顺序。</p>')
    parts.append('<p class="note">来源：已核对方方土官方视频的<a href="https://www.youtube.com/watch?v=kW3zGw96SaE">背景与信号K</a>、'
                 '<a href="https://www.youtube.com/watch?v=152osf_ULas">假突破</a>、'
                 '<a href="https://www.youtube.com/watch?v=hkTe6ODgoBc">突破回测</a>主题；本次未取得字幕或完整视频内容。理论参照Brooks官方的'
                 '<a href="https://www.brookstradingcourse.com/wp-content/uploads/2017/02/BTC-Video-15D-Signs-of-Strong-and-Weak-Breakouts.pdf">强弱突破清单</a>、'
                 '<a href="https://www.brookstradingcourse.com/ask-al/china-futures-daily-interview-part-1/">趋势、区间与二次进场问答</a>、'
                 '<a href="https://www.brookstradingcourse.com/futures-market/should-i-take-this-trade/">交易方程</a>；具体数值阈值由本项目设定，未验证为最优。</p>')
    parts.append('<p><b>80%以上实际胜率尚未验证，正式交易资格未通过。</b>研究计划和参考股数均不代表已发出的订单。</p></section>')
    return ''.join(parts)
