"""Render a standalone Chinese report for the next-session gross-return study."""
from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime, timedelta, timezone
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
    return _e(f"{number:,.{decimals}f}{suffix}") if math.isfinite(number) else "—"


def _pct(value: Any, decimals: int = 1) -> str:
    try:
        return _number(float(value) * 100, decimals, "%") if not isinstance(value, bool) else "—"
    except (TypeError, ValueError):
        return "—"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _local_timestamp(value: Any) -> str:
    if not value:
        return "未提供"
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    except (TypeError, ValueError):
        pass
    return str(value)


def _ci(metrics: Mapping[str, Any]) -> str:
    interval = _mapping(metrics.get("date_block_bootstrap")).get("win_rate_ci95")
    if isinstance(interval, (list, tuple)) and len(interval) == 2:
        return _pct(interval[0]) + " – " + _pct(interval[1])
    return "—"


def _parameters(parameters: Mapping[str, Any]) -> str:
    rule = {"ordinary": "普通走势", "bullish": "偏强走势", "pullback": "回调后企稳"}.get(
        str(parameters.get("entry_rule")), str(parameters.get("entry_rule") or "未提供"))
    threshold = parameters.get("threshold")
    threshold_note = "只按模型排序" if threshold == 0 and not isinstance(threshold, bool) else "分数门槛 " + _pct(threshold, 0)
    return (_e(rule) + " · 止盈 " + _pct(parameters.get("target")) + " · 止损 "
            + _pct(parameters.get("stop")) + " · " + threshold_note)


def _comparison_row(title: str, note: str, metrics: Mapping[str, Any], focus: bool = False) -> str:
    return ('<tr' + (' class="focus"' if focus else '') + '><td><b>' + _e(title)
            + '</b><small>' + _e(note) + '</small></td><td class="num">'
            + _number(metrics.get("n_attempts")) + '</td><td class="num">'
            + _number(metrics.get("n_unresolved")) + '</td><td class="num strong">'
            + _pct(metrics.get("win_rate")) + '</td><td class="num">'
            + _pct(metrics.get("ge3_rate")) + '</td><td class="num">'
            + _pct(metrics.get("ge6_rate")) + '</td><td class="num">'
            + _pct(metrics.get("mean_closed_gross_return"), 2) + '</td><td class="num ci">'
            + _ci(metrics) + '</td></tr>')


