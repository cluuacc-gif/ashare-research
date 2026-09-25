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


def us_market_brief(timeout: float = 5.0) -> dict:
    """Best-effort US snapshot with cache fallback (90 min). Never blocks mail forever."""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    cache_path = Path(__file__).resolve().parent.parent / "staging" / "us_market_cache.json"
    # allow running from repo root
    if not cache_path.parent.exists():
        cache_path = Path("staging/us_market_cache.json")
    symbols = {"^GSPC": "S&P500", "^IXIC": "Nasdaq", "^DJI": "Dow", "NVDA": "NVDA", "AAPL": "AAPL"}
    result = {"source": "yahoo_chart_public", "quotes": [], "note": "descriptive only"}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"

    def one(label, sym):
        req = urllib.request.Request(
            url.format(sym=sym),
            headers={"User-Agent": "Mozilla/5.0 (research-mail)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        quote = payload["chart"]["result"][0]["indicators"]["quote"][0]
        closes = [x for x in quote.get("close") or [] if x is not None]
        if len(closes) >= 2:
            chg = closes[-1] / closes[-2] - 1.0
            return {"label": label, "symbol": sym, "last": closes[-1], "change_1d": round(chg, 4)}
        raise ValueError("thin closes")

    try:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(one, label, sym): label for label, sym in symbols.items()}
            for fut in as_completed(futs, timeout=timeout + 3):
                label = futs[fut]
                try:
                    result["quotes"].append(fut.result())
                except Exception as e:
                    result.setdefault("errors", []).append(f"{label}:{type(e).__name__}")
    except Exception as e:
        result.setdefault("errors", []).append(f"pool:{type(e).__name__}")

    if result["quotes"]:
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
            if old.get("quotes"):
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
        if gap is None:
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
    from signal_quality import score_symbol

    quality_rows = []
    for x in scored:
        q = score_symbol(db, x.get("symbol"), base, x.get("open_gap"), x.get("open_to_close"))
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
    return {
        "stage": "auction",
        "version": VERSION,
        "generated_at": stamp(),
        "target_date": day,
        "calendar": cal,
        "information_cutoff": day + "T09:25:00+08:00",
        "base_trade_date": base,
        "auction_note": (
            "09:25集合竞价结束后的研究观察名单。"
            "本环境未接入逐笔竞价行情；名单来自昨收日线描述性筛选+隔夜消息，"
            "不是已验证的涨停预测，也不是买入指令。"
        ),
        "watchlist_research_only": ranked,
        "capital_limited_top": scored[:5],
        "capital_limited_vetoes": vetoes[:12],
        "measured_edge": {
            "rule": "open_gap 2-4% AND o2c>=+5% AND close<=8 AND not Friday AND non-ST AND no high/medium risk news",
            "sim_fill": "1-tick slippage; low<=fill<=high and close>=trigger",
            "preferred_exit": "E+1 close or +2% TP-close; do NOT hold to E+2 (worse)",
            "win_rate_tp_touch": 0.85,
            "win_rate_tp_close_pessimistic": 0.80,
            "win_rate_e1_close": 0.80,
            "mean_net_e1_close": 0.048,
            "regime_note": "up-regime e_close ~88% (n=136); flat/down too few samples",
            "oos_n_tp_touch": 27,
            "sizing": "1-2% risk per trade, max 1-3 names, skip if empty",
            "claim_80_allowed": False,
            "caveat": (
                "加1 tick滑点后保守样本n≈160：E+1收盘/悲观止盈约80%，均净约0.5%～4.8%；"
                "持有到E+2胜率很差。样本外仍稀疏，不作实盘80%承诺。"
            ),
        },
        "capital_limited_rule": (
            "实证优选：「开盘高开2%～4% + 当日开→收≥+5% + 收盘≤8元 + 非周五 + 非ST + 无高/中风险公告」；"
            "组内按成交额取1～2只。仓位：单笔风险1%～2%，最多1～3只，无信号则空仓。"
            "退出：优先E+1收盘或+2%止盈（收盘确认），不要扛到E+2。"
            "市场状态偏多时更稳。加滑点后保守胜率约80%（样本~160），仍非实盘承诺。"
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
        f"| 模型就绪 | **{model_ready}** |",
        f"| 生成时间 | {payload.get('generated_at')} |",
        "",
    ]

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
                    f"{row.get('limit_like_days') or 0} | {'是' if row.get('is_st') else '否'} |"
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
                    f"{_fmt_num(row.get('amount') or row.get('volume'), 0)} | {row.get('limit_like_days') or 0} | "
                    f"{'ST不碰' if row.get('is_st') else '可观察'} |"
                )
            edge = payload.get("measured_edge") or {}
            if edge:
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
                f"{_fmt_num(row.get('amount') or row.get('volume'), 0)} | {row.get('limit_like_days') or 0} | "
                f"{'是' if row.get('is_st') else '否'} |"
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
        if args.skip_closed:
            print(json.dumps(note, ensure_ascii=False))
            return 0
        # Still emit a short closed-day notice so schedules are observable.
        md = (
            f"# 休市跳过\n\n目标日 {args.day} 非交易日（交易所公告日历）。\n"
            f"下一交易日：{cal.get('next_session')}\n"
        )
        (out_dir / f"{args.stage}-{args.day}.md").write_text(md, encoding="utf-8")
        write_json(out_dir / f"{args.stage}-{args.day}.json", note)
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
