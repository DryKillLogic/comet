import asyncio
import atexit
import multiprocessing
import signal
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import TypeVar

from comet.core.logger import logger
from comet.core.models import settings

T = TypeVar("T")

FOREGROUND_EXECUTOR = "foreground"
BACKGROUND_EXECUTOR = "background"

_mp_context = None
try:
    _mp_context = multiprocessing.get_context("forkserver")
except ValueError:
    _mp_context = multiprocessing.get_context("spawn")

max_workers = settings.EXECUTOR_MAX_WORKERS
_single_pool_mode = max_workers == 1
_executors = {
    FOREGROUND_EXECUTOR: None,
    BACKGROUND_EXECUTOR: None,
}
_executor_lock = threading.Lock()
# if max_workers is None:
#     cpu_count = os.cpu_count() or 1
#     max_workers = min(cpu_count, 4)


def worker_initializer():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _create_executor():
    return ProcessPoolExecutor(
        max_workers=max_workers, mp_context=_mp_context, initializer=worker_initializer
    )


def _normalize_role(role: str) -> str:
    if _single_pool_mode and role == BACKGROUND_EXECUTOR:
        return FOREGROUND_EXECUTOR
    return role


def _setup_named_executor(role: str, force: bool = False):
    role = _normalize_role(role)

    with _executor_lock:
        executor = _executors.get(role)
        if executor is not None and not force:
            return executor

        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

        executor = _create_executor()
        _executors[role] = executor
        return executor


def setup_executor(role: str | None = None, force: bool = False):
    if role is None:
        return _setup_named_executor(FOREGROUND_EXECUTOR, force=force)

    return _setup_named_executor(role, force=force)


def _shutdown_named_executor(role: str, wait: bool = True):
    role = _normalize_role(role)

    with _executor_lock:
        executor = _executors.get(role)
        _executors[role] = None

    if executor:
        executor.shutdown(wait=wait, cancel_futures=True)


def shutdown_executor(role: str | None = None, wait: bool = True):
    if role is None:
        _shutdown_named_executor(FOREGROUND_EXECUTOR, wait=wait)
        if not _single_pool_mode:
            _shutdown_named_executor(BACKGROUND_EXECUTOR, wait=wait)
        return

    _shutdown_named_executor(role, wait=wait)


atexit.register(shutdown_executor)


def get_executor(role: str = FOREGROUND_EXECUTOR):
    role = _normalize_role(role)
    return _executors.get(role)


async def run_in_process_executor(
    func: Callable[..., T], *args, role: str = FOREGROUND_EXECUTOR
) -> T:
    role = _normalize_role(role)
    loop = asyncio.get_running_loop()
    task = partial(func, *args)

    try:
        return await loop.run_in_executor(setup_executor(role), task)
    except BrokenProcessPool:
        broken_reason = getattr(get_executor(role), "_broken", None) or "unknown"
        func_name = getattr(func, "__name__", repr(func))
        logger.warning(
            f"{role.capitalize()} process pool became unusable while running {func_name}; recreating executor ({broken_reason})."
        )
        return await loop.run_in_executor(setup_executor(role, force=True), task)
