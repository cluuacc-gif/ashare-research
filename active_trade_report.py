"""Bounded, dependency-free report for the enabled manual T+1 rule system.

Presentation never promotes a rule or a minute reference fill into empirical
validation, a broker position, an actual return, or a calibrated win rate.
"""
from __future__ import annotations

import html
import math


VERSION = "active-tplus1-trade-system-v1"
POLICY_NAME = "主线领涨·尾盘确认T+1"
_STATUSES = {"conditional_plans_ready", "cash_no_qualifying_setup", "waiting_for_valid_source"}
_TOP_TEXT = ("base_trade_date", "target_trade_date", "D2_trade_date", "generated_at",
             "information_cutoff", "input_database_sha256", "policy_id", "protocol_sha256", "reason")
_COVERAGE = ("candidate_pool_count", "valid_pool_count", "base_cohort_count",
             "active_evaluated_count", "active_qualifying_count", "conditional_plan_count",
             "price_action_passed_count")
_PLAN_NUMBERS = ("base_close", "frozen_entry_floor", "frozen_entry_cap", "structural_stop",
                 "frozen_reserved_cash_cny", "frozen_reserved_risk_cny", "atr14", "rank_score")
_ACCOUNT_MONEY = ("initial_capital_cny", "available_cash_cny", "reserved_cash_cny",
                  "realized_reference_pnl_cny", "reference_profit_rate",
                  "reference_profit_fraction_unknown_as_not_won", "mean_reference_return",
                  "reference_ge3_rate", "reference_ge6_rate")
_ACCOUNT_COUNTS = ("open_positions", "reference_closed", "unknown_positions")


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= 10**15 and math.isfinite(value))


def _text(value, limit=96):
    return value[:limit] if isinstance(value, str) else None


def _count(value):
    if isinstance(value, (list, tuple)):
        return len(value)
    return int(value) if _finite(value) and value >= 0 and value == int(value) else None


def _identity(raw):
    if isinstance(raw, str):
        return raw[:180]
    if isinstance(raw, dict):
        return {key: _text(raw[key], 48) for key in ("classification", "sector_code", "sector") if key in raw}
    return None


def _plan(raw):
    """Keep only coherent frozen lots; presentation is not an execution check."""
    if not isinstance(raw, dict) or not isinstance(raw.get("symbol"), str) or not raw["symbol"]:
        return None
    floor, cap, stop = (raw.get(key) for key in ("frozen_entry_floor", "frozen_entry_cap", "structural_stop"))
    shares, cash = raw.get("frozen_quantity_shares"), raw.get("frozen_reserved_cash_cny")
    if not (all(_finite(x) for x in (floor, cap, stop, cash, shares))
            and 0 < stop < floor <= cap and shares > 0 and shares % 100 == 0 and cash > 0):
        return None
    result = {key: _text(raw[key], 64 if key != "name" else 48) for key in
              ("active_plan_id", "symbol", "name", "base_trade_date", "target_trade_date", "D2_trade_date", "generated_at")
              if key in raw}
    result.update({key: raw[key] if _finite(raw.get(key)) else None for key in _PLAN_NUMBERS if key in raw})
    result.update(sector_identity=_identity(raw.get("sector_identity")), target_pct=.03,
                  frozen_quantity_shares=int(shares), entry_confirmed=False, actual_fill=None)
    return result


