"""Offline checks for the probe classifier, the status map, and the guards.

Kept in the shipped package rather than in ``tests/`` because ``--self-test``
has to run inside a lab environment, where the test suite is not installed and
no AWS call is allowed. The pytest suite imports the same case tables, so the
two cannot drift: a case added here is checked in both places.

Every case is data. ``run_self_test`` walks the tables and prints a line per
case, because the transcript is the artifact someone pastes into a ticket.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from .output import section
from .probe import (
    EXIT_OK,
    EXIT_SELF_TEST,
    TRACKER_STATUS,
    Credentials,
    GuardError,
    Probe,
    ProbeResult,
    Verdict,
    classify,
    guard_credentials,
    to_method_name,
)


def make_error(code: str, message: str) -> ClientError:
    """Build a ClientError shaped the way botocore builds one."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "ProbeSelfTest")


CLASSIFIER_CASES: tuple[tuple[str, str, Verdict], ...] = (
    (
        "AccessDeniedException",
        (
            "with an explicit deny in a resource control policy: "
            "arn:aws:organizations::1:policy/x"
        ),
        Verdict.RCP_DENY,
    ),
    (
        "AccessDeniedException",
        (
            "User: arn:aws:sts::1:assumed-role/voclabs/user1 is not authorized to "
            "perform: lambda:CreateFunction on resource: "
            "arn:aws:lambda:us-east-1:1:function:x"
        ),
        Verdict.IAM_DENY,
    ),
    (
        "AccessDeniedException",
        "You don't have access to the model with the specified model ID.",
        Verdict.MODEL_ACCESS_NOT_GRANTED,
    ),
    (
        "AccessDenied",
        "Something unfamiliar happened.",
        Verdict.ACCESS_DENIED_UNCLASSIFIED,
    ),
    (
        "ResourceNotFoundException",
        "Function not found: hotel-booking-probe",
        Verdict.ALLOWED_NOT_FOUND,
    ),
    (
        "NoSuchEntity",
        "The role with name workshop-probe cannot be found.",
        Verdict.ALLOWED_NOT_FOUND,
    ),
    (
        "ValidationException",
        "The provided model identifier is invalid.",
        Verdict.INCONCLUSIVE_BAD_INPUT,
    ),
    (
        "ThrottlingException",
        "Too many requests, please wait before trying again.",
        Verdict.THROTTLED,
    ),
    (
        "ServiceQuotaExceededException",
        "The account quota was exceeded.",
        Verdict.QUOTA_EXCEEDED,
    ),
    (
        "SubscriptionRequiredException",
        "The account is not subscribed.",
        Verdict.OPT_IN_REQUIRED,
    ),
    (
        "ExpiredTokenException",
        "The security token included in the request is expired.",
        Verdict.CREDENTIALS_BROKEN,
    ),
    ("SomeBrandNewCode", "Nobody has seen this before.", Verdict.UNCLASSIFIED),
)

# Cases where the same code and message mean different things depending on
# which action was probed, so the pair alone cannot decide the verdict.
PROBED_CLASSIFIER_CASES: tuple[tuple[str, str, str, Verdict], ...] = (
    (
        "bedrock:InvokeModel",
        "ResourceNotFoundException",
        "Could not resolve the foundation model from the provided model identifier.",
        Verdict.MODEL_NOT_IN_REGION,
    ),
    (
        "bedrock:InvokeModel",
        "ResourceNotFoundException",
        (
            "Access denied. This Model is marked by provider as Legacy and "
            "you have not been actively using the model in the last 30 days."
        ),
        Verdict.MODEL_LEGACY_INACTIVE,
    ),
    (
        "bedrock:InvokeModelWithResponseStream",
        "ResourceNotFoundException",
        (
            "Model use case details have not been submitted for this account. "
            "Fill out the Anthropic use case details form before using the model."
        ),
        Verdict.MODEL_USE_CASE_NOT_SUBMITTED,
    ),
    (
        "lambda:GetFunction",
        "ResourceNotFoundException",
        "Function not found: hotel-booking-probe",
        Verdict.ALLOWED_NOT_FOUND,
    ),
    # bedrock-agentcore shares a prefix with bedrock and holds no models, so
    # matching the bare prefix turned every absent gateway, runtime, and memory
    # into a model missing from the Region. Measured live in Phase 3: probing
    # row 63 against a gateway that does not exist returned MODEL_NOT_IN_REGION
    # and told the operator to move the lab to us-east-1, from inside us-east-1.
    (
        "bedrock-agentcore:ListGatewayTargets",
        "ResourceNotFoundException",
        (
            "Failed to retrieve gateway because it doesn't exist. Retry the "
            "request with a different resource identifier."
        ),
        Verdict.ALLOWED_NOT_FOUND,
    ),
    (
        "bedrock-agentcore:GetMemory",
        "ResourceNotFoundException",
        "Memory not found: probe-nonexistent",
        Verdict.ALLOWED_NOT_FOUND,
    ),
    # A Service Control Policy usually names a wildcard rather than the single
    # action it happened to stop. The refused action then never equals the
    # probed one, and reading that as a dependency filed a real organization
    # ceiling as Untested and threw the policy ARN away with it.
    (
        "bedrock:InvokeModel",
        "AccessDeniedException",
        (
            "User: arn:aws:sts::1:assumed-role/voclabs/user1 is not authorized "
            "to perform: bedrock:* on resource: "
            "arn:aws:bedrock:us-east-1::foundation-model/x with an explicit "
            "deny in a service control policy: "
            "arn:aws:organizations::2:policy/o-example/"
            "service_control_policy/p-example"
        ),
        Verdict.SCP_DENY,
    ),
)