_CSS = r"""
:root{--ink:#183441;--muted:#5c737c;--navy:#143d4b;--teal:#087f7b;--paper:#f4f6f5;--line:#dce5e3;--soft:#e9f4f0;--amber:#79511c;--warm:#fff3de}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;color:var(--ink);background:var(--paper);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}a{color:var(--teal);text-decoration:none}a:hover{text-decoration:underline}a:focus-visible,button:focus-visible,summary:focus-visible{outline:3px solid #3bb5ae;outline-offset:4px}.top{background:var(--navy);color:#d0e1e5}.top>div{max-width:1220px;margin:auto;padding:14px 32px;display:flex;justify-content:space-between;gap:20px;font-size:12px;letter-spacing:.05em}.top b{color:white}.wrap{max-width:1220px;margin:auto;padding:34px 32px 48px}.hero{padding-bottom:25px;border-bottom:1px solid var(--line)}.eyebrow{font-size:11px;font-weight:700;letter-spacing:.13em;color:var(--teal);margin:0 0 10px}h1{font-size:clamp(30px,4vw,45px);line-height:1.26;letter-spacing:-.025em;margin:0 0 17px;max-width:930px}.lede{margin:0;max-width:950px;color:var(--muted);font-size:16px}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}.chip{border:1px solid #cdded8;border-radius:18px;padding:3px 11px;font-size:12px;background:white;color:#49666e}.chip.warm{background:var(--warm);color:var(--amber);border-color:#ead7b2}.notice{margin:24px 0;border-left:4px solid #b78538;background:var(--warm);padding:16px 20px;color:#674d28}.notice b{display:block;font-size:16px}.notice p{margin:3px 0 0;font-size:13px}.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.metric{border:1px solid var(--line);border-radius:12px;background:white;padding:20px;min-height:161px}.metric.primary{background:var(--navy);color:white;border-color:var(--navy)}.metric-label{font-size:12px;color:var(--muted)}.primary .metric-label,.primary .metric-note{color:#cadee3}.metric-value{font-size:34px;font-weight:700;line-height:1.2;letter-spacing:-.035em;margin:13px 0 8px;font-variant-numeric:tabular-nums}.metric-note{font-size:11px;color:var(--muted)}nav{display:flex;gap:22px;flex-wrap:wrap;font-size:13px;margin:23px 0 0;padding:12px 0;border-bottom:1px solid var(--line)}section{padding-top:32px;scroll-margin-top:20px}.section-head{display:flex;justify-content:space-between;align-items:baseline;gap:22px;margin-bottom:15px}h2{font-size:23px;letter-spacing:-.02em;margin:0}.kicker{font-size:12px;color:var(--muted)}.intro{color:var(--muted);margin:0 0 17px;max-width:980px}.card{background:white;border:1px solid var(--line);border-radius:12px;padding:23px}.timeline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}.step+.step{border-left:1px solid var(--line);padding-left:24px}.step-index{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--teal)}h3{font-size:18px;margin:6px 0 9px}.step p{font-size:13px;color:var(--muted);margin:0}.rule-grid{border-top:1px solid var(--line);margin-top:22px;padding-top:18px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px 25px}.rule-grid span{display:block;color:var(--muted);font-size:11px}.rule-grid b{font-size:13px;font-weight:600}.table-wrap{overflow-x:auto;background:white;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:15px 13px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{background:#f3f8f6;color:#536d76;font-size:11px;font-weight:600;white-space:nowrap}tbody tr:last-child td{border-bottom:0}td small{display:block;color:var(--muted);font-size:11px;max-width:245px;min-width:160px;font-weight:400;margin-top:4px}.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.strong{font-size:16px;font-weight:700;color:var(--teal)}.ci{font-size:11px}.focus td{background:#edf7f2}.note{font-size:12px;color:var(--muted);margin:10px 2px 0}.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px}.three-col{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.measure{border-top:3px solid #95bfb2}.measure b{display:block;font-size:18px;margin-bottom:7px}.measure p{font-size:13px;color:var(--muted);margin:0}.measure .formula{color:var(--ink);font-size:12px;margin-top:12px;padding-top:12px;border-top:1px solid var(--line)}.status{background:var(--soft);border:1px solid #c9e0d6;padding:16px 18px;border-radius:10px;margin-bottom:17px;color:#28564c}.status strong{display:block}.status p{font-size:13px;margin:4px 0 0}.filters{display:flex;align-items:center;flex-wrap:wrap;gap:9px;margin:0 0 14px;font-size:12px;color:var(--muted)}.filters button{border:1px solid #c9dad3;border-radius:18px;padding:5px 12px;background:white;color:#365b61;cursor:pointer;font:inherit}.filters button[aria-pressed=true]{background:var(--navy);color:white;border-color:var(--navy)}.pill{display:inline-block;border-radius:18px;background:#eff3f1;padding:3px 9px;color:#5b706f;font-size:11px;white-space:nowrap}.pill.pass{background:#e1f1e7;color:#2d694e}.empty{text-align:center;color:var(--muted);padding:24px}.subhead{font-size:17px;margin:0 0 13px}.definition{font-size:13px;color:var(--muted);margin:0 0 13px}.checks{list-style:none;padding:0;margin:0}.checks li{padding:10px 0 10px 23px;position:relative;border-bottom:1px solid #edf2ef;font-size:13px}.checks li:last-child{border-bottom:0}.checks li:before{content:"○";position:absolute;left:0;color:#a47736;top:7px;font-size:18px}.mini{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:0}.mini div{padding-bottom:9px;border-bottom:1px solid var(--line)}.mini dt{color:var(--muted);font-size:11px}.mini dd{font-size:22px;font-weight:600;margin:3px 0 0;font-variant-numeric:tabular-nums}.mini dd.word{font-size:16px}.source{font-size:11px;color:var(--muted);margin-top:13px}.source a{margin-right:9px}details{margin-top:19px;background:white;border:1px solid var(--line);border-radius:10px}summary{padding:14px 18px;cursor:pointer;font-size:13px;font-weight:600}pre{margin:0;padding:0 18px 19px;white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.7 Consolas,monospace;color:#526a71}.identity{border-top:1px solid var(--line);margin-top:24px;padding-top:14px;font-size:11px;color:var(--muted);overflow-wrap:anywhere}.identity p{margin:4px 0}.footer{display:flex;justify-content:space-between;gap:18px;font-size:11px;color:var(--muted);margin-top:18px}[hidden]{display:none!important}
@media(max-width:980px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.two-col{grid-template-columns:1fr}.three-col{grid-template-columns:1fr}.rule-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.section-head{display:block}.kicker{display:block;margin-top:4px}.wrap{padding:27px 22px 40px}.top>div{padding:13px 22px}}
@media(max-width:560px){body{font-size:14px}.wrap{padding:24px 16px 34px}.top>div{padding:12px 16px;font-size:11px}.hero h1{font-size:29px}.lede{font-size:14px}.metrics{gap:10px}.metric{padding:16px;min-height:151px}.metric-value{font-size:29px}.timeline{grid-template-columns:1fr;gap:18px}.step+.step{border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:16px}.card{padding:19px}.rule-grid{gap:14px}.section-head h2{font-size:21px}nav{gap:14px}.footer{display:block}.notice{padding:14px 16px}.notice b{font-size:15px}section{padding-top:27px}}
@media print{body{background:white;font-size:11px}.wrap{max-width:none;padding:20px 0}.top{background:white;color:var(--ink);border-bottom:1px solid #aaa}.top b{color:var(--ink)}nav,.filters,details{display:none}.card,.metric,.notice,.table-wrap{break-inside:avoid}.metric.primary{background:#eef5f2;color:var(--ink)}.primary .metric-label,.primary .metric-note{color:var(--muted)}.metric{min-height:130px}.metric-value{font-size:28px}.table-wrap{overflow:visible}th,td{padding:10px 7px}.section-head h2{font-size:19px}section{padding-top:22px}.footer{font-size:9px}}
"""


