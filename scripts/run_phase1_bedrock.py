#!/usr/bin/env -S uv run
"""Run the student-role Bedrock checks from Phase 1.

The general permission probe deliberately records only an authorization
verdict. Phase 1 also needs the complete model catalog and must consume a
Bedrock response stream, so those checks live in this focused runner.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from vocareum_tools.config import repository_root
from vocareum_tools.probe import (
    BOTO_CONFIG,
    ConfigError,
    GuardError,
    Verdict,
    build_session,
    classify,
    fetch_vocareum_credentials,
    guard_account,
    guard_credentials,
    guard_region,
    load_config,
    start_session,
)
from vocareum_tools.security import redact_text

CLAUDE_PROFILE_ID = "us.anthropic.claude-sonnet-4-20250514-v1:0"
CLAUDE_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
FALLBACK_CLAUDE_PROFILE_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
FALLBACK_CLAUDE_MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"


def json_default(value: Any) -> str:
    """Serialize the date values returned by botocore."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def call(action: str, operation: Callable[[], Any]) -> tuple[Verdict, Any | None, str]:
    """Run one AWS call and return a classified result."""
    try:
        response = operation()
    except ClientError as error:
        verdict, code, message = classify(error, action)
        detail = f"{code}: {redact_text(message)}"
        print(f"  {action}: {verdict} - {detail}")
        return verdict, None, detail
    except BotoCoreError as error:
        detail = redact_text(str(error))
        print(f"  {action}: UNCLASSIFIED - {detail}")
        return Verdict.UNCLASSIFIED, None, detail

    print(f"  {action}: OK")
    return Verdict.OK, response, "Call succeeded"


def consume_stream(response: dict[str, Any]) -> dict[str, Any]:
    """Consume every event so errors inside the response stream are observed."""
    chunks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for event in response["body"]:
        if "chunk" in event:
            chunks.append(json.loads(event["chunk"]["bytes"]))
        else:
            errors.append(event)
    if errors:
        raise RuntimeError(f"Bedrock stream returned error events: {errors}")
    return {"chunks": chunks}


def without_metadata(response: dict[str, Any]) -> dict[str, Any]:
    """Return a stable evidence payload without request-specific HTTP metadata."""
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run student-role Bedrock checks, Phase 1."
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Use the current lab session without starting or extending it.",
    )
    parser.add_argument("--region", help="Override AWS_REGION.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=repository_root() / ".env",
        help="Path to the repository environment file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=repository_root() / "docs" / "bedrock-foundation-models-us-east-1.json",
        help="Where to save the complete ListFoundationModels response.",
    )
    return parser.parse_args()


