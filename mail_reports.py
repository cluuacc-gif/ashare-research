#!/usr/bin/env python3
"""Trading-day research mail reports: evening / morning / post-auction.

Research only. Never places orders. Never promises an 80% win rate.
Formal probability output requires both data and model gates.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import collector as c
from short_term import write_json
from trade_calendar import context

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
VERSION = "ashare-mail-reports-0.1.0"
PRICE_LO, PRICE_HI = 5.0, 10.0


def stamp() -> str:
    return dt.datetime.now(TZ).isoformat()


def today() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def require_session(day: str) -> dict:
    cal = context(day)
    return cal


def open_db(db_path: Path):
    import sqlite3

    path = Path(db_path).resolve()
    if not path.is_file():
        raise ValueError("market database required: " + str(path))
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA application_id").fetchone()[0] != c.APP_ID:
        db.close()
        raise ValueError("wrong database identity")
    return db


def db_inventory(db) -> dict:
    return {
        "daily_rows": db.execute("SELECT COUNT(*) FROM daily_quotes").fetchone()[0],
        "latest_date": db.execute("SELECT MAX(trade_date) FROM daily_quotes").fetchone()[0],
        "securities": db.execute("SELECT COUNT(*) FROM security_master").fetchone()[0],
        "base_quotes": db.execute(
            "SELECT COUNT(*) FROM daily_quotes WHERE trade_date=(SELECT MAX(trade_date) FROM daily_quotes)"
        ).fetchone()[0],
    }


def screen_price_band(db, as_of: str, limit: int = 30) -> list[dict]:
    """Descriptive 5-10 yuan screen. Not a buy list until gates pass."""
    rows = db.execute(
        """
        SELECT symbol, close, open, high, low, volume, amount,
               (high/low - 1.0) AS intraday_range,
               (close/NULLIF(open,0) - 1.0) AS open_to_close
        FROM daily_quotes
        WHERE trade_date=? AND close BETWEEN ? AND ?
          AND volume IS NOT NULL AND volume > 0
        ORDER BY COALESCE(amount, volume) DESC
        LIMIT ?
        """,
        (as_of, PRICE_LO, PRICE_HI, limit),
    ).fetchall()
    out = []
    for r in rows:
        name = db.execute(
            "SELECT name FROM security_master WHERE symbol=?", (r["symbol"],)
        ).fetchone()
        hist = db.execute(
            "SELECT COUNT(*) FROM daily_quotes WHERE symbol=? AND close>0",
            (r["symbol"],),
        ).fetchone()[0]
        limit_like = db.execute(
            """
            SELECT COUNT(*) FROM daily_quotes
            WHERE symbol=? AND trade_date<=? AND close>0 AND high=low AND close>0
              AND ABS(close/NULLIF(
                (SELECT close FROM daily_quotes q2
                 WHERE q2.symbol=daily_quotes.symbol AND q2.trade_date<daily_quotes.trade_date
                 ORDER BY q2.trade_date DESC LIMIT 1), 0) - 1.0) >= 0.095
            """,
            (r["symbol"], as_of),
        ).fetchone()[0]
        ind = db.execute(
            "SELECT industry FROM industry_members WHERE symbol=? ORDER BY observed_at DESC LIMIT 1",
            (r["symbol"],),
        ).fetchone()
        industry = ind["industry"] if ind else None
        st = db.execute(
            "SELECT is_st FROM security_status_daily WHERE symbol=? ORDER BY observed_at DESC LIMIT 1",
            (r["symbol"],),
        ).fetchone()
        is_st = int(st["is_st"] or 0) if st else 0
        out.append(
            {
                "symbol": r["symbol"],
                "name": name["name"] if name else None,
                "close": r["close"],
                "volume": r["volume"],
                "amount": r["amount"],
                "open_to_close": round(r["open_to_close"] or 0.0, 4),
                "bars": hist,
                "limit_like_days": limit_like,
                "industry": industry,
                "is_st": bool(is_st),
            }
        )
    return out


def us_market_brief(timeout: float = 5.0, cache_path=None) -> dict:
    """Best-effort US snapshot with cache fallback (90 min). Never blocks mail forever."""
    import urllib.request
    import math
    from urllib.parse import quote
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache_path = Path(cache_path) if cache_path else Path(__file__).resolve().parent.parent / "staging" / "us_market_cache.json"
    # allow running from repo root
    if not cache_path.parent.exists():
        cache_path = Path("staging/us_market_cache.json")
    symbols = {"^GSPC": "S&P500", "^IXIC": "Nasdaq", "^DJI": "Dow", "NVDA": "NVDA", "AAPL": "AAPL"}
    result = {"schema_version":"us-background-v2", "source": "yahoo_chart_public", "quotes": [], "note": "descriptive only", "fetched_at":stamp()}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"

    def one(label, sym):
        req = urllib.request.Request(
            url.format(sym=quote(sym,safe='')),
            headers={"User-Agent": "Mozilla/5.0 (research-mail)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        chart = payload["chart"]["result"][0]
        meta = chart.get('meta') or {}
        if meta.get('symbol') != sym:
            raise ValueError('provider returned different ticker')
        values = chart["indicators"]["quote"][0]
        closes = [x for x in values.get("close") or [] if isinstance(x,(float,int)) and math.isfinite(x) and x>0]
        if len(closes) >= 2:
            chg = closes[-1] / closes[-2] - 1.0
            market_time = meta.get('regularMarketTime')
            quoted_at = dt.datetime.fromtimestamp(market_time,TZ).isoformat() if isinstance(market_time,(float,int)) else None
            return {"label": label, "symbol": sym, "last": closes[-1], "change_1d": round(chg, 4),"quoted_at":quoted_at}
        raise ValueError("thin closes")

    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(one, label, sym): label for sym, label in symbols.items()}
            for fut in as_completed(futs, timeout=timeout + 3):
                label = futs[fut]
                try:
                    result["quotes"].append(fut.result())
                except Exception as e:
                    result.setdefault("errors", []).append(f"{label}:{type(e).__name__}")
    except Exception as e:
        result.setdefault("errors", []).append(f"pool:{type(e).__name__}")

    if result["quotes"]:
        order={sym:i for i,sym in enumerate(symbols)}
        result['quotes'].sort(key=lambda r:order[r['symbol']])
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"saved_at": stamp(), "payload": result}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass
    elif cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            old = cached.get("payload") or {}
            saved_at=dt.datetime.fromisoformat(cached['saved_at'])
            age=dt.datetime.now(TZ)-saved_at if saved_at.tzinfo is not None else dt.timedelta(days=999)
            if old.get("quotes") and old.get('schema_version')=='us-background-v2' and dt.timedelta(0)<=age<=dt.timedelta(minutes=90):
                result = {
                    **old,
                    "source": "cached",
                    "cache_saved_at": cached.get("saved_at"),
                    "errors": result.get("errors") or [],
                }
        except Exception:
            pass
    return result


def load_handoff(path: Path | None) -> dict:
    if not path or not Path(path).is_file():
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def research_section(db, as_of: str) -> dict:
    inv = db_inventory(db)
    band = screen_price_band(db, as_of)
    return {
        "as_of": as_of,
        "inventory": inv,
        "price_band_5_10": band,
        "price_band_note": (
            "价格区间为描述性筛选，不是正式合格池；"
            "买入后E+1/E+2净盈利与触板概率尚未通过验证。"
        ),
        "data_status": "DATA NOT READY",
        "model_ready": False,
        "formal_prediction_allowed": False,
    }


def evening_report(db, day: str, handoff: dict) -> dict:
    cal = require_session(day)
    research = research_section(db, day if cal["is_session"] else cal.get("previous_session") or day)
    us = us_market_brief()
    return {
        "stage": "evening",
        "version": VERSION,
        "generated_at": stamp(),
        "target_date": day,
        "calendar": cal,
        "information_cutoff": day + "T18:00:00+08:00",
        "us_overnight_linkage": us,
        "research": research,
        "handoff_status": handoff.get("status"),
        "disclaimer": (
            "18:00盘后研究简报。含美股隔夜连带观察与5～10元描述性筛选。"
            "不是正式买入指令，不承诺80%胜率，不连接券商。"
        ),
    }


def morning_report(db, day: str, handoff: dict, news_path: Path | None = None) -> dict:
    cal = require_session(day)
    base = cal.get("previous_session") or day
    research = research_section(db, base)
    news = []
    if news_path and Path(news_path).is_file():
        news = json.loads(Path(news_path).read_text(encoding="utf-8"))
    return {
        "stage": "morning",
        "version": VERSION,
        "generated_at": stamp(),
        "target_date": day,
        "calendar": cal,
        "information_cutoff": day + "T09:00:00+08:00",
        "base_trade_date": base,
        "combined_with_evening": True,
        "public_news_until_0900": news,
        "research": research,
        "handoff_status": handoff.get("status"),
        "disclaimer": (
            "09:00晨间研究简报：合并前一交易日18:00数据与截至09:00公开消息。"
            "09:00之后消息不得混入。无正式概率，不承诺80%胜率。"
        ),
    }


def _load_yesterday_review() -> dict:
    """Load previous auction shortlist json for hindsight review."""
    root = Path(__file__).resolve().parent.parent / "mail-runtime"
    if not root.is_dir():
        root = Path("mail-runtime")
    if not root.is_dir():
        return {}
    days = sorted([p.name for p in root.iterdir() if p.is_dir() and p.name[:2] == "20"])
    if not days:
        return {}
    # find previous day folder with auction json
    for d in reversed(days):
        p = root / d / "auction.json"
        if not p.is_file():
            p = root / d / f"auction-{d}.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                top = (data.get("capital_limited_top") or [])[:5]
                return {"date": d, "names": [{"symbol": x.get("symbol"), "name": x.get("name")} for x in top]}
            except Exception:
                continue
    return {}


def auction_report(db, day: str, handoff: dict, news_path: Path | None = None) -> dict:
    cal = require_session(day)
    base = cal.get("previous_session") or day
    research = research_section(db, base)
    news = []
    if news_path and Path(news_path).is_file():
        news = json.loads(Path(news_path).read_text(encoding="utf-8"))
    # Research watch order: active + historical limit behavior + price band.
    ranked = sorted(
        [x for x in research["price_band_5_10"] if not x.get("is_st")],
        key=lambda x: (
            -(x.get("limit_like_days") or 0),
            -(x.get("amount") or x.get("volume") or 0),
        ),
    )[:15]
    # Prefer real 9:25 auction snapshot when present.
    auction_path = Path(__file__).resolve().parent.parent / "mail-runtime" / day / "auction_snapshot.json"
    if not auction_path.is_file():
        auction_path = Path("mail-runtime") / day / "auction_snapshot.json"
    auction_snap = {}
    auction_meta = None
    if auction_path.is_file():
        try:
            snap = json.loads(auction_path.read_text(encoding="utf-8"))
            auction_meta = {k: snap.get(k) for k in ("kind", "generated_at", "clock", "is_auction_window", "state_counts")}
            for row in snap.get("tradeable_top") or []:
                # map by code to symbol later via prefix match
                auction_snap[row.get("code")] = row
            for row in snap.get("tradeable_top") or []:
                pass
        except Exception:
            auction_meta = {"error": "auction_snapshot_unreadable"}

    def auction_row(sym: str):
        code = sym.split(".")[0]
        return auction_snap.get(code)
    # Limited-capital shortlist based on measured rule (search_high_winrate):
    # open gap 2-4%, o2c>=+2%, non-ST, no high/medium risk news, open<=trigger proxy via gap.
    # Scoring is NOT used to rank within this set (backtest showed ranking hurts).
    from news_risk import symbol_risk_map, veto_reason

    risks = symbol_risk_map(db)
    scored = []
    vetoes = []
    for x in ranked:
        sym = x.get("symbol")
        veto = veto_reason(risks.get(sym))
        if veto:
            vetoes.append({"symbol": sym, "name": x.get("name"), "reason": veto})
            continue
        close = float(x.get("close") or 0)
        amount = float(x.get("amount") or x.get("volume") or 0)
        gene = float(x.get("limit_like_days") or 0)
        o2c = float(x.get("open_to_close") or 0.0)
        gap = x.get("open_gap")
        arow = auction_row(sym)
        if arow and arow.get("open_gap") is not None:
            gap = arow["open_gap"]
            x = {**x, "auction_open": arow.get("auction_open"), "auction_state": arow.get("auction_state")}
        elif gap is None:
            try:
                row = db.execute(
                    "SELECT open, prev_close FROM daily_quotes WHERE symbol=? AND trade_date=?",
                    (sym, base),
                ).fetchone()
                if row and row[1]:
                    gap = float(row[0]) / float(row[1]) - 1.0
                else:
                    gap = None
            except Exception:
                gap = None
        if gap is not None and gap > 0.04:
            vetoes.append({"symbol": sym, "name": x.get("name"), "reason": f"open_gap_{gap*100:.1f}pct_gt_4"})
            continue
        # Measured subset: gap 2-4%, o2c>=+5%, close<=8, skip Friday (85% TP-model / 67% close-model).
        if gap is None or not (0.02 <= gap <= 0.04):
            vetoes.append({"symbol": sym, "name": x.get("name"), "reason": f"open_gap_{(gap or 0)*100:.1f}pct_not_2_4"})
            continue
        if o2c < 0.05:
            vetoes.append({"symbol": sym, "name": x.get("name"), "reason": f"o2c_{o2c*100:.1f}pct_lt_5"})
            continue
        if close > 8.0:
            vetoes.append({"symbol": sym, "name": x.get("name"), "reason": "close_gt_8"})
            continue
        try:
            import datetime as _dt

            if _dt.date.fromisoformat(base).weekday() == 4:
                vetoes.append({"symbol": sym, "name": x.get("name"), "reason": "friday_session"})
                continue
        except Exception:
            pass
        scored.append({**x, "open_gap": gap, "research_score": None})
    # Signal-quality layer: market gate, industry heat, height risk, open momentum.
    from signal_quality import score_symbol  # noqa: F401
    from signal_quality_plus import composite_quality

    quality_rows = []
    for x in scored:
        arow = auction_row(x.get("symbol") or "")
        q = composite_quality(db, x.get("symbol"), base, x.get("open_gap"), x.get("open_to_close"), arow)
        if q.get("veto"):
            vetoes.append({"symbol": x.get("symbol"), "name": x.get("name"), "reason": "quality:" + ",".join(q["veto"])})
            continue
        quality_rows.append({**x, "quality": q, "research_score": q.get("quality_score")})
    quality_rows.sort(
        key=lambda z: (
            -(z.get("research_score") or 0),
            -(z.get("amount") or z.get("volume") or 0),
        )
    )
    scored = quality_rows
    # Action card + yesterday review helpers
    top3 = scored[:3]
    action_card = []
    for row in top3:
        close = float(row.get("close") or 0)
        trigger = round(close * 1.005, 2)  # research proxy near open breakout
        cap = round(close * 1.025, 2)
        stop = round(close * 0.97, 2)
        action_card.append(
            {
                "symbol": row.get("symbol"),
                "name": row.get("name"),
                "limit_price": cap,
                "trigger_hint": trigger,
                "stop": stop,
                "quality": row.get("research_score"),
            }
        )
    yday = _load_yesterday_review()
    return {
        "stage": "auction",
        "version": VERSION,
        "generated_at": stamp(),
        "target_date": day,
        "calendar": cal,
        "information_cutoff": day + "T09:25:00+08:00",
        "base_trade_date": base,
        "action_card": action_card,
        "yesterday_review": yday,
        "empty_signal": not scored,
        "auction_note": (
            "09:25集合竞价后的研究观察。已接入公开行情的竞价撮合价（开盘价）作为真实竞价入口；"
            "仍未接逐笔/竞价量分档。名单为研究观察，不是已验证涨停预测，也不是买入指令。"
        ),
        "watchlist_research_only": ranked,
        "capital_limited_top": scored[:5],
        "capital_limited_vetoes": vetoes[:12],
        "auction_snapshot": auction_meta,
        "auction_source": "real_call_auction_open" if auction_meta and auction_meta.get("kind") else "daily_open_proxy",
        "measured_edge": {
            "rule": "open_gap 2-4% AND o2c>=+5% AND close<=8 AND not Friday AND non-ST AND no high/medium risk news",
            "sim_fill": "1-tick slippage; low<=fill<=high and close>=trigger",
            "preferred_exit": "E+1 close or +2% TP-close; do NOT hold to E+2 (worse)",
            "win_rate_tp_touch": None,
            "win_rate_tp_close_pessimistic": None,
            "win_rate_e1_close": None,
            "mean_net_e1_close": None,
            "regime_note": "旧研究未满足因果成交验证，不能作为当前策略成绩。",
            "oos_n_tp_touch": 0,
            "sizing": "1-2% risk per trade, max 1-3 names, skip if empty",
            "claim_80_allowed": False,
            "caveat": (
                "旧回测存在同日选股成交及买入当天退出等错误，原80%附近的静态胜率已经撤回。"
                "当前隔日毛收益研究须从冻结信号和完整交易重新统计。"
            ),
        },
        "capital_limited_rule": (
            "旧竞价规则停止作为实证策略引用。当前目标为第1天可买入、第2天争取3%～6%毛收益，"
            "以新研究版本的因果入场、目标退出和完整样本为准；没有通过验证时保持观察或空仓。"
        ),
        "public_news_until_0925": news,
        "research": research,
        "handoff_status": handoff.get("status"),
        "disclaimer": (
            "正式条件单需官方限价、状态、模型与买入后净盈利验证；当前均未通过。"
            "80%是待验证目标，不得当作承诺。"
        ),
    }


STAGE_TITLES = {
    "evening": "A股5～10元｜18:00盘后研究简报",
    "morning": "A股5～10元｜09:00晨间研究简报",
    "auction": "A股5～10元｜09:25集合竞价后观察",
}


def _fmt_num(value, digits=2):
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value, digits=2):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


def render_markdown(payload: dict) -> str:
    """Readable research mail body. Display only — data semantics unchanged."""
    stage = payload["stage"]
    cal = payload.get("calendar") or {}
    research = payload.get("research") or {}
    inv = research.get("inventory") or {}
    band = research.get("price_band_5_10") or []
    status_label = "开盘" if cal.get("is_session") else "休市"
    data_status = research.get("data_status") or payload.get("handoff_status") or "—"
    model_ready = "是" if research.get("model_ready") else "否"

    lines = [
        f"# {STAGE_TITLES[stage]}",
        "",
        f"**{payload.get('target_date')} · {status_label}** ｜ 信息截止 `{payload.get('information_cutoff')}`",
        "",
    ]
    if stage == "auction" and payload.get("empty_signal"):
        lines += ["> ## 今日无合格标的 → **建议空仓 / 不交易**", ""]
    if stage == "auction" and payload.get("action_card"):
        lines += [
            "## 今日操作卡（研究限价，非券商指令）",
            "",
            "| 标的 | 限价上限 | 触发参考 | 止损 | 质量分 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for a in payload["action_card"]:
            lines.append(
                f"| `{a.get('symbol')}` {a.get('name') or ''} | {_fmt_num(a.get('limit_price'))} | "
                f"{_fmt_num(a.get('trigger_hint'))} | {_fmt_num(a.get('stop'))} | {a.get('quality') or '—'} |"
            )
        lines += ["", "> 9:25–9:30 只挂限价；高开>4% 或已封涨停放弃。", ""]
    yr = payload.get("yesterday_review") or {}
    if yr.get("names"):
        lines += [f"## 昨日回顾（{yr.get('date')} 观察名单）", ""]
        for n in yr["names"]:
            lines.append(f"- `{n.get('symbol')}` {n.get('name') or ''}")
        lines += ["", "> 是否达标请对照实盘/模拟成交；累计胜率见模拟台账。", ""]
    lines += [
        "## 一眼速览",
        "",
        "| 项目 | 内容 |",
        "| --- | --- |",
        f"| 目标交易日 | **{payload.get('target_date')}**（{status_label}） |",
        f"| 上一 / 下一交易日 | {cal.get('previous_session') or '—'} / **{cal.get('next_session') or '—'}** |",
        f"| 最新行情日 | **{inv.get('latest_date') or '—'}** |",
        f"| 当日报价 / 证券数 | {_fmt_num(inv.get('base_quotes'), 0)} / {_fmt_num(inv.get('securities'), 0)} |",
        f"| 5～10 元筛选 | **{len(band)}** 只（描述性） |",
        f"| 数据状态 | **{data_status}** |",
        f"| 模型训练／预测流程就绪 | **{model_ready}** |",
        f"| 生成时间 | {payload.get('generated_at')} |",
        "",
    ]

    predictions = research.get('model_predictions')
    if predictions:
        validation = research['model_validation']
        lines += [f"## 价量模型预测（{predictions['target_trade_date']}）", "",
            f"模型 `{predictions['model_id']}`；输入截至 {predictions['base_trade_date']}；已保存 {predictions['prediction_count']} 只预测。", "",
            "P1：次日收盘相对本日收盘上涨；P2：次日收盘涨幅≥5%。以下按P2排序，均为原始未复权价格事件概率。", "",
            "| 排名 | 代码 | 名称 | 本日收盘 | P1 | P2 |", "| ---: | --- | --- | ---: | ---: | ---: |"]
        for row in predictions['rows'][:10]:
            lines.append(f"| {row['rank']} | `{row['symbol']}` | {row.get('name') or ''} | {row['close']:.2f} | {row['P1']:.1%} | {row['P2']:.1%} |")
        lines += ["", "### 历史价格事件测试（隔日毛收益目标单独验收）", "",
            "| 价格事件 | 测试样本 | 日期数 | Top10事件发生率 | Brier | 常数基准Brier |",
            "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for label, m in validation['endpoints'].items():
            lines.append(f"| {label} | {m['n']} | {m['independent_dates']} | {m['top10_event_frequency']:.1%} | {m['brier']:.5f} | {m['calibration_block_constant_baseline_brier']:.5f} |")
        lines += ["", "> 运行条件不要求80%胜率。以上为历史价格事件统计，不能当作成交净盈利胜率。官方状态、复权和涨停限价仍待核验；P3与E+1/E+2净盈利概率暂缺，交易准入未通过。", ""]

    if stage == "evening":
        us = payload.get("us_overnight_linkage") or {}
        lines += ["## 美股隔夜连带（背景，非信号）", ""]
        quotes = us.get("quotes") or []
        if quotes:
            lines += ["| 指数/个股 | 最新 | 日变动 |", "| --- | ---: | ---: |"]
            for q in quotes:
                lines.append(
                    f"| {q.get('label', q.get('symbol'))} | {_fmt_num(q.get('last'))} | {_fmt_pct(q.get('change_1d'))} |"
                )
        else:
            lines.append("（本轮未取到美股行情）")
        if us.get("errors"):
            lines += ["", f"获取失败：{'；'.join(us['errors'])}"]
        lines += [
            "",
            "> 美股科技强弱可能影响次日 A 股风险偏好，**仅作背景**，不构成买卖信号。",
            "",
        ]

    if stage in ("morning", "auction"):
        news = payload.get("public_news_until_0900") or payload.get("public_news_until_0925") or []
        cutoff = "09:00" if stage == "morning" else "09:25"
        lines += [f"## 截至 {cutoff} 的公开消息", ""]
        if news:
            for item in news[:15]:
                if isinstance(item, dict):
                    t = item.get("time") or item.get("published_at") or ""
                    title = item.get("title") or str(item)
                    lines.append(f"- `{t}` {title}" if t else f"- {title}")
                else:
                    lines.append(f"- {item}")
            if len(news) > 15:
                lines.append(f"- …另有 {len(news) - 15} 条，见完整记录")
        else:
            lines.append("- （暂无已入库公开消息，或新闻文件为空）")
        lines.append("")

    if stage == "auction":
        watch = payload.get("watchlist_research_only") or []
        lines += [
            "## 09:25 观察名单（研究用 · 非买入指令）",
            "",
            f"> 竞价数据源：**{'真实竞价撮合价' if (payload.get('auction_source') or '').startswith('real') else '日线开盘价代理'}**"
            + (f" ｜ 抓取 `{(payload.get('auction_snapshot') or {}).get('clock', '')}`" if payload.get('auction_snapshot') else ""),
            "",
        ]
        if watch:
            lines += [
                "| # | 代码 | 名称 | 行业 | 收盘 | 成交额 | 历史涨停特征 | ST |",
                "| ---: | --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
            for i, row in enumerate(watch, 1):
                lines.append(
                    f"| {i} | `{row.get('symbol')}` | {row.get('name') or ''} | {row.get('industry') or '—'} | "
                    f"{_fmt_num(row.get('close'))} | {_fmt_num(row.get('amount') or row.get('volume'), 0)} | "
                    f"{_fmt_num(row.get('limit_like_days'), 0)} | {'未知' if row.get('is_st') is None else ('是' if row.get('is_st') else '否')} |"
                )
            lines += [
                "",
                "> **历史涨停特征**：历史上近似涨停（涨约≥9.5% 且一字/高波动）天数，越大越“有涨停基因”；**不是**未来涨停概率。",
            ]
        else:
            lines.append("（当前无可列观察标的）")
        top = payload.get("capital_limited_top") or []
        if top:
            lines += [
                "",
                "## 资金有限优选（只做 1～3 只）",
                "",
                "| 优先 | 代码 | 名称 | 研究分 | 收盘 | 成交额 | 历史涨停特征 | 说明 |",
                "| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |",
            ]
            for i, row in enumerate(top, 1):
                score = row.get("research_score")
                score_s = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
                lines.append(
                    f"| {i} | `{row.get('symbol')}` | {row.get('name') or ''} | "
                    f"{score_s} | {_fmt_num(row.get('close'))} | "
                    f"{_fmt_num(row.get('amount') or row.get('volume'), 0)} | {_fmt_num(row.get('limit_like_days'), 0)} | "
                    f"{'ST不碰' if row.get('is_st') else '可观察'} |"
                )
            edge = payload.get("measured_edge") or {}
            if edge and all(type(edge.get(key)) in (int, float) for key in
                            ('win_rate_tp_touch', 'win_rate_tp_close_pessimistic',
                             'win_rate_e1_close', 'mean_net_e1_close')):
                lines += [
                    "",
                    f"> **实测（含1 tick滑点）**：{edge.get('preferred_exit') or ''}  ",
                    f"> 止盈触达 **{edge.get('win_rate_tp_touch', 0):.0%}** ｜ 悲观止盈 **{edge.get('win_rate_tp_close_pessimistic', 0):.0%}** ｜ E+1收盘 **{edge.get('win_rate_e1_close', 0):.0%}**（均净 {edge.get('mean_net_e1_close', 0):+.1%}）  ",
                    f"> 仓位：{edge.get('sizing') or ''}  ",
                    f"> ⚠️ {edge.get('caveat')}",
                ]
            lines += [
                "",
                f"> {payload.get('capital_limited_rule') or ''}",
            ]
            vetoes = payload.get("capital_limited_vetoes") or []
            if vetoes:
                lines += ["", "### 已否决（不进资金优选）", ""]
                for v in vetoes:
                    lines.append(f"- `{v.get('symbol')}` {v.get('name') or ''} → {v.get('reason')}")
            lines += [
                "",
                "**集合竞价/挂单纪律（研究口径）**  ",
                "1. 9:25 看竞价：高开 ≤ +2% 较理想；高开 > +4% 或一字/已触涨停 → **放弃**  ",
                "2. 9:25–9:30 只对优选标的挂**限价单**（不追市价）；单笔不超过你可承受风险  ",
                "3. 成交后：最早 **E+1** 卖出；最迟 **E+2 14:50** 时间退出；跌破计划失效位就认错  ",
                "4. 未成交/跌停锁住要如实记，不能当成功",
            ]
        else:
            lines += [
                "",
                "## 资金有限优选（只做 1～3 只）",
                "",
                "### 今日无合格标的 → **建议空仓 / 不交易**",
                "",
                "> 没有同时满足「高开2%～4% + 开收≥5% + 收盘≤8 + 非周五 + 非ST + 无高/中风险公告」的票。",
                "> **空仓也是策略**，不要为了出手而降低标准。",
            ]
        lines += ["", f"> {payload.get('auction_note') or ''}", ""]

    if band:
        lines += [
            "## 5～10 元成交额前排（描述性筛选）",
            "",
            "| 代码 | 名称 | 行业 | 收盘 | 开→收 | 成交额 | 历史涨停特征 | ST |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for row in band[:12]:
            lines.append(
                f"| `{row.get('symbol')}` | {row.get('name') or ''} | {row.get('industry') or '—'} | "
                f"{_fmt_num(row.get('close'))} | {_fmt_pct(row.get('open_to_close'))} | "
                f"{_fmt_num(row.get('amount') or row.get('volume'), 0)} | {_fmt_num(row.get('limit_like_days'), 0)} | "
                f"{'未知' if row.get('is_st') is None else ('是' if row.get('is_st') else '否')} |"
            )
        lines += ["", f"> {research.get('price_band_note') or ''}", ""]

    if stage == "evening" and payload.get("base_trade_date"):
        pass
    if payload.get("base_trade_date") and stage in ("morning", "auction"):
        lines += ["## 口径", "", f"- 基础行情日：`{payload.get('base_trade_date')}`", ""]

    lines += [
        "## 合规说明",
        "",
        f"{payload.get('disclaimer') or ''}",
        "",
        "本邮件为**研究材料**，不连接券商、不自动下单。80% 为待验证目标，**不是承诺**。",
        "",
        "---",
        f"`{payload.get('version')}` · 交接状态：{payload.get('handoff_status') or '—'}",
    ]
    return "\n".join(lines) + "\n"


def build(stage: str, db_path: Path, day: str, handoff_path: Path | None, news_path: Path | None) -> dict:
    handoff = load_handoff(handoff_path)
    db = open_db(db_path)
    try:
        if stage == "evening":
            return evening_report(db, day, handoff)
        if stage == "morning":
            return morning_report(db, day, handoff, news_path)
        if stage == "auction":
            return auction_report(db, day, handoff, news_path)
        raise ValueError("unknown stage: " + stage)
    finally:
        db.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", required=True, choices=("evening", "morning", "auction"))
    p.add_argument("--db", required=True)
    p.add_argument("--handoff")
    p.add_argument("--news")
    p.add_argument("--day", default=today())
    p.add_argument("--output-dir", default="mail-reports")
    p.add_argument("--skip-closed", action="store_true", help="exit 0 without writing when market closed")
    args = p.parse_args()

    cal = require_session(args.day)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not cal["is_session"]:
        note = {
            "stage": args.stage,
            "generated_at": stamp(),
            "target_date": args.day,
            "skipped": "market_closed",
            "calendar": cal,
        }
        write_json(out_dir / f"{args.stage}-{args.day}.skip.json", note)
        # Keep an internal skip record only. No closed-day notice body may be sent.
        for suffix in ('.md','.txt','.html','.eml','.json'):
            (out_dir / f"{args.stage}-{args.day}{suffix}").unlink(missing_ok=True)
        print(json.dumps(note, ensure_ascii=False))
        return 0

    payload = build(args.stage, Path(args.db), args.day, Path(args.handoff) if args.handoff else None, Path(args.news) if args.news else None)
    write_json(out_dir / f"{args.stage}-{args.day}.json", payload)
    md = render_markdown(payload)
    (out_dir / f"{args.stage}-{args.day}.md").write_text(md, encoding="utf-8")
    print(json.dumps({"ok": True, "stage": args.stage, "day": args.day, "output_dir": str(out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
