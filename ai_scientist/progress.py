"""Optional research progress hooks; no web dependencies."""
from typing import Callable


class Cancelled(RuntimeError):
    """The owning job requested cancellation."""


class PipelineFailure(RuntimeError):
    """A required pipeline stage did not produce its required output."""


def emit(on_event: Callable | None, type: str, phase: str | None = None, **data):
    if on_event is not None:
        on_event({"type": type, "phase": phase, "data": data})


def check_stop(should_stop: Callable | None):
    if should_stop is not None and should_stop():
        raise Cancelled("Stop requested")
