"""
copysim.py v3 - copy vetted Polymarket sports wallets onto Kalshi. Paper by default; live when enabled.

Rules (match the manual sim):
  * Vetted wallets: top 100 on Polymarket's SPORTS leaderboard this month AND top 200 all-time.
  * Trade only when 2+ vetted wallets hold the same side and vetted money on the other
    side is under 10% of theirs. Hedged wallets (both sides, smaller > 20%) don't count.
  * Enter only in the last hour before the game starts (LOCK_WINDOW_MIN).
  * Kalshi price must be within 3c of the leaders' average entry. Same line only.
  * One trade per game (the side with the most leader money). Flat $10 per trade.
  * If the leaders sell or flip before the game starts, the sim sells at Kalshi's bid.
  * Held to settlement otherwise.

Markets matched to Kalshi: moneylines, soccer win/draw, totals (O/U), spreads,
for NBA, NFL, NHL, college football and basketball, and major soccer leagues.

Run continuously (laptop):  python3 copysim.py
Run one cycle (scheduler):   python3 copysim.py --once
Print results:               python3 copysim.py report
Test live credentials:       python3 copysim.py check-keys

Modes (COPYSIM_MODE): paper (default, no keys, no orders) | live (real orders on Kalshi).
Live mode also requires COPYSIM_LIVE_ACK=yes and Kalshi keys (see kalshi_client.py),
and is capped by the LIVE_* limits below. Create a file named STOP next to this script
to halt all new entries immediately in any mode.
Writes REPORT.md and dashboard.html next to this file after every cycle.
"""
import html, json, math, os, re, sqlite3, sys, time, difflib
from datetime import datetime, timezone, timedelta
import requests

# ---------------- settings ----------------
START_BANKROLL  = float(os.environ.get("COPYSIM_START", 100))
STAKE_USD       = float(os.environ.get("COPYSIM_STAKE", 10))
MODE            = os.environ.get("COPYSIM_MODE", "paper").lower()
LIVE_ACK        = os.environ.get("COPYSIM_LIVE_ACK", "").lower() == "yes"
LIVE_MAX_PER_TRADE = float(os.environ.get("LIVE_MAX_PER_TRADE", 10))   # $ cap per order
LIVE_MAX_DAILY     = float(os.environ.get("LIVE_MAX_DAILY", 50))       # $ cap on new buys per day
LIVE_MAX_OPEN      = int(os.environ.get("LIVE_MAX_OPEN", 10))          # max open positions
MIN_WALLETS     = 2
OPP_MAX         = 0.10      # opposing vetted money must be under 10% of the agreeing side
HEDGE_RATIO     = 0.20
MAX_SLIP        = 0.03
MIN_PRICE, MAX_PRICE = 0.05, 0.95
LOCK_WINDOW_MIN = 60        # enter only when the game starts within this many minutes
POLL_SECONDS    = 300
POOL_REFRESH_H  = 6
MAX_POS_PAGES   = 10        # 500 positions per page
HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, "copysim.db")
DASH = os.path.join(HERE, "dashboard.html")
REPORT = os.path.join(HERE, "REPORT.md")
STOP_FILE = os.path.join(HERE, "STOP")
ORDER_LOG = os.path.join(HERE, "orders.log")

PM = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
KAL = "https://api.elections.kalshi.com/trade-api/v2"
GAME_SERIES = ["KXNBAGAME", "KXNFLGAME", "KXNHLGAME", "KXNCAAFGAME", "KXNCAAMBGAME", "KXNCAAWBGAME",
               "KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME",
               "KXLIGAMXGAME", "KXMLSGAME", "KXUCLGAME", "KXUELGAME"]
TOTAL_SERIES = ["KXNBATOTAL", "KXNHLTOTAL", "KXNFLTOTAL", "KXNCAAFTOTAL", "KXNCAAMBTOTAL", "KXEPLTOTAL",
                "KXLALIGATOTAL", "KXSERIEATOTAL", "KXBUNDESLIGATOTAL", "KXLIGUE1TOTAL", "KXLIGAMXTOTAL", "KXMLSTOTAL"]
SPREAD_SERIES = ["KXNBASPREAD", "KXNHLSPREAD", "KXNFLSPREAD", "KXNCAAFSPREAD", "KXNCAAMBSPREAD"]

S = requests.Session()
S.headers["User-Agent"] = "copysim/3.0"
NOW = lambda: datetime.now(timezone.utc)

def log(msg):
    print(f"[{NOW().isoformat(timespec='seconds')}] {msg}", flush=True)

