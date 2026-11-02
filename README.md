# Polymarket → Kalshi copy trader

Watches the most consistent sports bettors on Polymarket and copies the bets they agree on onto Kalshi.
**Paper mode is the default**: no keys, no orders, just a simulated bankroll. Live mode places real orders
on Kalshi and only turns on when you explicitly enable it.

## The rules

| Rule | Setting |
| --- | --- |
| Vetted wallets | Top 100 on Polymarket's sports leaderboard this month **and** top 200 all-time |
| Signal | 2+ vetted wallets on the same side (`MIN_WALLETS`) |
| Opposition | Vetted money on the other side must be under 10% of theirs (`OPP_MAX`) |
| Hedges | A wallet holding both sides (smaller side > 20%) doesn't count |
| Timing | Decide 5 to 25 minutes before the start, in the pre-game check (`PREGAME_WINDOW`, inside `LOCK_WINDOW_MIN`) |
| Price | Kalshi must be within 3¢ of the leaders' average entry (`MAX_SLIP`), same line only |
| Size | One trade per game, flat $10 (`COPYSIM_STAKE`) |
| Exits | If the leaders sell or flip before the start, sell; otherwise hold to settlement |

Markets: moneylines, spreads, totals, and soccer win/draw for NBA, NFL, NHL, college football and
basketball, and major soccer leagues.

## Files

- `copysim.py`: the strategy, matching, paper/live execution, reports
- `kalshi_client.py`: signed Kalshi API calls for live mode (Create Order V2)
- `REPORT.md` / `dashboard.html`: written after every run, with a per-game timeline of how the vetted money moved.
  The dashboard's layout lives in `dashboard_template.html`; `copysim.py` only fills in the data.
- `pregame_plan.json`: games worth a pre-game check, and when (`check_at`, 15 minutes before start)
- `copysim.db`: trade history, hourly snapshots and change notes (SQLite). `COPYSIM_DB=copysim_live.db` keeps a
  separate history and writes `REPORT_live.md`, `dashboard_live.html` and `pregame_plan_live.json`.

## Run the simulation

```bash
pip install -r requirements.txt
python3 copysim.py hourly     # snapshot every game in the next 24h, note changes, write the plan; never trades
python3 copysim.py pregame    # decide games starting in 5-25 min (--window 15-45 to widen); the only mode that trades
python3 copysim.py            # laptop: hourly snapshot + pre-game check every 5 minutes; keep the computer awake
python3 copysim.py --once     # one snapshot + pre-game check
python3 copysim.py report     # print the bankroll and trades
```

Start somewhere other than $100 with `COPYSIM_START=137.54`. For scheduled cloud runs, see `CLAUDE_CODE_SETUP.md`.

### Run it in the cloud, managed from your phone (Claude Code scheduled task)

1. At claude.ai/code/routines, create a scheduled task on this repo, hourly, a few minutes past the hour.
2. In the task's environment, set network access to Custom and allow:
   `data-api.polymarket.com`, `gamma-api.polymarket.com`, `api.elections.kalshi.com`, `external-api.kalshi.com`
3. Prompt: *Run `pip install -r requirements.txt && python3 copysim.py --once`. Then commit copysim.db,
   REPORT.md and dashboard.html and push to main. Don't change any code.*
4. Read `REPORT.md` in the GitHub app.

GitHub Actions also works: see `.github/workflows/copysim.yml` (off until you uncomment the schedule).

## Going live (when the sim says it's worth it)

Suggested bar first: 50+ settled paper trades with positive profit after fees.

1. **Test on Kalshi's demo exchange.** Make a demo account at demo.kalshi.co, create an API key there, then:
   ```bash
   export KALSHI_ENV=demo KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=~/kalshi-demo.key
   python3 copysim.py check-keys
   ```
2. **Create a production key** at kalshi.com → Account & security → API Keys. Keep the private key file
   outside this folder. It must never be committed (`.gitignore` blocks `*.key` and `*.pem`).
3. **Start small:**
   ```bash
   export COPYSIM_MODE=live COPYSIM_LIVE_ACK=yes
   export KALSHI_API_KEY_ID=... KALSHI_PRIVATE_KEY_PATH=~/kalshi.key
   export COPYSIM_START=50 LIVE_MAX_PER_TRADE=5 LIVE_MAX_DAILY=20 LIVE_MAX_OPEN=5 COPYSIM_STAKE=5
   python3 copysim.py check-keys && python3 copysim.py
   ```
   Use a fresh folder (or delete `copysim.db`) so live trades don't mix with paper history.
4. For GitHub Actions, store `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (the PEM text) as repository
   **secrets**, and `COPYSIM_MODE=live` and `COPYSIM_LIVE_ACK=yes` as repository variables.

### Live safety rails

- Live mode refuses to start without `COPYSIM_LIVE_ACK=yes`.
- Orders are immediate-or-cancel limit orders, capped at the leaders' entry + 3¢, so the script never chases price or leaves orders resting.
- Caps: `LIVE_MAX_PER_TRADE` ($ per order), `LIVE_MAX_DAILY` ($ of new buys per day), `LIVE_MAX_OPEN` (positions).
  It also checks your real Kalshi balance before each buy.
- **Kill switch:** create a file named `STOP` next to the script (or in the repo) and no new trades are entered.
- Every live order response is logged to `orders.log`.

Kalshi's API changes over time (prices moved to dollar strings and orders to `/portfolio/events/orders` in 2026).
If orders start failing, check https://docs.kalshi.com. Nothing here is financial advice.
