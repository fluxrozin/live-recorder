from __future__ import annotations

import re

_SECRET_QUERY = re.compile(
    r"(?i)(sign|sig|signature|token|expire|api[_-]?key|sessionid|access_token)=([^&\s]+)"
)
_URL = re.compile(r"https?://\S+")
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-+/=]+")


def redact(text: str) -> str:
    cleaned = _BEARER.sub("Bearer ***", text)
    cleaned = _SECRET_QUERY.sub(r"\1=***", cleaned)
    cleaned = _URL.sub("[url]", cleaned)
    return cleaned
