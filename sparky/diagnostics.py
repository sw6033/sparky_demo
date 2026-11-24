"""Per-turn structured diagnostics. Never record credentials or model reasoning."""
from contextvars import ContextVar
from contextlib import contextmanager
import time

_current=ContextVar("sparky_trace",default=None)

def note(stage, **values):
    trace=_current.get()
    if trace is not None:
        trace.append({"stage":stage,**values})

@contextmanager
def capture():
    rows=[]
    token=_current.set(rows)
    try:
        yield rows
    finally:
        _current.reset(token)

def elapsed(start):
    return round((time.monotonic()-start)*1000,1)
