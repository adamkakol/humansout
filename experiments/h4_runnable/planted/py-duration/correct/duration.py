import re

_PATTERN = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_duration(text: str) -> int:
    if not isinstance(text, str):
        raise ValueError("duration must be a string")
    value = text.strip()
    match = _PATTERN.fullmatch(value)
    if not value or match is None or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds
