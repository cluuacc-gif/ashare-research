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


def us_market_brief(timeout: float = 8.0) -> dict:
    """Best-effort US risk-on/off snapshot for overnight linkage notes."""
    import urllib.request

    symbols = {"^GSPC": "S&P500", "^IXIC": "Nasdaq", "^DJI": "Dow", "NVDA": "NVDA", "AAPL": "AAPL"}
    result = {"source": "yahoo_chart_public", "quotes": [], "note": "descriptive only"}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
    for sym, label in symbols.items():
        try:
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
                result["quotes"].append(
                    {"label": label, "symbol": sym, "last": closes[-1], "change_1d": round(chg, 4)}
                )
        except Exception as e:
            result.setdefault("errors", []).append(f"{label}:{type(e).__name__}")
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


def render_markdown(payload: dict) -> str:
    stage = payload["stage"]
    lines = [
        f"# {STAGE_TITLES[stage]}",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 目标交易日：{payload['target_date']}",
        f"- 信息截止：{payload['information_cutoff']}",
        f"- 版本：{payload['version']}",
        "",
        "## 交易日历",
        f"- 是否开盘：{payload['calendar']['is_session']}",
        f"- 上一交易日：{payload['calendar'].get('previous_session')}",
        f"- 下一交易日：{payload['calendar'].get('next_session')}",
        "",
    ]
    if stage == "evening":
        us = payload.get("us_overnight_linkage") or {}
        lines += ["## 美股隔夜连带（描述性）", ""]
        for q in us.get("quotes") or []:
            lines.append(
                f"- {q['label']}: {q['last']}  日变动 {q['change_1d']*100:.2f}%"
            )
        if us.get("errors"):
            lines.append(f"- 部分标的获取失败：{us['errors']}")
        lines += ["", "连带说明：美股科技强势/弱势可能影响次日A股科技与风险偏好，仅作背景，不构成信号。", ""]
    if stage in ("morning", "auction"):
        news = payload.get("public_news_until_0900") or payload.get("public_news_until_0925") or []
        lines += ["## 截止时点公开消息", ""]
        if news:
            for item in news[:20]:
                if isinstance(item, dict):
                    lines.append(f"- {item.get('time', '')} {item.get('title', item)}")
                else:
                    lines.append(f"- {item}")
        else:
            lines.append("- （未提供可核验新闻源，或新闻文件为空）")
        lines.append("")
    if stage == "auction":
        lines += ["## 集合竞价后研究观察（非买入指令）", ""]
        for i, row in enumerate(payload.get("watchlist_research_only") or [], 1):
            lines.append(
                f"{i}. {row['symbol']} {row.get('name') or ''}  close={row['close']}  "
                f"vol={row['volume']}  bars={row['bars']}  limit_like={row['limit_like_days']}"
            )
        lines.append("")
    research = payload.get("research") or {}
    inv = research.get("inventory") or {}
    lines += [
        "## 数据库存量",
        f"- 最新行情日：{inv.get('latest_date')}",
        f"- 日线行数：{inv.get('daily_rows')}",
        f"- 证券数：{inv.get('securities')}",
        f"- 当日报价：{inv.get('base_quotes')}",
        "",
        "## 5～10元描述性筛选（节选）",
        "",
    ]
    for row in (research.get("price_band_5_10") or [])[:12]:
        st = " ST" if row.get("is_st") else ""
        ind = f" {row['industry']}" if row.get("industry") else ""
        lines.append(
            f"- {row['symbol']}{st} {row.get('name') or ''}{ind} close={row['close']} vol={row['volume']} "
            f"o2c={row['open_to_close']} limit_like={row['limit_like_days']}"
        )
    lines += [
        "",
        f"说明：{research.get('price_band_note')}",
        f"数据状态：{research.get('data_status')}；模型就绪：{research.get('model_ready')}",
        f"交接状态：{payload.get('handoff_status')}",
        "",
        "## 合规声明",
        payload.get("disclaimer", ""),
        "",
        "本邮件为研究材料，不连接券商、不自动下单。80%净胜率为待验证目标，不是承诺。",
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
