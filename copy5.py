#!/usr/bin/env python3
"""copy5: five $100 paper accounts, each mirroring one Polymarket wallet's trades.

Paper only: no keys, no orders. Each run:
  1. pulls every trade the account's leader made since the last run (Polymarket data API, public),
  2. copies buys and sells at the market's price *now* (we can't get the leader's price after the fact) plus
     1 cent of slippage, sized to our bankroll; skips if the price ran >10c away or the game is decided,
  3. settles positions whose markets resolved and marks the rest at Polymarket's current price,
  4. writes docs/data.json + docs/index.html (GitHub Pages dashboard) and REPORT_copy5.md.

  python copy5.py run                 # one forward cycle (what the GitHub Action runs)
  python copy5.py backtest --days 14 --delay 15
                                      # replay the last N days of each leader's trades into separate accounts,
                                      # entering --delay minutes after the leader at the real price then
                                      # (--delay 0 = the leader's own price: an upper bound, not achievable)
  python copy5.py report              # print the summary

Sizing: each copied buy risks BASE_PCT (3%) of the account's equity, scaled by how big the leader's
bet is versus that leader's usual bet (0.25x to 2x), so a leader's big-conviction bets get more.
Sells: when the leader sells part of a position we copied, we sell the same fraction.
Fees: P&L is shown gross (Polymarket price + slippage) and after a Kalshi-style taker fee
(0.07 x contracts x p x (1-p)), since Kalshi is where real money would go.
"""
import json, math, os, re, sys, time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "copy5_accounts.json")
STATE_DIR = os.path.join(HERE, "copy5_state")
DOCS = os.path.join(HERE, "docs")
REPORT = os.path.join(HERE, "REPORT_copy5.md")
PM = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

START = float(os.environ.get("COPY5_START", 100))
BASE_PCT = float(os.environ.get("COPY5_BASE_PCT", 0.03))   # share of equity per normal-sized copied bet
MIN_MULT, MAX_MULT = 0.25, 2.0                              # clamp on leader-size scaling (max 6% per bet)
MIN_ORDER = 1.0                                             # Polymarket/Kalshi minimum practical order, $
SLIP = 0.01                                                 # we fill 1 cent worse than the market price
MAX_CHASE = float(os.environ.get("COPY5_MAX_CHASE", 0.10))  # skip if price moved >10c against us since the leader
KALSHI_FEE = 0.07
MERGE_SEC = 600                                             # fills of one token+side within 10 min = one bet
MAX_PAGES = int(os.environ.get("COPY5_MAX_PAGES", 10))      # x500 trades per leader per run
HISTORY_MAX = 4000

S = requests.Session()
S.headers["User-Agent"] = "copy5-paper-sim/1.0"
now_ts = lambda: int(time.time())
iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{iso(now_ts())}] {msg}", flush=True)


def get(url, **params):
    for attempt in range(4):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(3 * 2 ** attempt); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 3:
                log(f"request failed {url}: {e}")
                return None
            time.sleep(2 * (attempt + 1))
    return None


# ---------------- Polymarket data ----------------
def leader_trades(addr, since, max_pages=MAX_PAGES):
    """Leader's trades newer than `since`, oldest first. Returns None if the API failed (don't advance)."""
    out, cur = [], None
    for _ in range(max_pages):
        p = {"user": addr, "limit": 500}
        if cur:
            p["cursor"] = cur
        d = get(f"{PM}/v2/trades", **p)
        if d is None:
            return None
        rows = d.get("data") or []
        out += [r for r in rows if int(r["timestamp"]) > since]
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur or not rows or int(rows[-1]["timestamp"]) <= since:
            break
    return sorted(out, key=lambda r: int(r["timestamp"]))


def price_at(token, ts, window=1800):
    """Polymarket's price for `token` at the first recorded point at/after `ts` (else the last before it).
    For a live run `ts` is now, so this is the latest price."""
    d = get(f"{CLOB}/prices-history", market=token, startTs=ts - window, endTs=ts + window, fidelity=1)
    h = (d or {}).get("history") or []
    after = [x for x in h if x["t"] >= ts]
    before = [x for x in h if x["t"] < ts]
    pt = after[0] if after else (before[-1] if before else None)
    return float(pt["p"]) if pt else None


