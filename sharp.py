#!/usr/bin/env python3
"""sharp.py: "Sharp Filter", one $100 paper account that copies only the bets the data says are worth copying.

Paper only: no keys, no orders. Built on copy5.py's Polymarket helpers.

How it decides (see README "Sharp Filter"):
  1. Pool: Polymarket's all-time top sports wallets plus the Copy-5 leaders.
  2. Score every wallet by how much its copyable bets beat the price we would have paid (result minus
     our price 15 min after the leader, plus slippage and Kalshi's fee), not by win rate or profit.
     Only pre-game buys count, bets under half the wallet's usual size are ignored, and the score is
     shrunk toward zero for small samples. Scored again per sport, shrunk toward the wallet's overall score.
  3. Copy a wallet only in sports where its shrunk edge is at least MIN_EDGE.
  4. Per bet: skip if the game has started, the bet is under half the wallet's usual size, the price
     hasn't reacted since the leader bought, the price ran more than MAX_CHASE away, or another eligible
     wallet holds the other side. Another eligible wallet on the same side raises the stake.
  5. Size with a quarter of the Kelly stake for the estimated edge; caps per bet and per day; one bet per game.
  6. Leader sells half or more before the game: we sell. Wallets whose live copies lose are benched.

  python sharp.py run                        # one live cycle (rescoring the pool weekly)
  python sharp.py walkforward [--days 30]    # score on days 2N..N ago, trade days N..0, report
  python sharp.py report
"""
import json, math, os, sys, time
from concurrent.futures import ThreadPoolExecutor

import copy5 as c5
from copy5 import get, log, iso, now_ts, parse_ts, kfee, save_json, PM, GAMMA

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, "sharp_state")
REPORT = os.path.join(HERE, "REPORT_sharp.md")

DELAY = 15 * 60          # our copy happens ~15 min after the leader (GitHub runs every 15-30 min)
PREGAME = 15 * 60        # leader must bet at least this long before the start
MIN_CONV = 0.5           # ignore bets under half the wallet's median bet
MIN_REACT = 0.005        # price must have moved at least half a cent since the leader's fill
MAX_CHASE = 0.10         # ...but not more than 10 cents against us
PX_LO, PX_HI = 0.05, 0.95
K_WALLET, K_SPORT = 15, 8  # shrinkage: pretend each score already has this many zero-edge bets
MIN_N = 8                # need at least this many scored bets for a wallet
MIN_SPORT_N = 5          # ...and this many in a sport before copying it there
MIN_EDGE = 0.02          # copy only where the shrunk edge (after fees and slippage) is >= 2 cents
KELLY = 0.25
MAX_BET = 0.05           # of equity
MAX_DAY = 0.20           # new stakes per day, of equity
AGREE_MULT = 1.5
BENCH_N, BENCH_ROI = 12, -0.15  # bench a wallet once 12+ of our copies settled and they returned under -15%
POOL_ALL = 100           # all-time sports leaderboard size used for the pool
SCORE_DAYS = 30
RESCORE_SEC = 7 * 86400
MAX_SCORE_BETS = 120     # per wallet, most recent, to bound API calls
SCORE_PAGES = 16        # x500 fills per wallet when scoring, so busy wallets' history reaches back far enough
WORKERS = 5
SCORE_V = 2              # bump when the scoring rules change, to force a rescore on the next run


# ---------------- data ----------------
def leaderboard(period, n):
    out, cur = [], None
    while len(out) < n:
        p = {"time_period": period, "category": "sports", "limit": 100}
        if cur:
            p["cursor"] = cur
        d = get(f"{PM}/v2/leaderboard", **p) or {}
        rows = d.get("data") or []
        out += [(x["user_id"].lower(), x.get("user_name") or x["user_id"][:10]) for x in rows]
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur or not rows:
            break
    return out[:n]


def pool():
    names = dict(leaderboard("all", POOL_ALL))
    for a in c5.load_accounts():
        names.setdefault(a["addr"].lower(), a["name"])
    return names


