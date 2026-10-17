"""Render a self-contained, Chinese net-profit strategy research report."""
from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path
from typing import Any, Mapping


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _number(value: Any, decimals: int = 0, suffix: str = "") -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return _e(f"{number:,.{decimals}f}{suffix}")


def _pct(value: Any, decimals: int = 1) -> str:
    if value is None or isinstance(value, bool):
        return "—"
    try:
        return _number(float(value) * 100, decimals, "%")
    except (TypeError, ValueError):
        return "—"


def _interval(report: Mapping[str, Any]) -> str:
    interval = report.get("date_block_bootstrap", {}).get("win_rate_ci95")
    if not isinstance(interval, (list, tuple)) or len(interval) != 2:
        return "—"
    return _pct(interval[0]) + " – " + _pct(interval[1])


def _diagnostic_row(title: str, explanation: str, report: Mapping[str, Any], emphasis: bool = False) -> str:
    return (
        '<tr' + (' class="focus-row"' if emphasis else '') + '><td><strong>' + _e(title)
        + '</strong><span class="cell-note">' + _e(explanation) + '</span></td><td class="num">'
        + _number(report.get("n_attempted_trades")) + '</td><td class="num">'
        + _number(report.get("n_trades")) + '</td><td class="num">'
        + _number(report.get("n_unresolved")) + '</td><td class="num strong">'
        + _pct(report.get("win_rate")) + '</td><td class="num interval">' + _interval(report)
        + '</td><td class="num">' + _pct(report.get("mean_net_return"), 2)
        + '</td><td class="num">' + _number(report.get("independent_entry_dates")) + '</td></tr>'
    )


def render_report(summary: Mapping[str, Any]) -> str:
    """Return a standalone report; unavailable metrics are never filled with guesses."""
    protocol = summary.get("protocol") or {}
    policy = summary.get("policy") or {}
    costs = protocol.get("costs") or {}
    filters = protocol.get("filters") or {}
    splits = summary.get("split") or {}
    shadow = summary.get("shadow_diagnostic") or {}
    base = summary.get("base_trade_date") or "未提供"
    forward = protocol.get("fresh_forward_signal_start_not_before") or "未提供"
    min_trades = protocol.get("research_policy_min_completed_trades")
    min_dates = protocol.get("research_policy_min_entry_dates")
    required_lower = protocol.get("research_policy_date_block_95pct_lower_strictly_above")
    real_rate = _pct(summary.get("real_execution_win_rate"))
    policy_state = "研究阈值已选定" if policy.get("active") is True else "研究阈值未通过门槛"
    policy_detail = (
        "已在阈值选择段选定 " + _pct(policy.get("selected_threshold")) + " 的研究信号门槛；仍须经过正式证据验收。"
        if policy.get("active") is True else
        "当前没有阈值满足预设研究门槛。系统允许空仓，80% 固定阈值仅用于影子观察。"
    )
    candidate_state = "未开放" if summary.get("formal_trade_ready") is not True else "以正式验收记录为准"

    variants = (
        ("基准策略", "按模型排序取前 3，只检验费用后的基准表现。", "baseline_top3", False),
        ("固定 80% 影子策略", "加入市场与个股过滤；80% 是预测分数门槛。", "shadow_diagnostic", True),
        ("选择段通过的策略", "仅在选择段通过门槛后，才产生该项诊断。", "selected_policy_diagnostic", False),
        ("滑点压力情景", "保持影子规则，将买卖每侧滑点提高到 20 个基点。", "stress_20bp", False),
    )
    diagnostic_rows = "".join(_diagnostic_row(title, explanation, summary.get(key) or {}, emphasis)
                              for title, explanation, key, emphasis in variants)
    selection_rows = ""
    for item in summary.get("selection_thresholds") or []:
        validation = item.get("validation") or {}
        verdict = "通过研究门槛" if item.get("research_policy_eligible") is True else "未通过"
        selection_rows += (
            '<tr><td class="num">' + _pct(item.get("threshold")) + '</td><td class="num">'
            + _number(validation.get("n_trades")) + '</td><td class="num">'
            + _number(validation.get("n_unresolved")) + '</td><td class="num">'
            + _number(validation.get("independent_entry_dates")) + '</td><td class="num">'
            + _pct(validation.get("win_rate")) + '</td><td class="num interval">'
            + _interval(validation) + '</td><td><span class="pill">' + _e(verdict) + '</span></td></tr>'
        )
    if not selection_rows:
        selection_rows = '<tr><td colspan="7" class="empty">尚无阈值选择结果。</td></tr>'

    split_rows = ""
    for key, title, use in (
        ("train", "01 · 训练段", "拟合净盈利模型"),
        ("calibration", "02 · 校准段", "校准预测分数"),
        ("selection", "03 · 阈值选择段", "只在此段比较阈值"),
        ("diagnostic", "04 · 历史诊断段", "报告表现，不据此继续调参"),
    ):
        item = splits.get(key) or {}
        date_range = _e(item.get("first_input_date") or "—") + " → " + _e(item.get("last_input_date") or "—")
        split_rows += (
            '<tr><td><strong>' + _e(title) + '</strong><span class="cell-note">' + _e(use)
            + '</span></td><td class="date-range">' + date_range + '</td><td class="num">'
            + _number(item.get("independent_dates")) + '</td><td class="num">'
            + _number(item.get("labeled_closed_rows")) + '</td><td class="num">'
            + _number(item.get("unresolved_rows")) + '</td><td class="date-range">'
            + _e(item.get("last_outcome_date") or "—") + '</td></tr>'
        )

    blocker_names = {
        "official_pit_universe_status_limits_actions_unverified": "历史时点的股票池、风险状态、涨跌停价及公司行动尚未完成官方证据核验。",
        "broker_fills_and_actual_fee_evidence_unavailable": "尚无逐笔真实成交和实际费用凭证，日线开收盘价无法证明账户成交。",
        "new_forward_execution_track_record_unavailable": "尚无冻结规则之后积累的新交易记录，不能验收真实净胜率。",
    }
    blockers = "".join('<li>' + _e(blocker_names.get(str(item), item)) + '</li>'
                       for item in summary.get("formal_blockers") or [])
    if not blockers:
        blockers = '<li>以逐项证据验收为准；本页不将研究运行成功视为交易验证。</li>'
    protocol_json = _e(json.dumps(protocol, ensure_ascii=False, indent=2, allow_nan=False))
    report_identity = _e(summary.get("protocol_sha256") or "未提供")
    database_identity = _e(summary.get("input_database_sha256") or "未提供")
    generated = _e(summary.get("generated_at") or "未提供")

    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>A股净盈利策略 · 研究验收</title>
