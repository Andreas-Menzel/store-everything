"""Which language a piece of text is in.

One implementation for every extractor that produces text, because the answer has to mean the
same thing everywhere: the well-known `language` key drives language-aware full-text search
([F-004/FR-4](../../../features/F-004-document-text-extraction.md)), and two extractors
disagreeing about what "de" means would make that search worse, not better.

**German and English only, deliberately.** Those are the languages this instance's search is
being built for ([Q14](../../../OPEN-QUESTIONS.md)), and a detector asked to choose between
seventy candidates guesses confidently on short strings — "Panorama" is a word in a dozen of
them. Restricted to two, `lingua` is reliable on a sentence and honest on a fragment; adding a
language later is one entry in the list below and a re-run.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from lingua import Language, LanguageDetectorBuilder

#: BCP 47 tags, which is what the API and the search layer speak — not the detector's enum.
_TAGS = {Language.ENGLISH: "en", Language.GERMAN: "de"}

#: Below this, do not guess. Two words are not evidence, and a wrong language tag is worse than
#: none: it sends the text through the wrong analyser and makes it *less* findable.
MIN_CHARACTERS = 40


@cache
def _detector() -> Any:
    """Built once. Loading the models costs tens of milliseconds and a few megabytes."""
    return LanguageDetectorBuilder.from_languages(*_TAGS).build()


def detect_language(text: str) -> str | None:
    """The BCP 47 tag of the language this text is in, or `None` when it is not worth saying."""
    trimmed = " ".join(text.split())
    if len(trimmed) < MIN_CHARACTERS:
        return None
    detected = _detector().detect_language_of(trimmed)
    return _TAGS.get(detected) if detected is not None else None


__all__ = ["MIN_CHARACTERS", "detect_language"]
