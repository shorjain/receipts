"""One live x_search call with the national insiders, saved as a test fixture.

Also checks the per-search handle limit: the list is 11 handles with @AdamSchefter
(who posts constantly) last. If his posts come back, the 11th handle was searched.

Run:  python -m scripts.probe_xsearch [--hours 6]
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from scripts._fixture import save
from sources import xsearch

HANDLES = ["RapSheet", "TomPelissero", "MikeGarafolo", "DMRussini", "JFowlerESPN",
           "Schultz_Report", "FieldYates", "MikeReiss", "RichCimini", "BrookePryor", "AdamSchefter"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    hours = ap.parse_args().hours
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    client = xsearch.XSearchClient()
    res = client.search(HANDLES, now - timedelta(hours=hours), now)
    print(f"{len(res.citations)} citations, {res.tool_calls} searches, ~${res.cost_estimate_usd:.3f}")
    ids = [pid for u in res.citations if (pid := xsearch.post_id_from_url(u))]
    embeds = {pid: xsearch.fetch_oembed(pid) for pid in ids}
    authors = Counter((e or {}).get("author_url", "lookup failed").rsplit("/", 1)[-1] for e in embeds.values())
    print("authors:", dict(authors))
    print("11th handle (AdamSchefter) searched:", authors.get("AdamSchefter", 0) > 0)
    save("xsearch_response.json", {
        "handles": HANDLES, "window_start": now - timedelta(hours=hours), "window_end": now,
        "content": res.content, "citations": res.citations, "usage": res.usage,
        "tool_calls": res.tool_calls, "oembed": embeds,
    })


if __name__ == "__main__":
    main()
