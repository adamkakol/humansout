import unicodedata


def slugify(text: str) -> str:
    out, gap = [], False
    for ch in unicodedata.normalize("NFKD", text):
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
