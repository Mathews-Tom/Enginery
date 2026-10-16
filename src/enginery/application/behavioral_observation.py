"""Transport observations, deliberately separate from acceptance verdicts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ObservationData:
    """Actual bounded output from one observer; never an acceptance decision.

    ``returncode`` is the command exit code, or the HTTP status for a service.
    DOM results refer to assertion positions in the custody-owned check. Missing
    results mean not observed, not success. Captures contain original media bytes.
    Infrastructure errors must be classified as inconclusive by calibration.
    """

    observer: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    body: str = ""
    produced_files: tuple[tuple[str, str], ...] = ()
    dom_results: tuple[tuple[int, bool], ...] = ()
    captures: tuple[tuple[str, bytes], ...] = ()
    started_at: str = ""
    ended_at: str = ""
    infrastructure_error: str | None = None
    mock: bool = False
    ran: bool = True
    skipped: bool = False
