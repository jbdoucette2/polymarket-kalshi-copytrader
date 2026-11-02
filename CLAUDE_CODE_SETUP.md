# Claude Code setup: hourly checks, 15-minute pre-game decisions, paper and live

Open this repo in Claude Code (claude.ai/code or the Code tab in the Claude app) and say:
**"Read CLAUDE_CODE_SETUP.md and do Part 1."** Then do Parts 2 and 3 when you're ready.

## Where you'll see results

A claude.ai artifact can't run this. Artifacts only run while open on your screen and can't call
Polymarket or Kalshi. The engine runs as Claude Code scheduled tasks in the cloud, and every run commits:

- `REPORT.md`: bankroll, trades, and a timeline of position changes per game. Read it in the GitHub app.
- `dashboard.html`: the same, styled. Download it from GitHub to open in a browser.

---

## Part 1: Build (one interactive Claude Code session)

Paste this:

> Update copysim.py in this repo. Keep every existing rule, paper/live executor, safety cap, and the STOP
> kill switch exactly as they are. Add:
>
> 1. **`python3 copysim.py hourly`** (snapshot and plan; never opens trades):
>    - For every game starting in the next 24 hours, record a row per market in a new `snapshots` table:
>      ts, event_slug, game, market title, side with the most vetted money, wallet names and $ per wallet,
>      lead_usd, opp_usd, leader avg entry, Kalshi ticker and ask (if matched), start time, and whether it
>      currently passes the rules (with the failing reason if not).
>    - Compare with the previous snapshot of the same market and write human-readable change notes,
>      for example: "HomeRunHazard joined Wizards (+$12.3k)", "UpTheBlues left", "majority flipped to
>      Magic", "Kalshi 43¢ → 39¢", "now passes", "now fails: opposition 14%". Store them in a `changes`
>      table with ts and event_slug.
>    - Still run exits (leaders sold or flipped before start) and settlement, as `--once` does now.
>    - Write `pregame_plan.json`: one entry per game that has at least one market passing or within one
>      rule of passing, with `check_at` = start time − 15 minutes (UTC ISO).
> 2. **`python3 copysim.py pregame`** (the decision):
>    - For games starting in the next 5 to 25 minutes: re-pull positions, take a fresh snapshot (with
>      change notes), then pick the single best trade per game: of the markets that pass every rule, the
>      one with the most leader money (ties: more wallets, then lower Kalshi price). Enter it through the
>      existing executor. If nothing passes, log the reason per market in `skips`.
> 3. Entries now happen **only** in `pregame` mode. Keep `--once` and the default loop working for laptop
>    use, with the default loop doing an hourly snapshot plus a pregame check every 5 minutes.
> 4. `COPYSIM_DB` env var (default `copysim.db`) so paper and live keep separate histories.
>    REPORT.md and dashboard.html go next to the DB with the same prefix (`copysim_live` → `REPORT_live.md`).
> 5. REPORT.md: add a "Position changes" section grouped by game, newest first, showing every hourly and
>    pre-game snapshot (time, leaders and $, opposition, Kalshi price, pass/fail) so I can see how a
>    game's money moved before the decision. Add the same timeline inside each trade card in dashboard.html.
> 6. Test both modes once in paper mode against live data, show me the output, then commit and push.

---

## Part 2: Run the simulation on a schedule (cloud)

Paste this into Claude Code (it can do most of it with `/schedule`; do anything it can't on
claude.ai/code/routines):

> Set up scheduled tasks for this repo:
>
> **Environment `copytrader-paper`**: network access Custom, allowing data-api.polymarket.com,
> gamma-api.polymarket.com, api.elections.kalshi.com, external-api.kalshi.com. Setup script:
> `pip install -r requirements.txt`.
>
> **Task "copytrader hourly"**: runs every hour at 7 minutes past, in `copytrader-paper`. Prompt:
> "Run `python3 copysim.py hourly`. Then, for each entry in pregame_plan.json whose check_at is still in
> the future and isn't already listed in scheduled_runs.json, create a one-off run of the
> 'copytrader pregame' task at that check_at, and record it in scheduled_runs.json. Commit copysim.db,
> REPORT.md, dashboard.html, pregame_plan.json and scheduled_runs.json and push to main. Don't edit code."
>
> **Task "copytrader pregame"**: no recurring schedule (it's started by the hourly task's one-off runs),
> same environment. Prompt: "Run `python3 copysim.py pregame`. Commit copysim.db, REPORT.md and
> dashboard.html and push to main. Don't edit code."
>
> Run each once now to confirm it works.
>
> If a scheduled run can't create one-off runs, tell me, and instead add a second recurring task at
> 37 minutes past each hour that runs `python3 copysim.py pregame` with its window changed to 15–45
> minutes before start.

Notes:
- Cloud tasks run at most hourly on their own, which is why the hourly task books the 15-minute pre-game runs.
- Every run uses your Claude plan's usage (about 24 hourly runs plus one per game day-slot). Check
  claude.ai/settings/usage after the first day.
- A run that starts exactly on the hour can start a few minutes late; :07 avoids that.

---

## Part 3: The real deal (only after the paper sim earns it)

Suggested bar: 50+ settled paper trades, profitable after fees.

**You do these yourself (never paste keys into a chat):**
1. Create a Kalshi demo account at demo.kalshi.co and an API key there. Later, a production key at
   kalshi.com → Account & security → API Keys.
2. In Claude Code's cloud environment settings, create a separate environment **`copytrader-live`**
   (same network allowlist and setup script) with these environment variables:
   `COPYSIM_MODE=live`, `COPYSIM_LIVE_ACK=yes`, `COPYSIM_DB=copysim_live.db`, `KALSHI_ENV=demo`,
   `KALSHI_API_KEY_ID=<your key id>`, `KALSHI_PRIVATE_KEY=<the PEM text>`,
   `COPYSIM_START=50`, `COPYSIM_STAKE=5`, `LIVE_MAX_PER_TRADE=5`, `LIVE_MAX_DAILY=20`, `LIVE_MAX_OPEN=5`.
   Environment variables are visible to anyone who uses that environment, so use it only for this.

Then paste this into Claude Code:

> Create "copytrader live hourly" and "copytrader live pregame" tasks, copies of the paper tasks but in
> the `copytrader-live` environment, committing copysim_live.db, REPORT_live.md, dashboard_live.html,
> and the live plan files instead. First run `python3 copysim.py check-keys` in that environment and
> show me the balance. Don't print or commit any key material.

3. Run on **demo** for a few days and compare REPORT_live.md with the paper REPORT.md.
4. To go to real money, change only `KALSHI_ENV` to `prod` (and your production key) in `copytrader-live`.

**Stopping it:** add a file named `STOP` to the repo root (the GitHub app can do this). The next run
enters no new trades. Pause the tasks at claude.ai/code/routines to stop everything.
