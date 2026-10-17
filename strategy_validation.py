"""Cost-aware strategy diagnostics and a fail-closed evidence gate.

This module neither trades nor opens a database.  Default fees are configurable
research assumptions, not a statement of a broker's actual fees.  Daily OHLC
results remain proxies regardless of their apparent win rate.  Verification
flags refer to evidence reviewed by the caller; this module does not authenticate
a broker statement or infer point-in-time data quality from a profitable result.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import math
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence


def _number(value: Any, name: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return result


def _day(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO trading date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO trading date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class CostConfig:
    """Illustrative mainland stock costs; replace with the user's agreed rates.

    ``slippage_bps`` applies on each side of a simulated trade.  ``lot_size`` is
    fixed at 100 for this strategy's entry policy; odd-lot liquidation is outside
    this module's supported trade shape.
    """

    commission_rate: float = 0.0003
    min_commission: float = 5.0
    transfer_rate: float = 0.00001
    sell_tax_rate: float = 0.0005
    slippage_bps: float = 10.0
    lot_size: int = 100

    def __post_init__(self) -> None:
        for key in ("commission_rate", "min_commission", "transfer_rate", "sell_tax_rate", "slippage_bps"):
            _number(getattr(self, key), key, nonnegative=True)
        if self.lot_size != 100 or isinstance(self.lot_size, bool):
            raise ValueError("this strategy requires 100-share entry lots")
        if self.slippage_bps >= 10000:
            raise ValueError("slippage_bps must be below 10000 per side")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "assumption_only": True, "slippage_basis": "bps_per_side"}


def _config(value: CostConfig | Mapping[str, Any] | None) -> CostConfig:
    if value is None:
        return CostConfig()
    if isinstance(value, CostConfig):
        return value
    return CostConfig(**{k: v for k, v in value.items() if k not in {"assumption_only", "slippage_basis"}})


_FEE_KEYS = ("buy_commission", "sell_commission", "buy_transfer", "sell_transfer", "sell_tax")
_EXECUTION_SOURCES = {"broker_fill", "tick_execution_audit"}


def _execution_evidence_verified(evidence: Mapping[str, Any] | None) -> bool:
    """Require explicit reviewed fill and fee references, not a bare boolean."""
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("source_type") not in _EXECUTION_SOURCES:
        return False
    required = ("verified", "actual_fill_prices", "fees_verified")
    if not all(evidence.get(key) is True for key in required):
        return False
    if not all(isinstance(evidence.get(key), str) and evidence[key].strip()
               for key in ("buy_fill_id", "sell_fill_id", "fee_statement_id")):
        return False
    if evidence["buy_fill_id"] == evidence["sell_fill_id"]:
        return False
    fees = evidence.get("actual_fees")
    if not isinstance(fees, Mapping) or not all(key in fees for key in _FEE_KEYS):
        return False
    try:
        for key in _FEE_KEYS:
            _number(fees[key], key, nonnegative=True)
    except ValueError:
        return False
    # A market print alone does not establish that this account filled an order.
    if evidence["source_type"] == "tick_execution_audit" and evidence.get("order_fill_verified") is not True:
        return False
    return True


def calculate_trade(
    buy_price: float,
    sell_price: float,
    shares: int,
    entry_date: str,
    exit_date: str,
    *,
    config: CostConfig | Mapping[str, Any] | None = None,
    trading_dates: Sequence[str] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate a completed, whole-lot trade's net cash result.

    Without reviewed fill/fee evidence, prices are reference prices adjusted by
    configured per-side slippage and fees.  With evidence, prices must be the
    actual fill prices and actual itemized fees replace the assumptions; applying
    simulated slippage a second time would double-charge it.  ``trading_dates``
    verifies calendar membership; absent a calendar only a later date is checked.
    """
    costs = _config(config)
    buy = _number(buy_price, "buy_price")
    sell = _number(sell_price, "sell_price")
    if min(buy, sell) <= 0:
        raise ValueError("buy and sell prices must be positive")
    if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0 or shares % 100:
        raise ValueError("shares must be a positive multiple of 100")
    entry = _day(entry_date, "entry_date")
    exit_ = _day(exit_date, "exit_date")
    if exit_ <= entry:
        raise ValueError("T+1 requires selling after the entry trading date")
    calendar_verified = trading_dates is not None
    if trading_dates is not None:
        sessions = [_day(d, "trading_dates") for d in trading_dates]
        if sessions != sorted(set(sessions)):
            raise ValueError("trading_dates must be unique and increasing")
        if entry not in sessions or exit_ not in sessions:
            raise ValueError("entry and exit must belong to trading_dates")
        if sessions.index(exit_) <= sessions.index(entry):
            raise ValueError("T+1 requires a later trading session")

    execution_verified = _execution_evidence_verified(evidence)
    if execution_verified:
        fill_buy, fill_sell = buy, sell
        fees = {key: _number(evidence["actual_fees"][key], key, nonnegative=True) for key in _FEE_KEYS}
        slippage_cost = 0.0
    else:
        slip = float(costs.slippage_bps) / 10000.0
        fill_buy, fill_sell = buy * (1.0 + slip), sell * (1.0 - slip)
        fees = {
            "buy_commission": max(float(costs.min_commission), fill_buy * shares * float(costs.commission_rate)),
            "sell_commission": max(float(costs.min_commission), fill_sell * shares * float(costs.commission_rate)),
            "buy_transfer": fill_buy * shares * float(costs.transfer_rate),
            "sell_transfer": fill_sell * shares * float(costs.transfer_rate),
            "sell_tax": fill_sell * shares * float(costs.sell_tax_rate),
        }
        slippage_cost = ((fill_buy - buy) + (sell - fill_sell)) * shares
    buy_cash = fill_buy * shares + fees["buy_commission"] + fees["buy_transfer"]
    sell_cash = fill_sell * shares - fees["sell_commission"] - fees["sell_transfer"] - fees["sell_tax"]
    net_pnl = sell_cash - buy_cash
    return {
        "status": "closed", "entry_date": entry, "exit_date": exit_, "shares": shares,
        "reference_buy_price": buy, "reference_sell_price": sell,
        "buy_fill_price": fill_buy, "sell_fill_price": fill_sell,
        "fees": fees, "fee_total": sum(fees.values()), "slippage_cost": slippage_cost,
        "total_cost": sum(fees.values()) + slippage_cost,
        "buy_cash": buy_cash, "sell_cash": sell_cash, "gross_pnl": (sell - buy) * shares,
        "net_pnl": net_pnl, "net_return": net_pnl / buy_cash, "win": net_pnl > 0,
        "execution_verified": execution_verified, "fees_verified": execution_verified,
        "execution_evidence": dict(evidence) if evidence is not None else None,
        "source": evidence["source_type"] if execution_verified else "cost_assumption_proxy",
        "cost_basis": "reviewed_actual_fees" if execution_verified else "configurable_research_assumptions",
        "cost_config": costs.to_dict(), "calendar_verified": calendar_verified,
        "t_plus_one_date_order_verified": True,
    }


