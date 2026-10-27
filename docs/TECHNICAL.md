# receipts: technical reference

The full design notes: data model, detection and scoring formulas, costs, API quirks, and every setting.
For a quick tour and setup, see the [main README](../README.md).

**receipts** (repo: `nfl-lines`) is a personal NFL analytics app that answers two questions:

1. **Why did the line move?** Detect odds moves on Kalshi and sportsbooks, then show the tracked-insider posts that most likely caused them, with citations.
2. **Which insiders can you trust?** Track every factual claim insiders make, check it against nflverse, and rank them by accuracy, earliness and market impact.

## Status

| # | Milestone | State |
|---|-----------|-------|
| 1 | Skeleton, `.env.example`, `store/` with schema, probe scripts + fixtures | done |
| 2 | nflverse loaders; GAMES + ROSTERS for the current season | done |
| 3 | Odds ingestion (Kalshi + The Odds API) and move detection | done (Odds API path tested on a hand-built fixture until a key is added) |
| 3b | Polymarket venue + Kalshi vs Polymarket gaps and lead-lag | done |
| 4 | X sweeps + on-move searches with citation verification | done (embeddings run in Snowflake after each sync) |
| 5 | Move explanations + Moves page | done |
| 6 | Claims, resolution, scores, Leaderboard + Insider pages | |
| 7 | Season backfill | done (runs in the loop, resumable) |
| 8 | Win-probability model + Games page | done (doesn't beat the closing line yet; reported honestly) |
| 9 | Ask chatbot | done |
| 10 | DuckDB fallback store (local embeddings + Ollama) | done |
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
python -m pipelines.ingest_odds --loop              # everything: poll, detect, compare, X search, sync
python -m pipelines.detect_moves --hours 48         # write LINE_MOVES
python -m pipelines.compare_venues --hours 48       # VENUE_GAPS + who moved first
python -m pipelines.search_moves --hours 24         # X search around each new move
python -m pipelines.sweep_insiders [--force]        # scheduled insider sweep
python -m pipelines.explain_moves --hours 24        # grounded explanations (Snowflake Cortex)
python -m pipelines.extract_claims                  # claims from new posts (Cortex structured output)
python -m pipelines.resolve_claims                  # check claims against nflverse
python -m pipelines.score_insiders                  # INSIDER_SCORES + credibility weights
python -m pipelines.backfill --status               # season backfill progress (loop stopped)
python -m pipelines.train_model                     # walk-forward report + predictions
python -m pipelines.sync_snowflake                  # copy new local rows to Snowflake
streamlit run app/Home.py                           # UI (reads data/ui_snapshot.duckdb)
uvicorn api.main:app --reload                       # JSON API: /moves, /moves/{id}, /ask?q=
```

### Run it in the background (macOS)

```bash
python -m scripts.service install
```

This installs a per-user LaunchAgent (`~/Library/LaunchAgents/com.nfl-lines.ingest.plist`) that runs `ingest_odds --loop`:
- it starts at login and restarts the loop if it exits (at most once a minute);
- it runs under `caffeinate -s`, so the Mac stays awake only while plugged in; on battery it sleeps normally and the loop pauses;
- it logs to `data/logs/ingest.log` (rotated at 10 MB, 5 files).

`python -m scripts.service status`, `logs` and `uninstall` do what they say. Only one process may write the DuckDB file, so stop any terminal loop before installing; the UI and API read the snapshot and can run alongside.

Probe scripts hit each API once and save a fixture to `tests/fixtures/`:

```bash
python -m scripts.probe_kalshi       # public, no key
python -m scripts.probe_nflverse     # public
python -m scripts.probe_polymarket   # public, read-only (needs GAMES loaded)
python -m scripts.probe_odds_api     # needs ODDS_API_KEY, costs ~3 credits
python -m scripts.probe_snowflake --guardrails   # needs Snowflake creds + ACCOUNTADMIN
python -m scripts.probe_xsearch      # needs XAI_API_KEY, ~$0.15
python -m scripts.verify_handles [--find]        # check / correct handles in insiders.yaml
```

## Snowflake setup

1. Create a key pair outside the repo:
   `mkdir -p ~/.snowflake && openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out ~/.snowflake/rsa_key.p8 -nocrypt && chmod 600 ~/.snowflake/rsa_key.p8`
2. Register the public half (`openssl rsa -in ~/.snowflake/rsa_key.p8 -pubout`, without the BEGIN/END lines) in a worksheet. Your internal user name can differ from your login name, so let Snowflake fill it in:
   ```sql
   SET me = '"' || CURRENT_USER() || '"';
   ALTER USER IDENTIFIER($me) SET RSA_PUBLIC_KEY='<public key>';
   ```
3. In `.env`: `SNOWFLAKE_USER` is your login name, and `SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/rsa_key.p8`.
4. `python -m scripts.probe_snowflake --guardrails` creates the X-Small warehouse (auto-suspend 60 s), the resource monitor, the database (owned by `SNOWFLAKE_ROLE`) and the tables, then checks `AI_EMBED` and `AI_COMPLETE`.

Cortex functions take the model name as a SQL literal, not a bind parameter; `store/snowflake.py` validates the name before inlining it.

## Store

`STORE_BACKEND=duckdb` (default) writes to `data/nfl_lines.duckdb` with no account. `STORE_BACKEND=snowflake` uses Snowflake.

**How the two are used together.** Minute-by-minute polling writes to local DuckDB. A warehouse woken every minute would never suspend, which is about 24 credits a day. Once an hour the loop runs `pipelines/sync_snowflake.py`. Every upsert stamps `updated_at`, so the sync copies only rows changed since the last run. It then fills missing `POSTS` and `LINE_MOVES` embeddings server-side with `AI_EMBED` (embedding columns are never copied from DuckDB) and suspends the warehouse immediately instead of idling 60 s. The first sync (46K rows) took 31 s; hourly ones take seconds. Both implement the same interface (`store/base.py`). All SQL lives in `store/`; `tests/test_repo_rules.py` fails if SQL shows up anywhere else.

- Table definitions live once in `store/schema.py` in neutral types; each backend maps them to its DDL (`VECTOR(FLOAT, 768)` in Snowflake, `FLOAT[768]` in DuckDB).
- `create_schema()` also adds any columns that `schema.py` gained since the table was created, so existing databases migrate in place.
- Every write is an upsert (`MERGE`) on the table's key, so re-running a pipeline never duplicates rows.
- All timestamps are naive UTC.
- Every raw pull (Kalshi markets, Odds API events, nflverse tables) is also written to `data/raw/<source>/<date>/*.parquet`.

Changes from the brief's data model: `ROSTERS` (for matching claim names to player IDs), `API_USAGE` (one row per paid call), `VENUE_GAPS` (Kalshi vs Polymarket disagreements) and `SYNC_STATE` (local sync bookkeeping) are added. Every table has `updated_at`. `SEARCH_RUNS` also records `move_id`, `game_id`, `posts_kept` and `posts_rejected`. `INSIDER_SCORES.window` is named `score_window` because `WINDOW` is a reserved word in both Snowflake and DuckDB. `LINE_MOVES` gains `venues`, `explanation`, `detected_at`, and the venue comparison columns `venue_reactions`, `leader_venue` and `lead_lag_minutes`; `GAMES` gains closing lines, roof, rest days and division flag for the model.

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

## Insider posts from X

All X data comes from Grok's server-side `x_search` tool (`sources/xsearch.py`). Trust rules:

- **Citations only.** A post is kept only if its URL is in the response's server-side citations.
- **Time from the ID.** `posted_at` is decoded from the post ID: `(id >> 22) + 1288834974657` ms.
- **Author and text from X, not the model.** Both come from X's public oEmbed endpoint (`publish.x.com/oembed`) for that post ID. A post whose author isn't one of the searched handles is rejected. `text_source` is `oembed` (verified text) or `citation_only`. The brief's `grok_quote` isn't used, since oEmbed gives verified text.
- **Every call is logged** in `SEARCH_RUNS`: handles, window, the model's reply, citations, rejected citations with reasons, and the cost estimate.

**On-move searches** (`pipelines/search_moves.py`) look from 60 minutes before each move through its end. Only posts **about that game** are linked (`common/relevance.py`). A post counts if it names either team (capitalized nickname or city, hashtags like #ChiefsKingdom, or an unambiguous abbreviation), names a player on either roster (full name, or a surname only one player in the league has), or comes from a beat reporter who covers either team. National insiders post about every team, so timing alone isn't enough: before this rule, 844 of 1,240 links (68%) were off-topic, e.g. a Rapoport post about Giants RB Cam Skattebo attached to a Broncos–Chiefs move. `scripts/relink_moves.py` cleans existing links. They search the national insiders plus both teams' beat reporters, and link posts in `MOVE_POSTS` with `lead_minutes` (move start minus post time; negative means after). A game searched within `XAI_MOVE_COOLDOWN_MINUTES` reuses the stored posts instead of searching again.

**Sweeps** (`pipelines/sweep_insiders.py`) run every 15 minutes from 90 minutes before a kickoff until it, and hourly otherwise. Each covers the time since the last sweep. They search the national insiders plus beat reporters for teams playing in the next 36 hours.

**Alerts.** After every search the loop checks today's estimated spend. It sends a macOS notification once it passes `XAI_NOTIFY_USD`, and again when it reaches the cap and searches pause until midnight UTC.

**Turning it off.** xAI bills per call. With `XAI_ENABLED=false` the loop makes no X searches; posts already stored stay in place.

**Budget.** `XAI_DAILY_BUDGET_USD` caps estimated spend on move searches and sweeps. Sweeps stop at `XAI_SWEEP_BUDGET_SHARE` (80%) of it, so move searches always have room. One-off setup searches (`verify_handles`) don't count.

**What probing the API turned up (Oct 2026):**
- `to_date` filters by whole days, so `to_date=now` drops today's posts. Searches pass only `from_date` and filter to the exact window using ID timestamps.
- The prompt must name each handle and ask to search each one. Vaguer prompts made the model run searches that matched nothing.
- 11 handles per search worked (the brief expected 10). Batches default to 10.
- A search returns roughly 60 posts, so quiet accounts in a busy batch can be missed.

**Handles.** `scripts/verify_handles.py` confirms each handle posted recently and that the account's display name matches the configured name. `--find` looks up the right handle by name, outlet and team; a beat reporter's posts must also mention their team, so same-named strangers don't pass. That corrected 21 of the beat handles written from memory. 67 of 70 are verified. AdamTeicher, DNewtonespn and agetzenberg remain unverified and are skipped.

## Move explanations

`pipelines/explain_moves.py` runs in the loop for every move whose posts have been linked:

- **Scoring each linked post:**
  - **relevance** is the embedding similarity between the post and the move's `summary_text` (`AI_EMBED`, one Snowflake query per move), rescaled from 0.65–0.90 to 0–1. It gets +0.15 when the post names either team or a player on either roster.
  - **timing** is 1.0 for posts 0–20 minutes before the move, falling linearly to 0 at 60 minutes before. Posts after the move get 0.3 × e^(lead/10), since they report on the move rather than cause it.
  - **attribution** = relevance × timing × `credibility_weight`, which is 0.5 until milestone 6 scores the insiders.
- **No post before the move:** the move is marked "Unexplained: no insider news before this move", with no AI call. These are worth watching, since they may be sharp money or news from outside the tracked list.
- **Otherwise:** a numbered evidence block (`[M1]` is the move with its numbers from SQL; `[P1]`… are the top 5 posts, each labeled with how many minutes before or AFTER the move it was posted) goes to `AI_COMPLETE` at temperature 0.
- **Grounding guard** (`rag/guard.py`, lined up with shipyard-rs's `enforce`), applied to the reply:
  - bare references like "P2" become "[P2]";
  - sentences with numbers not in the evidence are dropped. Matching is exact after normalizing $, commas, % and trailing zeros: 45% matches 45.0%, but 0.45 and 45.04 don't. Numbers from the user's question may be repeated back;
  - citations that aren't in the evidence are removed;
  - causal claims with no cited post are dropped;
  - posts published after the move can't be named as the cause;
  - at most 3 sentences survive. If none do, the status is `refused` rather than inventing an explanation.

With X search off, moves link only to posts already stored. A replay of Saturday Oct 24 showed one case clearly. The Ravens' win probability vs CIN fell 47.5% → 42.5% on both Kalshi and Polymarket, with Kalshi 8 minutes ahead. The tracked insiders' "Lamar Jackson ruled out" posts came 30 and 36 minutes after the move started. The market knew first, so the app marks the move unexplained and shows those posts as reporting on it.

## Season backfill

`pipelines/backfill.py` gives the app this season's history. It runs inside the loop, about 20 seconds per minute (`--backfill-seconds`), so live polling continues and the loop stays the only DuckDB writer. Each unit of work is a task in `BACKFILL_STATE`, and a finished task is never redone, so the backfill resumes wherever it stopped. It replans every 12 hours to pick up newly finished games.

| Task | What it does | Cost |
|---|---|---|
| `kalshi:<game>` | 1-minute candles for both teams' winner markets, from market open (at most 14 days) to kickoff, fetched 3 days per call (Kalshi's limit is ~5,000 candles). Turned into the same normalized P(home win) series live polling stores, with the home market's bid/ask and rolling 24 h volume | free |
| `kalshi_spread:<game>`, `kalshi_total:<game>` | 1-minute candles for the ladder rungs within 4 points of the game's closing line (about 15 rungs a game instead of ~45), rebuilt minute by minute into the same derived lines, with their rung sets, that live polling stores. Finishing one reopens the game's detection | free |
| `poly:<game>` | Polymarket's minute price history for the moneyline. It has no bid/ask or volume to check, so it's stored as `polymarket_history`, which can **confirm** a Kalshi move (making it multi-venue) but never creates a move alone | free |
| `detect:<game>` | after both fetches: move detection and venue comparison for the game's pre-kickoff window, then one search task per new move | free |
| `search:<move>` | X search around the past move (with `to_date` = the next day, which works for past windows), biggest and multi-venue moves first, while `BACKFILL_XAI_USD` (default $3) and the daily cap allow. After that, moves are linked to posts already stored. The loop's explain step then explains them, whatever their age | xAI, capped |
| `oddsapi:<day>` | optional, `BACKFILL_ODDS_API_CREDITS` > 0: one historical snapshot per game day at noon ET | 30 credits each; off by default |

Why confirm-only: the first backfill run treated that history as liquid and found 1,106 moves in 94 games, 1,015 of them Polymarket-only. Most were overnight spikes on thin books (for example 37.5% → 41% → 38% between 3 and 4 AM ET) that live polling would have filtered with real order books. A one-off migration in the loop relabeled that history, dropped the moves it created alone, and redid detection.

Backfilled moves are all more than 24 hours before kickoff. That isn't missing data: prices run right up to kickoff, but by then the week's injury news is priced in. On Oct 25, live polling also saw no 3-point moves on that day's games between 3 AM and kickoff.

The Odds API part is off by default because at 30 credits per snapshot the free tier buys about 15 snapshots a month. That's too few to detect book moves, and nflverse already provides closing lines for the model benchmark. Backfilled prices are stored like live ones: a point is kept when it moves ≥ 0.5 pp or 10 minutes pass.

## Insider credibility

**Claim extraction** (`pipelines/extract_claims.py`, hourly in the loop) sends new posts in batches of 15 to `AI_COMPLETE` with structured output. Each reply is a JSON list matching the CLAIMS schema: player, team, type, predicted value, hedging (confirmed / expected / rumor), who the post credits, and the quoted words.
- **Player matching:** names are fuzzy-matched to nflverse IDs, first within the stated team (score ≥ 90). Failing that, across all rosters with a near-exact unique match (≥ 95), which catches a player named under his old team. Two players with the same name are never guessed; the claim is stored with `player_id` null.
- **Game and deadline:** each claim gets the game it's about and a `resolve_by` time.
- **Stories:** the same player, type and value within 6 hours is one story. The earliest post that doesn't credit anyone gets the scoop (`is_original`); later posts and posts that credit someone don't.

**Resolution** (`pipelines/resolve_claims.py`, every 6 hours) checks claims against nflverse. Claims whose data hasn't arrived stay pending; after `resolve_by` + 7 days they become unresolvable.

| Claim | Correct when |
|---|---|
| active / questionable_will_play | the player took a snap in that game (`snap_counts`, via the gsis → PFR ID crosswalk) |
| inactive | the player took no snap |
| starter | QBs: threw the team's first pass (`pbp`). Others: played ≥ 50% of the team's offensive or defensive snaps |
| trade | the player's team changed in weekly rosters before `resolve_by` (to the named team, if one was named) |
| signing | he appears on the named team |
| release | he's cut or off the team |
| return_date | the first week he records a snap equals the predicted week |
| other, or no matched player | unresolvable, and doesn't count |

Starter claims wait until the game is played. 2026 play-by-play participation isn't published, and depth charts lag injuries: a trial run marked a backup QB "incorrect" as the starter because the chart still listed the injured starter first. Hence the snap-share stand-in.

**Scores** (`pipelines/score_insiders.py`), per author for the season and the last 4 weeks:

- **accuracy** = (Σw·correct + 2) / (Σw + 4) over resolved claims. w = 0.5 for hedged claims and 2 for a confirmed miss. Raw hit rate and n are shown next to it.
- **original_ratio** = claims that don't credit anyone ÷ all claims.
- **scoop_rate** = of their own (uncredited) claims, the share where they were first among tracked accounts in the story. The brief's "is_original" and "first" overlap once later posters are marked unoriginal, so `original_ratio` counts uncredited claims and `scoop_rate` counts firsts.
- **median_lead_minutes** = for their scoops, minutes until the next tracked account posted the same story.
- **market_impact** = mean over their own claims about a game of |ΔP(home win) in the next 30 min| minus that game's typical 30-minute move over the prior 24 h (Kalshi, else Polymarket, else consensus).
- **credibility_weight** (`credibility_weight()`, the one function to tune) = 0.60 accuracy + 0.15 scoop_rate + 0.15 min(1, market_impact / 2 pp) + 0.10 original_ratio. A missing signal counts as 0.5. The result is shrunk toward 0.5 by n_resolved / (n_resolved + 5), so new insiders start neutral. `explain_moves` uses it in attribution.

The **Leaderboard** page recomputes these with the same function for any filter (season, window, claim types, team) and awards badges: 🎯 Most accurate (≥ 5 resolved), ⚡ Fastest (largest median lead), 📈 Moves markets, 🔁 Mostly reposts (< 40% original, ≥ 5 claims). Every insider links to their **Insider** page: the full receipt log with each claim, its outcome, the evidence (table and value) and a link to the post.

## Win-probability model

`pipelines/train_model.py` retrains every 6 hours in the loop.

**Features** are all known before kickoff, and each enters as home minus away:
- rolling EPA per play over the last 4 and 8 games, offense and defense;
- success rate and QB EPA per dropback over the last 8;
- rest-day difference, the away team's travel distance, and a neutral-site flag;
- injuries: snap share (from earlier weeks this season) of players listed Out or Doubtful, on offense and defense, plus a starting-QB-out flag.

Rolling stats are computed over every scheduled game, so an upcoming game gets each team's latest form, and the current game is always excluded. Data comes from nflverse 2019–present; past seasons are cached as Parquet under `data/raw/model/`.

**Models:** logistic regression and gradient boosting (scikit-learn).

**Validation:** walk-forward by week over the last four seasons. Each week is predicted by a model fit only on games before it; a test checks that no training row is from the future. Benchmarks:
- the **closing line**: nflverse's consensus closing moneylines with the vig removed. Pinnacle's close would need paid Odds API history, so this stands in;
- **Kalshi's** P(home win) one hour before kickoff, for 2026 games with backfilled data.

Results (Oct 25 2026), where lower is better:

| Comparison | Games | Model log loss / Brier | Market |
|---|---|---|---|
| logreg vs closing line, 2023–26 | 952 | 0.646 / 0.228 | 0.613 / 0.213 |
| gbm vs closing line | 952 | 0.665 / 0.236 | 0.613 / 0.213 |
| logreg vs Kalshi, 2026 | 98 | 0.680 / 0.244 | Kalshi 0.661 / 0.233, close 0.661 / 0.233 |

**The model doesn't beat the closing line**, as the brief expected for v1. Logistic regression beats gradient boosting, so it's the live model. A big gap between model and market more often means the model is missing news than that the market is wrong. Ideas for v2: a recent-credible-insider-news flag (from Feature B), QB-level ratings, and weather.

**Games** page:
- each upcoming game's model and market probability, flagged ⚠️ when they differ by ≥ `MODEL_DISAGREE_POINTS` (8 pts);
- the five features that moved each prediction most (logistic-regression contributions);
- the walk-forward report card and calibration chart, from `data/model/report.json`.

## Ask chatbot

`rag/` answers questions only from stored data:

1. **`parse.py`** turns the question into filters with plain rules, no model. It finds teams (nicknames, full names, cities; ambiguous abbreviations like NO or LA only in upper case), players (full name, or a last name only one rostered player has), insiders (handle, name, or a last name not already used by a player: "Lamar Jackson" doesn't pull in reporter Zac Jackson), claim types ("injury" → active / inactive / questionable / return date), a time window ("this morning", "yesterday", "on Friday", "week 7", "last 3 hours", "this season", ET-based) and an intent (moves, scoop, scores, predictions).
2. **`retrieve.py`** filters with SQL through `store.ask_*`. In Snowflake, posts and moves are ranked by `VECTOR_COSINE_SIMILARITY` to the question. Questions about an insider's accuracy get stats for exactly the asked claim types and window, computed with `score_insiders`' formulas.
3. **`answer.py`** builds a numbered evidence block (`[M#]` moves, `[P#]` posts, `[C#]` claims, `[S#]` insider stats, `[G#]` predictions) and calls `AI_COMPLETE` at temperature 0.
4. **Grounding guard:** `rag/guard.py` with `require_citation`, so every sentence must cite evidence. As in shipyard-rs, the guard's removals are shown as warnings under the answer, and an answer about a move that cites no posts is flagged "explains no cause". "No evidence for X in that window" and "the draft didn't survive the checks" get separate messages, and the evidence is still shown either way. It also drops numbers not in the evidence, strips unknown citations, and caps answers at 4 sentences. With no evidence it refuses without calling the model.

Asks read from Snowflake, which holds the embeddings, so data is up to one hourly sync old. `ASK_STORE=local` reads the local snapshot instead (fresher, ranked by recency) and still uses Cortex to write the answer. Available on the **Ask** page and as `GET /ask?q=`.

Examples from Oct 25 2026:
- "Who broke the Lamar Jackson news first?" → @jeffzrebiec at 3:31 PM ET Saturday, with Hensley and Cabot after him, all cited.
- "How accurate has Zrebiec been on injury calls this season?" → 11 injury claims, none resolved yet, so the smoothed 0.50 is a placeholder.
- "Why did the Bills line drop this morning?" → says the stored data doesn't show a Bills move this morning.

## After the Snowflake trial: running fully local

Snowflake was never the only copy. Every row is written to local DuckDB first and synced up, and raw pulls are also in `data/raw/` as Parquet. Only the Cortex AI functions need replacing, and `AI_BACKEND=local` swaps them:

| Snowflake | Local replacement |
|---|---|
| `AI_EMBED` (arctic-embed-m, 768-d) | sentence-transformers `all-mpnet-base-v2`, also 768-d, so the vector columns don't change |
| `VECTOR_COSINE_SIMILARITY` | DuckDB `array_cosine_similarity` |
| `AI_COMPLETE` (+ structured output) | Ollama `/api/generate` at temperature 0; the JSON schema goes in Ollama's `format` |

Switching over when the trial ends:

1. Install and start Ollama, then pull the model:
   ```bash
   brew install ollama
   ```
   ```bash
   ollama serve
   ```
   ```bash
   ollama pull qwen2.5:3b
   ```
   That's about 1.9 GB. `qwen2.5:7b` (4.7 GB) writes better explanations and extracts claims more reliably, and also fits a 16 GB Apple-silicon Mac; set `OLLAMA_MODEL` to switch.
2. In `.env`: set `AI_BACKEND=local` and clear `SNOWFLAKE_ACCOUNT`. That stops the sync, which can't reach a closed account anyway.
3. Restart the loop. It embeds stored posts and move summaries locally (500 rows per 10-minute pass), and claim extraction, explanations and Ask switch to Ollama.

A local check on Oct 25 2026 (M4 MacBook Air, 16 GB, `qwen2.5:3b`):
- 1,028 posts embedded in 12 s;
- claim extraction: 4 claims from 15 posts in 6 s;
- one move explanation and one Ask answer: about 5 s each.

The 3B model follows the citation rules, but it reasons less carefully than Claude on Cortex. Asked "Who broke the Lamar Jackson news first?", it named Hensley, when the claims evidence shows Zrebiec 6 minutes earlier. The guard checks citations and numbers, not whether the evidence was read correctly. Use `qwen2.5:7b` or larger when quality matters.

The local and Snowflake embedding models score similarity on different scales, so each backend carries its own range for rescaling relevance (Snowflake 0.65–0.90, local 0.15–0.75). If Ollama isn't running, the AI steps wait and log how to start it; polling, detection, resolution and scoring keep going.

## UI and API

```bash
streamlit run app/Home.py      # http://localhost:8501
```

The look is modeled on the Tech@NYU site, using its visual language only (no logo or name): a near-black background with a faint grid, Oxanium headings, Inter body text, monospace labels and buttons, and thin borders with square corners. It's monochrome, with green and red only for numbers going up or down. The theme lives in `.streamlit/config.toml` and `app/ui.py`; the name and tagline are `APP_NAME` and `TAGLINE` in `app/ui.py`.

Every page is written for someone who doesn't bet. Each starts with a plain one-line summary and a "How to read this page" box, shows times in Eastern with "hours before kickoff", and writes moves and claims as sentences ("Ravens' chances to beat the Bengals fell from 48% to 42%", "Lamar Jackson (BAL) won't play"). Pages in the top bar:

| Page | What it shows |
|---|---|
| Home | what the app is, live status (last odds check, moves this week, claims tracked), how it works, a glossary |
| Line moves | each move as a sentence, whether 2 markets confirmed it, what probably happened (or "no clear cause"), a chart, and the posts cited as [P1]… |
| Insider scorecard | trust score 0–100 (everyone starts at 50), right calls "4 of 5 checked", first to report, badges |
| Insider receipts | one reporter's claims as sentences, how sure they sounded, the result and how it was checked |
| Game odds | market vs model win chance per game, big disagreements flagged, and a plain verdict on how good the model is |
| Ask | plain-English questions answered from stored data, with sources |

A move whose explanation is grounded but says the posts don't explain it gets the status `unclear`, so it isn't counted as "cause found".

DuckDB lets only one process open its file for writing, so the ingest loop writes a read-only copy, `data/ui_snapshot.duckdb`, every 5 minutes. The Streamlit pages (`app/`) and the FastAPI app (`api/main.py`) read that copy, so they never wake Snowflake.

The **Moves** page lists every move:
- game, market, before → after, venues, and which venue moved first;
- the explanation;
- a chart of each venue's series around the window, with the move shaded and post times marked;
- the top posts as cards with links to X.

Filters: days back, week, team, venue, and explained, unexplained or pending.

## Move detection

A move is any of the following within `lookback_minutes` (30), using pre-kickoff snapshots only:

| Market | Trigger | magnitude_score |
|---|---|---|
| winner | \|Δ P(home)\| ≥ 3 pp | \|Δp\| / 0.03 |
| spread | line touches or passes 3, 7, 10 or 14, with ≥ 0.5 pts of movement | max(\|Δline\| / 1.0, 1.0) |
| total | \|Δ total\| ≥ 1.5 | \|Δ\| / 1.5 |

- Kalshi and Polymarket points count only when the yes bid-ask spread is under 4¢ (`*_MAX_SPREAD`) and 24h volume is at least `*_MIN_VOLUME`. Only their derived per-game series are compared, never single ladder rungs. Polymarket price history (backfill only) has no quotes, so it can confirm a move but not create one.
- Overlapping windows for the same game, market type and **direction** merge across venues. A spike and its reversal stay two moves. A merged move is `multi` when two or more venues are in it.
- Derived spread/total lines remember which ladder rungs they came from, and two points are only compared when their rung sets overlap by at least 50% (`min_rung_overlap`). This is shipyard-rs's strike-overlap check: when liquid rungs appear or vanish, the interpolated line can jump without the market moving. `magnitude_score` uses the same formula as shipyard-rs's shift score, the largest metric divided by its threshold.
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
| `SNOWFLAKE_PASSWORD` / `SNOWFLAKE_PRIVATE_KEY_PATH` | | auth; key pair recommended (see "Snowflake setup") |
| `SNOWFLAKE_CREDIT_CAP` | `20` | monthly credits before the resource monitor suspends the warehouse |
| `SNOWFLAKE_SYNC_MINUTES` | `60` | minutes between DuckDB → Snowflake syncs in the loop |
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
| `XAI_API_KEY`, `XAI_MODEL` | `grok-4.20-non-reasoning` | Grok x_search (milestone 4) |
| `XAI_ENABLED` | `true` | `false` stops the ingest loop from making any X searches |
| `XAI_MAX_HANDLES_PER_SEARCH` | `10` | handle batch size (11 verified to work) |
| `XAI_PRICE_IN_PER_M`, `XAI_PRICE_OUT_PER_M`, `XAI_SEARCH_FEE_USD` | `1.25`, `2.5`, `0.005` | cost-estimate rates |
| `XAI_DAILY_BUDGET_USD` | `2.0` | daily cap on move searches + sweeps |
| `XAI_SWEEP_BUDGET_SHARE` | `0.8` | share of the cap sweeps may use |
| `XAI_NOTIFY_USD` | `2.0` | Mac notification when today's spend passes this, and again at the cap |
| `XAI_TIMEOUT_SECONDS` | `120` | per-call timeout |
| `XAI_MOVE_COOLDOWN_MINUTES` | `15` | min gap between searches for one game |
| `TRADING_ENABLED`, `DRY_RUN`, `KALSHI_ENV`, `MAX_ORDER_USD`, `MAX_DAILY_USD`, `MAX_OPEN_EXPOSURE_USD`, `KILL_SWITCH_FILE` | off / dry / demo | optional trading module |
| `BACKFILL_ENABLED` | `true` | run season backfill slices in the loop |
| `BACKFILL_XAI_USD` | `3.0` | total xAI spend for searching past moves |
| `BACKFILL_ODDS_API_CREDITS` | `0` | credits for historical Odds API snapshots (30 each) |
| `MODEL_DISAGREE_POINTS` | `0.08` | Games page flags model-vs-market gaps at least this big |
| `AI_BACKEND` | (auto) | `snowflake` or `local`; empty = snowflake when `SNOWFLAKE_ACCOUNT` is set |
| `LOCAL_EMBED_MODEL` | `sentence-transformers/all-mpnet-base-v2` | local embeddings |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_TIMEOUT_SECONDS` | `http://localhost:11434`, `qwen2.5:3b`, `300` | local LLM |
| `ASK_STORE` | `snowflake` | where Ask retrieves from: `snowflake` (vector ranking) or `local` (fresher snapshot) |
| `RAW_DATA_DIR` | `data/raw` | Parquet copies |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FILE` | | write logs to this file with rotation (set by the background service) |

## Costs

- **Kalshi:** free (public REST market data).
- **Polymarket:** free (public Gamma + CLOB read APIs).
- **nflverse:** free.
- **The Odds API:** the default `paced` mode (`config/thresholds.yaml`) fits the free 500-credit tier. It polls at 10:00 and 18:00 ET, plus once when Kalshi or Polymarket detect a move (30-minute cooldown). It never makes more calls in a day than (credits left − `ODDS_API_MIN_CREDITS`) ÷ days left in the month ÷ 3, which is about 5 a day on a fresh month. `mode: interval` restores the brief's schedule. One `/odds` call for h2h + spreads + totals with ≤ 10 books costs 3 credits. The brief's schedule (every 30 min Mon–Sat, every 10 min Sunday 9 AM ET to the last kickoff) is about 380 calls a week, roughly **1,150 credits a week or 4,900 a month**. That fits the $30/month tier (20,000 credits) but not the free 500/month tier. Historical snapshots cost 10× (30 credits each), which matters for the backfill.
- **Snowflake:** an X-Small warehouse uses 1 credit per running hour, billed per second after the first minute. `AUTO_SUSPEND = 60` plus the resource monitor (`SNOWFLAKE_CREDIT_CAP`) bound the spend. Cortex `AI_EMBED` and `AI_COMPLETE` are billed per token on top of that.
- **xAI:** every call is logged with a cost estimate in `SEARCH_RUNS`. The estimate is tokens × price plus searches × fee, with env-tunable rates; check the Logs page on console.x.ai for exact charges. Measured on a Sunday morning: one 10-handle, 6-hour sweep batch cost $0.10–0.58 (the national insiders are the busiest). A full sweep of all 67 verified handles is about $1.20–1.50. The default $2/day cap therefore allows about one full sweep plus move searches; the brief's 15-minute/hourly schedule needs a larger budget or fewer handles per sweep.

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
sources/    kalshi.py, polymarket.py, ladder.py (shared exchange math), odds_api.py, nflverse.py,
            xsearch.py
pipelines/  load_nflverse, ingest_odds, detect_moves, compare_venues, search_moves,
            sweep_insiders, search_x (shared runner), explain_moves, extract_claims,
            resolve_claims, score_insiders, sync_snowflake, run_all
rag/        parse.py, retrieve.py, answer.py (Ask) and guard.py (shared grounding guard)
app/        Streamlit: Home.py (router + theme), views/ (home, moves, scorecard, insider, games, ask), ui.py
api/        FastAPI read-only endpoints
scripts/    probe_*.py
tests/      unit tests on recorded fixtures (no network)
```
