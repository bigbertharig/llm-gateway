"""Validation of user-provided plan inputs."""

import re
from typing import Any

CONFIG_KEY_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
GITHUB_URL_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
INLINE_FILE_KEYS = ("QUERY_FILE", "REQUEST_FILE", "INPUT_FILE", "PROMPT_FILE", "CLAIM_FILE")


def sanitize_text(value: Any, *, max_len: int = 12000, single_line: bool = False) -> str:
    """Sanitize text by removing control characters and optionally collapsing whitespace."""
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_CHAR_RE.sub("", text)
    text = text[:max_len]
    if single_line:
        text = " ".join(text.split())
    return text.strip()


def sanitize_config_value(value: Any) -> Any:
    """Sanitize a single config value (must be scalar JSON type)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_text(value, max_len=4000, single_line=False)
    raise ValueError("config values must be scalar JSON types (string/number/bool/null)")


def sanitize_config_object(raw_cfg: dict[str, Any]) -> dict[str, Any]:
    """Sanitize entire config object, validating keys and values."""
    if len(raw_cfg) > 120:
        raise ValueError("too many config keys (max 120)")
    out: dict[str, Any] = {}
    for k, v in raw_cfg.items():
        if not isinstance(k, str):
            raise ValueError("config keys must be strings")
        key = k.strip().upper()
        if not CONFIG_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid config key: {k!r}")
        out[key] = sanitize_config_value(v)
    return out


def normalize_github_url(url: str) -> str:
    """Normalize GitHub URL to canonical form."""
    normalized = sanitize_text(url, max_len=300, single_line=True)
    m = GITHUB_URL_RE.match(normalized)
    if not m:
        raise ValueError("repo URL must be https://github.com/<owner>/<repo>")
    owner, repo = m.group(1), m.group(2)
    return f"https://github.com/{owner}/{repo}"
