"""Shared AWS constants, and parsing for the free-text messages a denial carries."""

from __future__ import annotations

import re

# Where the lab runs unless something says otherwise. Shared so the diagnostic
# and the probe cannot disagree about which account region they are describing.
DEFAULT_REGION = "us-east-1"

# AWS names the refused action in the body of the message rather than in any
# structured field. Reading it back is how a denial that belongs to a
# dependency gets told apart from a denial for the action being measured, and
# how the diagnostic finds the action to quote on a support ticket. The action
# half admits ``*`` because a policy denial can name a wildcard.
DENIED_ACTION_PATTERN = re.compile(
    r"not authorized to perform:?\s*([A-Za-z0-9-]+):([A-Za-z0-9_*]+)"
)


def denied_action(message: str) -> str | None:
    """Return the IAM action named in a denial message, when there is one."""
    match = DENIED_ACTION_PATTERN.search(message)
    if match is None:
        return None
    return f"{match.group(1)}:{match.group(2)}"


def names_other_action(message: str, probed_action: str) -> bool:
    """Report whether a denial names a single action other than the one probed.

    A wildcard is not a different action. A policy that refuses ``bedrock:*``
    refuses the action being measured along with everything else, so the denial
    does belong to this measurement and has to be classified as a denial rather
    than set aside as somebody else's. Treating one as a dependency filed a real
    Service Control Policy block as ``Untested`` and dropped the policy ARN with
    it, which is the one outcome the verdicts exist to prevent.
    """
    named = denied_action(message)
    if named is None or not probed_action or "*" in named:
        return False
    return named.lower() != probed_action.lower()
