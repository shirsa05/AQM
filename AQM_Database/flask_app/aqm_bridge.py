"""
aqm_bridge.py — Compatibility helpers for calling async AQM code from Flask.

Flask is sync by default. This module provides thread-safe wrappers so app.py
can call any async AQM coroutine without managing event loops manually.
"""

import asyncio
import threading
from functools import wraps

# One persistent event loop running in a daemon thread
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            t = threading.Thread(target=_loop.run_forever, daemon=True)
            t.start()
        return _loop


def run_async(coro):
    """
    Run an async coroutine from synchronous Flask code.

    Usage:
        result = run_async(some_async_fn(arg1, arg2))
    """
    loop = _get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=15)


def async_route(f):
    """
    Decorator: make an async Flask route function work transparently.

    Usage:
        @app.route("/api/foo")
        @async_route
        async def foo():
            result = await some_async_call()
            return jsonify(result)
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        return run_async(f(*args, **kwargs))
    return wrapper
