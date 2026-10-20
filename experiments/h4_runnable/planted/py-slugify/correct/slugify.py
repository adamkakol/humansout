import unicodedata

_EXTRA = {"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ß": "ss"}


def slugify(text: str) -> str:
    out, gap = [], False
    for ch in unicodedata.normalize("NFKD", "".join(_EXTRA.get(c, c) for c in text)):
        if unicodedata.category(ch) == "Mn":
            continue
        if ch.isascii() and ch.isalnum():
            if gap and out:
                out.append("-")
            out.append(ch.lower())
            gap = False
        else:
            gap = True
    return "".join(out)