def wilson_lower(wins: int, count: int, z: float = 1.959963984540054) -> float | None:
    """Lower end of the two-sided 95% Wilson interval (IID diagnostic only)."""
    if count == 0:
        return None
    p = wins / count
    z2 = z * z
    return (p + z2 / (2 * count) - z * math.sqrt(p * (1 - p) / count + z2 / (4 * count * count))) / (1 + z2 / count)


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    p = (len(ordered) - 1) * q
    lo = math.floor(p)
    hi = math.ceil(p)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (p - lo)


def _bootstrap(groups: Mapping[str, list[dict[str, Any]]], samples: int, seed: int) -> dict[str, Any]:
    """Resample entry dates, keeping all selections and unresolved rows per day."""
    if not groups:
        return {"method": "entry_date_block_percentile", "samples": samples, "seed": seed,
                "confidence": 0.95, "win_rate_ci95": None, "mean_net_return_ci95": None}
    blocks = []
    for day in sorted(groups):
        rows = groups[day]
        returns = [r["net_return"] for r in rows if r.get("net_return") is not None]
        blocks.append((sum(r.get("win") is True for r in rows), len(rows), sum(returns), len(returns)))
    rng = random.Random(seed)
    win_rates, means = [], []
    for _ in range(samples):
        wins = count = valid = 0
        net = 0.0
        for _ in blocks:
            w, n, total, n_valid = blocks[rng.randrange(len(blocks))]
            wins += w
            count += n
            net += total
            valid += n_valid
        win_rates.append(wins / count)
        if valid:
            means.append(net / valid)
    return {"method": "entry_date_block_percentile", "samples": samples, "seed": seed, "confidence": 0.95,
            "win_rate_ci95": [_percentile(win_rates, .025), _percentile(win_rates, .975)],
            "mean_net_return_ci95": [_percentile(means, .025), _percentile(means, .975)] if means else None}


