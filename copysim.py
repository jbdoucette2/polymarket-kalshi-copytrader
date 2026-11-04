"""
copysim.py v3 - copy vetted Polymarket sports wallets onto Kalshi. Paper by default; live when enabled.

Rules (match the manual sim):
  * Vetted wallets: top 100 on Polymarket's SPORTS leaderboard this month AND top 200 all-time.
  * Trade only when 3+ vetted wallets hold the same side (or 2 with $5k+ between them) and vetted money on the other
    side is under 10% of theirs. Hedged wallets (both sides, smaller > 20%) don't count.
  * Enter only in about the last hour before the game starts (LOCK_WINDOW_MIN).
  * Kalshi price must be within 3c of the leaders' average entry. Same line only.
  * One trade per game (the side with the most leader money). Flat $10 per trade.
  * If the leaders sell or flip before the game starts, the sim sells at Kalshi's bid.
  * Held to settlement otherwise.

Markets matched to Kalshi: moneylines, soccer win/draw, totals (O/U), spreads,
for NBA, NFL, NHL, college football and basketball, and major soccer leagues.

Entries happen only in the pre-game check, 5 to 25 minutes before the start (PREGAME_WINDOW).

Hourly snapshot and plan:    python3 copysim.py hourly     (never opens trades; writes pregame_plan.json)
Pre-game decision:           python3 copysim.py pregame    (optionally --window 15-45)
15-minute check:             python3 copysim.py verify     (open trades starting within 25 min must still pass
                                                            the 2-wallet and opposition rules, or they are sold;
                                                            next_check.json says when to run it next)
Run continuously (laptop):   python3 copysim.py            (hourly snapshot + pre-game check every 5 min)
Run one cycle:               python3 copysim.py --once     (snapshot + pre-game check)
Print results:               python3 copysim.py report
Test live credentials:       python3 copysim.py check-keys

Modes (COPYSIM_MODE): paper (default, no keys, no orders) | live (real orders on Kalshi).
Live mode also requires COPYSIM_LIVE_ACK=yes and Kalshi keys (see kalshi_client.py),
and is capped by the LIVE_* limits below. Create a file named STOP next to this script
to halt all new entries immediately in any mode.
COPYSIM_DB (default copysim.db) picks the history file. REPORT.md, dashboard.html and pregame_plan.json
are written next to it with the same suffix (copysim_live.db -> REPORT_live.md).
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
MIN_WALLETS     = 2         # never fewer than this many vetted wallets on the pick
MIN_POINTS      = 3         # conviction needed: 1 point per vetted wallet, +1 when their money reaches BIG_MONEY_USD
BIG_MONEY_USD   = 5000      # so 3+ wallets pass, and 2 wallets pass with $5k+ between them (backtest, Nov 2-3)
# Time-out: a vetted wallet that keeps losing us money or trades a market hundreds of times is benched. A benched
# wallet doesn't count for or against any trade, but its pre-game picks are kept as a shadow record; it comes back
# once that record proves it. `python3 copysim.py timeout NAME [reason]` / `release NAME` do it by hand.
TIMEOUT_STREAK  = 3         # lost our last this-many copied trades in a row
TIMEOUT_MIN_N, TIMEOUT_MAX_WIN = 5, 0.40   # or won under 40% of 5+ copied trades
TRADER_MEDIAN, TRADER_MIN_N, TRADER_ONE = 30, 3, 200   # or a median of 30+ trades per copied market (3+ markets), or 200+ on one
RELEASE_N, RELEASE_WIN = 5, 0.60           # released after shadow picks in 5+ games settle, 60%+ won ...
RELEASE_EDGE    = 0.05      # ... and beating their average price by 5+ points (so 90¢ favorites don't count)
SHADOW_MIN_USD  = 500       # shadow picks are positions of $500+ priced MIN_PRICE-MAX_PRICE, like the ones we'd copy
SEED_TIMEOUTS = {           # benched by hand when the time-out list started
    "0x821dab0565ebf5b327f51db06223fdcfe01acf16": ("Flaznorp", "trader: 400-1,300 trades per game; 4-11 on our trades"),
}
OPP_MAX        = 0.10      # opposing vetted money must be under 10% of the agreeing side
HEDGE_RATIO     = 0.20
MAX_SLIP        = 0.03
MIN_PRICE, MAX_PRICE = 0.05, 0.95
LOCK_WINDOW_MIN = 65        # enter only when the game starts within this many minutes (an hourly run's
                            # 5-65 window leaves no gap between one run and the next)
PREGAME_WINDOW  = (5, 25)   # the pre-game check decides games starting this many minutes from now
PLAN_LEAD_MIN   = 15        # pregame_plan.json asks for the check this long before the start
VERIFY_LEAD_MIN = 15        # the 15-minute check runs this long before each open trade's start
VERIFY_AHEAD_MIN = 20       # checks run every 15 min, so each open trade is re-checked once or twice in its last 20 min
SNAPSHOT_HOURS  = 24        # the hourly snapshot covers games starting within this many hours
TIMELINE_HOURS  = 48        # REPORT.md shows position changes for games that started this recently
POLL_SECONDS    = 300
HOURLY_SECONDS  = 3600
POOL_REFRESH_H  = 6
MAX_POS_PAGES   = 10        # 500 positions per page
HERE = os.path.dirname(os.path.abspath(__file__))
DB   = os.path.join(HERE, os.environ.get("COPYSIM_DB", "copysim.db"))
_base = os.path.splitext(os.path.basename(DB))[0]
SUFFIX = _base[len("copysim"):] if _base.startswith("copysim") else "_" + _base
DASH = os.path.join(os.path.dirname(DB), f"dashboard{SUFFIX}.html")
REPORT = os.path.join(os.path.dirname(DB), f"REPORT{SUFFIX}.md")
PLAN = os.path.join(os.path.dirname(DB), f"pregame_plan{SUFFIX}.json")
NEXT_CHECK = os.path.join(os.path.dirname(DB), f"next_check{SUFFIX}.json")
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

FAILED = []   # URLs that failed this cycle; missing data must not read as "the leaders left"
KALSHI_GAP = 0.2   # seconds between Kalshi requests; its public API rate-limits bursts
KALSHI_WAIT_BUDGET = 120   # seconds of rate-limit waiting per cycle; after that Kalshi is skipped so the run still finishes
_k_last = [0.0]
_k_gap = [KALSHI_GAP]      # grows after each 429, reset every cycle
_k_waited = [0.0]

def reset_kalshi_pacing():
    _k_gap[0], _k_waited[0] = KALSHI_GAP, 0.0

def get(url, **params):
    kal = url.startswith(KAL)
    for attempt in range(5):   # rate limits get 5 tries with growing waits; other errors get 3
        if kal:
            if _k_waited[0] >= KALSHI_WAIT_BUDGET:
                FAILED.append(url); return None
            time.sleep(max(0.0, _k_last[0] + _k_gap[0] - time.time()))
            _k_last[0] = time.time()
        try:
            r = S.get(url, params=params, timeout=30)
            if r.status_code == 404:
                return None
            if r.status_code == 429:
                wait = r.headers.get("Retry-After", "")
                wait = min(float(wait) if wait.isdigit() else 3 * 2 ** attempt, 30)
                if kal:
                    _k_gap[0] = min(_k_gap[0] * 2, 2.0)
                    if _k_waited[0] + wait > KALSHI_WAIT_BUDGET:
                        _k_waited[0] = KALSHI_WAIT_BUDGET
                        log(f"Kalshi rate limit: waited {KALSHI_WAIT_BUDGET}s this run, skipping the rest of its calls")
                        FAILED.append(url); return None
                    _k_waited[0] += wait
                time.sleep(wait); continue
            if r.status_code == 410:
                log(f"API retired: {url} -> {r.text[:200]}"); return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt >= 2:
                log(f"request failed {url}: {e}"); FAILED.append(url); return None
            time.sleep(2 * (attempt + 1))
    log(f"rate limited: {url}"); FAILED.append(url)
    return None

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
    CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, event_slug TEXT, game TEXT, cond TEXT, title TEXT, pick TEXT,
      wallets TEXT, n_wallets INT, lead_usd REAL, opp_usd REAL, leader_avg REAL,
      kalshi_ticker TEXT, kalshi_side TEXT, kalshi_ask REAL, start_ts TEXT, passes INT, reason TEXT, fails INT);
    CREATE INDEX IF NOT EXISTS snapshots_cond ON snapshots(cond, id);
    CREATE INDEX IF NOT EXISTS snapshots_event ON snapshots(event_slug, id);
    CREATE TABLE IF NOT EXISTS changes(
      id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, event_slug TEXT, game TEXT, cond TEXT, title TEXT, note TEXT);
    CREATE INDEX IF NOT EXISTS changes_event ON changes(event_slug, id);
    CREATE TABLE IF NOT EXISTS timeouts(addr TEXT PRIMARY KEY, name TEXT, reason TEXT, since TEXT,
      released TEXT, release_note TEXT);
    CREATE TABLE IF NOT EXISTS shadow(addr TEXT, cond TEXT, idx INT, start_ts TEXT, avg REAL, usd REAL, ts TEXT,
      game TEXT, title TEXT, pick TEXT, won INT, PRIMARY KEY(addr, cond));
    CREATE TABLE IF NOT EXISTS wallet_market_trades(addr TEXT, cond TEXT, n INT, PRIMARY KEY(addr, cond));
    """)
    for addr, (name, reason) in SEED_TIMEOUTS.items():
        c.execute("INSERT OR IGNORE INTO timeouts(addr,name,reason,since) VALUES(?,?,?,?)",
                  (addr, name, reason, NOW().isoformat(timespec="seconds")))
    c.commit()
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
    """Return {cond: {"sides": {idx: [leader dicts]}, "pos": sample position, "bench": [...]}} excluding hedged legs.
    Wallets in time-out go to "bench" instead of "sides", so they count neither for nor against a trade."""
    pool = c.execute("SELECT addr, name FROM pool").fetchall()
    benched = benched_wallets(c)
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
            m = markets.setdefault(cond, {"sides": {}, "pos": p, "bench": []})
            leg = dict(addr=addr, name=name, avg=float(p.get("avgPrice") or 0),
                       usd=float(p.get("currentValue") or 0), size=float(p["size"]))
            if addr.lower() in benched:
                m["bench"].append(dict(leg, idx=p["outcomeIndex"], outcome=p.get("outcome")))
            else:
                m["sides"].setdefault(p["outcomeIndex"], []).append(leg)
        time.sleep(0.2)
    return {cond: m for cond, m in markets.items() if m["sides"] or m["bench"]}

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

