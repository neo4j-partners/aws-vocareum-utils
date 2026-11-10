#!/usr/bin/env -S uv run
"""Repeat the Bedrock calls in notebook 1.0 against the open lab session.

A student whose ``1.0_verify_environment.ipynb`` ends in ``Bedrock access is not
ready`` has either run it before the lab stack finished, or been allocated an
account Bedrock refuses (Error 002). The notebook cannot tell those apart from
the student's side. This makes the same calls from here, read-only, so one run
says which it is. The model ids are the notebook's, not ``poolscan``'s, because
the question is whether the notebook will pass.

It reads an open session and never starts one, so it costs no launch.

    ./scripts/check_bedrock.py --course graphrag

Exit codes:
    0  Every call returned.
    1  Configuration error.
    2  A guard refused to report, or credentials could not be obtained.
    3  A call failed. The error code, message, and RequestId are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from vocareum_tools.config import add_course_argument, repository_root
from vocareum_tools.probe import (
    BOTO_CONFIG,
    ConfigError,
    GuardError,
    load_config,
    open_guarded_session,
)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_GUARD = 2
EXIT_FAILED = 3

SONNET_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
NOVA_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"
EMBEDDING_DIMENSIONS = 1024
MESSAGES = [{"role": "user", "content": [{"text": "Reply with one word: ready"}]}]
INFERENCE_CONFIG = {"maxTokens": 32}
NOVA_REQUEST = {
    "taskType": "SINGLE_EMBEDDING",
    "singleEmbeddingParams": {
        "embeddingPurpose": "GENERIC_INDEX",
        "embeddingDimension": EMBEDDING_DIMENSIONS,
        "text": {"truncationMode": "END", "value": "access check"},
    },
}


def invoke_sonnet(runtime: Any) -> str:
    response = runtime.converse(
        modelId=SONNET_MODEL_ID, messages=MESSAGES, inferenceConfig=INFERENCE_CONFIG
    )
    blocks = response["output"]["message"]["content"]
    return f"answered {''.join(b.get('text', '') for b in blocks).strip()!r}"


def stream_sonnet(runtime: Any) -> str:
    """Consume the stream, because opening it and receiving text are separate steps.

    ``converse_stream`` can return a stream object and still deliver no text, so a
    call that did not raise is not yet a pass.
    """
    response = runtime.converse_stream(
        modelId=SONNET_MODEL_ID, messages=MESSAGES, inferenceConfig=INFERENCE_CONFIG
    )
    pieces = []
    for event in response["stream"]:
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if delta.get("text"):
            pieces.append(delta["text"])
    text = "".join(pieces).strip()
    if not text:
        raise ValueError("streaming Sonnet returned no text")
    return f"answered {text[:40]!r}"


def invoke_nova(runtime: Any) -> str:
    response = runtime.invoke_model(
        modelId=NOVA_MODEL_ID,
        body=json.dumps(NOVA_REQUEST),
        contentType="application/json",
        accept="application/json",
    )
    embeddings = json.loads(response["body"].read()).get("embeddings", [])
    vector = embeddings[0].get("embedding", []) if embeddings else []
    return f"returned a {len(vector)}-dimension vector"


def attempt(label: str, call: Callable[[], str]) -> bool:
    """Run one call and print what AWS said, including the RequestId."""
    try:
        print(f"PASS {label}: {call()}")
    except ClientError as error:
        info = error.response.get("Error", {})
        meta = error.response.get("ResponseMetadata", {})
        print(f"FAIL {label}: {info.get('Code')}: {info.get('Message')}")
        print(f"  HTTP {meta.get('HTTPStatusCode')} RequestId {meta.get('RequestId')}")
        return False
    except (BotoCoreError, KeyError, ValueError) as error:
        print(f"FAIL {label}: {type(error).__name__}: {error}")
        return False
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", type=Path, default=repository_root() / ".env")
    parser.add_argument("--region", default=None)
    add_course_argument(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(
            args.env_file.expanduser(),
            args.region,
            None,
            need_vocareum=True,
            course=args.course,
        )
        session = open_guarded_session(
            config,
            source="vocareum",
            no_start=True,
            account_named=False,
            ambient_refusal="Ambient credentials are not used by this check.",
        )
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except GuardError as error:
        print(error, file=sys.stderr)
        return EXIT_GUARD

    runtime = session.client(
        "bedrock-runtime", region_name=config.region, config=BOTO_CONFIG
    )
    results = [
        attempt("Sonnet 4.6 InvokeModel", lambda: invoke_sonnet(runtime)),
        attempt("Sonnet 4.6 streaming", lambda: stream_sonnet(runtime)),
        attempt("Nova multimodal embeddings", lambda: invoke_nova(runtime)),
    ]
    return EXIT_OK if all(results) else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
