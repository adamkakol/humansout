import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int:
    if not isinstance(text, str):
        raise ValueError("duration must be a string")
    value = text.strip()
    parts = re.findall(r"(\d+)([hms])", value)
    if not parts or "".join(n + u for n, u in parts) != value:
        raise ValueError(f"invalid duration: {text!r}")
    return sum(int(n) * _UNITS[u] for n, u in parts)