def market_meta(conds):
    """{cond: {start, closed, win, closed_ts}} from Gamma."""
    out, conds = {}, sorted(set(c for c in conds if c))
    chunks = [conds[i:i + 40] for i in range(0, len(conds), 40)]

    def one(chunk):
        res = {}
        for closed in ("false", "true"):
            for m in get(f"{GAMMA}/markets", condition_ids=chunk, closed=closed, limit=100) or []:
                try:
                    prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
                except Exception:
                    continue
                if not prices:
                    continue
                done = bool(m.get("closed")) and max(prices) > 0.98
                start = parse_ts(m.get("gameStartTime"))
                res[m["conditionId"]] = {"start": start, "closed": done, "prices": prices,
                                         "win": prices.index(max(prices)) if done else None,
                                         "closed_ts": (parse_ts(m.get("closedTime")) or
                                                       (start + 4 * 3600 if start else None)) if done else None}
        return res
    with ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(one, chunks):
            out.update(r)
    return out


def sport_of(g):
    return (g.get("event_slug") or g.get("slug") or "other").split("-")[0]


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def wallet_bets(addr, since, pages=8):
    """Leader's merged BUY bets since `since`, plus its median bet size and its sell groups."""
    tr = c5.leader_trades(addr, since, pages)
    if not tr:
        return [], None, []
    groups = c5.group_fills(tr)
    buys = [g for g in groups if g["side"] == "BUY"]
    return buys, median([g["usd"] for g in buys]), [g for g in groups if g["side"] == "SELL"]


def two_sided(buys):
    """Markets where the wallet bought more than one outcome: hedging or market making, not a pick."""
    sides = {}
    for g in buys:
        sides.setdefault(g["cond"], set()).add(g["idx"])
    return {c for c, s in sides.items() if len(s) > 1}


def features(g, meta, med, at=None):
    """Bet-level facts for one leader buy. at = when we'd copy (default leader time + DELAY)."""
    m = meta.get(g["cond"]) or {}
    vwap = g["usd"] / g["shares"] if g["shares"] else None
    at = at or g["ts"] + DELAY
    f = {"sport": sport_of(g), "vwap": vwap, "conv": g["usd"] / med if med else 1.0, "start": m.get("start"),
         "pregame": bool(m.get("start")) and g["ts"] <= m["start"] - PREGAME and at < m["start"],
         "closed": m.get("closed"), "won": (int(g["idx"]) == m["win"]) if m.get("closed") and g["idx"] is not None else None,
         "closed_ts": m.get("closed_ts")}
    return f


def passes(f, px):
    """Bet-level filters at our copy price px. Returns None if OK, else the reason."""
    if not f["pregame"]:
        return "not pre-game"
    if f["conv"] < MIN_CONV:
        return "small bet for this wallet"
    if px is None:
        return "no price"
    if not PX_LO <= px <= PX_HI:
        return "price outside 5-95c"
    if abs(px - f["vwap"]) < MIN_REACT:
        return "market didn't react"
    if px - f["vwap"] > MAX_CHASE:
        return "price ran away"
    return None


def cost_per_contract(px):
    p = min(px + c5.SLIP, 0.99)
    return p + c5.KALSHI_FEE * p * (1 - p)


# ---------------- scoring ----------------
def score_bets(rows):
    """rows: [{wallet, sport, edge}] -> {wallet: {n, edge, sports: {sport: {n, edge}}}} (shrunk)."""
    by = {}
    for r in rows:
        w = by.setdefault(r["wallet"], {"n": 0, "sum": 0.0, "sports": {}})
        w["n"] += 1; w["sum"] += r["edge"]
        s = w["sports"].setdefault(r["sport"], {"n": 0, "sum": 0.0})
        s["n"] += 1; s["sum"] += r["edge"]
    out = {}
    for wa, w in by.items():
        we = w["sum"] / (w["n"] + K_WALLET)
        out[wa] = {"n": w["n"], "raw": round(w["sum"] / w["n"], 4), "edge": round(we, 4),
                   "sports": {sp: {"n": s["n"], "edge": round((s["sum"] + K_SPORT * we) / (s["n"] + K_SPORT), 4)}
                              for sp, s in w["sports"].items()}}
    return out


def eligible(scores, wallet, sport):
    """Shrunk edge for this wallet in this sport if it's copyable there, else None. The wallet must be
    positive overall and have at least MIN_SPORT_N scored bets in the sport."""
    w = scores.get(wallet)
    if not w or w["n"] < MIN_N or w["edge"] <= 0:
        return None
    s = w["sports"].get(sport)
    if not s or s["n"] < MIN_SPORT_N:
        return None
    return s["edge"] if s["edge"] >= MIN_EDGE else None


def copyable(scores, wallet):
    w = scores.get(wallet) or {}
    return any(eligible(scores, wallet, sp) is not None for sp in w.get("sports", {}))


