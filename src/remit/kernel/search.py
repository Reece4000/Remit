"""Search: what a listing's `search` matches, defined once for every store.

A document declares the fields it can be found by (`searchable = ("code", "name")`). The
kernel folds their text, so that case, accents and width make no difference, and a
search matches a document when every word of it appears somewhere in that text. Folding
happens here, in one place, for the text and for the search alike: a store only ever
looks for one folded string inside another, so every store finds the same documents.

On PostgreSQL the folded text is kept beside the documents and indexed by trigram
(`pg_trgm`). That index makes the lookup fast; it does not decide what matches.
"""

import unicodedata
from collections.abc import Sequence
from typing import Any

VERSION = 1
"""Raise this when folding changes, so that installing rebuilds what stores have kept."""

MAX_LENGTH = 200
"""The longest search the kernel accepts, in characters."""

_FIELD = "\n"
"""Separates one field's text from the next. Folded text never contains it."""


def fold(text: str) -> str:
    """Text as search compares it: without case, accents or width, in single-spaced words."""
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    bare = "".join(c for c in unicodedata.normalize("NFKD", decomposed) if not _is_mark(c))
    return " ".join(bare.split())


def terms(search: str) -> tuple[str, ...]:
    """The words of a search, folded. A document must hold every one of them."""
    return tuple(fold(search).split())


def searched(body: dict[str, Any], fields: Sequence[str]) -> str:
    """The folded text of a document's searchable fields, each on its own line."""
    held = [
        folded for field in fields for text in _texts(body.get(field)) if (folded := fold(text))
    ]
    return _FIELD + _FIELD.join(held) + _FIELD if held else ""


def holds(text: str, wanted: Sequence[str]) -> bool:
    """Whether folded text holds every one of the words."""
    return all(term in text for term in wanted)


def rank(text: str, wanted: Sequence[str]) -> int:
    """How well folded text matches a search; lower is better.

    0 when a field is exactly the search, 1 when a field starts with it, 2 otherwise.
    """
    phrase = " ".join(wanted)
    if _FIELD + phrase + _FIELD in text:
        return 0
    return 1 if _FIELD + phrase in text else 2


def holding_pattern(term: str) -> str:
    """`holds`, for one word, as a SQL LIKE pattern over folded text."""
    return f"%{_literal(term)}%"


def ranked_patterns(wanted: Sequence[str]) -> tuple[str, str]:
    """`rank` as SQL LIKE patterns: text matching the first ranks 0, else the second 1."""
    phrase = _literal(" ".join(wanted))
    return f"%{_FIELD}{phrase}{_FIELD}%", f"%{_FIELD}{phrase}%"


def _literal(text: str) -> str:
    """Text as LIKE reads it literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _texts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items: list[object] = value  # pyright: ignore[reportUnknownVariableType]
        return [item for item in items if isinstance(item, str)]
    return []


def _is_mark(character: str) -> bool:
    return unicodedata.category(character) == "Mn"
