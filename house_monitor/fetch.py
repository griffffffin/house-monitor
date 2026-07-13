"""Shared HTTP helper for the scrapers: GET with retry on connection drops."""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional, Tuple, TypeVar

import aiohttp

T = TypeVar("T")


async def _get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    read_response: Callable[[aiohttp.ClientResponse], Awaitable[T]],
    *,
    attempts: int,
    backoff: float,
    **kwargs: Any,
) -> T:
    """Shared retry core for fetch_text/fetch_bytes.

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
                # Reading the body stays inside the try: the connection can
                # also drop mid-body, not just while the request is sent.
                return await read_response(resp)
        except aiohttp.ClientConnectionError as e:
            if attempt == attempts:
                raise
            logging.warning(f"{url}: connection error, retrying ({attempt}/{attempts}): {e}")
            await asyncio.sleep(backoff * attempt)
    raise AssertionError("unreachable")  # the loop either returns or raises


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    **kwargs: Any,
) -> Tuple[int, str]:
    """GET the URL and return (status, body text). See _get_with_retry."""

    async def _read(resp: aiohttp.ClientResponse) -> Tuple[int, str]:
        return resp.status, await resp.text()

    return await _get_with_retry(session, url, _read, attempts=attempts, backoff=backoff, **kwargs)


async def fetch_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    attempts: int = 3,
    backoff: float = 2.0,
    **kwargs: Any,
) -> Tuple[int, bytes, Optional[str]]:
    """GET the URL and return (status, raw body bytes, declared charset).

    For callers that must decode the body themselves — Goldgrube runs the raw
    bytes through decode_utf8_or_latin1() because the server sometimes
    mislabels UTF-8 responses as iso-8859-1 (resp.text() would trust the
    wrong label and produce mojibake). See _get_with_retry for retry rules.
    """

    async def _read(resp: aiohttp.ClientResponse) -> Tuple[int, bytes, Optional[str]]:
        return resp.status, await resp.read(), resp.charset

    return await _get_with_retry(session, url, _read, attempts=attempts, backoff=backoff, **kwargs)
