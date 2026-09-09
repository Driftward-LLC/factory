from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any


class PublishError(Exception):
    """Raised when the Hatchet event publisher fails."""


def publish_hatchet_event(
    event_name: str,
    payload: dict[str, Any],
    *,
    command: str | None = None,
    timeout: int = 30,
) -> str | None:
    command = command or os.environ.get("HATCHET_EVENT_PUBLISHER_COMMAND")
    if not command:
        raise PublishError("HATCHET_EVENT_PUBLISHER_COMMAND is required")

    request = {
        "eventName": event_name,
        "payload": payload,
        "metadata": {
            "source": "hatchet-event-ingress",
            "event_key": str(payload.get("eventKey") or ""),
        },
    }
    try:
        result = subprocess.run(
            shlex.split(command),
            input=json.dumps(request, sort_keys=True),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublishError(f"publisher unavailable: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise PublishError(result.stderr.strip() or result.stdout.strip() or "publisher failed")

    output_line = ""
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("{"):
            output_line = line.strip()
            break
    if not output_line:
        raise PublishError(f"publisher returned no JSON: {result.stdout.strip()}")

    try:
        output = json.loads(output_line)
    except json.JSONDecodeError as exc:
        raise PublishError(f"publisher returned invalid JSON: {output_line}") from exc
    event_id = output.get("eventId")
    return str(event_id) if event_id else None