_kg_cache = {}
def k_game(ev, start):
    """Find the Kalshi game event for a Polymarket event. Returns (event, {pm_team: kalshi_market})."""
    if ev.get("slug") not in _kg_cache:
        _kg_cache[ev.get("slug")] = _k_game(ev, start)
    return _kg_cache[ev.get("slug")]

def _k_game(ev, start):
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

# ---------------- rules ----------------
def conviction(n, usd):
    """Points for n vetted wallets holding usd on one side, and why it falls short (None if it passes)."""
    pts = n + (usd >= BIG_MONEY_USD)
    if n >= MIN_WALLETS and pts >= MIN_POINTS:
        return pts, None
    return pts, (f"only {n} vetted wallet{'' if n == 1 else 's'} with {money(usd)} "
                 f"(needs {MIN_POINTS}+ wallets, or {MIN_WALLETS} with {money(BIG_MONEY_USD)}+)")

def evaluate(c, markets, lo_min, hi_min):
    """Every vetted-wallet market whose game starts lo_min..hi_min minutes from now, checked against every
    rule except timing (the window is the timing). Each candidate lists all the rules it fails."""
    now = NOW()
    lo, hi = now + timedelta(minutes=lo_min), now + timedelta(minutes=hi_min)
    held_games = {g for (g,) in c.execute("SELECT event_slug FROM trades")}
    out = []
    for cond, m in markets.items():
        p = m["pos"]
        slug = p.get("eventSlug") or ""
        d = re.search(r"(\d{4}-\d\d-\d\d)$", slug)   # dated game slugs: skip far-off games without a lookup
        if d and abs((parse_ts(d.group(1)) - now).total_seconds()) > 2 * 86400:
            continue
        ev = pm_event(slug)
        if not ev:
            continue
        mk = pm_market(ev, cond)
        if not mk:
            continue
        st = start_time(ev, mk)
        if not st or not (lo < st <= hi):
            continue
        outs = json.loads(mk["outcomes"]) if isinstance(mk["outcomes"], str) else mk["outcomes"]
        for b in m["bench"]:   # a benched wallet's pick before the start, for its shadow record
            if b["usd"] < SHADOW_MIN_USD or not (MIN_PRICE < b["avg"] < MAX_PRICE):
                continue
            # the latest pre-start side counts: a wallet that switches sides before the start is graded on the switch
            c.execute("INSERT INTO shadow(addr,cond,idx,start_ts,avg,usd,ts,game,title,pick) VALUES(?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(addr,cond) DO UPDATE SET idx=excluded.idx, avg=excluded.avg, usd=excluded.usd, "
                      "pick=excluded.pick WHERE won IS NULL",
                      (b["addr"].lower(), cond, b["idx"], st.astimezone(timezone.utc).isoformat(timespec="seconds"), b["avg"],
                       b["usd"], now.isoformat(timespec="seconds"), ev.get("title"), mk.get("question"),
                       outs[b["idx"]] if b["idx"] is not None and b["idx"] < len(outs) else b.get("outcome")))
        if not m["sides"]:
            continue
        main, lead, lead_usd, opp, avg = best_side(m)
        prices = json.loads(mk["outcomePrices"]) if isinstance(mk.get("outcomePrices"), str) else mk.get("outcomePrices")
        fails = []
        short = conviction(len(lead), lead_usd)[1]
        if short:
            fails.append(short)
        if opp > OPP_MAX * lead_usd:
            fails.append(f"opposition {opp / lead_usd:.0%}" if lead_usd else "no money on the leading side")
        price = float(prices[main]) if prices else 0
        if not (MIN_PRICE < price < MAX_PRICE):
            fails.append(f"Polymarket price {price * 100:.0f}¢ outside {MIN_PRICE * 100:.0f}–{MAX_PRICE * 100:.0f}¢")
        if ev["slug"] in held_games:
            fails.append("already traded this game")
        km = side = ask = None
        res, why = match_kalshi(ev, mk, main, st)
        if not res:
            fails.append(why)
        else:
            km, side = res
            ask = k_price(km, side, "ask")
            if km.get("status") != "active" or not (0 < ask < 1):
                fails.append("Kalshi market not open")
            elif ask - avg > MAX_SLIP:
                fails.append(f"Kalshi {ask*100:.0f}c vs leaders {avg*100:.0f}c")
        out.append(dict(cond=cond, ev=ev, mk=mk, idx=main, lead=lead, lead_usd=lead_usd, opp=opp, avg=avg,
                        start=st.astimezone(timezone.utc), game=ev.get("title"), slug=ev["slug"],
                        title=mk.get("question"), pick=outs[main], km=km, side=side, ask=ask,
                        fails=fails, held=ev["slug"] in held_games))
    c.commit()
    return out

# ---------------- snapshots & change notes ----------------
def money(x):
    return f"${x / 1000:.1f}k" if abs(x) >= 1000 else f"${x:.0f}"