def dependency_message(named: str) -> str:
    """Return an IAM denial message naming ``named`` as the refused action."""
    return (
        "User: arn:aws:sts::1:assumed-role/voclabs/user1 is not authorized "
        f"to perform: {named} on resource: arn:aws:x:::y because no "
        f"identity-based policy allows the {named} action"
    )


# A denial that names a different action than the one being probed measures
# the dependency, not the probe. Attributing a dependency's denial to the
# probed action would record a permission result that was never taken.
DEPENDENCY_CASES: tuple[tuple[str, str, Verdict], ...] = (
    ("dynamodb:CreateTable", "kms:CreateGrant", Verdict.DEPENDENCY_DENIED),
    ("ssm:GetParameters", "ssm:GetParameters", Verdict.IAM_DENY),
    ("lambda:CreateFunction", "iam:PassRole", Verdict.DEPENDENCY_DENIED),
    # A wildcard covers the probed action, so the denial is this probe's answer
    # however little it looks like the action that was asked for.
    ("bedrock:InvokeModel", "bedrock:*", Verdict.IAM_DENY),
    ("bedrock:InvokeModel", "bedrock:Invoke*", Verdict.IAM_DENY),
)

# Verdict to tracker Status, checked so the two tables cannot drift apart.
STATUS_CASES: tuple[tuple[Verdict, str], ...] = (
    (Verdict.SCP_DENY, "Denied (SCP)"),
    (Verdict.IAM_DENY, "Denied (IAM)"),
    (Verdict.ALLOWED_NOT_FOUND, "Allowed"),
    (Verdict.MODEL_ACCESS_NOT_GRANTED, "Allowed"),
    (Verdict.MODEL_LEGACY_INACTIVE, "Allowed"),
    (Verdict.MODEL_USE_CASE_NOT_SUBMITTED, "Allowed"),
    (Verdict.INCONCLUSIVE_BAD_INPUT, "Untested"),
    (Verdict.UNCLASSIFIED, "Untested"),
)

METHOD_NAME_CASES: tuple[tuple[str, str], ...] = (
    ("GetCallerIdentity", "get_caller_identity"),
    ("ListFoundationModels", "list_foundation_models"),
    ("InvokeModelWithResponseStream", "invoke_model_with_response_stream"),
    ("RunInstances", "run_instances"),
    ("DescribeVpcs", "describe_vpcs"),
    ("PutBucketPublicAccessBlock", "put_bucket_public_access_block"),
    ("BatchGetImage", "batch_get_image"),
    ("CreateAgentRuntimeEndpoint", "create_agent_runtime_endpoint"),
)

# Every field that a terminated Vocareum session is known to blank out.
GUARD_CASES: tuple[tuple[str, Credentials], ...] = (
    ("all three empty", Credentials("", "", "")),
    ("empty token only", Credentials("ASIAEXAMPLE", "secret", "")),
    ("whitespace token", Credentials("ASIAEXAMPLE", "secret", "   ")),
    ("empty secret only", Credentials("ASIAEXAMPLE", "", "token")),
    ("empty access key only", Credentials("", "secret", "token")),
)

COMPLETE_CREDENTIALS = Credentials("ASIAEXAMPLE", "secret", "token")

SAFETY_CASES: tuple[tuple[Probe, bool], ...] = (
    (Probe("bedrock:ListFoundationModels"), True),
    (Probe("lambda:GetFunction", {"FunctionName": "x"}), True),
    (Probe("lambda:CreateFunction", {"FunctionName": "x"}), False),
    (Probe("dynamodb:CreateTable"), False),
)

# Verdicts no case above can produce because nothing in classify() emits them.
# They are raised by the harness around it instead, so they are exempt from the
# reachability check rather than missing from it.
VERDICTS_OUTSIDE_CLASSIFY = frozenset(
    {Verdict.OK, Verdict.ENDPOINT_UNREACHABLE, Verdict.UNKNOWN_ACTION}
)