def build_scores(names, t_from, t_to, cache=None):
    """Score every pool wallet on its pre-game buys placed in [t_from, t_to) that settled before t_to."""
    cache = cache if cache is not None else {}
    per = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda a: (a, wallet_bets(a, t_from, pages=SCORE_PAGES)), names))
    conds = []
    for a, (buys, med, _) in res:
        buys = [g for g in buys if g["ts"] < t_to]
        per[a] = (buys, med)
        conds += [g["cond"] for g in buys]
    meta = market_meta(conds)
    todo = []
    for a, (buys, med) in per.items():
        cand, hedged = [], two_sided(buys)
        for g in buys:
            if g["cond"] in hedged:
                continue
            f = features(g, meta, med)
            if f["pregame"] and f["conv"] >= MIN_CONV and f["closed"] and f["closed_ts"] and f["closed_ts"] < t_to:
                cand.append((g, f))
        for g, f in cand[-MAX_SCORE_BETS:]:
            todo.append((a, g, f))
    log(f"scoring {len(todo)} bets from {len(per)} wallets")

    def px(item):
        a, g, f = item
        k = f"{g['token']}:{g['ts'] + DELAY}"
        if k not in cache:
            cache[k] = c5.price_at(g["token"], g["ts"] + DELAY)
        return cache[k]
    with ThreadPoolExecutor(WORKERS) as ex:
        prices = list(ex.map(px, todo))
    rows = []
    for (a, g, f), p in zip(todo, prices):
        if passes(f, p):
            continue
        rows.append({"wallet": a, "sport": f["sport"], "edge": (1.0 if f["won"] else 0.0) - cost_per_contract(p)})
    scores = score_bets(rows)
    for a in scores:
        scores[a]["name"] = names.get(a, a[:10])
        scores[a]["median_usd"] = per[a][1]
    return scores, per, meta


# ---------------- account ----------------
def new_account(since):
    st = c5.new_state({"id": "S", "name": "Sharp Filter", "addr": "-"}, since)
    st.update({"lead": {}, "seen": {}, "day": {}, "live": {}, "bench": []})
    return st


def stake_for(st, edge, px, mult, day):
    c = cost_per_contract(px)
    kelly = edge / (1 - c) if c < 1 else 0
    eq = c5.equity_at_cost(st)
    stake = min(KELLY * kelly * mult, MAX_BET) * eq
    room = MAX_DAY * eq - st["day"].get(day, 0.0)
    return round(max(0.0, min(stake, room, st["cash"])), 2)


def try_copy(st, g, f, px, at, edge, wallet, wname, others):
    """others: {(cond, idx): set(wallets)} of eligible wallets' recent buys, for agreement/opposition."""
    why = passes(f, px)
    if not why:
        same = others.get((g["cond"], g["idx"]), set()) - {wallet}
        opp = set().union(*[v for (cd, ix), v in others.items() if cd == g["cond"] and ix != g["idx"]] or [set()]) - {wallet}
        if opp:
            why = "another sharp wallet is on the other side"
        elif any(p["event_slug"] == g["event_slug"] for p in st["positions"].values()):
            why = "already have a bet on this game"
        elif wallet in st["bench"]:
            why = "wallet benched (our copies of it are losing)"
    if why:
        st["skipped"] += 1
        c5.note(st, at, "skip", g, reason=why, price=round(px, 3) if px else None,
                leader_price=round(f["vwap"], 3), leader=wname)
        return
    mult = AGREE_MULT if same else 1.0
    day = iso(at)[:10]
    stake = stake_for(st, edge, px, mult, day)
    if stake < c5.MIN_ORDER:
        st["skipped"] += 1
        c5.note(st, at, "skip", g, reason="daily limit or cash", leader=wname)
        return
    p = min(px + c5.SLIP, 0.99)
    sh = stake / p
    fee = kfee(sh, p)
    st["positions"][g["token"]] = {"cond": g["cond"], "idx": g["idx"], "outcome": g["outcome"], "title": g["title"],
                                   "slug": g["slug"], "event_slug": g["event_slug"], "shares": sh, "cost": stake,
                                   "kfee": fee, "opened": iso(at), "leader": wallet, "leader_name": wname,
                                   "leader_shares": g["shares"], "edge": edge}
    st["cash"] = round(st["cash"] - stake, 4)
    st["kfees"] = round(st["kfees"] + fee, 4)
    st["day"][day] = round(st["day"].get(day, 0.0) + stake, 2)
    st["copied"] += 1
    c5.note(st, at, "buy", g, stake=stake, price=round(p, 3), leader_price=round(f["vwap"], 3), leader=wname,
            edge=round(edge, 3), agree=bool(same))