def change_notes(prev, cur):
    """Human-readable differences between two snapshots of the same market."""
    if not prev or not prev["n_wallets"]:
        if not cur["n_wallets"]:
            return []
        note = f"{'first seen' if not prev else 'back'}: {cur['n_wallets']} vetted on {cur['pick']} ({money(cur['lead_usd'])})"
        return [note + (", passes" if cur["passes"] else f", fails: {cur['reason']}")]
    if not cur["n_wallets"]:
        return [f"all vetted wallets left {prev['pick']}"]
    notes = []
    if cur["pick"] != prev["pick"]:
        notes.append(f"majority flipped to {cur['pick']} ({money(cur['lead_usd'])} vs {money(cur['opp_usd'])})")
    else:
        was = {w["name"]: w["usd"] for w in json.loads(prev["wallets"])}
        now = {w["name"]: w["usd"] for w in json.loads(cur["wallets"])}
        for n, usd in now.items():
            if n not in was:
                notes.append(f"{n} joined {cur['pick']} (+{money(usd)})")
            elif abs(usd - was[n]) >= max(1000, 0.25 * was[n]):
                notes.append(f"{n} {'added' if usd > was[n] else 'cut'} {money(abs(usd - was[n]))} on {cur['pick']}")
        notes += [f"{n} left" for n in was if n not in now]
    if prev["kalshi_ask"] and cur["kalshi_ask"] and round(prev["kalshi_ask"] * 100) != round(cur["kalshi_ask"] * 100):
        notes.append(f"Kalshi {prev['kalshi_ask'] * 100:.0f}¢ → {cur['kalshi_ask'] * 100:.0f}¢")
    elif cur["kalshi_ask"] and not prev["kalshi_ask"]:
        notes.append(f"Kalshi matched at {cur['kalshi_ask'] * 100:.0f}¢")
    if cur["passes"] and not prev["passes"]:
        notes.append("now passes")
    elif prev["passes"] and not cur["passes"]:
        notes.append(f"now fails: {cur['reason']}")
    return notes

SNAP_COLS = ["ts", "kind", "event_slug", "game", "cond", "title", "pick", "wallets", "n_wallets", "lead_usd", "opp_usd",
             "leader_avg", "kalshi_ticker", "kalshi_side", "kalshi_ask", "start_ts", "passes", "reason", "fails"]

def record(c, row):
    prev = rows(c, "SELECT * FROM snapshots WHERE cond=? ORDER BY id DESC LIMIT 1", (row["cond"],))
    prev = prev[0] if prev else None
    c.execute(f"INSERT INTO snapshots({','.join(SNAP_COLS)}) VALUES({','.join('?' * len(SNAP_COLS))})",
              [row[k] for k in SNAP_COLS])
    # notes only for markets that are (or were) passing or one rule away; the rest is noise
    if row["fails"] <= 1 or (prev and prev["fails"] <= 1):
        for note in change_notes(prev, row):
            c.execute("INSERT INTO changes(ts,kind,event_slug,game,cond,title,note) VALUES(?,?,?,?,?,?,?)",
                      (row["ts"], row["kind"], row["event_slug"], row["game"], row["cond"], row["title"], note))

def snapshot(c, cands, kind, lo_min, hi_min, pregame_window=None):
    """Record one snapshot row per market, plus change notes against each market's previous snapshot.
    Rows for games inside pregame_window (minutes from now) are labelled 'pregame'."""
    now = NOW()
    ts = now.isoformat(timespec="seconds")
    def kind_of(start):
        if pregame_window and now + timedelta(minutes=pregame_window[0]) < start <= now + timedelta(minutes=pregame_window[1]):
            return "pregame"
        return kind
    for cd in cands:
        lead = sorted(cd["lead"], key=lambda l: -l["usd"])
        record(c, dict(ts=ts, kind=kind_of(cd["start"]), event_slug=cd["slug"], game=cd["game"], cond=cd["cond"], title=cd["title"],
                       pick=cd["pick"], wallets=json.dumps([dict(name=l["name"], usd=round(l["usd"])) for l in lead]),
                       n_wallets=len(lead), lead_usd=round(cd["lead_usd"]), opp_usd=round(cd["opp"]),
                       leader_avg=round(cd["avg"], 3), kalshi_ticker=(cd["km"] or {}).get("ticker"),
                       kalshi_side=cd["side"], kalshi_ask=cd["ask"], start_ts=cd["start"].isoformat(),
                       passes=int(not cd["fails"]), reason="; ".join(cd["fails"]), fails=len(cd["fails"])))
    # markets snapshotted before in this window that no vetted wallet holds any more
    # (skipped when any lookup failed: a missing market would look like everyone left)
    seen = {cd["cond"] for cd in cands}
    lo = (now + timedelta(minutes=lo_min)).isoformat()
    hi = (now + timedelta(minutes=hi_min)).isoformat()
    for prev in [] if FAILED else rows(c, "SELECT s.* FROM snapshots s JOIN (SELECT cond, MAX(id) id FROM snapshots "
                        "GROUP BY cond) l ON s.id=l.id WHERE s.start_ts>? AND s.start_ts<=? AND s.n_wallets>0", (lo, hi)):
        if prev["cond"] not in seen:
            record(c, dict(prev, ts=ts, kind=kind_of(parse_ts(prev["start_ts"])), wallets="[]", n_wallets=0, lead_usd=0, opp_usd=0, kalshi_ask=None,
                           passes=0, reason="no vetted wallets hold it now", fails=1))
    # once a game has started, drop markets that were never passing or one rule away: no report shows them
    c.execute("DELETE FROM snapshots WHERE start_ts<? AND cond NOT IN (SELECT cond FROM trades) AND cond IN "
              "(SELECT cond FROM snapshots GROUP BY cond HAVING MIN(fails)>=2)", (ts,))
    c.commit()