def unreachable_verdicts() -> list[Verdict]:
    """Return every verdict that no classifier case and no guard can produce.

    A verdict nothing can emit is a lie in the vocabulary: it reads as a case
    that gets handled when in fact that outcome would fall through to
    ``UNCLASSIFIED``.
    """
    produced = {expected for _, _, expected in CLASSIFIER_CASES}
    produced |= {expected for _, _, _, expected in PROBED_CLASSIFIER_CASES}
    produced |= {expected for _, _, expected in DEPENDENCY_CASES}
    produced |= VERDICTS_OUTSIDE_CLASSIFY
    return [verdict for verdict in Verdict if verdict not in produced]


def verdicts_without_status() -> list[Verdict]:
    """Return every verdict the tracker status map does not cover."""
    return [verdict for verdict in Verdict if verdict not in TRACKER_STATUS]


def run_self_test() -> int:
    """Check the classifier, the status map, and the guards without any AWS call."""
    failures: list[str] = []

    section("SELF-TEST 1: error classification")
    for code, message, expected in CLASSIFIER_CASES:
        verdict, _, _ = classify(make_error(code, message))
        ok = verdict is expected
        print(f"  {'PASS' if ok else 'FAIL'}  {code:32s} -> {verdict}")
        if not ok:
            failures.append(f"{code} classified as {verdict}, expected {expected}")

    # A few verdicts depend on which action was probed, so they cannot be
    # checked by code and message alone.
    for probed, code, message, expected in PROBED_CLASSIFIER_CASES:
        verdict, _, _ = classify(make_error(code, message), probed)
        ok = verdict is expected
        print(f"  {'PASS' if ok else 'FAIL'}  {code:32s} on {probed:26s} -> {verdict}")
        if not ok:
            failures.append(f"{code} on {probed} gave {verdict}, expected {expected}")

    section("SELF-TEST 2: a denial for a dependency is not an answer")
    for probed, named, expected in DEPENDENCY_CASES:
        error = make_error("AccessDeniedException", dependency_message(named))
        verdict, _, _ = classify(error, probed)
        ok = verdict is expected
        print(
            f"  {'PASS' if ok else 'FAIL'}  probed {probed:22s} "
            f"denial named {named:20s} -> {verdict}"
        )
        if not ok:
            failures.append(
                f"{probed} with {named} denial gave {verdict}, expected {expected}"
            )

    section("SELF-TEST 3: verdict to tracker status")
    for verdict, expected_status in STATUS_CASES:
        actual = ProbeResult("x", verdict, "").status
        ok = actual == expected_status
        print(f"  {'PASS' if ok else 'FAIL'}  {verdict:28s} -> {actual}")
        if not ok:
            failures.append(f"{verdict} mapped to {actual}, expected {expected_status}")

    missing_status = verdicts_without_status()
    if missing_status:
        print(f"  FAIL  verdicts with no tracker status: {missing_status}")
        failures.append(f"verdicts missing from TRACKER_STATUS: {missing_status}")
    else:
        print(f"  PASS  every one of {len(list(Verdict))} verdicts has a status")

    unreachable = unreachable_verdicts()
    if unreachable:
        print(f"  FAIL  verdicts nothing can emit: {unreachable}")
        failures.append(f"unreachable verdicts: {unreachable}")
    else:
        print("  PASS  every verdict is reachable")

    section("SELF-TEST 4: IAM action to boto3 method name")
    for operation, expected_name in METHOD_NAME_CASES:
        actual = to_method_name(operation)
        ok = actual == expected_name
        print(f"  {'PASS' if ok else 'FAIL'}  {operation:34s} -> {actual}")
        if not ok:
            failures.append(f"{operation} became {actual}, expected {expected_name}")

    section("SELF-TEST 5: credential guard refuses blank fields")
    for label, credentials in GUARD_CASES:
        try:
            guard_credentials(credentials)
        except GuardError:
            print(f"  PASS  {label:24s} refused, nothing reported")
        else:
            print(f"  FAIL  {label:24s} was accepted")
            failures.append(f"credential guard accepted {label}")

    try:
        guard_credentials(COMPLETE_CREDENTIALS)
    except GuardError:
        print("  FAIL  a complete triple was refused")
        failures.append("credential guard refused a complete triple")
    else:
        print("  PASS  a complete triple was accepted")

    section("SELF-TEST 6: probe safety classification")
    for probe, expected_safe in SAFETY_CASES:
        actual_safe = probe.is_safe()
        ok = actual_safe is expected_safe
        print(f"  {'PASS' if ok else 'FAIL'}  {probe.action:34s} safe={actual_safe}")
        if not ok:
            failures.append(
                f"{probe.action} safety was {actual_safe}, expected {expected_safe}"
            )

    section("SELF-TEST RESULT")
    if failures:
        print(f"{len(failures)} failure(s):")
        for item in failures:
            print(f"  - {item}")
        return EXIT_SELF_TEST
    print("All self-tests passed. No AWS call was made.")
    return EXIT_OK