def main() -> int:
    """Guard the identity, run all student checks, and save the model catalog."""
    args = parse_args()
    try:
        config = load_config(args.env_file, args.region, None, True)
        if not args.no_start:
            start_session(config)
        credentials = fetch_vocareum_credentials(config, wait=not args.no_start)
        guard_credentials(credentials)
        session = build_session(credentials)
        guard_account(session, config.region, config.expected_account)
        if not guard_region(config.region):
            return 2
    except (ConfigError, GuardError) as error:
        print(f"REFUSED TO REPORT: {error}")
        return 2

    bedrock = session.client("bedrock", region_name=config.region, config=BOTO_CONFIG)
    runtime = session.client(
        "bedrock-runtime", region_name=config.region, config=BOTO_CONFIG
    )

    results: dict[str, dict[str, Any]] = {}

    verdict, catalog, detail = call(
        "bedrock:ListFoundationModels", bedrock.list_foundation_models
    )
    results["listFoundationModels"] = {
        "verdict": str(verdict),
        "detail": detail,
    }
    if catalog is not None:
        stable_catalog = without_metadata(catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(stable_catalog, indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
        model_ids = {item.get("modelId") for item in catalog.get("modelSummaries", [])}
        results["listFoundationModels"]["savedTo"] = str(args.output)
        results["listFoundationModels"]["modelCount"] = len(model_ids)
        results["listFoundationModels"]["requiredModels"] = {
            CLAUDE_MODEL_ID: CLAUDE_MODEL_ID in model_ids,
            NOVA_MODEL_ID: NOVA_MODEL_ID in model_ids,
        }

    for model_id in (CLAUDE_MODEL_ID, NOVA_MODEL_ID):
        verdict, response, detail = call(
            "bedrock:GetFoundationModel",
            lambda model_id=model_id: bedrock.get_foundation_model(
                modelIdentifier=model_id
            ),
        )
        results[f"getFoundationModel:{model_id}"] = {
            "verdict": str(verdict),
            "detail": detail,
            "modelDetails": (
                without_metadata(response)["modelDetails"] if response else None
            ),
        }

        verdict, response, detail = call(
            "bedrock:GetFoundationModelAvailability",
            lambda model_id=model_id: bedrock.get_foundation_model_availability(
                modelId=model_id
            ),
        )
        results[f"getFoundationModelAvailability:{model_id}"] = {
            "verdict": str(verdict),
            "detail": detail,
            "availability": without_metadata(response) if response else None,
        }

    profile_arn = (
        f"arn:aws:bedrock:{config.region}:{config.expected_account}:"
        f"inference-profile/{CLAUDE_PROFILE_ID}"
    )
    anthropic_body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1,
            "temperature": 0,
            "messages": [{"role": "user", "content": "Reply with OK."}],
        }
    )

    verdict, response, detail = call(
        "bedrock:InvokeModel",
        lambda: runtime.invoke_model(
            modelId=profile_arn,
            body=anthropic_body,
            contentType="application/json",
            accept="application/json",
        ),
    )
    results["invokeModel:claude"] = {
        "verdict": str(verdict),
        "detail": detail,
    }
    primary_invoke_ok = verdict is Verdict.OK
    if response is not None:
        payload = json.loads(response["body"].read())
        results["invokeModel:claude"]["stopReason"] = payload.get("stop_reason")
        results["invokeModel:claude"]["usage"] = payload.get("usage")

    verdict, response, detail = call(
        "bedrock:InvokeModelWithResponseStream",
        lambda: runtime.invoke_model_with_response_stream(
            modelId=profile_arn,
            body=anthropic_body,
            contentType="application/json",
            accept="application/json",
        ),
    )
    results["invokeModelWithResponseStream:claude"] = {
        "verdict": str(verdict),
        "detail": detail,
    }
    primary_stream_ok = verdict is Verdict.OK
    if response is not None:
        try:
            stream = consume_stream(response)
        except RuntimeError as error:
            results["invokeModelWithResponseStream:claude"]["verdict"] = str(
                Verdict.UNCLASSIFIED
            )
            results["invokeModelWithResponseStream:claude"]["detail"] = str(error)
        else:
            results["invokeModelWithResponseStream:claude"]["eventCount"] = len(
                stream["chunks"]
            )

    if not (primary_invoke_ok and primary_stream_ok):
        fallback_arn = (
            f"arn:aws:bedrock:{config.region}:{config.expected_account}:"
            f"inference-profile/{FALLBACK_CLAUDE_PROFILE_ID}"
        )
        verdict, response, detail = call(
            "bedrock:InvokeModel",
            lambda: runtime.invoke_model(
                modelId=fallback_arn,
                body=anthropic_body,
                contentType="application/json",
                accept="application/json",
            ),
        )
        results["invokeModel:claudeFallback"] = {
            "modelId": FALLBACK_CLAUDE_PROFILE_ID,
            "foundationModelId": FALLBACK_CLAUDE_MODEL_ID,
            "verdict": str(verdict),
            "detail": detail,
        }
        if response is not None:
            payload = json.loads(response["body"].read())
            results["invokeModel:claudeFallback"]["stopReason"] = payload.get(
                "stop_reason"
            )
            results["invokeModel:claudeFallback"]["usage"] = payload.get("usage")

        verdict, response, detail = call(
            "bedrock:InvokeModelWithResponseStream",
            lambda: runtime.invoke_model_with_response_stream(
                modelId=fallback_arn,
                body=anthropic_body,
                contentType="application/json",
                accept="application/json",
            ),
        )
        results["invokeModelWithResponseStream:claudeFallback"] = {
            "modelId": FALLBACK_CLAUDE_PROFILE_ID,
            "foundationModelId": FALLBACK_CLAUDE_MODEL_ID,
            "verdict": str(verdict),
            "detail": detail,
        }
        fallback_stream_ok = verdict is Verdict.OK
        if response is not None:
            try:
                stream = consume_stream(response)
            except RuntimeError as error:
                results["invokeModelWithResponseStream:claudeFallback"]["verdict"] = (
                    str(Verdict.UNCLASSIFIED)
                )
                results["invokeModelWithResponseStream:claudeFallback"]["detail"] = str(
                    error
                )
            else:
                results["invokeModelWithResponseStream:claudeFallback"][
                    "eventCount"
                ] = len(stream["chunks"])

        if not fallback_stream_ok:
            verdict, response, detail = call(
                "bedrock:InvokeModelWithResponseStream",
                lambda: runtime.invoke_model_with_response_stream(
                    modelId=FALLBACK_CLAUDE_PROFILE_ID,
                    body=anthropic_body,
                    contentType="application/json",
                    accept="application/json",
                ),
            )
            results["invokeModelWithResponseStream:claudeFallbackById"] = {
                "modelId": FALLBACK_CLAUDE_PROFILE_ID,
                "verdict": str(verdict),
                "detail": detail,
            }
            if response is not None:
                try:
                    stream = consume_stream(response)
                except RuntimeError as error:
                    results["invokeModelWithResponseStream:claudeFallbackById"][
                        "verdict"
                    ] = str(Verdict.UNCLASSIFIED)
                    results["invokeModelWithResponseStream:claudeFallbackById"][
                        "detail"
                    ] = str(error)
                else:
                    results["invokeModelWithResponseStream:claudeFallbackById"][
                        "eventCount"
                    ] = len(stream["chunks"])

    nova_body = json.dumps(
        {
            "taskType": "SINGLE_EMBEDDING",
            "singleEmbeddingParams": {
                "embeddingPurpose": "GENERIC_INDEX",
                "embeddingDimension": 1024,
                "text": {"truncationMode": "END", "value": "permission test"},
            },
        }
    )
    verdict, response, detail = call(
        "bedrock:InvokeModel",
        lambda: runtime.invoke_model(
            modelId=NOVA_MODEL_ID,
            body=nova_body,
            contentType="application/json",
            accept="application/json",
        ),
    )
    results["invokeModel:nova"] = {
        "verdict": str(verdict),
        "detail": detail,
    }
    if response is not None:
        payload = json.loads(response["body"].read())
        embeddings = payload.get("embeddings", [])
        vector = embeddings[0].get("embedding", []) if embeddings else []
        results["invokeModel:nova"]["embeddingCount"] = len(embeddings)
        results["invokeModel:nova"]["dimension"] = len(vector)

    print("\nPHASE 1 RESULTS")
    print(json.dumps(results, indent=2, default=json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
