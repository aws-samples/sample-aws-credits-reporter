"""Test helper: make S3 ``LastModified`` agree with a simulated clock.

``moto`` stamps objects with the real current time, but several tests run the
orchestrator with an injected clock in the past. The snapshot store compares
S3's ``LastModified`` with the snapshot's ``taken_at``, so
this proxy rewrites ``LastModified`` on ``get_object`` to a few seconds after
the ``taken_at`` stored in the object, the way a real scheduled run would look.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone


class ClockedS3:
    """Wrap a boto3 S3 client; every other attribute passes through unchanged."""

    def __init__(self, client, write_delay: timedelta = timedelta(seconds=5)) -> None:
        self._client = client
        self._write_delay = write_delay

    def __getattr__(self, name):
        return getattr(self._client, name)

    def get_object(self, **kwargs):
        response = self._client.get_object(**kwargs)
        data = response["Body"].read()
        try:
            taken_at = datetime.fromisoformat(json.loads(data)["taken_at"])
            response["LastModified"] = taken_at.astimezone(timezone.utc) + self._write_delay
        except (ValueError, KeyError, TypeError):
            pass
        response["Body"] = io.BytesIO(data)
        return response
