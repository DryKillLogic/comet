from collections import OrderedDict, defaultdict
from threading import Event, Lock

from pydantic import ValidationError
from RTN import normalize_title, parse, title_match

from comet.core.logger import logger
from comet.core.models import settings
from comet.utils.languages import COUNTRY_TO_LANGUAGE
from comet.utils.parsing import ensure_multi_language

if settings.RTN_FILTER_DEBUG:

    def _log_exclusion(msg):
        logger.log("FILTER", msg)
else:

    def _log_exclusion(msg):
        pass


def quick_alias_match(text_normalized: str, ez_aliases_normalized: list[str]):
    return any(alias in text_normalized for alias in ez_aliases_normalized)


def scrub(t: str):
    return " ".join(normalize_title(t).split())


def _strip_leading_english_articles(t: str):
    tokens = scrub(t).split()
    while tokens and tokens[0] in {"the", "a", "an"}:
        tokens = tokens[1:]
    return " ".join(tokens)


def _article_insensitive_title_match(expected_title: str, parsed_title: str):
    expected = _strip_leading_english_articles(expected_title)
    parsed = _strip_leading_english_articles(parsed_title)
    return bool(expected) and expected == parsed


def _is_release_noise_token(token: str):
    if token in {
        "readnfo",
        "nfo",
        "proper",
        "repack",
        "rerip",
        "internal",
        "extended",
        "unrated",
    }:
        return True

    if len(token) == 4 and token.isdigit():
        year = int(token)
        if 1900 <= year <= 2100:
            return True

    return False


def _trailing_noise_title_match(expected_title: str, parsed_title: str):
    expected_tokens = _strip_leading_english_articles(expected_title).split()
    parsed_tokens = _strip_leading_english_articles(parsed_title).split()

    if not expected_tokens or len(parsed_tokens) <= len(expected_tokens):
        return False

    if parsed_tokens[: len(expected_tokens)] != expected_tokens:
        return False

    trailing_tokens = parsed_tokens[len(expected_tokens) :]
    return bool(trailing_tokens) and all(
        _is_release_noise_token(token) for token in trailing_tokens
    )


def _alias_variants(scrubbed_alias: str, main_title_scrubbed: str):
    variants = {scrubbed_alias}

    if not main_title_scrubbed or scrubbed_alias == main_title_scrubbed:
        return variants

    prefix = f"{main_title_scrubbed} "
    if scrubbed_alias.startswith(prefix):
        remainder = scrubbed_alias[len(prefix) :].strip()
        if " " in remainder:
            variants.add(remainder)

    suffix = f" {main_title_scrubbed}"
    if scrubbed_alias.endswith(suffix):
        remainder = scrubbed_alias[: -len(suffix)].strip()
        if " " in remainder:
            variants.add(remainder)

    return variants


def _lang_from_alias_key(alias_key: str):
    key = (alias_key or "").strip().lower()
    if not key or key == "ez":
        return "neutral"

    # Trakt aliases use country codes (e.g. "mx"), while TMDB translations
    # can use language ("es") or language-region ("es-MX") keys.
    if key in COUNTRY_TO_LANGUAGE:
        return COUNTRY_TO_LANGUAGE[key]

    if "-" in key:
        lang_part = key.split("-", 1)[0]
        if len(lang_part) == 2 and lang_part.isalpha():
            return lang_part

    if len(key) == 2 and key.isalpha():
        return key

    return "neutral"


def _region_from_alias_key(alias_key: str):
    key = (alias_key or "").strip().lower()
    if not key or key == "ez":
        return None

    # TMDB language-region keys (e.g. "es-mx").
    if "-" in key:
        _, region_part = key.split("-", 1)
        region_part = region_part.strip().lower()
        if len(region_part) == 2 and region_part.isalpha():
            return region_part

    # Country code keys from Trakt (e.g. "mx") map directly to a region tag.
    # Avoid treating generic language codes (e.g. "es") as regions.
    if key in COUNTRY_TO_LANGUAGE and len(key) == 2:
        return key

    return None


