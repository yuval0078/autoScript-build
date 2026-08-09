import base64
import binascii
import json
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status


INVALID_CURSOR_DETAIL = "Invalid pagination cursor."


def encode_cursor(created_at: datetime, item_id: uuid.UUID) -> str:
    """Encode the stable sort key without exposing it as API structure."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)
    payload = json.dumps(
        {
            "v": 1,
            "created_at": created_at.isoformat(),
            "id": str(item_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode and strictly validate a cursor supplied by an API client."""
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("Unsupported cursor version.")
        if set(payload) != {"v", "created_at", "id"}:
            raise ValueError("Unexpected cursor fields.")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("Cursor timestamp must include a timezone.")
        item_id = uuid.UUID(payload["id"])
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=INVALID_CURSOR_DETAIL,
        ) from exc
    return created_at.astimezone(timezone.utc), item_id


def escaped_contains_pattern(value: str) -> str:
    """Return an ILIKE substring pattern with wildcard characters made literal."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
