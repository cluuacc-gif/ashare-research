#!/usr/bin/env python3
"""Risk-grade news titles for research veto. Pure classification, no invention."""
from __future__ import annotations

RISK_HIGH = ("退市", "立案", "处罚", "警告函", "警示函", "公开谴责", "移送", "冻结", "占用", "违规担保")
RISK_MED = ("问询", "关注函", "监管函", "减持", "质押", "诉讼", "仲裁", "亏损", "预亏", "商誉", "暂停")
RISK_LOW = ("更正", "说明", "问询函回复", "年报", "季报")


def risk_level(title: str) -> str:
    t = title or ""
    if any(k in t for k in RISK_HIGH):
        return "high"
    if any(k in t for k in RISK_MED):
        return "medium"
    return "low"


def symbol_risk_map(db, days: int = 30) -> dict:
    """Return symbol -> {'high': n, 'medium': n, 'latest': ...} for recent news."""
    rows = db.execute(
        "SELECT symbols_json, title, published_at, event_at, retrieved_at FROM news_events"
    ).fetchall()
    out = {}
    for symbols_json, title, published_at, event_at, retrieved_at in rows:
        level = risk_level(title)
        if level == "low":
            continue
        try:
            import json

            symbols = json.loads(symbols_json or "[]")
        except Exception:
            symbols = []
        for sym in symbols:
            item = out.setdefault(sym, {"high": 0, "medium": 0, "titles": []})
            item[level] = item.get(level, 0) + 1
            if len(item["titles"]) < 5:
                item["titles"].append({"level": level, "title": title[:80], "published_at": published_at})
    return out


def veto_reason(risk: dict | None) -> str | None:
    if not risk:
        return None
    if risk.get("high"):
        return f"risk_news_high×{risk['high']}"
    if risk.get("medium", 0) >= 3:
        return f"risk_news_medium×{risk['medium']}"
    return None
