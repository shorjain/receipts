"""Table definitions in dialect-neutral types. Each store maps them to its own DDL.

All timestamps are stored as UTC without a zone (TIMESTAMP_NTZ / TIMESTAMP).
"""
from __future__ import annotations

EMBED_DIM = 768  # snowflake-arctic-embed-m-v1.5 and all-mpnet-base-v2 both emit 768

STR, INT, FLOAT, BOOL, TS, JSON, VEC = "STR", "INT", "FLOAT", "BOOL", "TS", "JSON", "VEC"

# table -> (columns, primary key). Snowflake does not enforce keys; upserts use them.
TABLES: dict[str, tuple[list[tuple[str, str]], list[str]]] = {
    "GAMES": ([
        ("game_id", STR), ("season", INT), ("week", INT), ("game_type", STR),
        ("home", STR), ("away", STR), ("kickoff_at", TS),
        ("home_score", INT), ("away_score", INT),
        ("close_spread_line", FLOAT), ("close_total_line", FLOAT),
        ("roof", STR), ("home_rest", INT), ("away_rest", INT), ("div_game", BOOL),
    ], ["game_id"]),
    # Not in the brief's model; needed to match claim names to player IDs.
    "ROSTERS": ([
        ("season", INT), ("week", INT), ("team", STR), ("player_id", STR),
        ("full_name", STR), ("position", STR), ("status", STR),
    ], ["season", "week", "team", "player_id"]),
    "MARKETS": ([
        ("market_id", STR), ("venue", STR), ("game_id", STR), ("market_type", STR),
        ("line", FLOAT), ("side", STR), ("source_ref", STR),
    ], ["market_id"]),
    "ODDS_SNAPSHOTS": ([
        ("market_id", STR), ("taken_at", TS), ("implied_prob", FLOAT), ("price", FLOAT),
        ("line", FLOAT), ("bid", FLOAT), ("ask", FLOAT), ("volume", FLOAT), ("source", STR),
    ], ["market_id", "taken_at"]),
    "LINE_MOVES": ([
        ("move_id", STR), ("game_id", STR), ("market_type", STR), ("venue_scope", STR),
        ("venues", STR), ("window_start", TS), ("window_end", TS),
        ("prob_before", FLOAT), ("prob_after", FLOAT),
        ("line_before", FLOAT), ("line_after", FLOAT),
        ("crossed_key_number", INT), ("magnitude_score", FLOAT),
        ("summary_text", STR), ("embedding", VEC), ("explain_status", STR),
        ("explanation", STR), ("detected_at", TS),
    ], ["move_id"]),
    "POSTS": ([
        ("post_id", STR), ("url", STR), ("author", STR), ("posted_at", TS),
        ("text", STR), ("text_source", STR), ("embedding", VEC),
    ], ["post_id"]),
    "SEARCH_RUNS": ([
        ("run_id", STR), ("purpose", STR), ("window_start", TS), ("window_end", TS),
        ("handles", STR), ("raw_response", JSON), ("citations", JSON), ("error", STR),
        ("cost_estimate_usd", FLOAT), ("ran_at", TS),
    ], ["run_id"]),
    "MOVE_POSTS": ([
        ("move_id", STR), ("post_id", STR), ("lead_minutes", FLOAT),
        ("relevance", FLOAT), ("attribution_score", FLOAT),
    ], ["move_id", "post_id"]),
    "CLAIMS": ([
        ("claim_id", STR), ("post_id", STR), ("author", STR), ("posted_at", TS),
        ("player_id", STR), ("player_name", STR), ("team", STR), ("claim_type", STR),
        ("predicted_value", STR), ("hedging", STR), ("is_original", BOOL),
        ("story_id", STR), ("resolve_by", TS),
    ], ["claim_id"]),
    "CLAIM_RESOLUTIONS": ([
        ("claim_id", STR), ("outcome", STR), ("evidence", JSON), ("resolved_at", TS),
    ], ["claim_id"]),
    "INSIDER_SCORES": ([
        ("author", STR), ("season", INT), ("score_window", STR), ("accuracy", FLOAT),
        ("raw_hit_rate", FLOAT), ("n_claims", INT), ("n_resolved", INT),
        ("scoop_rate", FLOAT), ("median_lead_minutes", FLOAT), ("market_impact", FLOAT),
        ("original_ratio", FLOAT), ("credibility_weight", FLOAT), ("computed_at", TS),
    ], ["author", "season", "score_window"]),
    "PREDICTIONS": ([
        ("game_id", STR), ("model_version", STR), ("made_at", TS),
        ("home_win_prob", FLOAT), ("market_prob_at_time", FLOAT), ("features_json", JSON),
    ], ["game_id", "model_version", "made_at"]),
    # Not in the brief's model: one row per paid API call, for cost tracking.
    "API_USAGE": ([
        ("called_at", TS), ("source", STR), ("endpoint", STR), ("cost_units", FLOAT),
        ("remaining", FLOAT), ("note", STR),
    ], ["called_at", "source", "endpoint"]),
}
