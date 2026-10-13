"""URL shapes for the Vocareum REST API, shared by every command-line tool.

Held in one place because all three tools address the same resources and a
wrong path is answered with a bare HTTP 400 that names nothing, so a URL built
two slightly different ways in two files is an expensive kind of drift.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

DEFAULT_API_BASE = "https://api.vocareum.com/api/v2"


@dataclass(frozen=True)
class PartEndpoints:
    """The course, assignment, and part coordinates every tool addresses."""

    api_base: str
    course_id: str
    assignment_id: str
    part_id: str

    def course_url(self) -> str:
        """Return the course URL, which is the root of the roster collection."""
        return f"{self.api_base}/courses/{self.course_id}"

    def assignments_url(self, page: int | None = None) -> str:
        """Return the assignment collection URL, optionally for one page."""
        url = f"{self.course_url()}/assignments"
        if page is None:
            return url
        return f"{url}?{urllib.parse.urlencode({'page': page})}"

    def assignment_url(self) -> str:
        """Return the selected assignment URL."""
        return f"{self.assignments_url()}/{self.assignment_id}"

    def parts_url(self, page: int | None = None) -> str:
        """Return the selected assignment's parts URL."""
        url = f"{self.assignment_url()}/parts"
        if page is None:
            return url
        return f"{url}?{urllib.parse.urlencode({'page': page})}"

    def roster_url(
        self, page: int | None = None, *, include_dropped: bool = False
    ) -> str:
        """Return the course roster URL, optionally for one page.

        ``page`` is **zero-indexed**, which is the trap in this endpoint.
        Measured 2026-08-06 against a course holding one student: ``page=0``
        returns that student and ``page=1`` returns an empty ``users`` array
        while ``total_records`` still reads 1. So the off-by-one does not
        error, it silently reports an empty course, and a caller that starts
        at page 1 ends nobody's session and says the roster was empty.

        Omitting ``page`` returns the first page too, so the safe call is no
        parameter at all until ``total_records`` proves more rows exist.

        ``include_dropped`` defaults to false because that is what every prior
        measurement in ``docs/vocareum-api.md`` used. Measured 2026-08-27
        against a course after unenrolling its roster: the default view
        answered ``total_records: 0``, and ``include_dropped=true`` answered
        ``total_records: 113`` with ``dropped: true`` rows. A cohort tool that
        does not pass it reports an emptied course as already clear.
        """
        query: dict[str, str | int] = {}
        if page is not None:
            query["page"] = page
        if include_dropped:
            query["include_dropped"] = "true"
        if not query:
            return f"{self.course_url()}/users"
        return f"{self.course_url()}/users?{urllib.parse.urlencode(query)}"

    def part_url(self) -> str:
        """Return the part resource URL, which is also the upload endpoint."""
        return f"{self.parts_url()}/{self.part_id}"

    def files_url(self, query: dict[str, str]) -> str:
        """Return the files URL for a query mapping."""
        return f"{self.part_url()}/files?{urllib.parse.urlencode(query)}"

    def transaction_url(self, transaction_id: str) -> str:
        """Return the transaction status URL for a transaction id."""
        return f"{self.api_base}/transaction/{transaction_id}"


@dataclass(frozen=True)
class SessionEndpoints(PartEndpoints):
    """A part addressed for one student, which is where a lab session lives."""

    user_id: str

    def resource_url(self) -> str:
        """Return the session resource URL for this student.

        The user id is the last path segment, not a ``/users/{id}`` collection.
        Vocareum answers any other shape with a bare HTTP 400 "Invalid
        Request", which names nothing.
        """
        return f"{self.part_url()}/resources/{self.user_id}"
