"""External-source registry.

Coat is the context layer. External data sources (weather, supply-chain
news, sanctions lists, market data, third-party benchmarks) are
registered at the tenant level. Coat fetches from them on schedule and
materializes records into the EXTERNAL_SIGNALS table, keyed by business
entity. Agents never hold API keys, never make outbound HTTP, never know
what an RSS feed is — they receive assembled bundles, and the bundle
assembler joins from EXTERNAL_SIGNALS.

In the MVP, each source module emits synthetic-but-realistic records so
the demo runs deterministically. In production the same module would
hit a real API or feed and write the same records.

Add a new external source by:
  1. Writing a module here exposing a `seed(conn)` function (or a real
     `fetch(conn)` for production) that inserts into EXTERNAL_SIGNALS.
  2. Adding a config entry under config/connections/external/<name>.yaml
     declaring the source's auth, schedule, and entity scope.
  3. Listing the source in `SOURCES` below so `seed_all` picks it up.
"""
from __future__ import annotations

from typing import Callable

from . import weather, shipping_news

# Each source module exposes a `seed(conn)` callable used by the MVP demo.
# In production, replace with `fetch(conn, since)` returning the same shape.
SOURCES: dict[str, Callable] = {
    "weather": weather.seed,
    "shipping_news": shipping_news.seed,
}


def seed_all(conn) -> dict[str, int]:
    """Seed every registered source. Returns counts by source for sanity."""
    counts: dict[str, int] = {}
    for name, fn in SOURCES.items():
        counts[name] = fn(conn)
    return counts
