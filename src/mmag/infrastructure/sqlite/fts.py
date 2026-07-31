"""SQLite FTS helpers shared by migrations and repositories."""

import re

_CJK_PATTERN = re.compile(r"[一-鿿㐀-䶿]")


def cjk_tokenize_for_fts(text: str) -> str:
    """Insert spaces between consecutive CJK characters for unicode61 FTS."""
    result: list[str] = []
    previous_was_cjk = False
    for character in text:
        is_cjk = bool(_CJK_PATTERN.match(character))
        if is_cjk and previous_was_cjk:
            result.append(f" {character}")
        else:
            result.append(character)
        previous_was_cjk = is_cjk
    return "".join(result)
