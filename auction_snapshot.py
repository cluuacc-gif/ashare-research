#!/usr/bin/env python3
"""Real 9:25 call-auction snapshot via public quote APIs.

At 09:25 the exchange open price is the call-auction match price.
We fetch that plus volume/amount/limits for the universe and write
a local snapshot used by the auction mail shortlist.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent


def clear_proxy():
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)


def to_tx(sym: str) -> str:
    code, ex = sym.split(".")
    return ("sh" if ex == "SH" else "sz" if ex == "SZ" else "bj") + code


def parse_tx(line: str):
    if "=" not in line or "~" not in line:
        return None
    body = line.split("=", 1)[1].strip().strip('"')
    parts = body.split("~")
    if len(parts) < 50:
        return None
    try:
        code = parts[2]
        name = parts[1]
        last = float(parts[3]) if parts[3] else None
        prev = float(parts[4]) if parts[4] else None
        open_px = float(parts[5]) if parts[5] else None
        vol = float(parts[6]) if parts[6] else 0.0
        high = float(parts[33]) if parts[33] else None
        low = float(parts[34]) if parts[34] else None
        amount_wan = float(parts[37]) if parts[37] else 0.0
        limit_up = float(parts[47]) if len(parts) > 47 and parts[47] else None
        limit_down = float(parts[48]) if len(parts) > 48 and parts[48] else None
        gap = (open_px / prev - 1.0) if (open_px and prev) else None
        return {
            "code": code,
            "name": name,
            "prev_close": prev,
            "auction_open": open_px,
            "last": last,
            "high": high,
            "low": low,
            "volume_lots": vol,
            "amount_wan": amount_wan,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "open_gap": gap,
            "is_st": 1 if "ST" in name.upper() else 0,
        }
    except Exception:
        return None


def fetch_symbols(symbols, batch=50):
    out = {}
    for i in range(0, len(symbols), batch):
        chunk = symbols[i : i + batch]
        codes = ",".join(to_tx(s) for s in chunk)
        try:
            req = urllib.request.Request(
                "https://qt.gtimg.cn/q=" + codes,
                headers={"User-Agent": "Mozilla/5.0 (auction-snapshot)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("gbk", errors="replace")
            for line in text.splitlines():
                row = parse_tx(line)
                if not row:
                    continue
                for s in chunk:
                    if s.startswith(row["code"] + "."):
                        out[s] = row
                        break
        except Exception as e:
            print(f"batch {i}: {type(e).__name__}: {e}", flush=True)
        time.sleep(0.08)
    return out


def classify(row: dict) -> dict:
    gap = row.get("open_gap")
    if gap is None:
        state = "no_open"
    elif row.get("limit_up") and row.get("auction_open") and row["auction_open"] >= row["limit_up"] - 1e-6:
        state = "open_at_limit"
    elif gap > 0.04:
        state = "gap_too_high"
    elif 0.02 <= gap <= 0.04:
        state = "gap_2_4"
    elif 0.0 <= gap < 0.02:
        state = "gap_0_2"
    else:
        state = "gap_down"
    return {
        **row,
        "auction_state": state,
        "tradeable_research": state in ("gap_2_4", "gap_0_2") and not row.get("is_st"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbols-file", help="JSON list of symbols; default from market DB")
    p.add_argument("--db")
    p.add_argument("--output", required=True)
    p.add_argument("--max-symbols", type=int, default=0)
    args = p.parse_args()
    clear_proxy()
    now = datetime.now(TZ)
    if args.symbols_file:
        symbols = json.loads(Path(args.symbols_file).read_text(encoding="utf-8"))
    elif args.db:
        import sqlite3

        db = sqlite3.connect(Path(args.db).as_uri() + "?mode=ro", uri=True)
        symbols = [r[0] for r in db.execute("SELECT symbol FROM security_master ORDER BY symbol")]
        db.close()
    else:
        raise SystemExit("--symbols-file or --db required")
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    quotes = fetch_symbols(symbols)
    rows = [classify(v) for v in quotes.values()]
    tradeable = [r for r in rows if r.get("tradeable_research")]
    # Prefer gap_2_4 then by amount
    tradeable.sort(key=lambda z: (0 if z["auction_state"] == "gap_2_4" else 1, -(z.get("amount_wan") or 0)))
    result = {
        "kind": "auction_snapshot_0925",
        "generated_at": now.isoformat(),
        "clock": now.strftime("%H:%M:%S"),
        "is_auction_window": (now.hour, now.minute) >= (9, 15) and (now.hour, now.minute) <= (9, 35),
        "symbols_requested": len(symbols),
        "symbols_received": len(rows),
        "state_counts": {
            k: sum(1 for r in rows if r["auction_state"] == k)
            for k in sorted({r["auction_state"] for r in rows})
        },
        "tradeable_top": tradeable[:30],
        "note": "auction_open is the call-auction match price from public quote feeds (Tencent qt).",
    }
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in result if k != "tradeable_top"}, ensure_ascii=False, indent=2))
    print("tradeable", len(tradeable))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