def leader_sold(st, g, px, at, wallet):
    """Leader sold part of a token we copied from it: if they've now sold half or more, we sell all."""
    p = st["positions"].get(g["token"])
    if not p or p.get("leader") != wallet:
        return
    p["leader_sold"] = p.get("leader_sold", 0.0) + g["shares"]
    if p["leader_sold"] < 0.5 * p["leader_shares"] or px is None:
        return
    sell = max(px - c5.SLIP, 0.01)
    proceeds = p["shares"] * sell
    fee = kfee(p["shares"], sell)
    st["cash"] = round(st["cash"] + proceeds, 4)
    st["kfees"] = round(st["kfees"] + fee, 4)
    track_live(st, p, proceeds - p["cost"] - p["kfee"] - fee)
    c5.realize(st, g["token"], p, 1.0, proceeds - p["cost"], "sold with leader", at, sell, p["kfee"] + fee)


def track_live(st, p, net):
    w = st["live"].setdefault(p.get("leader", "?"), {"n": 0, "net": 0.0, "cost": 0.0})
    w["n"] += 1; w["net"] = round(w["net"] + net, 2); w["cost"] = round(w.get("cost", 0.0) + p["cost"], 2)
    st["bench"] = [a for a, v in st["live"].items()
                   if v["n"] >= BENCH_N and v.get("cost") and v["net"] / v["cost"] < BENCH_ROI]


def settle(st, meta, upto, live=False):
    for token, p in list(st["positions"].items()):
        m = meta.get(p["cond"])
        if m and m["closed"] and (live or (m.get("closed_ts") and m["closed_ts"] <= upto)):
            won = int(p["idx"]) == m["win"]
            payout = p["shares"] if won else 0.0
            st["cash"] = round(st["cash"] + payout, 4)
            track_live(st, p, payout - p["cost"] - p["kfee"])
            c5.realize(st, token, p, 1.0, payout - p["cost"], "won" if won else "lost",
                       upto if live else m["closed_ts"], 1.0 if won else 0.0, p["kfee"])


# ---------------- walk-forward test ----------------
def cmd_walkforward(days):
    ts = now_ts()
    t_test = ts - days * 86400
    names = pool()
    log(f"pool: {len(names)} wallets; training on days {2 * days}-{days} ago, trading the last {days} days")
    cache = {}
    scores, _, _ = build_scores(names, t_test - days * 86400, t_test, cache)
    elig = {a: s for a, s in scores.items() if copyable(scores, a)}
    log(f"{len(elig)} wallets have a sport with edge >= {MIN_EDGE}")
    # test period: every buy/sell of eligible wallets
    events, conds, meds = [], [], {}
    with ThreadPoolExecutor(WORKERS) as ex:
        res = list(ex.map(lambda a: (a, wallet_bets(a, t_test, pages=SCORE_PAGES)), elig))
    for a, (buys, med, sells) in res:
        meds[a] = med
        events += [(g["ts"] + DELAY, "buy", a, g) for g in buys]
        events += [(g["ts"] + DELAY, "sell", a, g) for g in sells]
        conds += [g["cond"] for g in buys]
    meta = market_meta(conds)
    cand, sides = [], {}
    for e in sorted(events, key=lambda x: x[0]):
        at, kind, a, g = e
        if kind == "buy":
            s = sides.setdefault((a, g["cond"]), set())
            s.add(g["idx"])
            if len(s) > 1:   # this wallet now holds both sides of this market: not a pick
                continue
            f = features(g, meta, meds[a])
            edge = eligible(scores, a, f["sport"])
            if edge is not None and f["pregame"] and f["conv"] >= MIN_CONV:
                cand.append((e, f, edge))
    toks = {(c[0][2], c[0][3]["token"]) for c in cand}
    cand += [(e, None, None) for e in events if e[1] == "sell" and (e[2], e[3]["token"]) in toks]
    log(f"test period: {sum(1 for c in cand if c[1])} candidate bets")
    with ThreadPoolExecutor(WORKERS) as ex:
        prices = list(ex.map(lambda c: c5.price_at(c[0][3]["token"], c[0][0]), cand))
    st = new_account(t_test)
    recent = []  # (at, cond, idx, wallet) of eligible wallets' copyable buys, for agreement/opposition
    for (e, f, edge), px in sorted(zip(cand, prices), key=lambda x: x[0][0][0]):
        at, kind, a, g = e
        settle(st, meta, at)
        if kind == "sell":
            leader_sold(st, g, px, at, a)
            continue
        recent = [r for r in recent if at - r[0] < 86400]
        others = {}
        for r in recent:
            others.setdefault((r[1], r[2]), set()).add(r[3])
        try_copy(st, g, f, px, at, edge, a, names.get(a, a[:10]), others)
        if not passes(f, px):
            recent.append((at, g["cond"], g["idx"], a))
        st["history"].append({"t": iso(at), "equity": round(c5.equity_at_cost(st), 2)})
    settle(st, meta, ts)
    eq = c5.settle_and_mark(st, c5.market_info([p["cond"] for p in st["positions"].values()]), ts)
    st["history"].append({"t": iso(ts), "equity": eq})
    s = c5.summary(st)
    out = {"run": iso(ts), "days": days, "summary": s, "eligible": {a: elig[a] for a in elig},
           "skips": count_skips(st)}
    save_json(os.path.join(STATE_DIR, f"walkforward_{days}d.json"), {**out, "state": st})
    log(f"WALK-FORWARD {days}d: ${s['equity']:.2f} ({s['pnl']:+.2f}; after Kalshi fees {s['pnl_after_kalshi_fees']:+.2f}), "
        f"{s['won']}-{s['lost']}, sold early {s['sold_early']}, open {s['open']}, copied {s['copied']}, skipped {s['skipped']}")
    for a, v in sorted(elig.items(), key=lambda x: -x[1]["edge"]):
        good = {sp: x for sp, x in v["sports"].items() if eligible(scores, a, sp) is not None}
        log(f"  {v['name']}: {v['n']} bets, raw {v['raw']:+.3f}, shrunk {v['edge']:+.3f}; copy in {good}")
    log(f"  skips: {out['skips']}")
    return out


