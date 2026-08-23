"""Helpers for keeping blocking I/O off the shared event loop.

araka runs one gunicorn worker with a single background asyncio loop serving
every user. The Google API client (`googleapiclient`) is synchronous, so a
call made directly inside an `async def` freezes that loop for the whole round
trip — typically 300-700 ms, and several seconds for a loop of deletes. While
it is frozen no other user's message advances, no reminder fires, and the
webhook ACK for anything arriving in that window is delayed too.

`@offloaded` marks a function whose body is synchronous but whose callers
await it: the body runs in a worker thread and the loop stays free.
"""

import asyncio
import functools


def offloaded(fn):
    """Run a synchronous function in a thread, exposing it as a coroutine.

    Use on service functions that only wrap blocking client libraries. The
    public signature is unchanged, so existing `await service.foo(...)` callers
    keep working — the body simply stops blocking the loop.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return wrapper
