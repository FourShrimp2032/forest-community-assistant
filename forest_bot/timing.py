"""Per-request latency measurement (stage durations, LLM calls, retries, rate-limit waits).

A `RequestTiming` is bound to the current asyncio task with a context variable, so lower layers
(LLM client, retrieval) can record into it without passing it around. Only durations, model names and
counters are recorded — never message text, document content or credentials.
"""

from __future__ import annotations

import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

_current: ContextVar["RequestTiming | None"] = ContextVar("request_timing", default=None)


@dataclass
class RequestTiming:
    started: float = field(default_factory=time.monotonic)
    stages: dict[str, float] = field(default_factory=dict)
    llm_calls: list[tuple[str, float, str]] = field(default_factory=list)   # (model, seconds, outcome)
    waits: float = 0.0
    retries: int = 0
    status: str = ""
    evidence_docs: int | None = None
    budget: float | None = None          # seconds allowed for the whole request (None = unlimited)

    @property
    def deadline(self) -> float | None:
        return self.started + self.budget if self.budget else None

    def add_stage(self, name: str, seconds: float) -> None:
        self.stages[name] = self.stages.get(name, 0.0) + seconds

    @property
    def total(self) -> float:
        return time.monotonic() - self.started

    def summary(self) -> str:
        status = self.status
        stages = " ".join(f"{k}={v:.2f}s" for k, v in self.stages.items())
        calls = ", ".join(f"{m}:{s:.2f}s:{o}" for m, s, o in self.llm_calls) or "-"
        return (f"total={self.total:.2f}s {stages} llm_calls=[{calls}] retries={self.retries} "
                f"rate_limit_wait={self.waits:.2f}s evidence_docs={self.evidence_docs if self.evidence_docs is not None else '-'}"
                + (f" status={status}" if status else ""))


def current() -> RequestTiming | None:
    return _current.get()


def remaining() -> float:
    """Seconds left in the current request's time budget (infinite outside a budgeted request)."""
    t = current()
    if t is None or t.deadline is None:
        return float("inf")
    return t.deadline - time.monotonic()


@contextlib.contextmanager
def request(budget: float | None = None):
    timing = RequestTiming(budget=budget)
    token = _current.set(timing)
    try:
        yield timing
    finally:
        _current.reset(token)


@contextlib.contextmanager
def stage(name: str):
    started = time.monotonic()
    try:
        yield
    finally:
        t = current()
        if t is not None:
            t.add_stage(name, time.monotonic() - started)


def record_llm_call(model: str, seconds: float, outcome: str) -> None:
    t = current()
    if t is not None:
        t.llm_calls.append((model, seconds, outcome))


def record_wait(seconds: float) -> None:
    t = current()
    if t is not None:
        t.waits += seconds
        t.retries += 1
