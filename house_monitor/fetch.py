"""Shared HTTP helper for the scrapers: GET with retry on connection drops."""

import asyncio
import logging
from typing import Any, Tuple

import aiohttp


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    **kwargs: Any,
) -> Tuple[int, str]:
    """GET the URL and return (status, body text).

    Retries connection-level failures (aiohttp.ClientConnectionError — the
    common ancestor of ClientOSError/"Broken pipe" and
    ServerDisconnectedError). These come from the shared session's keep-alive
    pool: while a slow page is being processed, the server closes the idle
    kept-alive socket, and the next request of the pagination loop writes
    into the dead connection. aiohttp drops the broken socket from the pool
    on the error, so the retry runs on a fresh connection — which is why a
    single retry almost always succeeds.

    Deliberately NOT retried: non-200 statuses (the server answered; the
    callers' own status handling stays in charge) and timeouts (those show
    up at the 16:00 concurrent peak, where piling on more 30s attempts
    would only make the run slower).
    """
    for attempt in range(1, attempts + 1):
        try:
            async with session.get(url, **kwargs) as resp:
                # .text() stays inside the try: the connection can also drop
                # mid-body, not just while the request is being sent.
                return resp.status, await resp.text()
        except aiohttp.ClientConnectionError as e:
            if attempt == attempts:
                raise
            logging.warning(f"{url}: connection error, retrying ({attempt}/{attempts}): {e}")
            await asyncio.sleep(backoff * attempt)
    raise AssertionError("unreachable")  # the loop either returns or raises