def count_skips(st):
    c = {}
    for l in st["log"]:
        if l["kind"] == "skip":
            c[l.get("reason")] = c.get(l.get("reason"), 0) + 1
    return c


# ---------------- live ----------------
def cmd_run():
    ts = now_ts()
    os.makedirs(STATE_DIR, exist_ok=True)
    sp = os.path.join(STATE_DIR, "scores.json")
    sc = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}
    if ts - sc.get("ts", 0) > RESCORE_SEC or sc.get("v") != SCORE_V:
        names = pool()
        scores, _, _ = build_scores(names, ts - SCORE_DAYS * 86400, ts)
        sc = {"ts": ts, "scored": iso(ts), "scores": scores, "v": SCORE_V}
        save_json(sp, sc)
        log(f"rescored {len(scores)} wallets")
    scores = sc["scores"]
    ap = os.path.join(STATE_DIR, "account.json")
    st = json.load(open(ap, encoding="utf-8")) if os.path.exists(ap) else new_account(ts)
    elig = [a for a in scores if copyable(scores, a)]
    seen = st["seen"]
    news, conds = [], []
    for a in elig:
        since = seen.get(a, ts - 3600)
        buys, _, sells = wallet_bets(a, since, pages=2)
        news += [("buy", a, g) for g in buys] + [("sell", a, g) for g in sells]
        conds += [g["cond"] for g in buys + sells]
        last = max([g["ts_last"] for g in buys + sells], default=None)
        if last:
            seen[a] = last
        elif a not in seen:
            seen[a] = since
    meta = market_meta(conds + [p["cond"] for p in st["positions"].values()])
    recent = [r for r in st.get("recent", []) if ts - r[0] < 86400]
    for kind, a, g in sorted(news, key=lambda x: x[2]["ts"]):
        if kind == "sell":
            if g["token"] in st["positions"]:
                leader_sold(st, g, c5.price_at(g["token"], ts), ts, a)
            continue
        sd = st.setdefault("sides", {}).setdefault(a, {})
        idxs = sd.setdefault(g["cond"], [])
        if g["idx"] not in idxs:
            idxs.append(g["idx"])
        if len(idxs) > 1:   # leader holds both sides of this market: not a pick
            continue
        f = features(g, meta, scores[a].get("median_usd"), at=ts)
        edge = eligible(scores, a, f["sport"])
        if edge is None or not f["pregame"]:
            continue
        px = c5.price_at(g["token"], ts)
        others = {}
        for r in recent:
            others.setdefault((r[1], r[2]), set()).add(r[3])
        try_copy(st, g, f, px, ts, edge, a, scores[a].get("name", a[:10]), others)
        if not passes(f, px):
            recent.append([ts, g["cond"], g["idx"], a])
    st["recent"] = recent
    for a, sd in st.get("sides", {}).items():   # keep the last 300 markets per wallet
        if len(sd) > 300:
            st["sides"][a] = dict(list(sd.items())[-300:])
    settle(st, meta, ts, live=True)
    eq = c5.settle_and_mark(st, c5.market_info([p["cond"] for p in st["positions"].values()]), ts)
    st["history"].append({"t": iso(ts), "equity": eq})
    st["history"] = st["history"][-4000:]
    st["day"] = {k: v for k, v in st["day"].items() if k >= iso(ts - 3 * 86400)[:10]}
    save_json(ap, st)
    write_report(st, sc)
    s = c5.summary(st)
    log(f"Sharp Filter: ${s['equity']:.2f} ({s['pnl']:+.2f}; after fees {s['pnl_after_kalshi_fees']:+.2f}), "
        f"{s['won']}-{s['lost']}, {s['open']} open; {len(elig)} eligible wallets, {len(news)} new leader bets")


