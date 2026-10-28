"""Admission control for expensive blocking jobs, including timed-out workers."""
from functools import wraps
import threading


_WORKERS = threading.BoundedSemaphore(2)
_LOCAL = threading.local()


def bounded_work(error_type):
    """Reject overload instead of queuing; nested work reuses its worker slot.

    The slot lives in the worker, not its awaiting coroutine: cancelling an
    asyncio.to_thread caller must not admit more work while its thread runs.
    """
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if getattr(_LOCAL, "active", False):
                return function(*args, **kwargs)
            if not _WORKERS.acquire(blocking=False):
                raise error_type("Сервис занят обработкой других запросов. Попробуйте позже.")
            _LOCAL.active = True
            try:
                return function(*args, **kwargs)
            finally:
                _LOCAL.active = False
                _WORKERS.release()
        return wrapped
    return decorate
