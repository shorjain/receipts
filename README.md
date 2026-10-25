# nfl-lines

A personal NFL analytics app that answers two questions:

1. **Why did the line move?** Detect odds moves on Kalshi and sportsbooks, then show the tracked-insider posts that most likely caused them, with citations.
2. **Which insiders can you trust?** Track every factual claim insiders make, check it against nflverse, and rank them by accuracy, earliness and market impact.

## Status

| # | Milestone | State |
|---|-----------|-------|
| 1 | Skeleton, `.env.example`, `store/` with schema, probe scripts + fixtures | done |
| 2 | nflverse loaders; GAMES + ROSTERS for the current season | done |
| 3 | Odds ingestion (Kalshi + The Odds API) and move detection | done (Odds API path tested on a hand-built fixture until a key is added) |
| 3b | Polymarket venue + Kalshi vs Polymarket gaps and lead-lag | done |
| 4 | X sweeps + on-move searches with citation verification | next: needs `XAI_API_KEY` |
| 5 | Move explanations + Moves page | |
| 6 | Claims, resolution, scores, Leaderboard + Insider pages | |
| 7 | Season backfill | |
| 8 | Win-probability model + Games page | |
| 9 | Ask chatbot | |
| 10 | DuckDB fallback store (local embeddings + Ollama) | base tables done early; AI functions pending |
| 11 | Trading module (optional, off) | |

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # fill in what you have; everything else is optional
.venv/bin/python -m pytest -q # no network needed
```

Run everything implemented so far, once (idempotent):

```bash
.venv/bin/python -m pipelines.run_all
```

Individual stages:

```bash
python -m pipelines.load_nflverse --season 2026     # GAMES + ROSTERS
python -m pipelines.ingest_odds --once              # one Kalshi poll; Odds API if due
python -m pipelines.ingest_odds --loop              # keep polling (Kalshi every 60 s)
python -m pipelines.detect_moves --hours 48         # write LINE_MOVES
python -m pipelines.compare_venues --hours 48       # VENUE_GAPS + who moved first
```

Probe scripts hit each API once and save a fixture to `tests/fixtures/`:

```bash
python -m scripts.probe_kalshi       # public, no key
python -m scripts.probe_nflverse     # public
python -m scripts.probe_polymarket   # public, read-only (needs GAMES loaded)
python -m scripts.probe_odds_api     # needs ODDS_API_KEY, costs ~3 credits
python -m scripts.probe_snowflake --guardrails   # needs Snowflake creds + ACCOUNTADMIN
```

## Store

`STORE_BACKEND=duckdb` (default) writes to `data/nfl_lines.duckdb` with no account. `STORE_BACKEND=snowflake` uses Snowflake. Both implement the same interface (`store/base.py`). All SQL lives in `store/`; `tests/test_repo_rules.py` fails if SQL shows up anywhere else.

- Table definitions live once in `store/schema.py` in neutral types; each backend maps them to its DDL (`VECTOR(FLOAT, 768)` in Snowflake, `FLOAT[768]` in DuckDB).
- `create_schema()` also adds any columns that `schema.py` gained since the table was created, so existing databases migrate in place.
- Every write is an upsert (`MERGE`) on the table's key, so re-running a pipeline never duplicates rows.
- All timestamps are naive UTC.
- Every raw pull (Kalshi markets, Odds API events, nflverse tables) is also written to `data/raw/<source>/<date>/*.parquet`.

Changes from the brief's data model: `ROSTERS` (for matching claim names to player IDs), `API_USAGE` (one row per paid call) and `VENUE_GAPS` (Kalshi vs Polymarket disagreements) are added. `INSIDER_SCORES.window` is named `score_window` because `WINDOW` is a reserved word in both Snowflake and DuckDB. `LINE_MOVES` gains `venues`, `explanation`, `detected_at`, and the venue comparison columns `venue_reactions`, `leader_venue` and `lead_lag_minutes`; `GAMES` gains closing lines, roof, rest days and division flag for the model.

## Conventions

- **Spreads** are stored in betting convention from the home side: `-3.5` means the home team is favored by 3.5. (nflverse's `spread_line` is the opposite sign; the loader flips it.)
- **Winner probability** is always P(home wins), with the vig removed.
- **Venues** used for move detection: `kalshi` and `polymarket` (derived per-game series), `pinnacle`, and `consensus` (median across returned books). Individual books are stored but not compared against each other, since books copying each other would make every move look multi-venue.

### Kalshi spreads and totals

Kalshi lists spreads and totals as ladders of binary markets ("Green Bay wins by over 7.5?"). For each poll, the ingest step turns the ladder into one implied line: it collects (strike, P(over strike)) from liquid rungs, forces the curve to be non-increasing, and interpolates where it crosses 50%. Away-team rungs are mirrored onto the home margin. On today's LA at LV fixture this gives LV +3.1 / 48.4 against nflverse's closing LV +3.0 / 48.5.

### Polymarket

Polymarket lists the same NFL game markets (moneyline, a spread ladder, a totals ladder) on its public Gamma and CLOB APIs. A full event listing is ~50 MB, so ingestion works in two steps:

1. **Discover** every 30 minutes: Gamma `/markets` filtered to `sports_market_types` moneyline, spreads and totals for the NFL tag (~1,500 markets in a week, ~12 s). Each market's first-outcome token is mapped to our game, side and strike.
2. **Price** every poll: CLOB `POST /books` for up to 500 tokens per call (~1.6 s for a week).

The same ladder math as Kalshi (`sources/ladder.py`) turns the quotes into one probability or line per game. On the LA at LV fixture Polymarket implies LV +3.3 / 48.4 against Kalshi's +3.1 / 48.4. Polymarket blocks US trading on its international exchange, but reading prices is unaffected, and this app never trades there.

## Kalshi vs Polymarket

`pipelines/compare_venues.py` writes two things:

**Gaps (`VENUE_GAPS`).** The two derived series are aligned (a stored value stays current for 11 minutes, longer than the storage heartbeat). A gap is at least 2 aligned points in a row where the difference, minus its median over the previous 3 hours, is at least 2.5 pp (winner) or 0.5 pts (spread, total). Subtracting that baseline keeps a standing offset (one venue's fees or a stale quote) from counting as news. Each gap records:
- `mover`: the venue that changed more from just before the gap to its peak.
- `resolution`: `follower_caught_up` (the other venue moved to match), `mover_reverted` (the mover came back), or `open`.

**Who moved first (`LINE_MOVES.venue_reactions`).** For every move, each venue's reaction time is when it crossed the move's halfway price coming from the pre-move side, in minutes from `window_start` (negative = before). `leader_venue` is the earliest, and `lead_lag_minutes` is its lead over the next venue. `summarize()` reports how often each venue led and how often the other followed.

A first replay of 36 hours of history (27 games, Oct 24–25 2026, using Kalshi 1-minute candles and Polymarket price history) found 13 moves and 16 gaps:

- On moves where both venues reacted (6), each led 3 times.
- Every Polymarket-led gap (5 of 5) was followed by Kalshi. Only 3 of 10 Kalshi-led gaps were followed; the other 7 reverted.

That's a hint that a solo Kalshi move is more often noise, but the sample is small and mostly overnight. Re-check it with a full week of live polling.

## Move detection

A move is any of the following within `lookback_minutes` (30), using pre-kickoff snapshots only:

| Market | Trigger | magnitude_score |
|---|---|---|
| winner | \|Δ P(home)\| ≥ 3 pp | \|Δp\| / 0.03 |
| spread | line touches or passes 3, 7, 10 or 14, with ≥ 0.5 pts of movement | max(\|Δline\| / 1.0, 1.0) |
| total | \|Δ total\| ≥ 1.5 | \|Δ\| / 1.5 |

- Kalshi and Polymarket points count only when the yes bid-ask spread is under 4¢ (`*_MAX_SPREAD`) and 24h volume is at least `*_MIN_VOLUME`. Only their derived per-game series are compared, never single ladder rungs.
- Overlapping windows for the same game, market type and **direction** merge across venues. A spike and its reversal stay two moves. A merged move is `multi` when two or more venues are in it.
- Move IDs are stable across re-runs: a new window that overlaps an existing move reuses its ID and keeps its `explain_status`.
- Kalshi and Polymarket snapshots are stored when the mid moves ≥ 0.5 pp, the derived line moves, or 10 minutes pass (a heartbeat, so the 30-minute lookback always has a reference point).

All thresholds are in `config/thresholds.yaml`.

A replay of the last 24 hours of Kalshi 1-minute candles (27 games, Oct 24–25 2026) found 4 winner moves. The largest was BAL 47.5% → 42.5% vs CIN.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `STORE_BACKEND` | `duckdb` | `duckdb` or `snowflake` |
| `DUCKDB_PATH` | `data/nfl_lines.duckdb` | DuckDB file |
| `SNOWFLAKE_ACCOUNT`, `_USER`, `_ROLE`, `_WAREHOUSE`, `_DATABASE`, `_SCHEMA` | | connection |
| `SNOWFLAKE_PASSWORD` / `SNOWFLAKE_PRIVATE_KEY_PATH` | | auth (key pair preferred) |
| `SNOWFLAKE_CREDIT_CAP` | `20` | monthly credits before the resource monitor suspends the warehouse |
| `SNOWFLAKE_EMBED_MODEL` | `snowflake-arctic-embed-m-v1.5` | `AI_EMBED` model (768 dims) |
| `SNOWFLAKE_COMPLETE_MODEL` | `claude-sonnet-4-5` | `AI_COMPLETE` model |
| `KALSHI_BASE_URL` | production public API | |
| `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` | | WebSocket + trading only |
| `KALSHI_MIN_VOLUME` | `500` | min 24h contracts for a Kalshi quote to count |
| `KALSHI_MAX_SPREAD` | `0.04` | max yes bid-ask in dollars |
| `KALSHI_POLL_SECONDS` | `60` | REST poll period |
| `POLYMARKET_MIN_VOLUME` | `500` | min 24h USD volume for a Polymarket quote to count |
| `POLYMARKET_MAX_SPREAD` | `0.04` | max bid-ask in dollars |
| `ODDS_API_KEY` | | The Odds API |
| `ODDS_API_BOOKMAKERS` | pinnacle, DK, FD, MGM, Caesars (`williamhill_us`), ESPN BET | ≤ 10 books bill as one region |
| `ODDS_API_MIN_CREDITS` | `50` | stop calling below this |
| `ODDS_API_LIVE_MIN_CREDITS` | `5000` | keep started games only above this |
| `XAI_API_KEY`, `XAI_MODEL` | | Grok x_search (milestone 4) |
| `XAI_MAX_HANDLES_PER_SEARCH` | `10` | handle batch size |
| `XAI_COST_PER_CALL_USD` | `0.03` | logged cost estimate |
| `XAI_MOVE_COOLDOWN_MINUTES` | `15` | min gap between searches for one game |
| `TRADING_ENABLED`, `DRY_RUN`, `KALSHI_ENV`, `MAX_ORDER_USD`, `MAX_DAILY_USD`, `MAX_OPEN_EXPOSURE_USD`, `KILL_SWITCH_FILE` | off / dry / demo | optional trading module |
| `RAW_DATA_DIR` | `data/raw` | Parquet copies |
| `LOG_LEVEL` | `INFO` | |

## Costs

- **Kalshi:** free (public REST market data).
- **Polymarket:** free (public Gamma + CLOB read APIs).
- **nflverse:** free.
- **The Odds API:** one `/odds` call for h2h + spreads + totals with ≤ 10 books costs 3 credits. The brief's schedule (every 30 min Mon–Sat, every 10 min Sunday 9 AM ET to the last kickoff) is about 380 calls a week, roughly **1,150 credits a week or 4,900 a month**. That fits the $30/month tier (20,000 credits) but not the free 500/month tier. Historical snapshots cost 10× (30 credits each), which matters for the backfill.
- **Snowflake:** an X-Small warehouse uses 1 credit per running hour, billed per second after the first minute. `AUTO_SUSPEND = 60` plus the resource monitor (`SNOWFLAKE_CREDIT_CAP`) bound the spend. Cortex `AI_EMBED` and `AI_COMPLETE` are billed per token on top of that.
- **xAI:** every call is logged with a cost estimate in `SEARCH_RUNS` (milestone 4).

## Where the brief and current docs disagree

- **nfl_data_py is archived** (Sept 2025) in favor of `nflreadpy`, which this repo uses. nflverse has a `trades` table but no general signings/releases feed. Those claims will resolve from week-to-week roster status changes.
- **Kalshi WebSocket needs an API key.** Only REST market data is public. Ingestion polls REST every 60 s now, and a WebSocket client can be added once you have a key.
- **x_search handle limit:** third-party docs consistently report 10 handles per call (one source says 20). The default batch is 10 (`XAI_MAX_HANDLES_PER_SEARCH`) and will be confirmed against the live API in milestone 4.
- **nflverse snap counts are keyed by PFR player ID**, not GSIS. Claim resolution will need the nflverse player ID crosswalk.
- **The Odds API free tier can't support the brief's polling schedule** (see Costs).
- **Pinnacle** sits in The Odds API's `eu` region, so it's requested by bookmaker key rather than region.

## Repo layout

```
config/     insiders.yaml (tracked accounts), thresholds.yaml
common/     settings, team mapping, game matching, raw Parquet writer, logging
store/      schema.py (tables), base.py (portable SQL), duckdb.py, snowflake.py
sources/    kalshi.py, polymarket.py, ladder.py (shared exchange math), odds_api.py, nflverse.py
pipelines/  load_nflverse, ingest_odds, detect_moves, compare_venues, run_all
scripts/    probe_*.py
tests/      unit tests on recorded fixtures (no network)
```