def get(url, **params):
    for attempt in range(3):
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1)); continue
            if r.status_code == 410:
                log(f"API retired: {url} -> {r.text[:200]}"); return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                log(f"request failed {url}: {e}"); return None
            time.sleep(2 * (attempt + 1))

def parse_ts(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

# ---------------- database ----------------
def db():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS pool(addr TEXT PRIMARY KEY, name TEXT);
    CREATE TABLE IF NOT EXISTS trades(
      id INTEGER PRIMARY KEY, ts TEXT, game TEXT, event_slug TEXT, cond TEXT, title TEXT, pick TEXT,
      pm_outcome_idx INT, start_ts TEXT, leaders TEXT, lead_usd REAL, opp_usd REAL, leader_avg REAL,
      kalshi_ticker TEXT, kalshi_side TEXT, price REAL, contracts INT, cost REAL, fee REAL,
      status TEXT, exit_price REAL, exit_fee REAL, exit_ts TEXT, exit_reason TEXT,
      result TEXT, pnl REAL, settled_ts TEXT, mode TEXT, order_id TEXT, exit_order_id TEXT);
    CREATE TABLE IF NOT EXISTS skips(id INTEGER PRIMARY KEY, ts TEXT, game TEXT, title TEXT, pick TEXT,
      reason TEXT, lead_usd REAL, opp_usd REAL, UNIQUE(title, pick, reason));
    """)
    if not c.execute("SELECT 1 FROM meta WHERE k='cash'").fetchone():
        c.execute("INSERT INTO meta VALUES('cash', ?)", (str(START_BANKROLL),))
        c.execute("INSERT INTO meta VALUES('start_ts', ?)", (NOW().isoformat(),))
    c.commit()
    return c

def cash(c, delta=None):
    v = float(c.execute("SELECT v FROM meta WHERE k='cash'").fetchone()[0])
    if delta is not None:
        v = round(v + delta, 2)
        c.execute("UPDATE meta SET v=? WHERE k='cash'", (str(v),))
    return v

# ---------------- vetted wallets (v2 leaderboard) ----------------
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

def refresh_pool(c):
    last = c.execute("SELECT v FROM meta WHERE k='pool_ts'").fetchone()
    if last and time.time() - float(last[0]) < POOL_REFRESH_H * 3600:
        return
    mo, al = leaderboard("month", 100), leaderboard("all", 200)
    if not mo or not al:
        log("leaderboard unavailable; keeping current pool"); return
    names = dict(mo + al)
    keep = {a for a, _ in mo} & {a for a, _ in al}
    c.execute("DELETE FROM pool")
    c.executemany("INSERT INTO pool VALUES(?,?)", [(a, names[a]) for a in keep])
    c.execute("INSERT OR REPLACE INTO meta VALUES('pool_ts', ?)", (str(time.time()),))
    c.commit()
    log(f"vetted wallets: {len(keep)}")

def all_positions(addr):
    """All open positions for a wallet via Polymarket's v2 data API, in the field names the rest of the code uses."""
    out, cur = [], None
    for _ in range(MAX_POS_PAGES):
        p = {"user": addr, "limit": 500}
        if cur:
            p["cursor"] = cur
        d = get(f"{PM}/v2/positions", **p) or {}
        for x in d.get("data") or []:
            if float(x.get("current_size") or 0) < 20:
                continue
            out.append({"conditionId": x["condition_id"], "outcomeIndex": x.get("outcome_index"),
                        "outcome": x.get("outcome"), "size": float(x.get("current_size") or 0),
                        "avgPrice": float(x.get("avg_price") or 0), "curPrice": float(x.get("current_price") or 0),
                        "currentValue": float(x.get("current_value") or 0), "redeemable": x.get("redeemable"),
                        "asset": x.get("token_id"), "slug": x.get("slug"), "eventSlug": x.get("event_slug"),
                        "title": x.get("title")})
        cur = (d.get("pagination") or {}).get("next_cursor")
        if not cur or not d.get("data"):
            break
    return out

# ---------------- consensus ----------------
def consensus(c):
    """Return {cond: {"sides": {idx: [leader dicts]}, "pos": sample position}} excluding hedged legs."""
    pool = c.execute("SELECT addr, name FROM pool").fetchall()
    markets = {}
    for addr, name in pool:
        by_cond = {}
        for p in all_positions(addr):
            if not p.get("redeemable"):
                by_cond.setdefault(p["conditionId"], []).append(p)
        for cond, legs in by_cond.items():
            if len({l["outcomeIndex"] for l in legs}) > 1:
                sizes = sorted(float(l["size"]) for l in legs)
                if sizes[-1] and sizes[0] / sizes[-1] > HEDGE_RATIO:
                    continue  # hedged: not a prediction
            p = max(legs, key=lambda l: float(l["size"]))
            m = markets.setdefault(cond, {"sides": {}, "pos": p})
            m["sides"].setdefault(p["outcomeIndex"], []).append(dict(
                addr=addr, name=name, avg=float(p.get("avgPrice") or 0),
                usd=float(p.get("currentValue") or 0), size=float(p["size"])))
        time.sleep(0.2)
    return markets

def best_side(m):
    tot = {k: sum(l["usd"] for l in v) for k, v in m["sides"].items()}
    main = max(tot, key=tot.get)
    lead = m["sides"][main]
    lead_usd = tot[main]
    opp = sum(v for k, v in tot.items() if k != main)
    avg = sum(l["avg"] * l["usd"] for l in lead) / lead_usd if lead_usd else 0
    return main, lead, lead_usd, opp, avg

# ---------------- Polymarket market info ----------------
_ev_cache = {}
def pm_event(slug):
    if slug not in _ev_cache:
        r = get(f"{GAMMA}/events", slug=slug)
        _ev_cache[slug] = r[0] if r else None
    return _ev_cache[slug]

def pm_market(ev, cond):
    for m in ev.get("markets", []):
        if m.get("conditionId") == cond:
            return m
    return None

def start_time(ev, mk):
    return parse_ts((mk or {}).get("gameStartTime")) or parse_ts(ev.get("startTime"))

def pm_teams(ev):
    """Two team names for the game, from the moneyline market or the event title."""
    for m in ev.get("markets", []):
        if m.get("slug") == ev.get("slug"):
            outs = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
            if len(outs) == 2 and "Yes" not in outs:
                return outs
    t = re.split(r"\s+vs\.?\s+", re.sub(r"\s*-\s*More Markets.*$", "", ev.get("title", "")), flags=re.I)
    return t if len(t) == 2 else None

# ---------------- Kalshi matching ----------------
def norm(s):
    s = (s or "").lower().replace("&", "and").replace("st.", "state").replace("'", "")
    s = re.sub(r"\b(fc|cf|ac|afc|sc|cd|ud|us|ss|calcio|club|de|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def name_sim(pm_name, k_name):
    a, b = norm(pm_name), norm(k_name)
    if not a or not b:
        return 0
    if a == b or a.startswith(b + " ") or b.startswith(a + " ") or b in a.split(" ") or a in b.split(" "):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

_k_cache = {}
def k_events(series):
    if series not in _k_cache:
        out, cur = [], None
        for _ in range(10):
            p = {"series_ticker": series, "status": "open", "limit": 200, "with_nested_markets": "true"}
            if cur:
                p["cursor"] = cur
            r = get(f"{KAL}/events", **p) or {}
            out += r.get("events", [])
            cur = r.get("cursor")
            if not cur:
                break
        _k_cache[series] = out
    return _k_cache[series]

def k_game(ev, start):
    """Find the Kalshi game event for a Polymarket event. Returns (event, {pm_team: kalshi_market})."""
    teams = pm_teams(ev)
    if not teams:
        return None, None
    m_slug = re.match(r"^([a-z]+)-([a-z0-9]+)-([a-z0-9]+)-\d{4}-\d\d-\d\d", ev.get("slug", ""))
    best = (0, None, None)
    for series in GAME_SERIES:
        for ke in k_events(series):
            kms = [m for m in ke.get("markets", []) if (m.get("yes_sub_title") or "").lower() not in ("tie", "draw")]
            if len(kms) != 2:
                continue
            # date sanity: Kalshi expected expiration within 2 days of start
            exp = parse_ts(kms[0].get("expected_expiration_time"))
            if start and exp and abs((exp - start).total_seconds()) > 2 * 86400:
                continue
            names = [m["yes_sub_title"] for m in kms]
            s1 = min(name_sim(teams[0], names[0]), name_sim(teams[1], names[1]))
            s2 = min(name_sim(teams[0], names[1]), name_sim(teams[1], names[0]))
            sc, order = (s1, (kms[0], kms[1])) if s1 >= s2 else (s2, (kms[1], kms[0]))
            # pro-league fallback: same team codes in the ticker (e.g. nba-sac-nop -> ...SACNOP)
            if sc < 0.85 and m_slug:
                code = (m_slug.group(2) + m_slug.group(3)).upper()
                suf = [m["ticker"].split("-")[-1] for m in kms]
                if ke["event_ticker"].endswith(code):
                    away = [m for m in kms if code.startswith(m["ticker"].split("-")[-1])]
                    if away:
                        home = [m for m in kms if m is not away[0]][0]
                        sc, order = 0.95, (away[0], home)
            if sc > best[0]:
                best = (sc, ke, order)
    if best[0] < 0.85:
        return None, None
    return best[1], {teams[0]: best[2][0], teams[1]: best[2][1]}

def k_related(game_ev, series_list):
    code = game_ev["event_ticker"].split("-", 1)[1]
    for s in series_list:
        for ke in k_events(s):
            if ke["event_ticker"].split("-", 1)[1] == code:
                return ke
    return None

def k_price(mk, side, kind="ask"):
    v = mk.get(f"{side}_{kind}_dollars")
    if v not in (None, ""):
        return float(v)
    v = mk.get(f"{side}_{kind}")
    return (v or 0) / 100

def match_kalshi(ev, mk, idx, start):
    """Map a Polymarket market + outcome to (kalshi_market, side) or (None, reason)."""
    q = mk.get("question") or ""
    outs = json.loads(mk["outcomes"]) if isinstance(mk["outcomes"], str) else mk["outcomes"]
    pick = outs[idx]
    gev, team_map = k_game(ev, start)
    if not gev:
        return None, "no Kalshi game found"
    # 1) two-way moneyline
    if mk.get("slug") == ev.get("slug") and pick in team_map:
        return (team_map[pick], "yes"), None
    # 2) soccer "Will X win" / draw
    mw = re.match(r"Will (.+?) win on", q)
    if mw and pick in ("Yes", "No"):
        team = mw.group(1)
        tm = max(team_map, key=lambda t: name_sim(team, t))
        if name_sim(team, tm) >= 0.85:
            return (team_map[tm], pick.lower()), None
        return None, "team not matched"
    if "draw" in q.lower() and pick in ("Yes", "No"):
        tie = [m for m in gev["markets"] if (m.get("yes_sub_title") or "").lower() in ("tie", "draw")]
        return ((tie[0], pick.lower()), None) if tie else (None, "no Kalshi draw market")
    # 3) totals
    mt = re.search(r"O/U\s*([\d.]+)", q)
    if mt and pick in ("Over", "Under") and not re.search(r"1H|1st|Half|Team Total|Player", q, re.I):
        line = mt.group(1)
        tev = k_related(gev, TOTAL_SERIES)
        if not tev:
            return None, "no Kalshi totals market"
        for km in tev["markets"]:
            if re.search(rf"\b{re.escape(line)}\b", km.get("yes_sub_title", "")) and "over" in km.get("yes_sub_title", "").lower():
                return (km, "yes" if pick == "Over" else "no"), None
        return None, f"Kalshi has no {line} line"
    # 4) spreads: "Spread: Team (-3.5)"
    ms = re.match(r"Spread:\s*(.+?)\s*\(-([\d.]+)\)", q)
    if ms and not re.search(r"1H|1st|Half", q, re.I):
        fav, line = ms.group(1), ms.group(2)
        sev = k_related(gev, SPREAD_SERIES)
        if not sev:
            return None, "no Kalshi spread market"
        fav_k = team_map.get(max(team_map, key=lambda t: name_sim(fav, t)))
        fav_abbr = fav_k["ticker"].split("-")[-1] if fav_k else ""
        fav_name = (fav_k or {}).get("yes_sub_title", "")
        for km in sev["markets"]:
            sub = km.get("yes_sub_title", "")
            suf = km["ticker"].split("-")[-1]
            if re.search(rf"\b{re.escape(line)}\b", sub) and (suf.startswith(fav_abbr) or (fav_name and fav_name.lower() in sub.lower())):
                return (km, "yes" if name_sim(pick, fav) >= 0.85 else "no"), None
        return None, f"Kalshi has no {line} spread line"
    return None, "market type not supported"

def kalshi_fee(n, p):
    return math.ceil(0.07 * n * p * (1 - p) * 100) / 100

# ---------------- execution (paper or live) ----------------
class PaperExec:
    live = False
    def buy(self, km, side, n, ask, limit):
        return n, ask, kalshi_fee(n, ask), None
    def sell(self, ticker, side, n, bid):
        return n, bid, (kalshi_fee(n, bid) if bid > 0 else 0.0), None
    def funds_ok(self, c, need):
        return cash(c) >= need

class LiveExec:
    live = True
    def __init__(self):
        from kalshi_client import KalshiClient
        self.k = KalshiClient()
    def _log(self, what, raw):
        with open(ORDER_LOG, "a") as f:
            f.write(json.dumps({"ts": NOW().isoformat(), "what": what, "resp": raw}, default=str) + "\n")
    def buy(self, km, side, n, ask, limit):
        filled, paid, fee, oid, raw = self.k.buy(km["ticker"], side, n, limit)
        self._log("buy", raw)
        return int(filled), paid, fee, oid
    def sell(self, ticker, side, n, bid):
        filled, got, fee, oid, raw = self.k.sell(ticker, side, n, max(bid, 0.01))
        self._log("sell", raw)
        return int(filled), got, fee, oid
    def funds_ok(self, c, need):
        return cash(c) >= need and self.k.balance_usd() >= need

EXEC = None
def executor():
    global EXEC
    if EXEC is None:
        if MODE == "live":
            if not LIVE_ACK:
                raise SystemExit("Live mode needs COPYSIM_LIVE_ACK=yes. Refusing to place real orders.")
            EXEC = LiveExec()
            log(f"LIVE MODE on Kalshi ({os.environ.get('KALSHI_ENV', 'prod')}). Caps: ${LIVE_MAX_PER_TRADE}/trade, "
                f"${LIVE_MAX_DAILY}/day, {LIVE_MAX_OPEN} open.")
        else:
            EXEC = PaperExec()
    return EXEC

def entry_blocked(c, need):
    """Safety gates checked before every new entry."""
    if os.path.exists(STOP_FILE):
        return "STOP file present"
    if executor().live:
        if need > LIVE_MAX_PER_TRADE + 0.5:
            return "over per-trade cap"
        today = NOW().date().isoformat()
        spent = c.execute("SELECT COALESCE(SUM(cost+fee),0) FROM trades WHERE mode='live' AND substr(ts,1,10)=?",
                          (today,)).fetchone()[0]
        if spent + need > LIVE_MAX_DAILY:
            return "daily cap reached"
        if c.execute("SELECT COUNT(*) FROM trades WHERE mode='live' AND status='open'").fetchone()[0] >= LIVE_MAX_OPEN:
            return "max open trades reached"
    return None

# ---------------- entries ----------------
def skip(c, game, title, pick, reason, lead=0, opp=0):
    c.execute("INSERT OR IGNORE INTO skips(ts,game,title,pick,reason,lead_usd,opp_usd) VALUES(?,?,?,?,?,?,?)",
              (NOW().isoformat(timespec="seconds"), game, title, pick, reason, lead, opp))

def find_entries(c, markets):
    now = NOW()
    held_games = {g for (g,) in c.execute("SELECT event_slug FROM trades")}
    candidates = {}
    for cond, m in markets.items():
        main, lead, lead_usd, opp, avg = best_side(m)
        p = m["pos"]
        if len(lead) < MIN_WALLETS or opp > OPP_MAX * lead_usd:
            continue
        ev = pm_event(p.get("eventSlug") or "")
        if not ev:
            continue
        mk = pm_market(ev, cond)
        if not mk:
            continue
        st = start_time(ev, mk)
        if not st or not (now < st <= now + timedelta(minutes=LOCK_WINDOW_MIN)):
            continue
        outs = json.loads(mk["outcomes"]) if isinstance(mk["outcomes"], str) else mk["outcomes"]
        prices = json.loads(mk["outcomePrices"]) if isinstance(mk["outcomePrices"], str) else mk["outcomePrices"]
        if not (MIN_PRICE < float(prices[main]) < MAX_PRICE):
            continue
        game = ev.get("title")
        if ev["slug"] in held_games:
            continue
        cand = dict(cond=cond, ev=ev, mk=mk, idx=main, lead=lead, lead_usd=lead_usd, opp=opp, avg=avg,
                    start=st, game=game, title=mk.get("question"), pick=outs[main])
        candidates.setdefault(ev["slug"], [])
        candidates[ev["slug"]].append(cand)
    for slug, cands in candidates.items():
        cands.sort(key=lambda x: -x["lead_usd"])   # one trade per game: most leader money first
        for cd in cands:
            res, why = match_kalshi(cd["ev"], cd["mk"], cd["idx"], cd["start"])
            if not res:
                skip(c, cd["game"], cd["title"], cd["pick"], why, cd["lead_usd"], cd["opp"]); continue
            km, side = res
            ask = k_price(km, side, "ask")
            if km.get("status") != "active" or not (0 < ask < 1):
                skip(c, cd["game"], cd["title"], cd["pick"], "Kalshi market not open"); continue
            if ask - cd["avg"] > MAX_SLIP:
                skip(c, cd["game"], cd["title"], cd["pick"],
                     f"Kalshi {ask*100:.0f}c vs leaders {cd['avg']*100:.0f}c", cd["lead_usd"], cd["opp"]); continue
            n = int(STAKE_USD // ask)
            need = n * ask + kalshi_fee(n, ask)
            blocked = entry_blocked(c, need)
            if blocked:
                skip(c, cd["game"], cd["title"], cd["pick"], blocked); continue
            if n < 1 or not executor().funds_ok(c, need):
                skip(c, cd["game"], cd["title"], cd["pick"], "not enough cash"); continue
            limit = min(round(cd["avg"] + MAX_SLIP, 2), 0.99)
            try:
                n, ask, fee, oid = executor().buy(km, side, n, ask, limit)
            except Exception as e:
                skip(c, cd["game"], cd["title"], cd["pick"], f"order failed: {str(e)[:120]}"); continue
            if n < 1:
                skip(c, cd["game"], cd["title"], cd["pick"], "order did not fill"); continue
            cost = round(n * ask, 2)
            cash(c, -(cost + fee))
            leaders = [dict(name=l["name"], addr=l["addr"], avg=round(l["avg"], 3), usd=round(l["usd"]))
                       for l in cd["lead"]]
            c.execute("""INSERT INTO trades(ts,game,event_slug,cond,title,pick,pm_outcome_idx,start_ts,leaders,
                lead_usd,opp_usd,leader_avg,kalshi_ticker,kalshi_side,price,contracts,cost,fee,status,mode,order_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
                (NOW().isoformat(timespec="seconds"), cd["game"], slug, cd["cond"], cd["title"], cd["pick"],
                 cd["idx"], cd["start"].isoformat(), json.dumps(leaders), round(cd["lead_usd"]), round(cd["opp"]),
                 round(cd["avg"], 3), km["ticker"], side, ask, n, cost, fee,
                 "live" if executor().live else "paper", oid))
            c.commit()
            log(f"ENTER {cd['game']} | {cd['title']} -> {cd['pick']} | {n} x {ask:.2f} {side.upper()} {km['ticker']}")
            break
    c.commit()

# ---------------- exits & settlement ----------------
def check_exits(c, markets):
    now = NOW()
    for tid, cond, idx, leaders, tk, side, n, cost, fee, st in c.execute(
            "SELECT id,cond,pm_outcome_idx,leaders,kalshi_ticker,kalshi_side,contracts,cost,fee,start_ts "
            "FROM trades WHERE status='open'").fetchall():
        if parse_ts(st) <= now:
            continue  # game started: hold to settlement
        m = markets.get(cond)
        holding = {l["addr"] for l in (m or {"sides": {}})["sides"].get(idx, [])}
        orig = json.loads(leaders)
        still = sum(1 for l in orig if l["addr"] in holding)
        if still * 2 > len(orig):
            continue
        km = (get(f"{KAL}/markets/{tk}") or {}).get("market", {})
        if km.get("status") != "active":
            continue
        bid = k_price(km, side, "bid")
        try:
            sold, bid, xfee, xoid = executor().sell(tk, side, n, bid)
        except Exception as e:
            log(f"exit order failed for trade #{tid}: {e}"); continue
        if sold < n:
            log(f"exit for trade #{tid} filled {sold}/{n}; holding the rest to settlement")
            if sold < 1:
                continue
        proceeds = round(sold * bid - xfee, 2)
        cash(c, proceeds)
        rest = n - sold
        if rest:   # partial exit: keep remainder open as a new row-equivalent by shrinking this trade
            frac = rest / n
            c.execute("UPDATE trades SET contracts=?, cost=?, fee=? WHERE id=?",
                      (rest, round(cost * frac, 2), round(fee * frac, 2), tid))
            c.commit(); continue
        c.execute("UPDATE trades SET status='sold', exit_price=?, exit_fee=?, exit_ts=?, exit_reason=?, pnl=?, exit_order_id=? WHERE id=?",
                  (bid, xfee, now.isoformat(timespec="seconds"),
                   f"{len(orig) - still} of {len(orig)} leaders sold or flipped before start",
                   round(proceeds - cost - fee, 2), xoid, tid))
        c.commit()
        log(f"EXIT trade #{tid}: leaders left before start, sold at {bid:.2f}")

def settle(c):
    for tid, tk, side, n, cost, fee in c.execute(
            "SELECT id,kalshi_ticker,kalshi_side,contracts,cost,fee FROM trades WHERE status='open'").fetchall():
        km = (get(f"{KAL}/markets/{tk}") or {}).get("market", {})
        if km.get("status") not in ("finalized", "settled") or km.get("result") not in ("yes", "no"):
            continue
        won = km["result"] == side
        payout = float(n) if won else 0.0
        cash(c, payout)
        c.execute("UPDATE trades SET status=?, result=?, pnl=?, settled_ts=? WHERE id=?",
                  ("won" if won else "lost", km["result"], round(payout - cost - fee, 2),
                   NOW().isoformat(timespec="seconds"), tid))
        c.commit()
        log(f"SETTLED trade #{tid}: {'WON' if won else 'lost'}")

# ---------------- reporting ----------------
def rows(c, sql, args=()):
    c.row_factory = sqlite3.Row
    out = [dict(r) for r in c.execute(sql, args)]
    c.row_factory = None
    return out

def summary(c):
    t = rows(c, "SELECT * FROM trades")
    open_cost = sum(r["cost"] + r["fee"] for r in t if r["status"] == "open")
    closed = [r for r in t if r["status"] in ("won", "lost", "sold")]
    return dict(cash=cash(c), equity=round(cash(c) + open_cost, 2), open=sum(r["status"] == "open" for r in t),
                won=sum(r["status"] == "won" for r in t), lost=sum(r["status"] == "lost" for r in t),
                sold=sum(r["status"] == "sold" for r in t), pnl=round(sum(r["pnl"] or 0 for r in closed), 2), trades=t)

def local(ts):
    d = parse_ts(ts)
    if not d:
        return ""
    d = d.astimezone()
    return f"{d:%a %b} {d.day}, {d.hour % 12 or 12}:{d:%M %p}"

def fmt_pnl(p):
    return "" if p is None else f"${p:+.2f}"

def write_report(c):
    s = summary(c)
    lines = [f"# Copy-trade {'LIVE' if MODE == 'live' else 'sim (paper)'}", "",
             f"Updated {local(NOW().isoformat())}. Bankroll **${s['equity']:.2f}** (start ${START_BANKROLL:.0f}): "
             f"${s['cash']:.2f} cash + open trades at cost.", "",
             f"Record: {s['won']} won, {s['lost']} lost, {s['sold']} sold early. Realized P&L ${s['pnl']:+.2f}. Open: {s['open']}.", "",
             "| Entered | Game | Pick | Leaders | Kalshi | Status | P&L |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in sorted(s["trades"], key=lambda r: r["ts"], reverse=True):
        L = json.loads(r["leaders"])
        lead = ", ".join(f"[{l['name']}](https://polymarket.com/profile/{l['addr']})" for l in L)
        lines.append(f"| {local(r['ts'])} | [{r['game']}](https://polymarket.com/event/{r['event_slug']}) | "
                     f"{r['title']} → {r['pick']} | {lead} (${r['lead_usd']:,.0f}) | "
                     f"{r['contracts']} × {r['price']*100:.0f}¢ {r['kalshi_side'].upper()} `{r['kalshi_ticker']}` | "
                     f"{r['status']} | {fmt_pnl(r['pnl'])} |")
    sk = rows(c, "SELECT * FROM skips ORDER BY id DESC LIMIT 30")
    if sk:
        lines += ["", "## Recent skips", "", "| When | Game | Pick | Reason |", "| --- | --- | --- | --- |"]
        lines += [f"| {local(r['ts'])} | {r['game']} | {r['title']} → {r['pick']} | {r['reason']} |" for r in sk]
    with open(REPORT + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(REPORT + ".tmp", REPORT)
    write_dashboard(c, s, sk)

E = html.escape
def write_dashboard(c, s, sk):
    def card(r):
        L = json.loads(r["leaders"])
        who = "".join(f"<li><a href='https://polymarket.com/profile/{l['addr']}' target='_blank'>{E(l['name'])}</a> "
                      f"${l['usd']:,} at {l['avg']*100:.0f}¢ · <a target='_blank' href='{PM}/v2/trades?user={l['addr']}&condition={r['cond']}'>trades</a></li>" for l in L)
        pnl = "" if r["pnl"] is None else f"<b class='{'up' if r['pnl'] > 0 else 'down'}'>{'+' if r['pnl'] > 0 else '−'}${abs(r['pnl']):.2f}</b>"
        ex = f"<p>Sold at {r['exit_price']*100:.0f}¢: {E(r['exit_reason'] or '')}</p>" if r["status"] == "sold" else ""
        return (f"<details class='t {r['status']}'><summary><span>{r['status'].title()}</span>"
                f"<span><b>{E(r['pick'])}</b><small>{E(r['title'] or '')} · starts {local(r['start_ts'])}</small></span>"
                f"<span>{r['contracts']} × {r['price']*100:.0f}¢ {r['kalshi_side'].upper()}</span>{pnl}</summary>"
                f"<div><p>{len(L)} vetted wallets agreed with ${r['lead_usd']:,.0f} (opposition ${r['opp_usd']:,.0f}), "
                f"averaging {r['leader_avg']*100:.0f}¢. Kalshi was {r['price']*100:.0f}¢.</p>{ex}<ul>{who}</ul>"
                f"<p><a target='_blank' href='https://polymarket.com/event/{r['event_slug']}'>Polymarket</a> · "
                f"<a target='_blank' href='{KAL}/markets/{r['kalshi_ticker']}'>Kalshi data</a></p></div></details>")
    trades = sorted(s["trades"], key=lambda r: r["ts"], reverse=True)
    skips = "".join(f"<li>{E(r['game'] or '')}: {E(r['title'] or '')} → {E(r['pick'] or '')} <small>({E(r['reason'])})</small></li>" for r in sk)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="120"><title>Copy-trade sim</title><style>
:root{{--bg:#eef1f4;--card:#fff;--ink:#14213d;--quiet:#5c677d;--rule:#d5dbe3;--up:#137a4b;--down:#b3261e;--accent:#2b59c3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1624;--card:#172133;--ink:#e6ebf3;--quiet:#9aa6ba;--rule:#2a3750;--up:#46c48a;--down:#ff7b72;--accent:#7aa2ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif;font-variant-numeric:tabular-nums}}
main{{max-width:860px;margin:0 auto;padding:24px 16px 48px}} h1{{margin:0;font-size:26px}} .q{{color:var(--quiet)}}
.big{{font-size:44px;font-weight:800;margin:12px 0 0}} .up{{color:var(--up)}} .down{{color:var(--down)}} a{{color:var(--accent)}}
.t{{background:var(--card);border:1px solid var(--rule);border-left:4px solid var(--rule);border-radius:8px;margin:8px 0}}
.t.won{{border-left-color:var(--up)}} .t.lost{{border-left-color:var(--down)}} .t.open{{border-left-color:var(--accent)}}
summary{{display:grid;grid-template-columns:60px 1fr auto auto;gap:10px;padding:12px;cursor:pointer;align-items:center}}
summary small{{display:block;color:var(--quiet)}} .t div{{padding:0 14px 10px;border-top:1px solid var(--rule)}}
@media (max-width:600px){{summary{{grid-template-columns:1fr auto}}}}</style></head><body><main>
<h1>Copy-trade sim</h1><p class="q">Updated {local(NOW().isoformat())} · paper money</p>
<p class="big">${s['equity']:.2f}</p><p class="q">from ${START_BANKROLL:.0f} · {s['won']} won, {s['lost']} lost, {s['sold']} sold early · {s['open']} open · cash ${s['cash']:.2f}</p>
<h2>Trades</h2>{''.join(card(r) for r in trades) or '<p class="q">No trades yet.</p>'}
<h2>Recent skips</h2><ul class="q">{skips or '<li>None</li>'}</ul></main></body></html>"""
    with open(DASH + ".tmp", "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(DASH + ".tmp", DASH)

def report(c):
    s = summary(c)
    print(f"Bankroll ${s['equity']:.2f} (cash ${s['cash']:.2f}) | {s['won']}W {s['lost']}L {s['sold']} sold | "
          f"P&L ${s['pnl']:+.2f} | open {s['open']}")
    for r in sorted(s["trades"], key=lambda r: r["ts"]):
        print(f"  {r['status']:5} {r['game']} | {r['title']} -> {r['pick']} | {r['contracts']}x{r['price']:.2f} "
              f"{r['kalshi_side']} | {fmt_pnl(r['pnl'])}")

# ---------------- main ----------------
def cycle(c):
    _ev_cache.clear(); _k_cache.clear()
    refresh_pool(c)
    markets = consensus(c)
    check_exits(c, markets)
    find_entries(c, markets)
    settle(c)
    write_report(c)

def main():
    c = db()
    if "report" in sys.argv:
        return report(c)
    if "check-keys" in sys.argv:
        from kalshi_client import KalshiClient
        k = KalshiClient()
        print(f"Kalshi ({os.environ.get('KALSHI_ENV', 'prod')}) balance: ${k.balance_usd():.2f}. Keys work.")
        return
    executor()
    if "--once" in sys.argv:
        cycle(c); report(c); return
    while True:
        try:
            cycle(c)
        except Exception as e:
            log(f"cycle error (continuing): {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
