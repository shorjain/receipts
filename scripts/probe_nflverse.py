"""Pull nflverse tables once and save small fixtures (a few rows each).

Run:  python -m scripts.probe_nflverse [--season 2026]
"""
import argparse

from scripts._fixture import save
from sources import nflverse


def main() -> None:
    import nflreadpy
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=nflreadpy.get_current_season())
    season = ap.parse_args().season
    sched = nflverse.load_schedules(season)
    print(f"schedules: {len(sched)} rows")
    save("nflverse_schedules.json", sched.head(6).to_dict("records"))
    rost = nflverse.load_rosters_weekly(season)
    print(f"rosters_weekly: {len(rost)} rows")
    save("nflverse_rosters_weekly.json", rost[rost["team"].isin(["SEA", "NE"])].head(20).to_dict("records"))
    for name, fn in [("snap_counts", nflverse.load_snap_counts), ("injuries", nflverse.load_injuries),
                     ("depth_charts", nflverse.load_depth_charts), ("trades", nflverse.load_trades)]:
        try:
            df = fn(season)
            print(f"{name}: {len(df)} rows, columns: {list(df.columns)[:12]}...")
            save(f"nflverse_{name}.json", df.head(5).to_dict("records"))
        except Exception as e:
            print(f"{name}: FAILED ({e})")


if __name__ == "__main__":
    main()