def _language_family(language: str) -> str:
    normalized = (language or "").strip().lower()
    if not normalized:
        return normalized

    if normalized == "la":
        return "es"

    if "-" in normalized:
        return normalized.split("-", 1)[0]

    return normalized


def _is_specific_language(language: str) -> bool:
    normalized = (language or "").strip().lower()
    return bool(normalized) and ("-" in normalized or normalized == "la")


def _merge_alias_language(languages, candidate_language: str):
    candidate = (candidate_language or "").strip().lower()
    if not candidate:
        return

    if candidate in languages:
        return

    family = _language_family(candidate)
    same_family = [
        language
        for language in languages
        if isinstance(language, str) and _language_family(language) == family
    ]

    if not same_family:
        languages.append(candidate)
        return

    candidate_specific = _is_specific_language(candidate)
    existing_specific = [lang for lang in same_family if _is_specific_language(lang)]

    if candidate_specific:
        if existing_specific:
            return

        for idx, language in enumerate(languages):
            if isinstance(language, str) and _language_family(language) == family:
                languages[idx] = candidate
                return
        languages.append(candidate)
        return

    # Generic candidate: only add when family has no specific variant.
    if existing_specific:
        return

    languages.append(candidate)


def _looks_like_spanish_title(text: str):
    normalized = f" {scrub(text)} "
    if " de " not in normalized:
        return False

    spanish_markers = (
        " el ",
        " la ",
        " los ",
        " las ",
        " del ",
        " y ",
        " en ",
        " por ",
        " para ",
        " una ",
        " un ",
        " con ",
    )
    return any(marker in normalized for marker in spanish_markers)


def _remove_false_german_for_spanish_titles(parsed, torrent_title: str):
    languages = list(getattr(parsed, "languages", []) or [])
    if not languages:
        return

    normalized_languages = {
        language.lower() for language in languages if isinstance(language, str)
    }
    has_german = "de" in normalized_languages or any(
        language.startswith("de-") for language in normalized_languages
    )
    if not has_german:
        return

    parsed_title = getattr(parsed, "parsed_title", "") or ""
    has_spanish_signal = _looks_like_spanish_title(parsed_title) or any(
        language in normalized_languages for language in ("es", "la")
    )
    if not has_spanish_signal:
        return

    # Keep explicit German releases.
    title_tokens = set(scrub(torrent_title).split())
    if title_tokens & {"german", "deutsch", "deu", "ger", "aleman"}:
        return

    parsed.languages = [
        language
        for language in languages
        if not (
            isinstance(language, str)
            and (language.lower() == "de" or language.lower().startswith("de-"))
        )
    ]


class _ParseCacheShard:
    __slots__ = ("lock", "data", "inflight")

    def __init__(self):
        self.lock = Lock()
        self.data = OrderedDict()
        self.inflight = {}


_PARSE_CACHE_SIZE = settings.FILTER_PARSE_CACHE_SIZE
_PARSE_CACHE_SHARDS = max(settings.FILTER_PARSE_CACHE_SHARDS, 1)
_PARSE_CACHE_DEDUP_INFLIGHT = settings.FILTER_PARSE_CACHE_DEDUP_INFLIGHT
_PARSE_CACHE_DEDUP_TIMEOUT = 5.0

if _PARSE_CACHE_SIZE > 0:
    _PARSE_CACHE_EFFECTIVE_SHARDS = min(_PARSE_CACHE_SHARDS, _PARSE_CACHE_SIZE)
else:
    _PARSE_CACHE_EFFECTIVE_SHARDS = 0