def write_plan(cands):
    """pregame_plan.json: games with a market passing or one rule away, and when to run the pre-game check."""
    games = {}
    for cd in cands:
        if cd["held"] or len(cd["fails"]) > 1:
            continue
        games.setdefault(cd["slug"], []).append(cd)
    plan = []
    for slug, cs in sorted(games.items(), key=lambda kv: kv[1][0]["start"]):
        best = min(cs, key=lambda x: (len(x["fails"]), -x["lead_usd"]))
        st = cs[0]["start"]
        plan.append(dict(event_slug=slug, game=best["game"],
                         start=st.isoformat(timespec="seconds").replace("+00:00", "Z"),
                         check_at=(st - timedelta(minutes=PLAN_LEAD_MIN)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                         passing=sum(not x["fails"] for x in cs), near=sum(len(x["fails"]) == 1 for x in cs),
                         best=f"{best['title']} → {best['pick']} ({money(best['lead_usd'])}, {len(best['lead'])} wallets)",
                         status="passes" if not best["fails"] else f"fails: {best['fails'][0]}"))
    with open(PLAN + ".tmp", "w", encoding="utf-8") as f:
        json.dump(dict(updated=NOW().isoformat(timespec="seconds").replace("+00:00", "Z"), games=plan), f, indent=2,
                  ensure_ascii=False)
    os.replace(PLAN + ".tmp", PLAN)
    log(f"plan: {len(plan)} game(s) to check before start")
    return plan

# ---------------- entries ----------------
def skip(c, game, title, pick, reason, lead=0, opp=0):
    c.execute("INSERT OR IGNORE INTO skips(ts,game,title,pick,reason,lead_usd,opp_usd) VALUES(?,?,?,?,?,?,?)",
              (NOW().isoformat(timespec="seconds"), game, title, pick, reason, lead, opp))

def decide(c, cands):
    """The pre-game decision: per game, the passing market with the most leader money
    (ties: more wallets, then lower Kalshi price) goes to the executor."""
    games = {}
    for cd in cands:
        if not cd["held"]:
            games.setdefault(cd["slug"], []).append(cd)
    for slug, cs in games.items():
        ok = sorted((x for x in cs if not x["fails"]), key=lambda x: (-x["lead_usd"], -len(x["lead"]), x["ask"]))
        for cd in ok:
            if enter(c, cd):
                break
        else:
            near = [x for x in cs if len(x["fails"]) <= 1] or [min(cs, key=lambda x: len(x["fails"]))]
            for x in near:
                if x["fails"]:
                    skip(c, x["game"], x["title"], x["pick"], "; ".join(x["fails"]), x["lead_usd"], x["opp"])
            log(f"no trade: {cs[0]['game']} ({len(ok)} passing, {len(cs)} markets)")
    c.commit()

def enter(c, cd):
    """Size, safety-gate and place one entry through the executor. Returns True if it filled."""
    km, side, ask, slug = cd["km"], cd["side"], cd["ask"], cd["slug"]
    n = int(STAKE_USD // ask)
    need = n * ask + kalshi_fee(n, ask)
    blocked = entry_blocked(c, need)
    if blocked:
        skip(c, cd["game"], cd["title"], cd["pick"], blocked); return False
    if n < 1 or not executor().funds_ok(c, need):
        skip(c, cd["game"], cd["title"], cd["pick"], "not enough cash"); return False
    limit = min(round(cd["avg"] + MAX_SLIP, 2), 0.99)
    try:
        n, ask, fee, oid = executor().buy(km, side, n, ask, limit)
    except Exception as e:
        skip(c, cd["game"], cd["title"], cd["pick"], f"order failed: {str(e)[:120]}"); return False
    if n < 1:
        skip(c, cd["game"], cd["title"], cd["pick"], "order did not fill"); return False
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
    return True

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
        sell_open(c, tid, tk, side, n, cost, fee, f"{len(orig) - still} of {len(orig)} leaders sold or flipped before start")

def sell_open(c, tid, tk, side, n, cost, fee, reason):
    """Sell an open trade at Kalshi's bid through the executor. Returns True once it is fully sold."""
    km = (get(f"{KAL}/markets/{tk}") or {}).get("market", {})
    if km.get("status") != "active":
        log(f"can't sell trade #{tid}: Kalshi market {tk} is {km.get('status') or 'unreachable'}"); return False
    bid = k_price(km, side, "bid")
    try:
        sold, bid, xfee, xoid = executor().sell(tk, side, n, bid)
    except Exception as e:
        log(f"exit order failed for trade #{tid}: {e}"); return False
    if sold < n:
        log(f"exit for trade #{tid} filled {sold}/{n}; holding the rest to settlement")
        if sold < 1:
            return False
    proceeds = round(sold * bid - xfee, 2)
    cash(c, proceeds)
    rest = n - sold
    if rest:   # partial exit: keep remainder open as a new row-equivalent by shrinking this trade
        frac = rest / n
        c.execute("UPDATE trades SET contracts=?, cost=?, fee=? WHERE id=?",
                  (rest, round(cost * frac, 2), round(fee * frac, 2), tid))
        c.commit(); return False
    c.execute("UPDATE trades SET status='sold', exit_price=?, exit_fee=?, exit_ts=?, exit_reason=?, pnl=?, exit_order_id=? WHERE id=?",
              (bid, xfee, NOW().isoformat(timespec="seconds"), reason, round(proceeds - cost - fee, 2), xoid, tid))
    c.commit()
    log(f"EXIT trade #{tid}: {reason}; sold at {bid:.2f}")
    return True

def verify_open(c, markets, ahead_min):
    """The 15-minute check: every open trade whose game starts within ahead_min must still pass the candidate
    rules (conviction(): 3+ vetted wallets, or 2 with BIG_MONEY_USD+; opposition under OPP_MAX). One that fails is sold; price isn't re-checked."""
    now, ts = NOW(), NOW().isoformat(timespec="seconds")
    checked = 0
    for tid, game, slug, cond, title, pick, idx, tk, side, n, cost, fee, st in c.execute(
            "SELECT id,game,event_slug,cond,title,pick,pm_outcome_idx,kalshi_ticker,kalshi_side,contracts,cost,fee,start_ts "
            "FROM trades WHERE status='open' AND pm_outcome_idx IS NOT NULL").fetchall():
        if not now < parse_ts(st) <= now + timedelta(minutes=ahead_min):
            continue
        checked += 1
        sides = (markets.get(cond) or {"sides": {}})["sides"]
        ours = sides.get(idx, [])
        ours_usd = sum(l["usd"] for l in ours)
        opp = sum(l["usd"] for k, v in sides.items() if k != idx for l in v)
        fails = []
        short = conviction(len(ours), ours_usd)[1]
        if short:
            fails.append(f"{pick} has {short}" if len(ours) else f"no vetted wallets left on {pick}")
        if opp > OPP_MAX * ours_usd:
            fails.append(f"opposition {opp / ours_usd:.0%}" if ours_usd else "no vetted money left on the pick")
        summary = f"{len(ours)} wallets, {money(ours_usd)} for vs {money(opp)} against"
        if fails:
            reason = "failed the 15-minute check: " + "; ".join(fails)
            note = f"15-min check: {'; '.join(fails)} ({summary}); " + ("sold" if sell_open(c, tid, tk, side, n, cost, fee, reason) else "sell failed, holding")
        else:
            note = f"15-min check: still passes ({summary}); holding"
            log(f"HOLD trade #{tid} {game} -> {pick}: {summary}")
        c.execute("INSERT INTO changes(ts,kind,event_slug,game,cond,title,note) VALUES(?,?,?,?,?,?,?)",
                  (ts, "verify", slug, game, cond, title, note))
    c.commit()
    log(f"15-minute check: {checked} open trade(s) starting within {ahead_min} min")

def game_clock(ev):
    """Polymarket's live game feed: (state, score, clock), state being pre, live or final."""
    if not ev:
        return "pre", None, None
    if ev.get("ended") or ev.get("closed"):
        return "final", ev.get("score") or None, "Final"
    if ev.get("live"):
        clock = " · ".join(str(x) for x in (ev.get("period"), ev.get("elapsed")) if x)
        return "live", ev.get("score") or None, clock or "Live"
    return "pre", None, None

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_LEAGUES = {   # Polymarket slug prefix -> ESPN scoreboard; leagues missing here use Polymarket's own feed
    "nba": "basketball/nba", "nfl": "football/nfl", "nhl": "hockey/nhl", "cfb": "football/college-football",
    "cbb": "basketball/mens-college-basketball", "wnba": "basketball/wnba", "mlb": "baseball/mlb",
    "ucl": "soccer/uefa.champions", "uel": "soccer/uefa.europa", "epl": "soccer/eng.1", "elc": "soccer/eng.2",
    "el1": "soccer/eng.3", "lal": "soccer/esp.1", "bun": "soccer/ger.1", "sea": "soccer/ita.1", "fl1": "soccer/fra.1",
    "mls": "soccer/usa.1", "bra": "soccer/bra.1", "col1": "soccer/col.1", "per1": "soccer/per.1", "ecu1": "soccer/ecu.1",
    "bel1": "soccer/bel.1", "acle": "soccer/afc.champions",
}
_espn_cache = {}
_NAME_FILLER = {"fc", "cf", "sk", "ac", "as", "afc", "sc", "fk", "cd", "club", "the", "of", "de", "st", "state", "university"}

def _name_tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in _NAME_FILLER}

def espn_clock(slug, game, start):
    """(state, score, clock) from ESPN's scoreboard, score in the order of Polymarket's title, or None if
    the league isn't mapped, ESPN is unreachable, or no game matches both team names within 3 hours of start."""
    league = ESPN_LEAGUES.get(slug.split("-")[0])
    sides = game.split(" vs. ")
    if not league or len(sides) != 2 or not start:
        return None
    day = (start - timedelta(hours=5)).strftime("%Y%m%d")   # ESPN's scoreboard days are US Eastern
    key = (league, day)
    if key not in _espn_cache:
        extra = dict(groups=50, limit=500) if league.endswith("college-basketball") or league.endswith("college-football") else {}
        _espn_cache[key] = (get(f"{ESPN}/{league}/scoreboard", dates=day, **extra) or {}).get("events", [])
    want = [_name_tokens(s) for s in sides]
    best = None
    for ev in _espn_cache[key]:
        st = parse_ts(ev.get("date"))
        if not st or abs((st - start).total_seconds()) > 3 * 3600:
            continue
        comps = (ev.get("competitions") or [{}])[0].get("competitors", [])
        if len(comps) != 2:
            continue
        names = [_name_tokens(" ".join(str(t.get(k, "")) for k in ("displayName", "shortDisplayName", "name", "location")))
                 for t in (x.get("team", {}) for x in comps)]
        for order in ((0, 1), (1, 0)):
            hits = [len(want[i] & names[j]) for i, j in zip((0, 1), order)]
            if min(hits) and (best is None or sum(hits) > best[0]):
                best = (sum(hits), ev, [comps[j] for j in order])
    if not best:
        return None
    _, ev, ordered = best
    status = ev.get("status", {}).get("type", {})
    state = {"in": "live", "post": "final"}.get(status.get("state"), "pre")
    score = "-".join(str(x.get("score", "")) for x in ordered) if state != "pre" else None
    return state, score, status.get("shortDetail") or status.get("detail")

def live_marks(c):
    """Score and current Kalshi value of every open trade, keyed by trade id. Pre-game trades get a value only."""
    out = {}
    for tid, slug, game, st, tk, side, n in c.execute("SELECT id, event_slug, game, start_ts, kalshi_ticker, kalshi_side, "
                                                       "contracts FROM trades WHERE status='open'").fetchall():
        manual = slug.startswith("manual-")
        state, score, clock = (None if manual else espn_clock(slug, game, parse_ts(st))) \
            or game_clock(None if manual else pm_event(slug))
        km = (get(f"{KAL}/markets/{tk}") or {}).get("market", {})
        bid = k_price(km, side, "bid") if km else None
        out[tid] = dict(state=state, score=score, clock=clock, now_price=bid,
                        now_value=round(n * bid, 2) if bid is not None else None)
    return out

def write_next_check(c):
    """next_check.json: when the 15-minute check should next run (15 min before the earliest open trade's start)."""
    now, best = NOW(), None
    for tid, game, st in c.execute("SELECT id, game, start_ts FROM trades WHERE status='open' AND pm_outcome_idx IS NOT NULL"):
        at = parse_ts(st) - timedelta(minutes=VERIFY_LEAD_MIN)
        if at > now + timedelta(minutes=1) and (best is None or at < best[0]):
            best = (at, tid, game)
    data = dict(check_at=best[0].astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z") if best else None,
                trade_id=best[1] if best else None, game=best[2] if best else None)
    with open(NEXT_CHECK + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(NEXT_CHECK + ".tmp", NEXT_CHECK)
    return data

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

# ---------------- time-outs ----------------
def benched_wallets(c):
    return {a.lower() for (a,) in c.execute("SELECT addr FROM timeouts WHERE released IS NULL")}

def trade_wallets(c, leaders):
    """(addr, name) for each leader of a trade; hand-logged trades only have names, matched through the pool."""
    L = json.loads(leaders)
    if L and L[0].get("addr"):
        return [(l["addr"].lower(), l["name"]) for l in L]
    by_name = {n: a.lower() for a, n in c.execute("SELECT addr, name FROM pool")}
    by_name.update({n: a for a, (n, _) in SEED_TIMEOUTS.items()})
    names = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", L[0]["name"]) if L else []
    return [(by_name[n], n) for n in dict.fromkeys(names) if n in by_name]

def market_trade_count(c, addr, cond):
    """How many times a wallet traded one market (cached; pages capped at 2,000 trades)."""
    row = c.execute("SELECT n FROM wallet_market_trades WHERE addr=? AND cond=?", (addr, cond)).fetchone()
    if row:
        return row[0]
    n, cur = 0, None
    for _ in range(4):
        d = get(f"{PM}/v2/activity", user=addr, condition=cond, limit=500, **({"cursor": cur} if cur else {}))
        if d is None:
            return None
        n += sum(1 for x in d.get("data") or [] if x.get("type") == "TRADE")
        cur = (d.get("pagination") or {}).get("next_cursor") if (d.get("pagination") or {}).get("has_more") else None
        if not cur:
            break
    c.execute("INSERT OR REPLACE INTO wallet_market_trades VALUES(?,?,?)", (addr, cond, n))
    return n

def bench(c, addr, name, reason):
    ts = NOW().isoformat(timespec="seconds")
    c.execute("INSERT OR REPLACE INTO timeouts(addr,name,reason,since,released,release_note) VALUES(?,?,?,?,NULL,NULL)",
              (addr.lower(), name, reason, ts))
    c.execute("INSERT INTO changes(ts,kind,event_slug,game,cond,title,note) VALUES(?,?,?,?,?,?,?)",
              (ts, "timeout", "", name, "", "Time-out", f"{name} put in time-out: {reason}"))
    log(f"TIME-OUT {name}: {reason}")

def release(c, addr, name, note):
    if addr.lower() not in benched_wallets(c):
        raise SystemExit(f"{name} isn't in time-out")
    ts = NOW().isoformat(timespec="seconds")
    c.execute("UPDATE timeouts SET released=?, release_note=? WHERE addr=? AND released IS NULL", (ts, note, addr.lower()))
    c.execute("INSERT INTO changes(ts,kind,event_slug,game,cond,title,note) VALUES(?,?,?,?,?,?,?)",
              (ts, "timeout", "", name, "", "Time-out", f"{name} released: {note}"))
    log(f"RELEASED {name}: {note}")

def settle_shadow(c):
    """Mark benched wallets' shadow picks won or lost once Polymarket resolves the market."""
    due = c.execute("SELECT DISTINCT cond FROM shadow WHERE won IS NULL AND start_ts < ?",
                    ((NOW() - timedelta(hours=3)).isoformat(timespec="seconds"),)).fetchall()
    conds = [x for (x,) in due]
    for i in range(0, len(conds), 40):
        rows = get(f"{GAMMA}/markets", condition_ids=conds[i:i + 40], closed="true", limit=100) or []
        for m in rows:
            try:
                prices = [float(p) for p in json.loads(m.get("outcomePrices") or "[]")]
            except Exception:
                continue
            if prices and max(prices) > 0.98:
                c.execute("UPDATE shadow SET won=(idx=?) WHERE cond=? AND won IS NULL", (prices.index(max(prices)), m["conditionId"]))
    c.commit()

def wallet_record(c, addr, since=None):
    """Our settled copied trades this wallet led (oldest first), as True/False for won/lost, plus their markets."""
    out = []
    for status, leaders, cond, settled in c.execute(
            "SELECT status, leaders, cond, settled_ts FROM trades WHERE status IN ('won','lost') ORDER BY settled_ts"):
        if since and (settled or "") <= since:
            continue
        if addr in {a for a, _ in trade_wallets(c, leaders)}:
            out.append((status == "won", cond))
    return out

def shadow_games(c, addr, since, until=None):
    """(won, avg price) of a benched wallet's settled shadow picks, one per game: its biggest pick on each."""
    best = {}
    for game, won, avg, usd in c.execute("SELECT game, won, avg, usd FROM shadow WHERE addr=? AND ts>=? AND (? IS NULL OR ts<=?) "
                                         "AND won IS NOT NULL", (addr, since, until, until)):
        if game not in best or usd > best[game][2]:
            best[game] = (won, avg, usd)
    return [(w, a) for w, a, _ in best.values()]

def review_wallets(c):
    """Bench wallets that keep losing us money or trade like market makers; release benched ones whose shadow
    picks have proven themselves."""
    settle_shadow(c)
    benched = benched_wallets(c)
    last_release = {a.lower(): r for a, r in c.execute("SELECT addr, MAX(released) FROM timeouts GROUP BY addr")}
    seen = {}
    for (leaders,) in c.execute("SELECT leaders FROM trades WHERE status IN ('won','lost')").fetchall():
        for a, n in trade_wallets(c, leaders):
            seen.setdefault(a, n)
    for addr, name in seen.items():
        if addr in benched:
            continue
        rec = wallet_record(c, addr, last_release.get(addr))
        wins = sum(w for w, _ in rec)
        if len(rec) >= TIMEOUT_STREAK and not any(w for w, _ in rec[-TIMEOUT_STREAK:]):
            bench(c, addr, name, f"lost our last {TIMEOUT_STREAK} copied trades ({wins}-{len(rec) - wins} overall)"); continue
        if len(rec) >= TIMEOUT_MIN_N and wins / len(rec) < TIMEOUT_MAX_WIN:
            bench(c, addr, name, f"won {wins} of {len(rec)} copied trades"); continue
        counts = [n for n in (market_trade_count(c, addr, cond) for _, cond in rec if not cond.startswith("manual:"))
                  if n is not None]
        if counts and (max(counts) >= TRADER_ONE or (len(counts) >= TRADER_MIN_N
                                                      and sorted(counts)[len(counts) // 2] >= TRADER_MEDIAN)):
            bench(c, addr, name, f"trader: {sorted(counts)[len(counts) // 2]} trades per market (median), "
                                 f"up to {max(counts)}")
    for addr, name, since in c.execute("SELECT addr, name, since FROM timeouts WHERE released IS NULL").fetchall():
        picks = shadow_games(c, addr, since)
        if len(picks) < RELEASE_N:
            continue
        rate, price = sum(w for w, _ in picks) / len(picks), sum(a for _, a in picks) / len(picks)
        if rate >= RELEASE_WIN and rate >= price + RELEASE_EDGE:
            release(c, addr, name, f"shadow picks won {rate:.0%} of {len(picks)} games at an average price of {price * 100:.0f}¢")
    c.commit()

def timeout_rows(c):
    """Every wallet ever benched, with its shadow record since it was benched."""
    out = []
    for addr, name, reason, since, released, note in c.execute(
            "SELECT addr, name, reason, since, released, release_note FROM timeouts ORDER BY released IS NOT NULL, since DESC"):
        games = shadow_games(c, addr, since, released)
        w, n = sum(x for x, _ in games), len(games)
        pending = c.execute("SELECT COUNT(DISTINCT game) FROM shadow WHERE addr=? AND ts>=? AND won IS NULL", (addr, since)).fetchone()[0]
        out.append(dict(addr=addr, name=name, reason=reason, since=since, released=released, note=note,
                        shadow_won=w, shadow_n=n, shadow_pending=0 if released else pending))
    return out

def find_wallet(c, who):
    who_l = who.lower()
    for a, n in list(c.execute("SELECT addr, name FROM pool")) + list(c.execute("SELECT addr, name FROM timeouts")):
        if a.lower() == who_l or (n or "").lower() == who_l:
            return a.lower(), n
    raise SystemExit(f"no vetted or benched wallet named {who}")

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
        lead = ", ".join(f"[{l['name']}](https://polymarket.com/profile/{l['addr']})" if l.get("addr") else l["name"] for l in L)
        lines.append(f"| {local(r['ts'])} | [{r['game']}](https://polymarket.com/event/{r['event_slug']}) | "
                     f"{r['title']} → {r['pick']} | {lead} (${r['lead_usd']:,.0f}) | "
                     f"{r['contracts']} × {r['price']*100:.0f}¢ {r['kalshi_side'].upper()} `{r['kalshi_ticker']}` | "
                     f"{r['status']} | {fmt_pnl(r['pnl'])} |")
    lines += position_changes(c)
    tos = timeout_rows(c)
    if tos:
        lines += ["", "## Time-out", "", "Wallets that don't count for or against a trade until their shadow picks win "
                  f"{RELEASE_WIN:.0%}+ of {RELEASE_N}+ settled games.", "",
                  "| Wallet | Why | Since | Shadow picks | Status |", "| --- | --- | --- | --- | --- |"]
        for t in tos:
            rec = f"{t['shadow_won']}-{t['shadow_n'] - t['shadow_won']}"
            if t["shadow_pending"]:
                rec += f" ({t['shadow_pending']} pending)"
            status = f"released {local(t['released'])}: {t['note']}" if t["released"] else "benched"
            lines.append(f"| {t['name']} | {t['reason']} | {local(t['since'])} | {rec} | {status} |")
    sk = rows(c, "SELECT * FROM skips ORDER BY id DESC LIMIT 30")
    if sk:
        lines += ["", "## Recent skips", "", "| When | Game | Pick | Reason |", "| --- | --- | --- | --- |"]
        lines += [f"| {local(r['ts'])} | {r['game']} | {r['title']} → {r['pick']} | {r['reason']} |" for r in sk]
    with open(REPORT + ".tmp", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(REPORT + ".tmp", REPORT)
    write_dashboard(c, s, sk)

def leaders_text(wallets):
    return ", ".join(f"{w['name']} {money(w['usd'])}" for w in json.loads(wallets)) or "none"

def kalshi_text(r):
    return f"{r['kalshi_ask'] * 100:.0f}¢ {r['kalshi_side'].upper()}" if r["kalshi_ask"] else "—"

def verdict(r):
    return "passes" if r["passes"] else f"fails: {r['reason']}"

def timeline(c, slug, cond=None):
    """Snapshots (newest first) and change notes for one game. Without cond, only markets that were
    passing or one rule away at some point, since those are the ones the decision chose between."""
    if cond:
        snaps = rows(c, "SELECT * FROM snapshots WHERE cond=? ORDER BY id DESC", (cond,))
    else:
        snaps = rows(c, "SELECT * FROM snapshots WHERE event_slug=? AND cond IN (SELECT cond FROM snapshots "
                        "WHERE event_slug=? GROUP BY cond HAVING MIN(fails)<=1) ORDER BY id DESC", (slug, slug))
    notes = rows(c, "SELECT * FROM changes WHERE event_slug=?" + (" AND cond=?" if cond else "") + " ORDER BY id DESC",
                 (slug, cond) if cond else (slug,))
    return snaps, notes

def position_changes(c):
    since = (NOW() - timedelta(hours=TIMELINE_HOURS)).isoformat()
    games = rows(c, "SELECT event_slug, MAX(game) game, MAX(start_ts) start_ts, MAX(id) last FROM snapshots "
                    "WHERE start_ts>? AND fails<=1 GROUP BY event_slug ORDER BY last DESC", (since,))
    if not games:
        return []
    out = ["", "## Position changes", "",
           f"Games starting in the last {TIMELINE_HOURS} hours or later that had a market passing or one rule away. "
           "Newest first.", ""]
    for g in games:
        snaps, notes = timeline(c, g["event_slug"])
        out += [f"### {g['game']} · starts {local(g['start_ts'])}", ""]
        out += [f"- {local(n['ts'])} ({n['kind']}): {n['title']}: {n['note']}" for n in notes]
        out += ["", "| Time | Run | Market → side | Leaders | Opp. | Kalshi | Rules |", "| --- | --- | --- | --- | --- | --- | --- |"]
        out += [f"| {local(r['ts'])} | {r['kind']} | {r['title']} → {r['pick']} | {leaders_text(r['wallets'])} | "
                f"{money(r['opp_usd'])} | {kalshi_text(r)} | {verdict(r)} |" for r in snaps]
        out.append("")
    return out

TEMPLATE = os.path.join(HERE, "dashboard_template.html")
TRACKER_DIR = os.path.join(os.path.dirname(DB), f"tracker{SUFFIX}")   # row files the claude.ai tracker artifact loads

def hist_point(x):
    return [x["ts"], x["lead_usd"], x["opp_usd"], x["kalshi_ask"], x["passes"], x["n_wallets"]]

def note_row(n):
    return dict(ts=n["ts"], kind=n["kind"], title=n["title"], note=n["note"])

def dashboard_data(c, s, sk):
    """Everything dashboard_template.html renders, as one JSON-able dict."""
    now = NOW()
    marks = live_marks(c)
    trades = []
    for r in sorted(s["trades"], key=lambda r: r["ts"], reverse=True):
        snaps, notes = timeline(c, r["event_slug"], r["cond"])
        trades.append(dict(id=r["id"], ts=r["ts"], game=r["game"], slug=r["event_slug"], title=r["title"],
                           pick=r["pick"], start=r["start_ts"], status=r["status"], n=r["contracts"], price=r["price"],
                           side=r["kalshi_side"], ticker=r["kalshi_ticker"], cost=r["cost"], fee=r["fee"], pnl=r["pnl"],
                           exit_price=r["exit_price"], exit_reason=r["exit_reason"], closed=r["settled_ts"] or r["exit_ts"],
                           leaders=json.loads(r["leaders"]), lead=r["lead_usd"], opp=r["opp_usd"], avg=r["leader_avg"],
                           cond=r["cond"], hist=[hist_point(x) for x in reversed(snaps)], notes=[note_row(n) for n in notes],
                           live=marks.get(r["id"])))
    traded = {t["slug"] for t in trades}
    games = []
    since = (now - timedelta(hours=TIMELINE_HOURS)).isoformat()
    for g in rows(c, "SELECT event_slug, MAX(game) game, MAX(start_ts) start FROM snapshots WHERE start_ts>? AND fails<=1 "
                     "GROUP BY event_slug ORDER BY start", (since,)):
        snaps, notes = timeline(c, g["event_slug"])
        markets = {}
        for x in snaps:   # newest first, so the first row per market is its latest state
            m = markets.setdefault(x["cond"], dict(cond=x["cond"], title=x["title"], pick=x["pick"],
                                                   wallets=json.loads(x["wallets"]), lead=x["lead_usd"], opp=x["opp_usd"],
                                                   avg=x["leader_avg"], ask=x["kalshi_ask"], side=x["kalshi_side"],
                                                   ok=x["passes"], why=x["reason"], fails=x["fails"], hist=[]))
            m["hist"].insert(0, hist_point(x))
        ms = sorted(markets.values(), key=lambda m: (-m["ok"], m["fails"], -m["lead"]))
        games.append(dict(slug=g["event_slug"], game=g["game"], start=g["start"], traded=g["event_slug"] in traded,
                          markets=ms, notes=[note_row(n) for n in notes[:40]]))
    w = c.execute("SELECT v FROM meta WHERE k='window'").fetchone()
    window = [int(x) for x in w[0].split("-")] if w else list(PREGAME_WINDOW)
    curve, eq = [[s["trades"][0]["ts"] if s["trades"] else NOW().isoformat(), START_BANKROLL]], START_BANKROLL
    for t in sorted((t for t in trades if t["closed"]), key=lambda t: t["closed"]):
        eq = round(eq + (t["pnl"] or 0), 2)
        curve.append([t["closed"], eq])
    return dict(mode=MODE, env=os.environ.get("KALSHI_ENV", "prod"), updated=now.isoformat(timespec="seconds"),
                start=START_BANKROLL, stake=STAKE_USD,
                summary={k: s[k] for k in ("cash", "equity", "open", "won", "lost", "sold", "pnl")},
                at_risk=round(sum(t["cost"] + t["fee"] for t in trades if t["status"] == "open"), 2),
                open_value=round(sum(t["live"]["now_value"] if t["live"] and t["live"]["now_value"] is not None
                                     else t["cost"] + t["fee"] for t in trades if t["status"] == "open"), 2),
                rules=dict(min_wallets=MIN_WALLETS, min_points=MIN_POINTS, big_money=BIG_MONEY_USD, opp_max=OPP_MAX, hedge=HEDGE_RATIO, slip=MAX_SLIP,
                           min_price=MIN_PRICE, max_price=MAX_PRICE, window=window,
                           lock=LOCK_WINDOW_MIN, plan_lead=PLAN_LEAD_MIN, timeline_hours=TIMELINE_HOURS),
                trades=trades, games=games, curve=curve, timeouts=timeout_rows(c),
                skips=[dict(ts=r["ts"], game=r["game"], title=r["title"], pick=r["pick"], reason=r["reason"],
                            lead=r["lead_usd"]) for r in sk])

def write_tracker_files(c, d):
    """Row files for the claude.ai Copy-Trade Tracker artifact: overview, trades, games, activity."""
    s = d["summary"]
    files = {
        "overview": [dict(updated=d["updated"], mode=d["mode"], start=d["start"], equity=s["equity"], cash=s["cash"],
                          realized_pnl=s["pnl"], won=s["won"], lost=s["lost"], sold=s["sold"], open=s["open"],
                          at_risk=d["at_risk"], open_value=d["open_value"], stake=d["stake"],
                          timeouts=[dict(name=t["name"], reason=t["reason"], since=t["since"], released=t["released"],
                                         note=t["note"], shadow_won=t["shadow_won"], shadow_n=t["shadow_n"],
                                         shadow_pending=t["shadow_pending"]) for t in d["timeouts"]], window=f"{d['rules']['window'][0]}-{d['rules']['window'][1]}")],
        "trades": [dict(id=t["id"], entered=t["ts"], starts=t["start"], game=t["game"], market=t["title"], pick=t["pick"],
                        side=(t["side"] or "").upper(), contracts=t["n"], price=t["price"], cost=round(t["cost"] + t["fee"], 2),
                        status=t["status"], pnl=t["pnl"], closed=t["closed"], ticker=t["ticker"],
                        leaders=", ".join(f"{l['name']} {money(l['usd'])}" if l.get("addr") else l["name"] for l in t["leaders"]),
                        lead_usd=t["lead"], opp_usd=t["opp"], leader_avg=t["avg"], exit_reason=t["exit_reason"],
                        **{k: (t["live"] or {}).get(k2) for k, k2 in (("game_state", "state"), ("score", "score"),
                           ("clock", "clock"), ("now_price", "now_price"), ("now_value", "now_value"))})
                   for t in d["trades"]],
        "games": [dict(key=m["cond"], game=g["game"], starts=g["start"], traded=g["traded"], market=m["title"], pick=m["pick"],
                       wallets=len(m["wallets"]), leaders=", ".join(f"{w['name']} {money(w['usd'])}" for w in m["wallets"]),
                       lead_usd=m["lead"], opp_usd=m["opp"], kalshi=m["ask"], side=(m["side"] or "").upper(),
                       leader_avg=m["avg"], status="passes" if m["ok"] else ("one rule away" if m["fails"] <= 1 else "fails"),
                       reason=m["why"] or "")
                  for g in d["games"] for m in g["markets"]],
        "activity": sorted([dict(at=n["ts"], type="change", game=n["game"], market=n["title"], text=n["note"])
                            for n in rows(c, "SELECT * FROM changes ORDER BY id DESC LIMIT 80")]
                           + [dict(at=r["ts"], type="skip", game=r["game"], market=f"{r['title']} → {r['pick']}", text=r["reason"])
                              for r in d["skips"]], key=lambda r: r["at"], reverse=True),
    }
    os.makedirs(TRACKER_DIR, exist_ok=True)
    for name, data in files.items():
        with open(os.path.join(TRACKER_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

def write_dashboard(c, s, sk):
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read()
    d = dashboard_data(c, s, sk)
    write_tracker_files(c, d)
    data = json.dumps(d, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*__DATA__*/null", data)
    with open(DASH + ".tmp", "w", encoding="utf-8") as f:
        f.write(page)
    os.replace(DASH + ".tmp", DASH)

def import_manual(c, path):
    """Add a hand-run sim's trades (history/manual_sim.json) to this book, each once, and move cash to match."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    added = 0
    for t in d["trades"]:
        oid = "manual:" + t["id"]
        if c.execute("SELECT 1 FROM trades WHERE order_id=?", (oid,)).fetchone():
            continue
        n, price, status = int(t["contracts"]), float(t["price"]), t["status"]
        cost = round(n * price, 2)
        fee = round(float(t["cost"]) - cost, 2)   # the log's cost includes the entry fee
        exit_price = exit_fee = None
        if status == "sold":
            m = re.search(r"at (\d+)¢.*fee \$([\d.]+)", t.get("exit_reason") or "")
            exit_price, exit_fee = (int(m.group(1)) / 100, float(m.group(2))) if m else (None, None)
        iso = lambda s: parse_ts(s).isoformat() if s else None
        won = status == "won"
        c.execute("""INSERT INTO trades(ts,game,event_slug,cond,title,pick,pm_outcome_idx,start_ts,leaders,lead_usd,opp_usd,
            leader_avg,kalshi_ticker,kalshi_side,price,contracts,cost,fee,status,exit_price,exit_fee,exit_ts,exit_reason,
            result,pnl,settled_ts,mode,order_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (iso(t["entered"]), t["game"], "manual-" + t["id"], oid, t["market"], t["pick"], None, iso(t["starts"]),
             json.dumps([dict(name=t["leaders"], addr="", avg=t["leader_avg"], usd=t["lead_usd"])]),
             t["lead_usd"], t["opp_usd"], t["leader_avg"], t["ticker"], t["side"].lower(), price, n, cost, fee, status,
             exit_price, exit_fee, iso(t["closed"]) if status == "sold" else None, t.get("exit_reason"),
             (t["side"].lower() if won else {"YES": "no", "NO": "yes"}[t["side"]]) if status in ("won", "lost") else None,
             t["pnl"], iso(t["closed"]) if status in ("won", "lost") else None, "paper", oid))
        cash(c, t["pnl"] if t["pnl"] is not None else -float(t["cost"]))
        added += 1
    c.commit()
    log(f"imported {added} manual trade(s) from {path}; cash now ${cash(c):.2f}")

def report(c):
    s = summary(c)
    print(f"Bankroll ${s['equity']:.2f} (cash ${s['cash']:.2f}) | {s['won']}W {s['lost']}L {s['sold']} sold | "
          f"P&L ${s['pnl']:+.2f} | open {s['open']}")
    for r in sorted(s["trades"], key=lambda r: r["ts"]):
        print(f"  {r['status']:5} {r['game']} | {r['title']} -> {r['pick']} | {r['contracts']}x{r['price']:.2f} "
              f"{r['kalshi_side']} | {fmt_pnl(r['pnl'])}")

# ---------------- main ----------------
def cycle(c, hourly=True, pregame=True, window=PREGAME_WINDOW):
    """One pull of positions, then exits, the hourly snapshot + plan and/or the pre-game decision, and settlement.
    Only the pre-game decision opens trades."""
    _ev_cache.clear(); _espn_cache.clear(); _k_cache.clear(); _kg_cache.clear(); FAILED.clear(); reset_kalshi_pacing()
    refresh_pool(c)
    markets = consensus(c)
    if not c.execute("SELECT 1 FROM pool").fetchone() or any("/v2/positions" in u for u in FAILED):
        log("ERROR: could not pull every vetted wallet's positions; no exits, snapshots or entries this run")
        settle(c); write_report(c)
        return
    check_exits(c, markets)
    verify_open(c, markets, VERIFY_AHEAD_MIN)
    lo, hi = window
    if hourly:
        cands = evaluate(c, markets, 0, SNAPSHOT_HOURS * 60)
        now = NOW()
        due = [x for x in cands if now + timedelta(minutes=lo) < x["start"] <= now + timedelta(minutes=hi)]
        snapshot(c, cands, "hourly", 0, SNAPSHOT_HOURS * 60, window if pregame else None)
        log(f"snapshot: {len(cands)} markets in {len({x['slug'] for x in cands})} games over the next {SNAPSHOT_HOURS}h, "
            f"{sum(not x['fails'] for x in cands)} passing")
        write_plan(cands)
        c.execute("INSERT OR REPLACE INTO meta VALUES('hourly_ts', ?)", (str(time.time()),)); c.commit()
    elif pregame:
        due = evaluate(c, markets, lo, hi)
        snapshot(c, due, "pregame", lo, hi)
    if pregame:
        log(f"pre-game check: {len({x['slug'] for x in due})} game(s) starting in {lo}-{hi} min, "
            f"{sum(not x['fails'] for x in due)} passing market(s)")
        decide(c, due)
        c.execute("INSERT OR REPLACE INTO meta VALUES('window', ?)", (f"{lo}-{hi}",)); c.commit()
    settle(c)
    if hourly:
        review_wallets(c)
    write_report(c)
    log(f"next 15-minute check: {write_next_check(c)['check_at'] or 'none (no open trades ahead)'}")

def verify(c):
    """The 15-minute check on its own: fresh positions, re-check open trades about to start, settle, report.
    With none due but a game in progress it only refreshes scores, settles and reports.
    With neither it prints 'nothing due' and touches nothing."""
    now = NOW()
    due = [st for (st,) in c.execute("SELECT start_ts FROM trades WHERE status='open' AND pm_outcome_idx IS NOT NULL")
           if now < parse_ts(st) <= now + timedelta(minutes=VERIFY_AHEAD_MIN)]
    if not due:
        started = [st for (st,) in c.execute("SELECT start_ts FROM trades WHERE status='open'") if parse_ts(st) <= now]
        if not started:
            log(f"nothing due: no open trade starts within {VERIFY_AHEAD_MIN} min and none is in progress")
            return
        _ev_cache.clear(); _espn_cache.clear(); FAILED.clear(); reset_kalshi_pacing()
        settle(c)
        write_report(c)
        log(f"scores refreshed: {len(started)} open trade(s) in progress")
        return
    _ev_cache.clear(); _espn_cache.clear(); _k_cache.clear(); _kg_cache.clear(); FAILED.clear(); reset_kalshi_pacing()
    refresh_pool(c)
    markets = consensus(c)
    if not c.execute("SELECT 1 FROM pool").fetchone() or any("/v2/positions" in u for u in FAILED):
        log("ERROR: could not pull every vetted wallet's positions; holding every open trade this run")
    else:
        verify_open(c, markets, VERIFY_AHEAD_MIN)
    settle(c)
    write_report(c)
    log(f"next 15-minute check: {write_next_check(c)['check_at'] or 'none (no open trades ahead)'}")

def main():
    c = db()
    if "report" in sys.argv:
        return report(c)
    if "import-manual" in sys.argv:
        return import_manual(c, sys.argv[sys.argv.index("import-manual") + 1])
    if "timeout" in sys.argv:
        i = sys.argv.index("timeout")
        addr, name = find_wallet(c, sys.argv[i + 1])
        bench(c, addr, name, " ".join(sys.argv[i + 2:]) or "benched by hand"); c.commit(); write_report(c); return
    if "release" in sys.argv:
        addr, name = find_wallet(c, sys.argv[sys.argv.index("release") + 1])
        release(c, addr, name, "released by hand"); c.commit(); write_report(c); return
    if "check-keys" in sys.argv:
        from kalshi_client import KalshiClient
        k = KalshiClient()
        print(f"Kalshi ({os.environ.get('KALSHI_ENV', 'prod')}) balance: ${k.balance_usd():.2f}. Keys work.")
        return
    window = PREGAME_WINDOW
    if "--window" in sys.argv:
        lo, hi = sys.argv[sys.argv.index("--window") + 1].split("-")
        window = (int(lo), int(hi))
        if not 0 <= window[0] < window[1] <= LOCK_WINDOW_MIN:
            raise SystemExit(f"--window must be inside 0-{LOCK_WINDOW_MIN} minutes")
    executor()
    if "hourly" in sys.argv:
        cycle(c, hourly=True, pregame=False); report(c); return
    if "pregame" in sys.argv:
        cycle(c, hourly=False, pregame=True, window=window); report(c); return
    if "verify" in sys.argv:
        verify(c); report(c); return
    if "--once" in sys.argv:
        cycle(c, window=window); report(c); return
    while True:
        try:
            last = c.execute("SELECT v FROM meta WHERE k='hourly_ts'").fetchone()
            cycle(c, hourly=not last or time.time() - float(last[0]) >= HOURLY_SECONDS, window=window)
        except Exception as e:
            log(f"cycle error (continuing): {e}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