def compact(study):
    """Idempotent allowlist: two plans and account totals, never raw records."""
    if not isinstance(study, dict) or study.get("schema") != VERSION:
        return {}
    result = {
        "schema": VERSION, "policy_name": POLICY_NAME,
        "system_enabled": True, "signal_generation_enabled": True,
        "mode": "conditional_manual_execution", "order_execution_enabled": False,
        "empirical_validation_passed": False, "formal_trade_ready": False,
        "actual_win_rate": None, "execution_win_rate_80_validated": False,
        "actual_orders_submitted": False, "reference_fills_are_actual_fills": False,
        "broker_connected": False, "live_feed_connected": False, "score_is_probability": False,
    }
    result.update({key: _text(study[key]) for key in _TOP_TEXT if key in study})
    raw_plans = study.get("plans")
    # Examine at most the two frozen names; never substitute a later candidate.
    result["plans"] = [plan for row in (raw_plans[:2] if isinstance(raw_plans, (list, tuple)) else [])
                       if (plan := _plan(row)) is not None]
    status = study.get("status")
    result["status"] = status if isinstance(status, str) and status in _STATUSES else "waiting_for_valid_source"
    if result["status"] == "waiting_for_valid_source":
        result["plans"] = []
    elif result["plans"]:
        result["status"] = "conditional_plans_ready"
    else:
        result["status"] = "cash_no_qualifying_setup"
    raw_coverage = study.get("coverage")
    raw_coverage = raw_coverage if isinstance(raw_coverage, dict) else {}
    result["coverage"] = {key: _count(raw_coverage[key]) for key in _COVERAGE if key in raw_coverage}
    result["coverage"]["conditional_plan_count"] = len(result["plans"])
    account = study.get("reference_account")
    if isinstance(account, dict):
        result["reference_account"] = {key: account[key] if _finite(account.get(key)) else None
                                        for key in _ACCOUNT_MONEY if key in account}
        result["reference_account"].update({key: _count(account[key]) for key in _ACCOUNT_COUNTS if key in account})
        interval=account.get('reference_profit_rate_wilson95')
        if (isinstance(interval,(list,tuple)) and len(interval)==2
                and all(_finite(x) for x in interval) and 0<=interval[0]<=interval[1]<=1):
            result['reference_account']['reference_profit_rate_wilson95']=list(interval)
    return result


def _e(value):
    return html.escape(str(value), quote=True)


def _number(value, digits=0):
    return f"{value:,.{digits}f}" if _finite(value) else "—"


def _pct(value):
    return _number(value * 100, 2) + "%" if _finite(value) else "未知"


def _sector(value):
    if isinstance(value, dict):
        return " · ".join(str(value[key]) for key in ("sector", "classification", "sector_code") if value.get(key)) or "—"
    return value or "—"


def _table(headers, rows):
    return ('<div class="at-scroll"><table><thead><tr>'
            + ''.join('<th>' + _e(cell) + '</th>' for cell in headers)
            + '</tr></thead><tbody>'
            + ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows)
            + '</tbody></table></div>')


_STYLE = """<style>
.active-trade{--at-ink:#183441;--at-muted:#5c737c;--at-line:#dce5e3;color:var(--at-ink);margin:24px 0;padding:24px;background:#fff;border:1px solid var(--at-line);border-radius:14px;font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif}
.active-trade h2{font-size:24px;margin:0 0 8px}.active-trade h3{font-size:17px;margin:20px 0 10px}.active-trade p{margin:10px 0}.active-trade .at-muted{color:var(--at-muted);font-size:12px}.active-trade .at-state{background:#e9f4f0;border-left:4px solid #087f7b;border-radius:5px;padding:13px 16px;margin:16px 0}.active-trade .at-state strong{display:block}.active-trade .at-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}.active-trade .at-card{padding:13px;border:1px solid var(--at-line);border-radius:8px;background:#f6f9f7}.active-trade .at-card small{display:block;color:var(--at-muted);font-size:11px}.active-trade .at-card b{display:block;font-size:21px;margin:4px 0}.active-trade .at-card:first-child{background:#143d4b;color:#fff}.active-trade .at-card:first-child small{color:#cadee3}.active-trade .at-scroll{overflow-x:auto;border:1px solid var(--at-line);border-radius:8px}.active-trade table{border-collapse:collapse;width:100%;font-size:12px}.active-trade th,.active-trade td{padding:11px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--at-line)}.active-trade th{background:#f1f7f4;white-space:nowrap;color:#536d76}.active-trade tr:last-child td{border-bottom:0}.active-trade td small{display:block;color:var(--at-muted);font-size:11px}.active-trade .at-money{white-space:nowrap;font-variant-numeric:tabular-nums}.active-trade .at-empty{padding:17px;background:#f6f9f7;color:var(--at-muted);border-radius:8px}.active-trade details{margin-top:14px}.active-trade summary{cursor:pointer;font-weight:600}.active-trade .at-id{overflow-wrap:anywhere;border-top:1px solid var(--at-line);padding-top:12px;margin-top:18px}@media(max-width:750px){.active-trade{padding:17px}.active-trade .at-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.active-trade h2{font-size:21px}}
</style>"""


