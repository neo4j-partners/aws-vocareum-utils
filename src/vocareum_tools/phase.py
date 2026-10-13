"""Shared scaffolding for the phase runners in `scripts/`.

Every phase runner does the same four things around its probes: classify one
AWS call and record it against a tracker row, poll a resource until it settles,
register teardown for what it created, and run that teardown whether the block
finished or not. Only the probes themselves are per-phase.

That scaffolding was copy-pasted into each runner and then drifted. Three
versions of `Recorder` existed at once, and the newest of them had lost
`call_in_teardown`, the method whose whole purpose is to stop a failed delete
being reported as a clean account. A fix made in one runner did not reach the
others and nothing would have said so. This module is the single copy, so the
next fix lands everywhere and can be tested once.

Nothing here talks to AWS. It takes callables and classifies what they raise,
which is what makes it testable without a lab session.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from vocareum_tools.probe import TRACKER_STATUS, Verdict, classify
from vocareum_tools.security import redact_text

# The two spellings of "there is nothing here" that every service the phase
# runners wait on uses. Services outside that set spell it differently and pass
# their own to wait_until_gone; see its docstring.
GONE_CODES = frozenset({"ResourceNotFoundException", "ResourceNotFound"})


def json_default(value: Any) -> str:
    """Serialize the date values botocore returns."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


@dataclass
class Measurement:
    """One tracker row's measured outcome."""

    row: str
    action: str
    verdict: Verdict
    detail: str

    @property
    def status(self) -> str:
        """Return the permission tracker Status value for this measurement."""
        return TRACKER_STATUS.get(self.verdict, "Untested")

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-ready mapping."""
        return {
            "row": self.row,
            "action": self.action,
            "verdict": str(self.verdict),
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class Recorder:
    """Collect measurements and the teardown owed for what was created.

    `action_width` is the console column the action name is padded to. It is a
    field only so each runner keeps the output width its saved logs in
    `support/` were written at; nothing about a measurement depends on it.
    """

    measurements: list[Measurement] = field(default_factory=list)
    cleanups: list[tuple[str, Callable[[], None]]] = field(default_factory=list)
    action_width: int = 48

    def call(
        self,
        row: str,
        action: str,
        operation: Callable[[], Any],
        context: str = "",
    ) -> tuple[Verdict, Any | None]:
        """Run one AWS call, classify it, and record it against a tracker row.

        `context` names the resource the call was made against. A row measured
        once needs no such note, but a row measured once per resource the phase
        creates is indistinguishable in the output without it, and a denial on
        one resource and not another is exactly the kind of thing worth being
        able to see.
        """
        suffix = f" ({context})" if context else ""
        try:
            response = operation()
        except ClientError as error:
            verdict, code, message = classify(error, action)
            detail = f"{code}: {redact_text(message)}{suffix}"
        except ParamValidationError as error:
            # A request the SDK refused to send never reached authorization.
            verdict = Verdict.INCONCLUSIVE_BAD_INPUT
            detail = redact_text(str(error)) + suffix
        except BotoCoreError as error:
            verdict = Verdict.UNCLASSIFIED
            detail = redact_text(str(error)) + suffix
        else:
            verdict, detail = Verdict.OK, f"Call succeeded{suffix}"
            self._record(row, action, verdict, detail)
            return verdict, response

        self._record(row, action, verdict, detail)
        return verdict, None

    def call_in_teardown(
        self, row: str, action: str, operation: Callable[[], Any], context: str
    ) -> None:
        """Record a teardown call as a measurement, then let a failure through.

        Teardown deletes are measurements, so they belong in the record. But
        `call` absorbs the error in order to return a verdict, and a teardown
        that absorbs a failed delete reports a clean account while the resource
        is still in it. This records the verdict and re-raises, so
        `run_cleanups` still reports what was left behind.
        """
        try:
            operation()
        except ClientError as error:
            verdict, code, message = classify(error, action)
            self._record(
                row, action, verdict, f"{code}: {redact_text(message)} ({context})"
            )
            raise
        self._record(row, action, Verdict.OK, f"Call succeeded ({context})")

    def note(self, row: str, action: str, verdict: Verdict, detail: str) -> None:
        """Record a measurement that did not come from a single boto3 call."""
        self._record(row, action, verdict, detail)

    def _record(self, row: str, action: str, verdict: Verdict, detail: str) -> None:
        """Store and print one measurement."""
        measurement = Measurement(row, action, verdict, detail)
        self.measurements.append(measurement)
        marker = "ok " if verdict is Verdict.OK else "   "
        print(f"  {marker}row {row:<6} {action:<{self.action_width}} {verdict}")
        if verdict is not Verdict.OK:
            print(f"        {detail[:300]}")

    def defer(self, label: str, cleanup: Callable[[], None]) -> None:
        """Register teardown for something just created."""
        self.cleanups.append((label, cleanup))

    def run_cleanups(self) -> None:
        """Tear down in reverse order, never letting one failure stop the rest."""
        if not self.cleanups:
            return
        print("\n--- teardown ---")
        for label, cleanup in reversed(self.cleanups):
            try:
                cleanup()
            except (ClientError, BotoCoreError) as error:
                print(f"  LEFT BEHIND {label}: {redact_text(str(error))[:200]}")
            else:
                print(f"  removed {label}")
        self.cleanups.clear()


def wait_until(
    describe: Callable[[], str],
    done: set[str],
    failed: set[str],
    label: str,
    timeout: int = 300,
    interval: int = 10,
) -> str:
    """Poll a status until it settles, and report the state it settled in."""
    deadline = time.monotonic() + timeout
    state = ""
    while time.monotonic() < deadline:
        try:
            state = describe()
        except ClientError as error:
            return f"describe failed: {error.response['Error']['Code']}"
        if state in done or state in failed:
            print(f"        {label} settled in {state}")
            return state
        time.sleep(interval)
    print(f"        {label} still {state} after {timeout}s")
    return state


def wait_until_gone(
    describe: Callable[[], Any],
    label: str,
    timeout: int = 120,
    interval: int = 5,
    gone_codes: Collection[str] | None = None,
) -> bool:
    """Poll until a describe reports the resource missing, and say whether it went.

    A mutating call that returns is not a mutating call that finished. Phase 3
    met that shape four times and Phase 2.5 met it a fifth, and each time a run
    that stopped at the call would have claimed a clean account while the
    resource was still in it. It costs one describe per deleted resource.

    ``gone_codes`` exists because "there is nothing here" is not one string.
    The phase runners only ever waited on services that spell it
    ``ResourceNotFoundException``, so that pair was hard-coded. ECR says
    ``RepositoryNotFoundException`` and IAM says ``NoSuchEntity``, and against
    those the hard-coded set read a successful delete as a describe failure and
    reported the resource as a survivor. The default is unchanged, so a caller
    that never passes this keeps the behaviour its logs were written under.
    """
    codes = GONE_CODES if gone_codes is None else frozenset(gone_codes)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            describe()
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in codes:
                return True
            print(f"        describe while deleting {label}: {code}")
            return False
        time.sleep(interval)
    print(f"        {label} still present after {timeout}s")
    return False


@contextmanager
def teardown_guaranteed(recorder: Recorder) -> Iterator[None]:
    """Run teardown whether the block finished, failed, or was interrupted."""
    try:
        yield
    finally:
        recorder.run_cleanups()