def _record_execution_verified(row: Mapping[str, Any]) -> bool:
    """Check the reported net result reconciles to the reviewed fills and fees."""
    evidence = row.get("execution_evidence")
    if (row.get("execution_verified") is not True or row.get("fees_verified") is not True
            or not _execution_evidence_verified(evidence)):
        return False
    try:
        expected = calculate_trade(row.get("buy_fill_price"), row.get("sell_fill_price"),
                                   row.get("shares"), row.get("entry_date"), row.get("exit_date"),
                                   evidence=evidence)
        return all(math.isclose(_number(row.get(key), key), expected[key], rel_tol=1e-9, abs_tol=1e-8)
                   for key in ("net_pnl", "net_return", "buy_cash", "sell_cash"))
    except (TypeError, ValueError):
        return False


def validate_strategy(
    records: Iterable[Mapping[str, Any]],
    *,
    config: CostConfig | Mapping[str, Any] | None = None,
    target_win_rate: float = 0.8,
    min_trades: int = 200,
    min_entry_dates: int = 60,
    bootstrap_samples: int = 2000,
    seed: int = 20261017,
    dataset_kind: str = "proxy",
    verification: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate a fixed strategy without tuning it or discarding unknown exits.

    The primary win-rate denominator includes unresolved selections, counted as
    non-wins, while ``closed_trade_win_rate`` reports completed trades alone.
    Abstentions/cash are not trades or wins.  Unresolved outcomes cannot pass
    the formal gate, even if the conservative metric looks excellent.

    ``dataset_kind`` is ``live``, ``out_of_sample``, or ``proxy``.  A live record
    needs reviewed execution evidence for every trade.  An out-of-sample set also
    needs explicit out-of-sample verification.  The report distinguishes these
    scopes and never promotes proxy results into a realized/live track record.
    """
    costs = _config(config)
    target = _number(target_win_rate, "target_win_rate")
    if not .8 <= target < 1:
        raise ValueError("target_win_rate must be at least 0.8 and below 1; the target cannot be relaxed")
    if dataset_kind not in {"live", "out_of_sample", "proxy"}:
        raise ValueError("dataset_kind must be live, out_of_sample, or proxy")
    if isinstance(bootstrap_samples, bool) or not isinstance(bootstrap_samples, int) or bootstrap_samples < 1000:
        raise ValueError("at least 1000 date-block bootstrap samples are required")
    if not isinstance(min_trades, int) or isinstance(min_trades, bool) or min_trades < 200:
        raise ValueError("min_trades cannot relax the 200-trade minimum")
    if not isinstance(min_entry_dates, int) or isinstance(min_entry_dates, bool) or min_entry_dates < 60:
        raise ValueError("min_entry_dates cannot relax the 60-entry-date minimum")
    flags = dict(verification or {})
    closed, unresolved, abstentions = [], [], 0
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, original in enumerate(records):
        row = dict(original)
        status = str(row.get("status", "unresolved")).lower()
        if status in {"abstain", "cash", "no_signal"}:
            abstentions += 1
            continue
        problem = None
        try:
            entry = _day(row.get("entry_date"), "entry_date")
            if row.get("signal_date") is not None and _day(row["signal_date"], "signal_date") > entry:
                raise ValueError("signal_date is after entry_date")
        except ValueError as exc:
            entry, problem = None, str(exc)
        if status != "closed":
            problem = problem or str(row.get("reason") or f"unresolved status: {status}")
        if problem is None:
            try:
                exit_ = _day(row.get("exit_date"), "exit_date")
                if exit_ <= entry:
                    raise ValueError("T+1 requires exit_date after entry_date")
                pnl = _number(row.get("net_pnl"), "net_pnl")
                net_return = _number(row.get("net_return"), "net_return")
                if (pnl > 0) != (net_return > 0) or (pnl < 0) != (net_return < 0):
                    raise ValueError("net_pnl and net_return signs disagree")
                if net_return < -1:
                    raise ValueError("unlevered stock net_return cannot be below -1")
                normalized = {**row, "entry_date": entry, "exit_date": exit_, "net_pnl": pnl,
                              "net_return": net_return, "win": pnl > 0}
                closed.append(normalized)
                groups[entry].append(normalized)
            except ValueError as exc:
                problem = str(exc)
        if problem is not None:
            unknown = {"record_index": index, "symbol": row.get("symbol"), "entry_date": entry,
                       "status": "unresolved", "reason": problem, "win": False, "net_return": None}
            unresolved.append(unknown)
            # Unknown dates form one conservative block, but never count as an
            # independently verified date for the minimum-date requirement.
            groups[entry or "unknown_entry_date"].append(unknown)

    n = len(closed)
    attempts = n + len(unresolved)
    wins = sum(row["win"] for row in closed)
    returns = [row["net_return"] for row in closed]
    pnls = [row["net_pnl"] for row in closed]
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = -sum(x for x in pnls if x < 0)
    independent_dates = len({r["entry_date"] for r in closed})
    boot = _bootstrap(groups, bootstrap_samples, seed)
    win_rate = wins / attempts if attempts else None
    lower = boot["win_rate_ci95"][0] if boot["win_rate_ci95"] else None
    mean_return = statistics.mean(returns) if returns else None
    mean_pnl = statistics.mean(pnls) if pnls else None
    each_execution_verified = bool(closed) and all(_record_execution_verified(r) for r in closed)
    execution_verified = (flags.get("execution_evidence_verified") is True
                          and each_execution_verified and not unresolved and dataset_kind != "proxy")
    required_flags = ("pit_verified", "universe_verified", "status_verified", "action_verified")
    missing_evidence = [key for key in required_flags if flags.get(key) is not True]
    blockers = []
    if n < min_trades:
        blockers.append("insufficient_completed_trades")
    if independent_dates < min_entry_dates:
        blockers.append("insufficient_independent_entry_dates")
    if unresolved:
        blockers.append("unresolved_trade_or_exit_evidence")
    if win_rate is None or win_rate <= target:
        blockers.append("net_win_rate_not_above_target")
    if lower is None or lower <= target:
        blockers.append("date_block_95pct_lower_not_above_target")
    if mean_return is None or mean_return <= 0 or mean_pnl is None or mean_pnl <= 0:
        blockers.append("nonpositive_or_unknown_net_expectancy")
    if dataset_kind == "proxy":
        blockers.append("ohlc_or_other_proxy_cannot_verify_execution")
    if not execution_verified:
        blockers.append("execution_and_actual_fee_evidence_unverified")
    blockers.extend(missing_evidence)
    if dataset_kind == "out_of_sample" and flags.get("out_of_sample_verified") is not True:
        blockers.append("out_of_sample_split_unverified")
    formal_pass = not blockers
    return {
        "schema": "net_strategy_validation_v1", "dataset_kind": dataset_kind,
        "evidence_scope": {"live": "realized_account_trades", "out_of_sample": "held_out_execution_audit",
                           "proxy": "historical_price_proxy_only"}[dataset_kind],
        "cost_config": costs.to_dict(), "target_win_rate": target,
        "acceptance_rule": "strictly_greater_than_target", "min_trades": min_trades,
        "min_entry_dates": min_entry_dates, "n_trades": n, "n_attempted_trades": attempts,
        "n_unresolved": len(unresolved), "n_abstentions": abstentions,
        "independent_entry_dates": independent_dates, "wins": wins, "losses": sum(x < 0 for x in pnls),
        "breakeven_trades": sum(x == 0 for x in pnls), "win_rate": win_rate,
        "win_rate_denominator": "closed_plus_unresolved_non_abstention_records",
        "closed_trade_win_rate": wins / n if n else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "profit_factor_no_losses": bool(n and not gross_loss), "gross_profit": gross_profit,
        "gross_loss": gross_loss, "sum_net_pnl": sum(pnls), "mean_net_pnl": mean_pnl,
        "median_net_pnl": statistics.median(pnls) if pnls else None,
        "mean_net": mean_return, "median_net": statistics.median(returns) if returns else None,
        "mean_net_return": mean_return, "median_net_return": statistics.median(returns) if returns else None,
        "worst_net_return": min(returns) if returns else None, "best_net_return": max(returns) if returns else None,
        "wilson_lower_95": wilson_lower(wins, attempts), "wilson_assumption": "IID_diagnostic_not_date_dependence_adjusted",
        "date_block_bootstrap": boot, "execution_verified": execution_verified,
        "verification": {key: flags.get(key) is True for key in (*required_flags, "execution_evidence_verified", "out_of_sample_verified")},
        "each_trade_execution_evidence_verified": each_execution_verified,
        "formal_gate_passed": formal_pass, "realized_live_80pct_verified": formal_pass and dataset_kind == "live",
        "out_of_sample_80pct_verified": formal_pass and dataset_kind == "out_of_sample",
        "claim_80_allowed": formal_pass, "blockers": blockers, "unresolved_records": unresolved,
        "limitations": ["A historical confidence interval does not guarantee future win rate.",
                        "The caller must freeze the rule before the holdout and independently review source evidence.",
                        "Entry-date blocks address same-day clustering, not every form of serial or cross-asset dependence.",
                        "This report is not an order or a broker-statement authenticator."],
    }