def render(study):
    """Render a standalone HTML section; full and compact inputs are identical."""
    study = compact(study)
    if not study:
        return ""
    plans = study["plans"]
    status = study["status"]
    account = study.get("reference_account", {})
    state = {"conditional_plans_ready": "条件计划已生成，等待D1尾盘确认与人工执行",
             "cash_no_qualifying_setup": "当天空仓：没有满足全部条件的新计划",
             "waiting_for_valid_source": "等待有效新数据：当前不生成买入计划"}[status]
    parts = ['<section class="active-trade" id="active-trade-system">', _STYLE,
             '<h2>我的交易系统｜' + POLICY_NAME + '</h2>',
             '<p class="at-muted">规则系统已启用 · 每个交易日独立生成条件计划 · 未连接券商，订单由你人工确认与提交</p>',
             '<div class="at-state"><strong>' + state + '</strong>系统持续运行；当期数据与预定条件决定是否交易。真实胜率未知，80%以上实际胜率尚未验证。</div>',
             '<div class="at-cards"><div class="at-card"><small>规则系统</small><b>已启用</b><small>条件式人工执行</small></div>',
             '<div class="at-card"><small>本期新计划</small><b>' + str(len(plans)) + ' / 2</b><small>持仓与待入场合计最多两只</small></div>',
             '<div class="at-card"><small>已平仓参考胜率</small><b>' + _pct(account.get("reference_profit_rate"))
             + '</b><small>仅分钟参考，非账户收益率</small></div>',
             '<div class="at-card"><small>真实成交胜率</small><b>未知</b><small>真实盈利待成交回执核对</small></div></div>',
             '<p class="at-muted">基础日T：' + _e(study.get("base_trade_date") or "待有效数据")
             + '　买入日D1：' + _e(study.get("target_trade_date") or "待交易日历")
             + '　最早卖出日D2：' + _e(study.get("D2_trade_date") or "待交易日历") + '</p>']
    if plans:
        rows = []
        for plan in plans:
            rows.append([
                '<b>' + _e(plan.get("name") or plan["symbol"]) + '</b><small>' + _e(plan["symbol"])
                + ' · ' + _e(_sector(plan.get("sector_identity"))) + '</small>',
                '<span class="at-money">' + _number(plan["frozen_entry_floor"], 2) + '～'
                + _number(plan["frozen_entry_cap"], 2) + '</span><small>T日收盘参考 '
                + _number(plan.get("base_close"), 2) + '，尚无实际买价</small>',
                '<span class="at-money">' + _number(plan["structural_stop"], 2) + '</span>',
                '实际买价 × 1.03<small>3%触发后的下一分钟开盘参考退出</small>',
                '<span class="at-money">' + _number(plan["frozen_quantity_shares"]) + '股</span><small>冻结占款 '
                + _number(plan["frozen_reserved_cash_cny"], 2) + '元</small>',
            ])
        parts.append('<h3>本期冻结条件计划</h3>' + _table(
            ["股票／同源板块", "人工买价范围（元）", "结构止损参考（元）", "止盈触发", "冻结股数／资金"], rows))
        parts.append('<p class="at-muted">价格与数量在T日冻结。D1 14:40只使用此前已完成的分钟数据；确认成立后仍须核对券商新报价、非ST／停牌和涨跌停状态。报价超出范围、数据缺失或条件不成立则不买；不追价、不补配未触发计划的资金。</p>')
    else:
        parts.append('<div class="at-empty">' + ('有效的新数据到达后按同一规则重新评估。' if status == "waiting_for_valid_source"
                     else '没有合格新信号就保留现金；已有参考持仓仍按冻结退出规则处理。') + '</div>')
    parts.append('<h3>固定交易规则</h3>' + _table(["环节", "必须满足的条件"], [
        ["全池筛选", "事前全量候选池最多300只；市场宽度≥50%，市场1日、5日收益均不为负。"],
        ["主线与领涨", "同一数据源的板块资金净流入与占比均为正，板块转强；个股5日相对板块收益为正，位居全板块有效成员前30%。"],
        ["日线质量", "20日成交额中位数≥5000万元；ATR／收盘价0.8%～4%；20日上涨、处于20日均线上方0～8%；当日涨跌−2%～4%，收盘位置≥60%。"],
        ["D1尾盘入场", "14:40完成分钟确认：收在开盘价及成交量加权收盘代理上方，末根收强，后5分钟低点不低于此前5分钟。只在冻结范围内按冻结整手数量人工买入。"],
        ["资金与风险", "3万元参考资金；持仓及待入场合计最多两只，同一板块最多一只；每只≤1万元。按结构风险加2%跳空压力、每只150元预算取整手，故常低于1万元。"],
        ["目标与结构", "结构止损＝此前5日最低价−0.15 ATR，向下取分；买价与止损风险0.8%～2%，3%目标盈亏比≥1.5。6%仅作辅助观察，不是必须实现的收益。"],
        ["D2退出", "D1买入当天不卖。若D1收盘已失效，D2首个可交易开盘处理；否则先查跳空，再以完整分钟触及止损／实际买价×1.03确认，下一连续可交易分钟开盘作退出参考，同分钟两者都触发则止损优先。未触发时按事前约定的D2 14:50开盘参考退出；不主动延至D3。"],
    ]))
    parts.append('<p class="at-muted">150元是含假设跳空的计划预算，不是最大损失保证。跳空、跌停、停牌及无法成交可能扩大损失；无法成交的持仓保留为待处理或未知，不虚构平仓。胜率不是排序分数，也不由盈亏比推算。</p>')
    if account:
        parts.append('<h3>分钟参考账户｜与真实券商账户分开核对</h3>' + _table(
            ["初始资金", "可用现金", "冻结资金", "已实现参考盈亏", "持仓／已平仓／未知"], [[
                _number(account.get("initial_capital_cny"), 2) + '元',
                _number(account.get("available_cash_cny"), 2) + '元',
                _number(account.get("reserved_cash_cny"), 2) + '元',
                _number(account.get("realized_reference_pnl_cny"), 2) + '元',
                _number(account.get("open_positions")) + '／' + _number(account.get("reference_closed"))
                + '／' + _number(account.get("unknown_positions")),
            ]]))
        parts.append('<p class="at-muted">这里只汇总分钟参考成交记账，不代表实际券商持仓或实际盈利；收益率沿用参考账本口径，未知持仓仍保留。真实买卖价格、费用及盈亏须由成交回执确认。</p>')
        interval=account.get('reference_profit_rate_wilson95')
        parts.append('<p class="at-muted">参考胜率95%区间：'
            +('～'.join(_pct(x) for x in interval) if interval else '尚无已平仓样本')
            +'；将未知结果按未获胜计入：'+_pct(account.get('reference_profit_fraction_unknown_as_not_won'))
            +'；卖出参考收益≥3%／≥6%：'+_pct(account.get('reference_ge3_rate'))+'／'
            +_pct(account.get('reference_ge6_rate'))+'。区间以交易独立假设作描述，同板块及日期相关性会降低有效样本量。</p>')
        parts.append('<p class="at-muted">参考入场固定取D1 14:40开始分钟的开盘价；人工报价核对窗口为14:40—14:45，两者可能不同。统计不含费用，不将人工延后报价推定为相同成交。</p>')
    coverage = study["coverage"]
    parts.append('<details><summary>本期范围与规则身份</summary><p class="at-muted">候选池 '
                 + _number(coverage.get("candidate_pool_count")) + '只；评估 '
                 + _number(coverage.get("active_evaluated_count")) + '只；合格 '
                 + _number(coverage.get("active_qualifying_count")) + '只；冻结计划 '
                 + str(len(plans)) + '份。</p><p class="at-muted">系统启用独立于旧研究是否通过。运行与因果检查只能验证程序行为，不能证明历史或未来胜率；目前没有可据以宣称80%胜率的真实成交证据。</p></details>')
    parts.append('<div class="at-id at-muted">生成时间：' + _e(study.get("generated_at") or "未提供")
                 + '<br>规则身份：' + _e(study.get("policy_id") or study.get("protocol_sha256") or "未提供")
                 + '<br>输入指纹：' + _e(study.get("input_database_sha256") or "未提供") + '</div></section>')
    return ''.join(parts)