def write_report(st, sc):
    s = c5.summary(st)
    scores = sc.get("scores", {})
    L = [f"# Sharp Filter paper account\n\nUpdated {iso(now_ts())} UTC. Started with ${st['start']:.0f} on "
         f"{iso(st['since'])[:10]}. Paper only. Wallet scores from {sc.get('scored', '?')[:10]}.\n",
         f"**${s['equity']:.2f}** ({s['pnl']:+.2f}; after Kalshi fees {s['pnl_after_kalshi_fees']:+.2f}) · "
         f"{s['won']}-{s['lost']} · sold early {s['sold_early']} · {s['open']} open (${s['at_risk']:.2f}) · "
         f"max drawdown {s['max_drawdown_pct']}%\n",
         "## Wallets it copies (and in which sports)\n", "| Wallet | Scored bets | Edge | Sports copied |", "| --- | --- | --- | --- |"]
    for a, v in sorted(scores.items(), key=lambda x: -x[1]["edge"]):
        good = [f"{sp} {x['edge']:+.2f}" for sp, x in v["sports"].items() if eligible(scores, a, sp) is not None]
        if good:
            L.append(f"| [{v.get('name', a[:10])}](https://polymarket.com/profile/{a}) | {v['n']} | {v['edge']:+.3f} | "
                     f"{', '.join(good)}{' (benched)' if a in st['bench'] else ''} |")
    L += ["\n## Open\n", "| Game | Pick | Leader | Cost | Now |", "| --- | --- | --- | --- | --- |"]
    for p in st["positions"].values():
        now = "" if p.get("mark") is None else "%.0fc" % (p["mark"] * 100)
        L.append(f"| {p['title']} | {p['outcome']} | {p.get('leader_name', '')} | ${p['cost']:.2f} | {now} |")
    L += ["\n## Recent decisions\n", "| Time | Action | Game | Pick | Leader | Note |", "| --- | --- | --- | --- | --- | --- |"]
    for l in st["log"][-40:][::-1]:
        note = f"${l['stake']:.2f} at {l['price'] * 100:.0f}c" if l.get("stake") else l.get("reason") or ""
        L.append(f"| {l['ts'][5:16].replace('T', ' ')} | {l['kind']} | {l['title']} | {l['outcome']} | {l.get('leader', '')} | {note} |")
    wf = os.path.join(STATE_DIR, "walkforward_30d.json")
    if os.path.exists(wf):
        w = json.load(open(wf, encoding="utf-8"))
        ws = w["summary"]
        L.append(f"\n## Walk-forward test ({w['run'][:10]})\n\nScored wallets on days 60-30 ago, then traded the last 30 days "
                 f"with those scores: ${ws['equity']:.2f} ({ws['pnl']:+.2f}; after fees {ws['pnl_after_kalshi_fees']:+.2f}), "
                 f"{ws['won']}-{ws['lost']}, copied {ws['copied']}, skipped {ws['skipped']}.\n")
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def main():
    args = sys.argv[1:] or ["run"]
    if args[0] == "run":
        cmd_run()
    elif args[0] == "walkforward":
        cmd_walkforward(int(args[args.index("--days") + 1]) if "--days" in args else 30)
    elif args[0] == "report":
        print(open(REPORT, encoding="utf-8").read())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