def recent_median_usd(addr):
    d = get(f"{PM}/v2/trades", user=addr, limit=500) or {}
    usd = sorted(float(r["size"]) * float(r["price"]) for r in d.get("data") or [] if r.get("side") == "BUY")
    return usd[len(usd) // 2] if usd else None


def market_info(conds):
    """{cond: {"closed": bool, "prices": [..], "win": idx or None}} for the given condition ids."""
    out, conds = {}, sorted(set(conds))
    for i in range(0, len(conds), 40):
        chunk = conds[i:i + 40]
        for closed in ("false", "true"):
            for m in get(f"{GAMMA}/markets", condition_ids=chunk, closed=closed, limit=100) or []:
                try:
                    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except Exception:
                    continue
                if not prices:
                    continue
                done = bool(m.get("closed")) and max(prices) > 0.98
                out[m["conditionId"]] = {"closed": done, "prices": prices,
                                         "win": prices.index(max(prices)) if done else None,
                                         "closed_ts": (parse_ts(m.get("closedTime")) or _plus(parse_ts(
                                             m.get("gameStartTime") or m.get("endDate")), 4 * 3600)) if done else None}
    return out


def _plus(ts, sec):
    return ts + sec if ts else None


def parse_ts(s):
    """'2026-11-19 23:44:12.19806+00' -> unix seconds (None if unparseable). Any fraction length."""
    if not s:
        return None
    s = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        d = datetime.fromisoformat(s)
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        return None


# ---------------- account state ----------------
def new_state(acct, since):
    return {"id": acct["id"], "name": acct["name"], "addr": acct["addr"], "start": START, "cash": START,
            "since": since, "last_ts": since, "positions": {}, "leader_shares": {}, "median_usd": None,
            "closed": [], "log": [], "history": [], "kfees": 0.0, "copied": 0, "skipped": 0}


def load_state(path, acct, since):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return new_state(acct, since)


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def kfee(contracts, p):
    return math.ceil(KALSHI_FEE * contracts * p * (1 - p) * 100) / 100


def equity_at_cost(st):
    return st["cash"] + sum(p["cost"] for p in st["positions"].values())


def group_fills(trades):
    """Merge fills of the same token+side that are within MERGE_SEC of each other into one bet."""
    groups, open_g = [], {}
    for t in trades:
        key = (t["token_id"], t["side"])
        ts, sh, px = int(t["timestamp"]), float(t["size"]), float(t["price"])
        g = open_g.get(key)
        if g and ts - g["ts_last"] <= MERGE_SEC:
            g["shares"] += sh; g["usd"] += sh * px; g["ts_last"] = ts; g["fills"] += 1
        else:
            g = {"token": t["token_id"], "side": t["side"], "cond": t["condition_id"],
                 "idx": t.get("outcome_index"), "outcome": t.get("outcome"), "title": t.get("title"),
                 "slug": t.get("slug"), "event_slug": t.get("event_slug"), "ts": ts, "ts_last": ts,
                 "shares": sh, "usd": sh * px, "fills": 1}
            open_g[key] = g
            groups.append(g)
    return groups


def note(st, ts, kind, g, **kw):
    st["log"].append({"ts": iso(ts), "kind": kind, "title": g.get("title"), "outcome": g.get("outcome"),
                      "event_slug": g.get("event_slug"), "leader_usd": round(g.get("usd", 0), 2), **kw})
    st["log"] = st["log"][-400:]


def copy_group(st, g, mkt_px=None, at=None):
    """Copy one leader bet. `mkt_px` = the price we could actually get when our copy happens (None = leader's
    own price, i.e. instant copying). `at` = when our copy happens."""
    vwap = g["usd"] / g["shares"] if g["shares"] else 0
    base = vwap if mkt_px is None else mkt_px
    at = at or g["ts"]
    ls = st["leader_shares"]
    if g["side"] == "BUY":
        ls[g["token"]] = ls.get(g["token"], 0) + g["shares"]
        med = st.get("median_usd") or g["usd"]
        mult = min(MAX_MULT, max(MIN_MULT, g["usd"] / med if med else 1))
        stake = round(min(equity_at_cost(st) * BASE_PCT * mult, st["cash"]), 2)
        px = min(round(base + SLIP, 4), 0.99)
        reason = None
        if base <= 0.01 or base >= 0.98:
            reason = "too late: outcome already decided"
        elif px > vwap + MAX_CHASE:
            reason = f"price ran away ({vwap:.2f} -> {base:.2f})"
        elif stake < MIN_ORDER:
            reason = "out of cash" if st["cash"] < MIN_ORDER else "below $1 minimum"
        if reason:
            st["skipped"] += 1
            note(st, at, "skip", g, reason=reason, price=round(base, 3), leader_price=round(vwap, 3))
            return
        sh = stake / px
        fee = kfee(sh, px)
        p = st["positions"].setdefault(g["token"], {"cond": g["cond"], "idx": g["idx"], "outcome": g["outcome"],
                                                    "title": g["title"], "slug": g["slug"],
                                                    "event_slug": g["event_slug"], "shares": 0.0, "cost": 0.0,
                                                    "kfee": 0.0, "opened": iso(g["ts"])})
        p["shares"] += sh; p["cost"] = round(p["cost"] + stake, 4); p["kfee"] += fee
        st["cash"] = round(st["cash"] - stake, 4)
        st["kfees"] = round(st["kfees"] + fee, 4)
        st["copied"] += 1
        note(st, at, "buy", g, stake=stake, price=round(px, 3), leader_price=round(vwap, 3), mult=round(mult, 2))
    else:  # SELL
        before = ls.get(g["token"], 0)
        ls[g["token"]] = max(0.0, before - g["shares"])
        p = st["positions"].get(g["token"])
        if not p:
            return
        frac = min(1.0, g["shares"] / before) if before > 0 else 1.0
        px = max(round(base - SLIP, 4), 0.01)
        sh = p["shares"] * frac
        proceeds = sh * px
        cost = p["cost"] * frac
        fee = kfee(sh, px)
        st["cash"] = round(st["cash"] + proceeds, 4)
        st["kfees"] = round(st["kfees"] + fee, 4)
        realize(st, g["token"], p, frac, proceeds - cost, "sold with leader", at, px, p["kfee"] * frac + fee)
        note(st, at, "sell", g, fraction=round(frac, 2), price=round(px, 3), leader_price=round(vwap, 3),
             pnl=round(proceeds - cost, 2))


def realize(st, token, p, frac, pnl, how, ts, px, fees):
    st["closed"].append({"ts": iso(ts), "title": p["title"], "outcome": p["outcome"], "event_slug": p["event_slug"],
                         "cost": round(p["cost"] * frac, 2), "pnl": round(pnl, 2), "kfee": round(fees, 2),
                         "how": how, "exit_price": round(px, 3)})
    p["shares"] *= (1 - frac); p["cost"] *= (1 - frac); p["kfee"] *= (1 - frac)
    if p["shares"] < 1e-6 or frac >= 0.999:
        st["positions"].pop(token, None)


def settle_due(st, info, upto):
    """Backtest: pay out positions whose market closed at or before `upto`."""
    for token, p in list(st["positions"].items()):
        m = info.get(p["cond"])
        if m and m["closed"] and m.get("closed_ts") and m["closed_ts"] <= upto and p["idx"] is not None:
            won = int(p["idx"]) == m["win"]
            payout = p["shares"] if won else 0.0
            st["cash"] = round(st["cash"] + payout, 4)
            realize(st, token, p, 1.0, payout - p["cost"], "won" if won else "lost", m["closed_ts"],
                    1.0 if won else 0.0, p["kfee"])


def settle_and_mark(st, info, ts):
    mark = 0.0
    for token, p in list(st["positions"].items()):
        m = info.get(p["cond"])
        if m and m["closed"] and p["idx"] is not None:
            won = int(p["idx"]) == m["win"]
            payout = p["shares"] if won else 0.0
            st["cash"] = round(st["cash"] + payout, 4)
            realize(st, token, p, 1.0, payout - p["cost"], "won" if won else "lost", ts, 1.0 if won else 0.0, p["kfee"])
            continue
        cur = None
        if m and p["idx"] is not None and int(p["idx"]) < len(m["prices"]):
            cur = m["prices"][int(p["idx"])]
        p["mark"] = cur
        mark += p["shares"] * cur if cur is not None else p["cost"]
    return round(st["cash"] + mark, 2)


def summary(st):
    closed = [c for c in st["closed"] if c["how"] in ("won", "lost")]
    won = sum(1 for c in closed if c["how"] == "won")
    realized = sum(c["pnl"] for c in st["closed"])
    eq = st["history"][-1]["equity"] if st["history"] else st["cash"]
    peak, dd = st["start"], 0.0
    for h in st["history"]:
        peak = max(peak, h["equity"]); dd = max(dd, (peak - h["equity"]) / peak if peak else 0)
    return {"id": st["id"], "name": st["name"], "addr": st["addr"], "since": iso(st["since"]),
            "equity": eq, "cash": round(st["cash"], 2), "pnl": round(eq - st["start"], 2),
            "pnl_after_kalshi_fees": round(eq - st["start"] - st["kfees"], 2),
            "return_pct": round(100 * (eq - st["start"]) / st["start"], 1), "realized": round(realized, 2),
            "kalshi_fees": round(st["kfees"], 2), "settled": len(closed), "won": won,
            "lost": len(closed) - won, "sold_early": sum(1 for c in st["closed"] if c["how"] == "sold with leader"),
            "open": len(st["positions"]), "at_risk": round(sum(p["cost"] for p in st["positions"].values()), 2),
            "copied": st["copied"], "skipped": st["skipped"], "max_drawdown_pct": round(100 * dd, 1)}


def cycle(st, ts, max_pages=MAX_PAGES, replay=False, delay=0):
    if st.get("median_usd") is None or ts - st.get("median_ts", 0) > 86400:
        med = recent_median_usd(st["addr"])
        if med:
            st["median_usd"], st["median_ts"] = med, ts
    trades = leader_trades(st["addr"], st["last_ts"], max_pages)
    if trades is None:
        log(f"{st['name']}: trade feed failed, will retry next run")
    else:
        groups = group_fills(trades)
        # Replay (backtest): pay out games as they closed, so cash recycles the way it would have.
        hist_info = market_info([g["cond"] for g in groups]) if replay else {}
        if replay and delay:
            groups.sort(key=lambda g: g["ts"] + delay)
        for g in groups:
            if replay:
                at = g["ts"] + delay
                settle_due(st, hist_info, at)
                m = hist_info.get(g["cond"]) or {}
                if delay and m.get("closed_ts") and m["closed_ts"] <= at:
                    if g["side"] == "BUY":
                        st["skipped"] += 1
                        note(st, at, "skip", g, reason="market closed before our copy")
                    continue
                px = price_at(g["token"], at) if delay else None
                if delay and px is None:
                    st["skipped"] += 1
                    note(st, at, "skip", g, reason="no price history")
                    continue
                copy_group(st, g, px, at)
                st["history"].append({"t": iso(at), "equity": round(equity_at_cost(st), 2)})
            else:
                # Live: we act now, at whatever the market is now, however long ago the leader traded.
                px = price_at(g["token"], ts)
                if px is None:
                    st["skipped"] += 1
                    note(st, ts, "skip", g, reason="no current price (market closed?)")
                    continue
                copy_group(st, g, px, ts)
        if trades:
            st["last_ts"] = max(int(t["timestamp"]) for t in trades)
        log(f"{st['name']}: {len(trades)} new fills")
    info = market_info([p["cond"] for p in st["positions"].values()])
    eq = settle_and_mark(st, info, ts)
    st["history"].append({"t": iso(ts), "equity": eq})
    st["history"] = st["history"][-HISTORY_MAX:]
    return st


# ---------------- output ----------------
def write_outputs(states, backtests, accounts):
    meta = {a["id"]: {k: a.get(k) for k in ("focus", "risk", "rivo", "why")} for a in accounts}
    data = {"updated": iso(now_ts()), "start": START, "base_pct": BASE_PCT, "slip": SLIP,
            "accounts": [], "backtest": backtests}
    for st in states:
        s = summary(st)
        s.update(meta.get(st["id"], {}))
        s["history"] = st["history"][-1500:]
        s["positions"] = [{"title": p["title"], "outcome": p["outcome"], "event_slug": p["event_slug"],
                           "cost": round(p["cost"], 2), "shares": round(p["shares"], 2),
                           "avg": round(p["cost"] / p["shares"], 3) if p["shares"] else None,
                           "mark": p.get("mark"), "opened": p["opened"]} for p in st["positions"].values()]
        s["closed"] = st["closed"][-60:][::-1]
        s["log"] = st["log"][-60:][::-1]
        data["accounts"].append(s)
    save_json(os.path.join(DOCS, "data.json"), data)
    tpl = os.path.join(HERE, "copy5_dashboard.html")
    if os.path.exists(tpl):
        with open(tpl, encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report_md(data))


def report_md(data):
    L = [f"# Copy-5 paper accounts\n\nUpdated {data['updated']} UTC. Each account started with ${data['start']:.0f} "
         f"and mirrors one Polymarket wallet. Paper only.\n",
         "| Acct | Leader | Risk | Equity | P&L | After Kalshi fees | W-L | Sold early | Open | Max drawdown |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for a in data["accounts"]:
        L.append(f"| {a['id']} | [{a['name']}](https://polymarket.com/profile/{a['addr']}) | {a.get('risk','')} | "
                 f"${a['equity']:.2f} | {a['pnl']:+.2f} | {a['pnl_after_kalshi_fees']:+.2f} | {a['won']}-{a['lost']} | "
                 f"{a['sold_early']} | {a['open']} (${a['at_risk']:.2f}) | {a['max_drawdown_pct']}% |")
    for name, bt in (data.get("backtest") or {}).items():
        L.append(f"\n## Backtest: {name}\n")
        L.append("| Acct | Leader | Equity | P&L | After Kalshi fees | W-L | Open |")
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for a in bt["accounts"]:
            L.append(f"| {a['id']} | {a['name']} | ${a['equity']:.2f} | {a['pnl']:+.2f} | "
                     f"{a['pnl_after_kalshi_fees']:+.2f} | {a['won']}-{a['lost']} | {a['open']} |")
    return "\n".join(L) + "\n"


def load_accounts():
    with open(CONF, encoding="utf-8") as f:
        return json.load(f)["accounts"]


def load_backtests():
    p = os.path.join(STATE_DIR, "backtests.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def cmd_run():
    accounts, ts = load_accounts(), now_ts()
    states = []
    for a in accounts:
        path = os.path.join(STATE_DIR, f"{a['id']}.json")
        st = load_state(path, a, ts)
        if st["addr"].lower() != a["addr"].lower():
            log(f"account {a['id']} leader changed; starting a fresh $100 account")
            st = new_state(a, ts)
        st = cycle(st, ts)
        save_json(path, st)
        states.append(st)
    write_outputs(states, load_backtests(), accounts)
    for st in states:
        s = summary(st)
        log(f"{s['id']} {s['name']}: ${s['equity']:.2f} ({s['pnl']:+.2f}), {s['won']}-{s['lost']}, {s['open']} open")


def cmd_backtest(days, delay_min):
    accounts, ts = load_accounts(), now_ts()
    since = ts - int(days * 86400)
    res = []
    for a in accounts:
        st = new_state(a, since)
        st = cycle(st, ts, max_pages=40, replay=True, delay=int(delay_min * 60))
        res.append(summary(st))
        save_json(os.path.join(STATE_DIR, "backtest", f"{a['id']}_{days}d_{delay_min}m.json"), st)
    bts = load_backtests()
    label = "instant copy (leader's own price)" if not delay_min else f"copied {delay_min} min later"
    bts[f"{days} days, {label}"] = {"days": days, "delay_min": delay_min, "run": iso(ts), "accounts": res}
    save_json(os.path.join(STATE_DIR, "backtests.json"), bts)
    states = [load_state(os.path.join(STATE_DIR, f"{a['id']}.json"), a, ts) for a in accounts]
    write_outputs(states, bts, accounts)
    for s in res:
        log(f"BACKTEST {days}d +{delay_min}m {s['id']} {s['name']}: ${s['equity']:.2f} ({s['pnl']:+.2f}, after fees "
            f"{s['pnl_after_kalshi_fees']:+.2f}), {s['won']}-{s['lost']}, {s['open']} open, copied {s['copied']}, "
            f"skipped {s['skipped']}")


def main():
    args = sys.argv[1:] or ["run"]
    if args[0] == "run":
        cmd_run()
    elif args[0] == "backtest":
        days = float(args[args.index("--days") + 1]) if "--days" in args else 14
        delay = float(args[args.index("--delay") + 1]) if "--delay" in args else 15
        cmd_backtest(int(days) if days.is_integer() else days, int(delay) if delay.is_integer() else delay)
    elif args[0] == "report":
        print(report_md(json.load(open(os.path.join(DOCS, "data.json"), encoding="utf-8"))))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
