"""Shared text helpers: query normalization, quality/language/year detection.

Used by the search service (input parsing), the indexer (filename parsing)
and the TMDB cache key builder — one implementation everywhere.
"""
from __future__ import annotations

import re

QUALITY_RE = re.compile(r"\b(480p|720p|1080p|2160p|4320p|4k|8k)\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
EXT_RE = re.compile(r"\.[a-z0-9]{2,4}$", re.IGNORECASE)
NOISE_RE = re.compile(
    r"\b(uhd|hd|hq|hd-?cam|cam|hdts|dvdrip|dvdscr|webrip|web-?dl|bluray|brrip|"
    r"x264|x265|hevc|10bit|aac|dts|dd5\.1|esub|subs|proper|repack|extended|"
    r"unrated|imax|hdr|remux)\b",
    re.IGNORECASE,
)
LANG_RE = re.compile(
    r"\b(hindi|malayalam|mallu|tamil|telugu|kannada|english|multi|"
    r"dual[\s\-]?audio)\b",
    re.IGNORECASE,
)
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# --- Advanced query parsing: season / episode -------------------------------
# Matches "S01E02", "s1e2", "1x02" as well as "season 2" / "ep 5".
SE_EP_RE = re.compile(r"\b[Ss]0*(\d{1,2})\s*[Ee]0*(\d{1,3})\b")
X_EP_RE = re.compile(r"(?<![a-z0-9])(\d{1,2})[x\xd7]0*(\d{1,3})(?![0-9])")
SEASON_WORD_RE = re.compile(r"\b[Ss]eason\s*0*(\d{1,2})(?!\d)")
SEASON_BARE_RE = re.compile(r"\b[Ss]0*(\d{1,2})(?!\d)\b")
EPISODE_WORD_RE = re.compile(r"\b[Ee]p(?:isode)?\s*0*(\d{1,3})(?!\d)")

# Intent / noise words users add to queries ("premam full movie download").
# Stripped query-side only; the DB rows are untouched. If stripping leaves
# nothing, the original words are kept (e.g. the query "1080p" alone).
# Includes Manglish question markers ("kgf movie undo" -> "kgf").
QUERY_NOISE_RE = re.compile(
    r"\b(movies?|films?|full|download(?:s|ing)?|watch(?:ing)?|online|"
    r"dubbed|version|latest|hd|hq|please|pls|bluray|brrip|bdrip|"
    r"webrip|web-?dl|hdrip|dvdrip|hdtc|hdts|camrip|"
    r"undo|aano|alle|aakumo|aakum|entha|enth|evide|eppol|aar?a)\b",
    re.IGNORECASE,
)

QUALITY_CANON = {"4k": "2160p", "8k": "4320p"}
QUALITY_ORDER = {"480p": 0, "720p": 1, "1080p": 2, "2160p": 3, "4320p": 4}

LANG_CANON = {
    "mallu": "Malayalam",
    "malayalam": "Malayalam",
    "hindi": "Hindi",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "kannada": "Kannada",
    "english": "English",
    "multi": "Multi",
    "dual audio": "Multi",
    "dual-audio": "Multi",
}


def canon_quality(raw: str | None) -> str | None:
    """Canonicalize a quality token (``4K`` -> ``2160p``)."""
    if not raw:
        return None
    q = raw.strip().lower()
    return QUALITY_CANON.get(q, q)


def canon_language(raw: str | None) -> str | None:
    """Canonicalize a language token (``mallu`` -> ``Malayalam``)."""
    if not raw:
        return None
    key = raw.strip().lower().replace("-", " ")
    key = re.sub(r"\s+", " ", key)
    return LANG_CANON.get(key)


def detect_quality_language(text: str | None) -> tuple[str | None, str | None]:
    """Best-effort quality + language detection from a filename/caption."""
    if not text:
        return None, None
    qm = QUALITY_RE.search(text)
    lm = LANG_RE.search(text)
    return canon_quality(qm.group(1) if qm else None), canon_language(
        lm.group(1) if lm else None
    )


def clean_title(name: str | None) -> str:
    """Turn a raw filename into a human-readable title.

    ``Avengers.Endgame.2019.1080p.Hindi.mkv`` -> ``Avengers Endgame``.
    """
    if not name:
        return ""
    t = EXT_RE.sub("", name)
    t = t.replace(".", " ").replace("_", " ")
    t = QUALITY_RE.sub(" ", t)
    t = LANG_RE.sub(" ", t)
    t = YEAR_RE.sub(" ", t)
    t = NOISE_RE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t


def title_key(name: str | None) -> str:
    """Normalized grouping key: lowercase alphanumeric words joined by space."""
    t = clean_title(name).lower()
    t = NON_ALNUM_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def extract_year(text: str | None) -> int | None:
    """First plausible release year in the text, or None."""
    if not text:
        return None
    m = YEAR_RE.search(text)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return year
    return None


def _x_ep_plausible(m: re.Match) -> bool:
    """Reject NxM false positives like "24x7" (not a season/episode).

    ``NxM`` reads as season/episode only when the episode part is
    zero-padded ("1x02") or the season is a single digit ("2x7"). A
    two-digit season with a bare one-digit episode ("24x7") is a
    ratio/quantity, not S/E.
    """
    raw = m.group(0)
    after = raw.split("x", 1)[1] if "x" in raw else raw.split("×", 1)[1]
    return after.startswith("0") or len(m.group(1)) == 1


def _extract_season_episode(text: str) -> tuple[str, int | None, int | None]:
    """Pull season/episode tokens out of a query.

    Returns ``(remaining_text, season, episode)``. Handles ``S01E02``,
    ``1x02``, ``season 2``, ``ep 5`` and bare ``S01``.
    """
    season: int | None = None
    episode: int | None = None

    m = SE_EP_RE.search(text)
    if not m:
        m = X_EP_RE.search(text)
        if m and not _x_ep_plausible(m):
            m = None
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        text = text[: m.start()] + " " + text[m.end():]
    else:
        m = SEASON_WORD_RE.search(text)
        if m:
            season = int(m.group(1))
            text = text[: m.start()] + " " + text[m.end():]
        else:
            m = SEASON_BARE_RE.search(text)
            if m:
                season = int(m.group(1))
                text = text[: m.start()] + " " + text[m.end():]
        m = EPISODE_WORD_RE.search(text)
        if m:
            episode = int(m.group(1))
            text = text[: m.start()] + " " + text[m.end():]
    return text, season, episode


def parse_query(raw: str) -> dict:
    """Split a user query into a clean search phrase + structured filters.

    ``"avengers endgame 2019 1080p hindi"`` ->
    ``{"query": "avengers endgame", "year": 2019,
      "quality": "1080p", "language": "Hindi",
      "season": None, "episode": None}``.

    Advanced parsing on top: ``S01E02`` / ``season 2`` / ``ep 5`` become
    season/episode filters, dots/underscores split words
    (``avengers.endgame``), and intent noise (``full movie download``)
    is dropped. The clean phrase is what hits the full-text / trigram
    indexes; the filters narrow or boost the result set afterwards.
    """
    text = (raw or "").strip()
    text = re.sub(r"[._]+", " ", text)
    text, season, episode = _extract_season_episode(text)
    quality, language = detect_quality_language(text)
    year = extract_year(text)
    q = QUALITY_RE.sub(" ", text)
    q = LANG_RE.sub(" ", q)
    q = YEAR_RE.sub(" ", q)
    q = SE_EP_RE.sub(" ", q)
    q = QUERY_NOISE_RE.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        if quality or language:
            # Filters-only query ("1080p" / "hindi"): match on filters alone.
            q = ""
        else:
            # Noise removal ate everything — keep raw words so the query
            # still matches something.
            q = re.sub(r"\s+", " ", text).strip()
    return {
        "query": q,
        "quality": quality,
        "language": language,
        "year": year,
        "season": season,
        "episode": episode,
    }


# ---------------------------------------------------------------------------
# v10.14: display-name cleaning for result cards / captions.
# ---------------------------------------------------------------------------
_URL_TOKEN_RE = re.compile(r"https?://\S+|www\.\S+|t\.me/\S+", re.IGNORECASE)
_SE_TAG_RE = re.compile(r"s\d+e\d+", re.IGNORECASE)


def smart_title(text: str) -> str:
    """Title-case a display name without shouting.

    Only all-lowercase words are capitalized (``karma`` -> ``Karma``);
    words that already have capitals are left alone. ``s01e02``-style
    season/episode tags are uppercased (``S01E02``).
    """
    out = []
    for w in text.split():
        if _SE_TAG_RE.fullmatch(w):
            out.append(w.upper())
        elif w.islower():
            out.append(w.capitalize())
        else:
            out.append(w)
    return " ".join(out)


def clean_display_name(name: str | None, max_length: int = 64) -> str:
    """Turn a raw indexed filename into a clean display title.

    Strips URLs, channel-spam tokens (``[Foo]``, ``@foo``, ``www.…``),
    converts dots/underscores to spaces, title-cases, and truncates to
    ``max_length`` with an ellipsis.
    """
    if not name:
        return ""
    # 1. strip URL-ish substrings anywhere in the name.
    text = _URL_TOKEN_RE.sub(" ", name)
    # 2. drop the file extension (display only).
    text = EXT_RE.sub("", text)
    # 3. dots/underscores -> space so spam tokens separate out.
    text = re.sub(r"[._]+", " ", text)
    # 4. drop whitespace-separated spam tokens (channel tags etc.).
    kept = []
    for tok in text.split():
        low = tok.lower()
        if (tok.startswith("[") or tok.startswith("@")
                or low.startswith(("www.", "http", "t.me"))):
            continue
        kept.append(tok)
    text = re.sub(r"\s+", " ", " ".join(kept)).strip()
    # 5. title-case.
    text = smart_title(text)
    # 6. truncate.
    if len(text) > max_length:
        text = text[:max_length - 1].rstrip() + "…"
    return text