<style>
:root{--ink:#132c3a;--muted:#5c707c;--paper:#f3f6f7;--white:#fff;--line:#dce5e8;--teal:#087f81;--soft:#e8f4f2;--amber:#9a5b12;--amberbg:#fff5e3;--navy:#173c4d}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","Noto Sans CJK SC",sans-serif;font-size:15px;line-height:1.7}a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,summary:focus-visible{outline:3px solid #40b6b3;outline-offset:4px}.topbar{background:var(--navy);color:#c9dfe6}.topbar-inner{max-width:1200px;padding:15px 32px;margin:auto;display:flex;justify-content:space-between;gap:18px;font-size:12px;letter-spacing:.07em}.brand{color:#fff;font-weight:700}.wrap{max-width:1200px;margin:auto;padding:34px 32px 54px}.hero{padding:0 0 24px;border-bottom:1px solid var(--line)}.eyebrow{font-size:12px;letter-spacing:.13em;color:var(--teal);font-weight:700;margin:0 0 9px}.hero h1{font-size:clamp(28px,4vw,44px);line-height:1.25;letter-spacing:-.025em;max-width:900px;margin:0 0 16px}.lede{color:var(--muted);max-width:840px;font-size:16px;margin:0}.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:19px}.chip{font-size:12px;padding:4px 11px;border:1px solid #cedde0;border-radius:20px;color:#3f5a67;background:#fff}.chip.amber{background:var(--amberbg);border-color:#eddbba;color:var(--amber)}.notice{border-left:4px solid #bc8239;background:var(--amberbg);padding:17px 20px;margin:24px 0;color:#624924}.notice strong{display:block;font-size:16px;margin-bottom:4px}.notice p{margin:0}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.metric{padding:20px 22px;background:#fff;border:1px solid var(--line);border-radius:12px;min-height:154px}.metric.primary{background:var(--navy);color:white;border-color:var(--navy)}.metric-label{font-size:13px;color:var(--muted)}.primary .metric-label,.primary .metric-note{color:#c3d9e1}.metric-value{font-size:34px;line-height:1.2;font-weight:700;letter-spacing:-.035em;margin:11px 0 7px;font-variant-numeric:tabular-nums}.metric-value.word{font-size:25px}.metric-note{font-size:12px;color:var(--muted)}.nav{display:flex;gap:21px;flex-wrap:wrap;margin:24px 0 8px;padding:12px 0;border-bottom:1px solid var(--line);font-size:13px}.section{padding-top:32px;scroll-margin-top:20px}.section-top{display:flex;justify-content:space-between;gap:20px;align-items:baseline;margin-bottom:15px}.section h2{font-size:23px;letter-spacing:-.02em;margin:0}.section-kicker{color:var(--muted);font-size:12px}.section-intro{color:var(--muted);margin:0 0 17px;max-width:920px}.card{border:1px solid var(--line);border-radius:12px;background:#fff;padding:24px}.timeline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0;margin-bottom:22px}.step{position:relative;padding:3px 24px 3px 0}.step+.step{padding-left:24px;border-left:1px solid var(--line)}.step-index{display:block;color:var(--teal);font-size:12px;font-weight:700;letter-spacing:.08em}.step h3{font-size:19px;margin:7px 0}.step p{color:var(--muted);font-size:13px;margin:0}.rule-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));border-top:1px solid var(--line);padding-top:18px;gap:17px 25px}.rule-title{font-size:12px;color:var(--muted);display:block;margin-bottom:4px}.rule-value{font-size:14px;font-weight:600}.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:#fff}table{border-collapse:collapse;width:100%;font-size:13px}th{background:#f5f9fa;color:#536b76;font-size:11px;letter-spacing:.02em;font-weight:600;white-space:nowrap}th,td{padding:16px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}tbody tr:last-child td{border-bottom:0}.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.strong{font-size:16px;font-weight:700;color:var(--teal)}.cell-note{font-size:11px;color:var(--muted);display:block;margin-top:3px;min-width:160px;max-width:245px;font-weight:400}.focus-row td{background:#eff8f6}.interval{font-size:12px}.table-note{font-size:12px;color:var(--muted);margin:11px 2px 0}.two-col{display:grid;grid-template-columns:1.1fr 1fr;gap:18px}.subhead{font-size:17px;margin:0 0 11px}.pill{display:inline-block;white-space:nowrap;font-size:11px;line-height:1.5;padding:3px 9px;background:#eff3f5;color:#516670;border-radius:20px}.status{background:var(--soft);border:1px solid #c4e1da;color:#235b54;padding:15px 18px;border-radius:10px;margin-bottom:16px}.status strong{display:block;margin-bottom:3px}.status p{margin:0;font-size:13px}.checks{list-style:none;padding:0;margin:0}.checks li{padding:11px 0 11px 24px;position:relative;border-bottom:1px solid #edf1f2;font-size:13px}.checks li:last-child{border-bottom:0}.checks li:before{content:"○";position:absolute;left:0;color:var(--amber);font-size:17px;top:8px}.date-range{white-space:nowrap;font-size:12px;font-variant-numeric:tabular-nums}.costs{display:grid;grid-template-columns:1fr 1fr;gap:13px 20px;margin:0}.costs div{padding-bottom:9px;border-bottom:1px solid #edf1f2}.costs dt{color:var(--muted);font-size:12px}.costs dd{font-size:15px;font-weight:600;margin:3px 0 0}.fineprint{font-size:12px;color:var(--muted)}.gate-list{margin:10px 0 0;padding-left:20px;font-size:13px}.gate-list li{margin:9px 0}.forward{margin-top:18px;background:var(--navy);color:#fff;padding:24px;border-radius:12px;display:grid;grid-template-columns:160px 1fr;gap:25px}.forward-date{font-size:24px;font-weight:700;letter-spacing:-.02em}.forward-label{color:#c3d9e1;font-size:12px}.forward h3{font-size:17px;margin:0 0 8px}.forward p{font-size:13px;color:#d6e5ea;margin:0}.empty{text-align:center;color:var(--muted)}details{background:#fff;border:1px solid var(--line);border-radius:10px;margin-top:17px}summary{cursor:pointer;padding:15px 18px;font-size:13px;font-weight:600}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:0 18px 20px;color:#4e6671;font-size:11px;line-height:1.65;font-family:Consolas,monospace}.identity{margin-top:25px;border-top:1px solid var(--line);padding-top:17px;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.identity p{margin:5px 0}.footer{display:flex;justify-content:space-between;gap:15px;margin-top:18px;color:var(--muted);font-size:11px}
@media(max-width:900px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.two-col{grid-template-columns:1fr}.rule-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.section-top{display:block}.section-kicker{display:block;margin-top:4px}.topbar-inner{padding:13px 22px}.wrap{padding:27px 22px 40px}}
@media(max-width:560px){body{font-size:14px}.topbar-inner{padding:12px 16px}.topbar-inner span:last-child{letter-spacing:0}.wrap{padding:24px 16px 35px}.hero h1{font-size:29px}.lede{font-size:14px}.metrics{gap:10px}.metric{padding:16px;min-height:148px}.metric-value{font-size:30px}.metric-value.word{font-size:22px}.timeline{grid-template-columns:1fr}.step{padding:0 0 17px}.step+.step{padding:17px 0;border-left:0;border-top:1px solid var(--line)}.rule-grid{grid-template-columns:1fr 1fr;gap:14px}.card{padding:19px}.section{padding-top:27px}.section h2{font-size:21px}.nav{gap:13px;font-size:12px}.costs{gap:11px}.forward{grid-template-columns:1fr;gap:12px;padding:20px}.footer{display:block}.notice{padding:15px}.notice strong{font-size:15px}}
@media print{body{background:white;font-size:11px}.topbar{background:#fff;color:#132c3a;border-bottom:1px solid #aaa}.brand{color:#132c3a}.wrap{max-width:none;padding:20px 0}.nav{display:none}.card,.table-wrap,.metric,.notice{break-inside:avoid}.section{padding-top:22px}.section h2{font-size:19px}.metric.primary,.forward{background:#edf3f5;color:#132c3a}.primary .metric-label,.primary .metric-note,.forward p,.forward-label{color:#536b76}.metric{min-height:130px}.metric-value{font-size:28px}.table-wrap{overflow:visible}th,td{padding:10px 7px}details{display:none}.footer{font-size:9px}}
</style></head><body>
<div class="topbar"><div class="topbar-inner"><span class="brand">A股研究 / 净盈利策略</span><span>基准日 ''' + _e(base) + ''' · 研究验收版</span></div></div>
<main class="wrap"><header class="hero"><p class="eyebrow">STRATEGY RESEARCH · COST-AWARE</p><h1>把“上涨预测”升级为<br>可核验的净盈利策略</h1><p class="lede">按每笔交易扣除费用和滑点后的盈亏来评估；信号在收盘后确定，条件不足时允许空仓。历史表现、预测分数与真实成交记录分别列示。</p><div class="chips"><span class="chip">目标：真实净盈利胜率 &gt;80%</span><span class="chip">允许空仓</span><span class="chip amber">正式交易尚未通过验收</span></div></header>
<aside class="notice"><strong>尚未获得真实交易净胜率超过 80% 的证据</strong><p>本次为日线数据研究。即使某段历史结果或模型分数超过 80%，也不能据此声称真实账户已达到目标。正式候选暂不开放。</p></aside>
<div class="metrics"><div class="metric primary"><div class="metric-label">真实交易净盈利胜率</div><div class="metric-value">''' + real_rate + '''</div><div class="metric-note">待真实成交及实际费用验证</div></div><div class="metric"><div class="metric-label">固定阈值 · 历史影子胜率</div><div class="metric-value">''' + _pct(shadow.get("win_rate")) + '''</div><div class="metric-note">''' + _number(shadow.get("n_trades")) + ''' 笔已平仓 / ''' + _number(shadow.get("n_unresolved")) + ''' 笔未解决</div></div><div class="metric"><div class="metric-label">正式交易候选</div><div class="metric-value word">''' + _e(candidate_state) + '''</div><div class="metric-note">须同时通过收益、样本与证据门槛</div></div><div class="metric"><div class="metric-label">本次历史研究规模</div><div class="metric-value">''' + _number(summary.get("history_symbols")) + '''<span style="font-size:14px;font-weight:400"> 只</span></div><div class="metric-note">''' + _number(summary.get("history_signal_rows")) + ''' 条历史信号样本</div></div></div>
<nav class="nav" aria-label="报告导航"><a href="#rules">交易与过滤规则</a><a href="#results">真实研究结果</a><a href="#selection">阈值选择</a><a href="#dates">时间分段</a><a href="#acceptance">正式验收</a></nav>
<section class="section" id="rules"><div class="section-top"><h2>01 / 先固定交易过程</h2><span class="section-kicker">按先后发生的可用信息决策</span></div><div class="card"><div class="timeline"><div class="step"><span class="step-index">T · 收盘后</span><h3>生成并冻结信号</h3><p>只使用当时及更早的数据。预测目标改为费用后的净盈利，不再用次日收盘上涨代替。</p></div><div class="step"><span class="step-index">T+1 · 下一交易日</span><h3>以开盘价研究买入</h3><p>每笔预算 ''' + _number(protocol.get("position_budget_cny")) + ''' 元，''' + _number(protocol.get("entry_lot_shares")) + ''' 股整手，买入成本计入预算。</p></div><div class="step"><span class="step-index">T+2 · 再下一交易日</span><h3>以收盘价研究卖出</h3><p>遵守买入后下一交易日才可卖出的日期顺序；不假设当天止盈止损成交。</p></div></div><div class="rule-grid"><div><span class="rule-title">基础股票条件</span><span class="rule-value">信号日收盘价 5–10 元 · 至少 250 根历史日线</span></div><div><span class="rule-title">流动性</span><span class="rule-value">近 20 日成交额中位数 ≥ ''' + _number(filters.get("median_amount20_min")) + ''' 元</span></div><div><span class="rule-title">避免追涨</span><span class="rule-value">近 5 日涨幅 ≤ ''' + _pct(filters.get("ret5_max")) + '''；单日涨跌幅绝对值 ≤ ''' + _pct(filters.get("abs_ret1_max")) + '''</span></div><div><span class="rule-title">市场强弱</span><span class="rule-value">上涨股票占比 ≥ ''' + _pct(filters.get("market_breadth_min")) + '''；等权平均涨跌幅 ≥ ''' + _pct(filters.get("market_mean_return_min")) + '''</span></div><div><span class="rule-title">持仓约束</span><span class="rule-value">最多 ''' + _number(protocol.get("maximum_positions")) + ''' 仓；同一股票不重复占仓</span></div><div><span class="rule-title">无法判断成交时</span><span class="rule-value">一字价、缺失或冲突日线保留为未解决；继续占用仓位</span></div></div></div><p class="table-note">T+1 / T+2 表示观察到的后续市场交易日。本次尚未完成官方交易日历核验；日线开收盘价只是研究参考价，不是实际成交保证。卖出收盘回笼的资金不用于同一天开盘再买入。</p></section>
<section class="section" id="results"><div class="section-top"><h2>02 / 费用后的历史诊断</h2><span class="section-kicker">所有数字来自本次运行结果</span></div><p class="section-intro">净胜率＝净盈利笔数 ÷（已平仓笔数＋未解决笔数）。未解决交易按非胜计入分母；空仓不算盈利或交易。没有交易时显示“—”，不显示虚假的 0% 或 100%。</p><div class="table-wrap"><table><thead><tr><th>策略情景</th><th class="num">总尝试</th><th class="num">已平仓</th><th class="num">未解决</th><th class="num">保守净胜率</th><th class="num">95% 区间</th><th class="num">平均净收益</th><th class="num">入场日期数</th></tr></thead><tbody>''' + diagnostic_rows + '''</tbody></table></div><p class="table-note">区间按入场日期整组重采样，保留同日选股的关联；不消除全部跨日依赖。平均净收益仅基于已平仓交易，不等同组合收益率，也不包含未解决仓位的未知损益。固定 80% 指模型预测分数门槛，不能解释为已实现胜率。</p></section>
<section class="section" id="selection"><div class="section-top"><h2>03 / 达不到要求，就不启用</h2><span class="section-kicker">阈值只在选择段比较</span></div><div class="status"><strong>''' + _e(policy_state) + '''</strong><p>''' + policy_detail + '''</p></div><p class="section-intro">研究门槛同时要求：至少 ''' + _number(min_trades) + ''' 笔已平仓、''' + _number(min_dates) + ''' 个不同入场日期、无未解决交易、净胜率及日期分组 95% 区间下界均严格高于 ''' + _pct(required_lower, 0) + '''，且平均净收益为正。</p><div class="table-wrap"><table><thead><tr><th class="num">预测分数门槛</th><th class="num">已平仓</th><th class="num">未解决</th><th class="num">入场日期数</th><th class="num">保守净胜率</th><th class="num">95% 区间</th><th>研究判定</th></tr></thead><tbody>''' + selection_rows + '''</tbody></table></div><p class="table-note">即使通过上述研究门槛，也只获得研究观察资格；真实交易验收还必须补齐正式证据。若未通过，后续历史诊断不会反向修改阈值。</p></section>
<section class="section" id="dates"><div class="section-top"><h2>04 / 分开训练、选择与诊断</h2><span class="section-kicker">按时间顺序切分，并隔离跨段结果</span></div><p class="section-intro">分段边界要求前一段交易的结果日期早于下一段信号日期。旧历史在之前的研究中已被查看，本次四段结果都属于开发诊断，不能包装为全新、未见过的样本外验证。</p><div class="table-wrap"><table><thead><tr><th>时间段 / 用途</th><th>信号日期范围</th><th class="num">信号日期数</th><th class="num">已知结果样本</th><th class="num">未解决样本</th><th>最晚结果日期</th></tr></thead><tbody>''' + split_rows + '''</tbody></table></div><p class="table-note">这里的“样本”是股票与信号日的组合，数量不等于真正成交笔数，也不等于独立交易日期数。</p></section>
<section class="section" id="acceptance"><div class="section-top"><h2>05 / 正式验收还缺什么</h2><span class="section-kicker">80% 是目标，证据决定是否通过</span></div><div class="two-col"><div class="card"><h3 class="subhead">当前证据缺口</h3><ul class="checks">''' + blockers + '''</ul><ol class="gate-list"><li>冻结规则后收集新的逐笔记录，不用回填旧历史充当新验证。</li><li>逐笔核对买卖成交、实际费用、可交易状态及当时可知的信息。</li><li>满足样本、日期、收益与区间门槛后，再验收真实净盈利胜率。</li></ol></div><div class="card"><h3 class="subhead">本次费用为研究假设</h3><dl class="costs"><div><dt>买卖佣金率 · 各侧</dt><dd>''' + _pct(costs.get("commission_rate"), 3) + '''</dd></div><div><dt>每侧最低佣金</dt><dd>''' + _number(costs.get("min_commission"), 2, " 元") + '''</dd></div><div><dt>过户费率 · 各侧</dt><dd>''' + _pct(costs.get("transfer_rate"), 3) + '''</dd></div><div><dt>卖出印花税率</dt><dd>''' + _pct(costs.get("sell_tax_rate"), 3) + '''</dd></div><div><dt>滑点 · 买卖每侧</dt><dd>''' + _number(costs.get("slippage_bps"), 0, " 个基点") + '''</dd></div><div><dt>压力情景 · 每侧滑点</dt><dd>20 个基点</dd></div></dl><p class="fineprint">1 个基点＝0.01%。滑点分别加在买入与卖出方向，费用逐项扣除；实际账户验收必须使用实际成交与费用替换这些假设。</p></div></div><div class="forward"><div><div class="forward-label">新信号最早开始于</div><div class="forward-date">''' + _e(forward) + '''</div></div><div><h3>先积累前向记录，再判断是否达到目标</h3><p>该日期仅为协议允许的最早信号日期，不表示系统已部署或已有成交。应从规则冻结后实际取得的首个有效信号开始计数；允许长时间空仓。若按 T 日收盘选股，完整结果还需等后续买入和卖出日期发生并有证据支持。</p></div></div></section>
<details><summary>查看冻结的研究协议与参数</summary><pre>''' + protocol_json + '''</pre></details><div class="identity"><p>生成时间：''' + generated + '''　|　数据基准日：''' + _e(base) + '''</p><p>研究协议 SHA256：''' + report_identity + '''</p><p>输入数据库 SHA256：''' + database_identity + '''</p></div><footer class="footer"><span>日线研究尚未达到真实交易胜率 &gt;80% 的验证标准。</span><span>报告不构成下单指令；历史区间不能保证未来胜率。</span></footer></main></body></html>'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, help="Path to the generated summary.json")
    parser.add_argument("--output", required=True, help="Destination HTML path")
    args = parser.parse_args()
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    Path(args.output).write_text(render_report(summary), encoding="utf-8")


if __name__ == "__main__":
    main()