def render_report(summary: Mapping[str, Any]) -> str:
    """Render only reported metrics; missing values remain visibly unavailable."""
    protocol = _mapping(summary.get("protocol"))
    definition = _mapping(protocol.get("strategy_definition"))
    policy = _mapping(summary.get("policy"))
    diagnostic = _mapping(summary.get("diagnostic"))
    splits = _mapping(summary.get("split"))
    reference = _mapping(diagnostic.get("fixed_reference"))
    active = policy.get("active") is True
    focus = _mapping(diagnostic.get("selected_policy")) if active else reference
    focus_title = "选择段入选规则" if active else "固定候选影子规则"
    selected_params = _mapping(policy.get("selected_parameters")) if active else _mapping(policy.get("fallback_parameters"))
    base = summary.get("base_trade_date") or "未提供"
    comparison = "".join(_comparison_row(title, note, _mapping(diagnostic.get(key)) if key != "selected_policy" or active else {}, emphasize) for key, title, note, emphasize in (
        ("baseline", "基准对照", "移除信号过滤，仍按相同 D2 止盈止损退出。", False),
        ("fixed_reference", "固定候选影子规则", "普通走势 / 3% 止盈 / 2% 止损 / 50% 分数门槛。", not active),
        ("selected_policy", "选择段入选规则", "只在选择段挑选；未达研究门槛时保留为空。", active),
    ))
    grid = summary.get("selection_grid") or []
    development_note = ""
    if definition.get("development_revision_reason") == "selection_count_max3_insufficient":
        development_note = (
            '<p class="note">本次为扩展排序研究轮次：首轮选择段每组最多只有 3 笔，未选出规则；因此本轮只在同一历史开发数据上扩大分数范围，'
            '加入“只按模型排序”以及 35%、45% 门槛，保留 50% 门槛。固定影子对照仍为原 50% 门槛。'
            '本轮比较 ' + _number(len(grid)) + ' 组，累计已考察 '
            + _number(definition.get("cumulative_distinct_development_variants"))
            + ' 组不同开发参数。扩大范围旨在增加可观察样本，不能据此声称胜率已提高；多轮开发还会增加挑选偏差。'
            '真实 80% 胜率的 200 笔、60 个日期及区间下界标准保持不变，仍需新的成交证据。</p>'
        )
    selection_rows = ""
    for item in grid:
        item = _mapping(item)
        params, metrics = _mapping(item.get("parameters")), _mapping(item.get("metrics"))
        eligible = item.get("eligible_research") is True
        selection_rows += ('<tr data-target="' + _e(params.get("target")) + '"><td>' + _parameters(params)
                           + '</td><td class="num">' + _number(metrics.get("n_attempts"))
                           + '</td><td class="num">' + _number(metrics.get("n_unresolved"))
                           + '</td><td class="num">' + _pct(metrics.get("win_rate"))
                           + '</td><td class="num">' + _pct(metrics.get("ge3_rate"))
                           + '</td><td class="num ci">' + _ci(metrics)
                           + '</td><td><span class="pill' + (' pass' if eligible else '') + '">'
                           + ('通过研究门槛' if eligible else '未通过') + '</span></td></tr>')
    if not selection_rows:
        selection_rows = '<tr><td colspan="7" class="empty">尚无选择段结果。</td></tr>'
    watch_rows = ""
    for value in summary.get("current_watchlist") or []:
        item = _mapping(value)
        probabilities = _mapping(item.get("probabilities")) or item
        entry_notes = item.get("entry_proxy_notes") or "需在开盘时核验"
        if isinstance(entry_notes, str) and entry_notes.startswith("T-close research observation only;"):
            entry_notes = "仅依据收盘信息进入观察；次日须核验开盘价、买价上限、涨跌停状态和排队成交条件，当前没有已确认的买入价格。"
        if isinstance(entry_notes, (list, tuple)):
            entry_notes = "；".join(str(note) for note in entry_notes)
        watch_rows += ('<tr><td><b>' + _e(item.get("symbol") or "—") + '</b><small>'
                       + _e(item.get("name") or "—") + '</small></td><td class="num">'
                       + _number(item.get("close"), 2) + '</td><td class="num">'
                       + _pct(probabilities.get("p_profit")) + '</td><td class="num">'
                       + _pct(probabilities.get("p_ge3")) + '</td><td class="num">'
                       + _pct(probabilities.get("p_ge6")) + '</td><td class="num">'
                       + _pct(probabilities.get("p_day1_green")) + '</td><td><small>'
                       + _e(entry_notes) + '</small></td></tr>')
    watchlist = (
        '<section id="watchlist"><div class="section-head"><h2>下一交易日观察</h2><span class="kicker">当前研究分数 · 未核验可交易状态</span></div>'
        '<p class="intro">这些股票只进入观察表。四个分数都以参考开盘价买入的研究标签计算，尚不包含确定的下单价格；须等下一交易日核对实际开盘、跳空与涨跌停状态后才能判断是否有入场条件。</p>'
        '<div class="table-wrap"><table><thead><tr><th>股票</th><th class="num">基准日收盘 / 元</th><th class="num">D2 收盘毛盈利分数</th><th class="num">D2 收盘 ≥3% 分数</th><th class="num">D2 收盘 ≥6% 分数</th><th class="num">D1 上涨分数</th><th>入场核验</th></tr></thead><tbody>'
        + watch_rows + '</tbody></table></div><p class="note">模型分数不是已验证概率，不代表下一交易日必涨、涨停或可成交。该表不属于正式交易候选。</p></section>'
        if watch_rows else
        '<section id="watchlist"><div class="section-head"><h2>下一交易日观察</h2><span class="kicker">允许没有候选</span></div><div class="card"><p class="definition">当前没有列入观察表的股票；条件不足时保持为空，不为凑名单放宽门槛。</p></div></section>'
    )
    split_rows = ""
    for key, title, purpose in (("train", "01 · 训练", "拟合四个价格目标"),
                                ("calibration", "02 · 校准", "校准模型分数"),
                                ("selection", "03 · 规则选择", "有限网格只在此段比较"),
                                ("diagnostic", "04 · 历史诊断", "报告结果，不再反向选优")):
        part = _mapping(splits.get(key))
        split_rows += ('<tr><td><b>' + _e(title) + '</b><small>' + _e(purpose)
                       + '</small></td><td>' + _e(part.get("first_input_date") or part.get("first_signal_date") or "—")
                       + ' → ' + _e(part.get("last_input_date") or part.get("last_signal_date") or "—")
                       + '</td><td class="num">' + _number(part.get("independent_dates"))
                       + '</td><td class="num">' + _number(part.get("labeled_closed_rows"))
                       + '</td><td class="num">' + _number(part.get("unresolved_rows")) + '</td></tr>')
    green = _mapping(focus.get("first_day_green_conditional"))
    not_green = _mapping(focus.get("first_day_not_green_conditional"))
    condition_rows = "".join(
        '<tr><td>' + title + '</td><td class="num">' + _number(metrics.get("n"))
        + '</td><td class="num">' + _pct(metrics.get("win_rate")) + '</td><td class="num">'
        + _pct(metrics.get("ge3_rate")) + '</td><td class="num">' + _pct(metrics.get("ge6_rate")) + '</td></tr>'
        for title, metrics in (("第一天收盘高于买入参考价", green), ("第一天未上涨", not_green)))
    blocker_labels = {
        "official_point_in_time_universe_status_limits_actions_unverified": "当时可知的股票池、停复牌、风险状态、涨跌停价和公司行动尚未完整核验。",
        "actual_execution_evidence_unavailable": "尚无逐笔成交证据，日线参考价格无法证明实际买入或卖出。",
        "official_pit_universe_status_limits_actions_unverified": "当时可知的股票池、停复牌、风险状态、涨跌停价和公司行动尚未完整核验。",
        "broker_fills_and_actual_fee_evidence_unavailable": "尚无逐笔成交证据，日线参考价格无法证明实际买入或卖出。",
        "new_forward_execution_track_record_unavailable": "冻结规则之后的新成交记录尚未积累，不能验证真实胜率。",
    }
    blockers = "".join('<li>' + _e(blocker_labels.get(str(value), value)) + '</li>' for value in summary.get("formal_blockers") or [])
    if not blockers:
        blockers = '<li>日线行情无法证明委托排队、成交数量与先后顺序。</li><li>需补齐历史时点的股票状态、涨跌停价与公司行动证据。</li><li>需以冻结规则之后的新成交记录验收真实胜率。</li>'
    policy_title = "已在选择段选定研究规则" if active else "尚无规则通过研究门槛"
    policy_note = ("本页列示其在后续历史段的诊断；入选研究规则仍不代表可实盘。" if active else
                   "保留固定候选进行影子观察。没有通过门槛的规则时，正式候选继续为空。")
    protocol_json = _e(json.dumps(protocol, ensure_ascii=False, indent=2, allow_nan=False))
    sources = (
        '<a href="https://docs.static.szse.cn/www/lawrules/rule/trade/W020260424690713155663.pdf" target="_blank" rel="noopener">深交所 2026 交易规则 §3.1.4、§3.4.2</a>'
        '<a href="https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml" target="_blank" rel="noopener">上交所现行规则及生效日期</a>'
        '<a href="https://www.sse.com.cn/services/tradingtech/development/c/10816505/files/8006a51174524e7cae832568b5e726a6.pdf" target="_blank" rel="noopener">上交所 2026 涨跌幅调整说明</a>'
    )
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light"><title>A股隔日策略 · 毛收益研究</title><style>' + _CSS
            + '</style></head><body><div class="top"><div><b>A股研究 / 隔日毛收益策略</b><span>数据基准日 ' + _e(base)
            + '</span></div></div><main class="wrap"><header class="hero"><p class="eyebrow">NEXT-SESSION STRATEGY · GROSS RETURN</p>'
            '<h1>先买得进，再研究隔日上涨<br>把 3%–6% 作为止盈目标</h1><p class="lede">本版按你的新要求，研究收益不扣交易费用。'
            '主胜率看第二天退出后是否有毛利润，同时单独报告盈利至少 3%、至少 6% 的比例；不要求买入第一天先上涨。</p>'
            '<div class="chips"><span class="chip">主目标：隔日毛盈利胜率 &gt;80%</span><span class="chip">止盈网格：3% / 4.5% / 6%</span>'
            '<span class="chip warm">正式交易未开放</span></div></header><aside class="notice"><b>3%–6% 是交易目标，不是每笔收益保证</b>'
            '<p>以下是历史日线代理研究。毛盈利、达到目标收益、盘中最高价触及目标是三件不同的事。第一天收盘后才知道的涨跌，不能回头用来挑掉已经买入的亏损交易。</p></aside>'
            '<div class="metrics"><div class="metric primary"><div class="metric-label">' + _e(focus_title) + ' · 毛盈利率</div><div class="metric-value">'
            + _pct(focus.get("win_rate")) + '</div><div class="metric-note">全体已入场为分母；未解决按非胜计</div></div><div class="metric"><div class="metric-label">实际退出毛收益 ≥3%</div><div class="metric-value">'
            + _pct(focus.get("ge3_rate")) + '</div><div class="metric-note">达到 3% 的交易数 / 全体入场</div></div><div class="metric"><div class="metric-label">实际退出毛收益 ≥6%</div><div class="metric-value">'
            + _pct(focus.get("ge6_rate")) + '</div><div class="metric-note">不同于盘中价格曾经触及 6%</div></div><div class="metric"><div class="metric-label">真实成交胜率</div><div class="metric-value">'
            + _pct(summary.get("actual_win_rate")) + '</div><div class="metric-note">当前日线研究不能验收真实成交胜率</div></div></div>'
            '<nav aria-label="报告导航"><a href="#process">交易规则</a><a href="#results">研究结果</a><a href="#selection">规则比较</a><a href="#conditional">第一天涨跌</a><a href="#evidence">验证边界</a></nav>'
            '<section id="process"><div class="section-head"><h2>01 / 买入条件与退出过程</h2><span class="kicker">按信息真正出现的顺序决策</span></div><div class="card"><div class="timeline">'
            '<div class="step"><span class="step-index">T · 收盘后</span><h3>筛选普通、偏强或回调走势</h3><p>只用已知日线和当时模型分数。预测第二天退出毛盈利及 3%、6% 目标，不把买入后的上涨当成买入前条件。</p></div>'
            '<div class="step"><span class="step-index">D1 · 下一交易日</span><h3>先检验开盘买入代理</h3><p>过大高开、价格超出区间时取消入场。封死或缺失行情的成交能力不能从日线确认，不视为买入成功。</p></div>'
            '<div class="step"><span class="step-index">D2 · 再下一交易日</span><h3>按冻结的止盈止损退出</h3><p>目标 3%、4.5% 或 6%，止损 2% 或 3%；未触发时按兜底规则退出。买入当日不假设止盈或止损；跳空或退出受阻时，亏损可能超过止损比例。</p></div></div>'
            '<div class="rule-grid"><div><span>本页重点规则</span><b>' + _parameters(selected_params) + '</b></div><div><span>费用口径</span><b>不扣佣金、税费与滑点的毛收益研究</b></div>'
            '<div><span>无法买入</span><b>单列取消入场；不计为交易或盈利</b></div><div><span>买入后无法确认退出</span><b>保留未解决；不从胜率分母删除</b></div>'
            '<div><span>同日止盈与止损都触及</span><b>采用保守顺序，并单列乐观代理上界</b></div><div><span>第一天是否上涨</span><b>仅作事后分组观察，不是主策略入场要求</b></div></div></div>'
            '<p class="note">A股买入后的可卖日期须遵守交易规则；成交按价格和时间优先。这里的开盘价及目标触价都只是日线代理，不能证明实际排队成交。沪深 2026 规则自 7 月 6 日施行，跨期风险警示限制不能简单使用统一旧比例。</p><p class="source">官方依据：' + sources + '</p></section>'
            '<section id="results"><div class="section-head"><h2>02 / 毛盈利与目标达标分别比较</h2><span class="kicker">历史诊断段 · 不是新的未见样本</span></div>'
            '<div class="three-col"><div class="card measure"><b>毛盈利率</b><p>退出参考价高于买入参考价即为盈利；赚 0.1% 也属于此项。</p><p class="formula">毛收益 &gt;0 的笔数 / 全体已入场</p></div>'
            '<div class="card measure"><b>目标达标率</b><p>按退出规则计算，交易最终毛收益至少 3% 或至少 6% 的比例。</p><p class="formula">实际退出毛收益 ≥目标 / 全体已入场</p></div>'
            '<div class="card measure"><b>盘中触价率</b><p>第二天最高价曾到目标价，仅表示价格出现过，不能证明委托成交。</p><p class="formula">D2 最高价 ≥买入价×(1＋目标) / 全体已入场</p></div></div>'
            '<div class="table-wrap" style="margin-top:18px"><table><thead><tr><th>策略</th><th class="num">已入场</th><th class="num">未解决</th><th class="num">毛盈利率</th><th class="num">收益 ≥3%</th><th class="num">收益 ≥6%</th><th class="num">已平仓均值</th><th class="num">盈利率 95% 区间</th></tr></thead><tbody>'
            + comparison + '</tbody></table></div><p class="note">胜率与目标达标率的分母均含未解决交易；取消入场不算交易。3% 止盈规则通常较早退出，因此不能把它的 ≥6% 达标率与 6% 止盈规则等同解释。均值仅计算已平仓，不能替代组合收益，也未计未解决仓位的未知损益。区间按入场日期分组重采样，不消除全部跨日关联。</p>'
            '<div class="two-col" style="margin-top:18px"><div class="card"><h3 class="subhead">' + _e(focus_title) + ' · 交易去向</h3><dl class="mini"><div><dt>信号候选</dt><dd>'
            + _number(focus.get("n_signals")) + '</dd></div><div><dt>取消入场</dt><dd>' + _number(focus.get("n_cancelled")) + '</dd></div><div><dt>已平仓</dt><dd>'
            + _number(focus.get("n_closed")) + '</dd></div><div><dt>未解决</dt><dd>' + _number(focus.get("n_unresolved")) + '</dd></div></dl></div>'
            '<div class="card"><h3 class="subhead">触价与日线先后歧义</h3><dl class="mini"><div><dt>D2 触及 +3%</dt><dd>' + _pct(focus.get("day2_touch3_rate"))
            + '</dd></div><div><dt>D2 触及 +6%</dt><dd>' + _pct(focus.get("day2_touch6_rate")) + '</dd></div><div><dt>止盈止损先后不明</dt><dd>'
            + _number(focus.get("ambiguity_count")) + '</dd></div><div><dt>乐观毛盈利率上界 · 代理</dt><dd>' + _pct(focus.get("optimistic_win_rate_upper_proxy"))
            + '</dd></div><div><dt>D2 收盘高于 D1 收盘</dt><dd>' + _pct(focus.get("day2_close_up_rate"))
            + '</dd></div></dl><p class="note">D2 自身上涨是相对 D1 收盘，毛盈利则相对 D1 买入参考价；两者不同。上界只展示日线顺序假设的敏感程度，不是有证据保证的成交概率。</p></div></div></section>'
            '<section id="selection"><div class="section-head"><h2>03 / 在选择段综合规则</h2><span class="kicker">' + _number(len(grid))
            + ' 组有限候选 · 禁止在诊断段继续挑最优</span></div><div class="status"><strong>' + _e(policy_title) + '</strong><p>' + _e(policy_note)
            + '</p></div><p class="intro">比较不同入场走势、止盈止损和模型分数。研究选优至少要求 '
            + _number(definition.get("research_selection_min_trades")) + ' 笔交易与 '
            + _number(definition.get("research_selection_min_entry_dates"))
            + ' 个入场日期，仅用来保留可观察的规则。它不同于真实胜率 80% 的验收：正式门槛至少 '
            + _number(definition.get("formal_min_trades")) + ' 笔交易、'
            + _number(definition.get("formal_min_entry_dates")) + ' 个日期，且日期分组 95% 区间下界严格高于 '
            + _pct(definition.get("formal_date_block_95pct_lower_strictly_above"), 0)
            + '。提高胜率同时检查每笔收益与样本分散度；多组挑选本身会产生偏差，结果只作研究。</p>'
            + development_note
            + '<div class="filters" aria-label="按止盈目标筛选"><span>查看止盈目标</span><button type="button" data-filter="all" aria-pressed="true">全部</button><button type="button" data-filter="0.03" aria-pressed="false">3%</button><button type="button" data-filter="0.045" aria-pressed="false">4.5%</button><button type="button" data-filter="0.06" aria-pressed="false">6%</button></div>'
            '<div class="table-wrap"><table><thead><tr><th>冻结参数</th><th class="num">已入场</th><th class="num">未解决</th><th class="num">毛盈利率</th><th class="num">收益 ≥3%</th><th class="num">95% 区间</th><th>研究判定</th></tr></thead><tbody id="selection-body">'
            + selection_rows + '</tbody></table></div><p class="note">分数门槛是模型的预测分数，不是已实现胜率。正式验收需在新记录中同时满足足够的交易笔数、不同日期、区间下界及可交易证据。</p></section>'
            '<section id="conditional"><div class="section-head"><h2>04 / 第一天下跌也留在总成绩里</h2><span class="kicker">辅助条件概率 · 不用于事后删除失败交易</span></div><p class="intro">第一天是否上涨，直到买入后才知道。下表只是观察第二天的分组表现，主策略仍以全部已入场记录计分；不能只保留第一天涨的交易来声称整体胜率提高。</p>'
            '<div class="table-wrap"><table><thead><tr><th>' + _e(focus_title) + ' / D1 分组</th><th class="num">交易数</th><th class="num">D2 退出毛盈利率</th><th class="num">收益 ≥3%</th><th class="num">收益 ≥6%</th></tr></thead><tbody>'
            + condition_rows + '</tbody></table></div><p class="note">第一天涨跌已知记录 ' + _number(focus.get("n_known_first_day")) + ' 笔；第一天上涨占全体入场比例 '
            + _pct(focus.get("first_day_green_rate")) + '。若执行“第一天跌则第二天先退出”，也必须在买入前冻结该分支，并把相应亏损计入整体结果。</p></section>'
            + watchlist
            + '<section id="evidence"><div class="section-head"><h2>05 / 保留研究价值，也保留证据边界</h2><span class="kicker">正式候选保持关闭</span></div><div class="table-wrap"><table><thead><tr><th>阶段 / 用途</th><th>信号日期范围</th><th class="num">日期数</th><th class="num">已知结果样本</th><th class="num">未解决样本</th></tr></thead><tbody>'
            + split_rows + '</tbody></table></div><p class="note">按时间切分，并清除跨段结果。该批历史在之前研究中已被查看，因此四段都属于开发与历史诊断；股票×日期样本数不等于独立交易笔数。</p>'
            '<div class="two-col" style="margin-top:18px"><div class="card"><h3 class="subhead">正式验证仍需补齐</h3><ul class="checks">' + blockers
            + '</ul></div><div class="card"><h3 class="subhead">本次口径与研究范围</h3><dl class="mini"><div><dt>历史股票数</dt><dd>'
            + _number(summary.get("history_symbols")) + '</dd></div><div><dt>历史信号样本</dt><dd>' + _number(summary.get("history_signal_rows"))
            + '</dd></div><div><dt>费用与滑点</dt><dd class="word">本研究不扣除</dd></div><div><dt>真实执行胜率</dt><dd>'
            + _pct(summary.get("actual_win_rate")) + '</dd></div></dl><p class="note">不扣费仅改变本次目标定义；毛收益仍不同于账户净利润。日线代理不会自动升级为实盘建议，也不能保证次日上涨或涨停。</p></div></div></section>'
            '<details><summary>查看研究协议与参数</summary><pre>' + protocol_json + '</pre></details><div class="identity"><p>生成时间：'
            + _e(_local_timestamp(summary.get("generated_at"))) + '　|　行情基准日：' + _e(base) + '</p><p>研究协议 SHA256：'
            + _e(summary.get("protocol_sha256") or "未提供") + '</p><p>输入数据库 SHA256：' + _e(summary.get("input_database_sha256") or "未提供")
            + '</p></div><footer class="footer"><span>先确认可交易，再积累冻结规则之后的新结果。</span><span>目标收益及历史表现不保证未来收益。</span></footer></main>'
            '<script>document.querySelectorAll("[data-filter]").forEach(function(button){button.addEventListener("click",function(){var target=button.dataset.filter;document.querySelectorAll("[data-filter]").forEach(function(item){item.setAttribute("aria-pressed",String(item===button))});document.querySelectorAll("#selection-body [data-target]").forEach(function(row){row.hidden=target!=="all"&&Math.abs(Number(row.dataset.target)-Number(target))>0.000001})})});</script></body></html>')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", required=True, help="Path to a generated summary.json")
    parser.add_argument("--output", required=True, help="Destination standalone HTML path")
    args = parser.parse_args()
    data = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_report(data), encoding="utf-8")


if __name__ == "__main__":
    main()