if _PARSE_CACHE_EFFECTIVE_SHARDS > 0:
    _PARSE_CACHE_SHARD_SIZES = [
        (_PARSE_CACHE_SIZE // _PARSE_CACHE_EFFECTIVE_SHARDS)
        + (1 if i < (_PARSE_CACHE_SIZE % _PARSE_CACHE_EFFECTIVE_SHARDS) else 0)
        for i in range(_PARSE_CACHE_EFFECTIVE_SHARDS)
    ]
else:
    _PARSE_CACHE_SHARD_SIZES = []

_parse_cache = [_ParseCacheShard() for _ in range(_PARSE_CACHE_EFFECTIVE_SHARDS)]


def _parse_cache_shard_for(title: str):
    shard_idx = hash(title) % _PARSE_CACHE_EFFECTIVE_SHARDS
    return shard_idx, _parse_cache[shard_idx], _PARSE_CACHE_SHARD_SIZES[shard_idx]


def _clone_parsed(parsed):
    if hasattr(parsed, "model_copy"):
        return parsed.model_copy(deep=True)
    return parsed.copy(deep=True)


def _parse_with_cache(title: str):
    if _PARSE_CACHE_SIZE <= 0 or _PARSE_CACHE_EFFECTIVE_SHARDS <= 0:
        return parse(title)

    _, shard, max_size = _parse_cache_shard_for(title)
    if max_size <= 0:
        return parse(title)

    if _PARSE_CACHE_DEDUP_INFLIGHT:
        return _parse_with_cache_dedup(title, shard, max_size)
    else:
        return _parse_with_cache_simple(title, shard, max_size)


def _parse_with_cache_simple(title: str, shard: _ParseCacheShard, max_size: int):
    with shard.lock:
        cached = shard.data.get(title)
        if cached is not None:
            shard.data.move_to_end(title)
            return _clone_parsed(cached)

    parsed = parse(title)
    cached = _clone_parsed(parsed)

    with shard.lock:
        shard.data[title] = cached
        if len(shard.data) > max_size:
            shard.data.popitem(last=False)

    return parsed


def _parse_with_cache_dedup(title: str, shard: _ParseCacheShard, max_size: int):
    inflight_event = None
    do_parse = False

    with shard.lock:
        cached = shard.data.get(title)
        if cached is not None:
            shard.data.move_to_end(title)
            return _clone_parsed(cached)

        inflight_event = shard.inflight.get(title)
        if inflight_event is None:
            inflight_event = Event()
            shard.inflight[title] = inflight_event
            do_parse = True

    if not do_parse:
        if not inflight_event.wait(timeout=_PARSE_CACHE_DEDUP_TIMEOUT):
            return parse(title)

        with shard.lock:
            cached = shard.data.get(title)
            if cached is not None:
                shard.data.move_to_end(title)
                return _clone_parsed(cached)

        return parse(title)

    return _do_parse_and_cache(title, shard, max_size, inflight_event)


def _do_parse_and_cache(
    title: str,
    shard: _ParseCacheShard,
    max_size: int,
    inflight_event: Event,
):
    try:
        parsed = parse(title)
        cached = _clone_parsed(parsed)
        with shard.lock:
            shard.data[title] = cached
            if len(shard.data) > max_size:
                shard.data.popitem(last=False)
            shard.inflight.pop(title, None)
        return parsed
    except BaseException:
        with shard.lock:
            shard.inflight.pop(title, None)
        raise
    finally:
        inflight_event.set()


def filter_worker(
    torrents, title, year, year_end, media_type, aliases, remove_adult_content
):
    results = []

    tz_aliases = set()
    country_aliases = {}
    alias_to_langs = defaultdict(set)
    alias_to_regions = defaultdict(set)

    if settings.SMART_LANGUAGE_DETECTION:
        main_title_scrubbed = scrub(title)

        for country, titles in aliases.items():
            lang = _lang_from_alias_key(country)
            region = _region_from_alias_key(country)
            for t in titles:
                scrubbed_t = scrub(t)
                for alias_variant in _alias_variants(scrubbed_t, main_title_scrubbed):
                    tz_aliases.add(alias_variant)
                    alias_to_langs[alias_variant].add(lang)
                    if region:
                        alias_to_regions[alias_variant].add(region)

        # Only trust aliases that map to exactly one non-english language
        # and are not the main title itself.
        for scrubbed_t, langs in alias_to_langs.items():
            if scrubbed_t == main_title_scrubbed:
                continue

            non_english_langs = {
                lang for lang in langs if lang not in ("neutral", "en")
            }
            if len(non_english_langs) == 1:
                country_aliases[scrubbed_t] = next(iter(non_english_langs))
    else:
        main_title_scrubbed = scrub(title)
        for country, titles in aliases.items():
            for t in titles:
                scrubbed_t = scrub(t)
                tz_aliases.update(_alias_variants(scrubbed_t, main_title_scrubbed))

    ez_aliases_normalized = list(tz_aliases)
    min_year = 0
    max_year = float("inf")

    if year:
        if year_end:
            min_year = year
            max_year = year_end
        elif media_type == "series":
            min_year = year - 1
        else:
            min_year = year - 1
            max_year = year + 1

    for torrent in torrents:
        torrent_title = torrent["title"]
        torrent_title_lower = torrent_title.lower()

        if "sample" in torrent_title_lower or torrent_title == "":
            _log_exclusion(f"🚫 Rejected (Sample/Empty) | {torrent_title}")
            continue

        # temp fix while waiting for RTN to fix their parsing
        try:
            parsed = _parse_with_cache(torrent_title)
        except ValidationError:
            _log_exclusion(f"❌ Rejected (Parse Error) | {torrent_title}")
            continue

        if parsed.parsed_title and country_aliases:
            parsed_title_scrubbed = scrub(parsed.parsed_title)
            language = country_aliases.get(parsed_title_scrubbed)
            if language:
                non_english_langs = {
                    lang
                    for lang in alias_to_langs.get(parsed_title_scrubbed, set())
                    if lang not in ("neutral", "en")
                }
                if len(non_english_langs) == 1:
                    regions = alias_to_regions.get(parsed_title_scrubbed, set())
                    region = next(iter(regions)) if len(regions) == 1 else None

                    language_to_store = f"{language}-{region}" if region else language
                else:
                    language_to_store = language

                before_languages = list(parsed.languages)
                _merge_alias_language(parsed.languages, language_to_store)
                if parsed.languages != before_languages:
                    if region:
                        _log_exclusion(
                            f"🌎 Added Region (Alias) | {torrent_title} | {language_to_store}"
                        )
                    else:
                        _log_exclusion(
                            f"🏷️ Added Language (Alias) | {torrent_title} | {language_to_store}"
                        )

        _remove_false_german_for_spanish_titles(parsed, torrent_title)
        ensure_multi_language(parsed)

        if remove_adult_content and parsed.adult:
            _log_exclusion(f"🔞 Rejected (Adult) | {torrent_title}")
            continue

        if not parsed.parsed_title:
            _log_exclusion(f"❌ Rejected (No Parsed Title) | {torrent_title}")
            continue

        raw_norm = scrub(torrent_title)
        alias_matched = ez_aliases_normalized and quick_alias_match(
            raw_norm, ez_aliases_normalized
        )
        if not alias_matched:
            title_matches = title_match(title, parsed.parsed_title, aliases=aliases)
            if (
                not title_matches
                and _article_insensitive_title_match(title, parsed.parsed_title)
            ):
                title_matches = True
                _log_exclusion(
                    f"🧩 Accepted (Article-insensitive Title) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}"
                )

            if (
                not title_matches
                and _trailing_noise_title_match(title, parsed.parsed_title)
            ):
                title_matches = True
                _log_exclusion(
                    f"🧩 Accepted (Trailing-noise Title) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}"
                )

            if not title_matches:
                _log_exclusion(
                    f"❌ Rejected (Title Mismatch) | {torrent_title} | Parsed: {parsed.parsed_title} | Expected: {title}"
                )
                continue

        if year and parsed.year:
            if not (min_year <= parsed.year <= max_year):
                if year_end:
                    expected = f"{year}-{year_end}"
                elif media_type == "series":
                    expected = f">{year}"
                else:
                    expected = f"~{year}"

                _log_exclusion(
                    f"📅 Rejected (Year Mismatch) | {torrent_title} | Year: {parsed.year} | Expected: {expected}"
                )
                continue

        torrent["parsed"] = parsed
        results.append(torrent)
    return results
