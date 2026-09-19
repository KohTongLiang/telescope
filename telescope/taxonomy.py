"""Deterministic entity and topic extraction.

No model, no API, no cost, and the same answer every time. Everything here is a
curated dictionary plus regex rules, which means it can be read, tested and
tuned by hand. The trade-off is deliberate: this will miss things a language
model would catch, but it will never invent a release date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

PLATFORM = "platform"
ORG = "org"
GAME = "game"

MAX_ENTITIES = 12


@dataclass(frozen=True)
class Entity:
    type: str
    name: str


# ---------------------------------------------------------------- platforms

# Unambiguous enough to match case-insensitively.
PLATFORM_ALIASES: dict[str, str] = {
    "ps5": "PlayStation 5",
    "playstation 5": "PlayStation 5",
    "ps4": "PlayStation 4",
    "playstation 4": "PlayStation 4",
    "psvr2": "PlayStation VR2",
    "xbox series x": "Xbox Series X|S",
    "xbox series s": "Xbox Series X|S",
    "xbox series x|s": "Xbox Series X|S",
    "xbox one": "Xbox One",
    "nintendo switch 2": "Nintendo Switch 2",
    "switch 2": "Nintendo Switch 2",
    "nintendo switch": "Nintendo Switch",
    "steam deck": "Steam Deck",
    "epic games store": "Epic Games Store",
    "meta quest": "Meta Quest",
    "quest 3": "Meta Quest",
    "playdate": "Playdate",
    "gog": "GOG",
    "macos": "macOS",
    "linux": "Linux",
    "android": "Android",
    "ios": "iOS",
}

# Common English words: only match when capitalised, so "switch strategy" and
# "pc" inside a handle do not become platform mentions.
AMBIGUOUS_PLATFORMS: dict[str, str] = {
    "Switch": "Nintendo Switch",
    "PC": "PC",
    "Steam": "Steam",
    "Windows": "PC",
}

# ------------------------------------------------------------ studios/publishers

# Deliberately excludes ambiguous short names (Rare, King, DICE, EA, Meta) that
# appear constantly as ordinary words in games writing.
ORGS: tuple[str, ...] = (
    "Microsoft",
    "Xbox Game Studios",
    "Sony",
    "Sony Interactive Entertainment",
    "PlayStation Studios",
    "Nintendo",
    "Ubisoft",
    "Electronic Arts",
    "Activision",
    "Activision Blizzard",
    "Blizzard Entertainment",
    "Take-Two",
    "Rockstar Games",
    "Bethesda",
    "ZeniMax",
    "Square Enix",
    "Capcom",
    "Sega",
    "Atlus",
    "Bandai Namco",
    "Konami",
    "Koei Tecmo",
    "Nexon",
    "NCSoft",
    "Krafton",
    "Tencent",
    "NetEase",
    "miHoYo",
    "HoYoverse",
    "Epic Games",
    "Valve",
    "CD Projekt",
    "Larian Studios",
    "FromSoftware",
    "Remedy Entertainment",
    "Devolver Digital",
    "Annapurna Interactive",
    "Team17",
    "Paradox Interactive",
    "Focus Entertainment",
    "Embracer Group",
    "THQ Nordic",
    "Gearbox",
    "Riot Games",
    "Roblox",
    "Unity Technologies",
    "Amazon Games",
    "Netflix Games",
    "Apple",
    "Google",
    "Bungie",
    "Insomniac Games",
    "Naughty Dog",
    "Santa Monica Studio",
    "Guerrilla Games",
    "Sucker Punch",
    "id Software",
    "Arkane",
    "MachineGames",
    "Obsidian Entertainment",
    "inXile",
    "Playground Games",
    "Mojang",
    "Zynga",
    "Supercell",
    "Niantic",
    "Crytek",
    "Techland",
    "Frontier Developments",
    "Creative Assembly",
    "Sports Interactive",
    "Hello Games",
    "Supergiant Games",
    "Digital Extremes",
    "Warhorse Studios",
    "BioWare",
    "Respawn Entertainment",
    "Infinity Ward",
    "Treyarch",
    "Sledgehammer Games",
    "Halo Studios",
    "The Pokemon Company",
    "Game Freak",
    "PlatinumGames",
    "Arc System Works",
    "Nihon Falcom",
    "Marvelous",
    "Level-5",
    "Spike Chunsoft",
    "Grasshopper Manufacture",
    "Quantic Dream",
    "IO Interactive",
    "Housemarque",
    "Arrowhead Game Studios",
    "Saber Interactive",
    "Nacon",
    "tinyBuild",
    "Raw Fury",
    "No More Robots",
    "Fellow Traveller",
    "Yacht Club Games",
    "Behaviour Interactive",
    "Humble Games",
)

# ------------------------------------------------------------------- topics

TOPIC_RULES: dict[str, tuple[str, ...]] = {
    "Layoffs & closures": (
        r"\blayoffs?\b",
        r"\bredundanc",
        r"\bjob cuts?\b",
        r"\bshutting down\b",
        r"\bshut down\b",
        r"\bstudio closur",
        r"\brestructur",
        r"\baxes?\b.{0,20}\b(staff|jobs|team)\b",
        r"\bcancell?ed\b.{0,30}\b(project|game|studio)\b",
    ),
    "M&A & funding": (
        r"\bacqui",
        r"\bmerger\b",
        r"\bmerges?\b",
        r"\btakeover\b",
        r"\bbuyout\b",
        r"\bbuys?\b.{0,20}\bstudio\b",
        r"\binvestment\b",
        r"\binvests?\b",
        r"\bfunding\b",
        r"\bseries [a-e]\b",
        r"\bseed round\b",
        r"\bIPO\b",
        r"\braise[sd]? \$",
        r"\bmajority stake\b",
    ),
    "Releases & delays": (
        r"\brelease date",
        r"\bdelay(?:s|ed|ing)?\b",
        r"\bpostpon",
        r"\bpushes? back\b",
        r"\bslips? (?:to|into)\b",
        r"\bout now\b",
        r"\bavailable now\b",
        r"\blaunch(?:es|ing|ed)?\b",
        r"\bcoming to\b",
        r"\bgoes gold\b",
        r"\bearly access\b",
        r"\bdrops? (?:on|today)\b",
        r"\barrives?\b",
    ),
    "Platform & store": (
        r"\bapp store\b",
        r"\bplaystation store\b",
        r"\beshop\b",
        r"\bgame pass\b",
        r"\bps plus\b",
        r"\bswitch online\b",
        r"\bstorefront\b",
        r"\bstore policy\b",
        r"\bprice (?:hike|increase|cut|change)",
        r"\bsubscription\b",
        r"\brefund",
        r"\bdelist",
        r"\bregion lock",
        r"\bban(?:ned|s|ning)?\b",
    ),
    "Tools & engines": (
        r"\bunity\b",
        r"\bunreal\b",
        r"\bgodot\b",
        r"\bgame engine\b",
        r"\bengine\b",
        r"\bSDK\b",
        r"\bmiddleware\b",
        r"\bdev kit\b",
        r"\bcontent pipeline\b",
        r"\bmotion capture\b",
        r"\bmocap\b",
        r"\bAI (?:tool|generated|assisted)\b",
    ),
    "Legal & labour": (
        r"\blawsuit\b",
        r"\bsue[sd]?\b",
        r"\bsuing\b",
        r"\bcourt\b",
        r"\bjudge\b",
        r"\bunion(?:is|s|isation|ization)?\b",
        r"\bstrike\b",
        r"\bregulator",
        r"\bFTC\b",
        r"\bCMA\b",
        r"\bantitrust\b",
        r"\bclass action\b",
        # Require regulatory context: bare "investigation" matched game
        # descriptions such as "bio-investigation romance".
        r"\b(?:antitrust|regulatory|competition|regulator)\s+investigat",
        r"\binvestigation into\b",
        r"\btribunal\b",
        r"\bcrunch\b",
        r"\bharassment\b",
    ),
    "Hardware": (
        r"\bconsoles?\b",
        r"\bhardware\b",
        r"\bGPU\b",
        r"\bCPU\b",
        r"\bchip(?:s|set)?\b",
        r"\bsilicon\b",
        r"\bhandheld\b",
        r"\bsteam deck\b",
        r"\bswitch 2\b",
        r"\bcomponents?\b",
        r"\bsupply chain\b",
        r"\bfirmware\b",
        r"\bframe rate\b",
        r"\bray tracing\b",
        r"\bVR headset\b",
    ),
    "Business & earnings": (
        r"\bearnings\b",
        r"\brevenue\b",
        r"\bprofit",
        r"\bquarter(?:ly)?\b",
        r"\bfiscal\b",
        r"\bsales (?:figures|data|numbers)\b",
        r"\bunits sold\b",
        r"\bforecast",
        r"\bguidance\b",
        r"\bnet (?:income|loss)\b",
        r"\bheadcount\b",
    ),
    "Esports": (
        r"\besports?\b",
        r"\btournament\b",
        r"\bchampionship\b",
        r"\bgrand final",
        r"\broster\b",
    ),
}

DEFAULT_TOPIC_ORDER: tuple[str, ...] = (
    "Layoffs & closures",
    "M&A & funding",
    "Legal & labour",
    "Platform & store",
    "Tools & engines",
    "Hardware",
    "Business & earnings",
    # Broadest rules by far: almost every product story mentions a launch, a
    # release date or "out now". It sorts last so specific topics get their
    # section first, and it is the first topic dropped when an item matches
    # more than MAX_TOPICS.
    "Releases & delays",
    "Esports",
)

MAX_TOPICS = 3


# ---------------------------------------------------------------- machinery


def _alternation(names: tuple[str, ...] | list[str]) -> str:
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


@lru_cache(maxsize=1)
def _platform_regex() -> re.Pattern:
    return re.compile(
        rf"(?<![\w])({_alternation(list(PLATFORM_ALIASES))})(?![\w])", re.IGNORECASE
    )


@lru_cache(maxsize=1)
def _ambiguous_platform_regex() -> re.Pattern:
    return re.compile(
        rf"(?<![\w])({_alternation(list(AMBIGUOUS_PLATFORMS))})(?![\w])"
    )


@lru_cache(maxsize=128)
def _org_regex(extra: tuple[str, ...]) -> re.Pattern:
    names = list(ORGS) + list(extra)
    return re.compile(rf"(?<![\w])({_alternation(names)})(?![\w])", re.IGNORECASE)


@lru_cache(maxsize=1)
def _topic_regexes() -> tuple[tuple[str, re.Pattern], ...]:
    return tuple(
        (topic, re.compile("|".join(patterns), re.IGNORECASE))
        for topic, patterns in TOPIC_RULES.items()
    )


_QUOTED_RE = re.compile(r"[\"'\u201c\u201d\u2018\u2019]([^\"'\u201c\u201d\u2018\u2019]{3,60})[\"'\u201c\u201d\u2018\u2019]")


def canonical_platform(name: str) -> str:
    return PLATFORM_ALIASES.get(name.lower()) or AMBIGUOUS_PLATFORMS.get(name, name)


def extract_entities(
    text: str | None, *, extra_orgs: tuple[str, ...] | list[str] = ()
) -> list[Entity]:
    """Platforms, studios/publishers, and quoted game titles, in that order.

    Quoted strings are the only reliable game-title signal available without a
    model, so unquoted titles are simply not detected. Under-detecting is the
    right failure mode here: a false entity would corrupt cluster boundaries.
    """
    if not text:
        return []

    found: list[Entity] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, name: str) -> None:
        key = (kind, name)
        if key not in seen:
            seen.add(key)
            found.append(Entity(kind, name))

    for match in _platform_regex().finditer(text):
        add(PLATFORM, canonical_platform(match.group(1)))

    for match in _ambiguous_platform_regex().finditer(text):
        add(PLATFORM, AMBIGUOUS_PLATFORMS[match.group(1)])

    for match in _org_regex(tuple(extra_orgs)).finditer(text):
        add(ORG, match.group(1))

    for match in _QUOTED_RE.finditer(text):
        candidate = match.group(1).strip()
        if candidate and not candidate.isdigit():
            add(GAME, candidate)

    return found[:MAX_ENTITIES]


def entity_names(entities: list[Entity]) -> list[str]:
    names: list[str] = []
    for entity in entities:
        if entity.name not in names:
            names.append(entity.name)
    return names


def classify_topics(text: str | None, *, limit: int = MAX_TOPICS) -> list[str]:
    """Rule-based topic tags, ordered by the canonical topic order."""
    if not text:
        return []
    hits = [topic for topic, regex in _topic_regexes() if regex.search(text)]
    hits.sort(key=lambda t: DEFAULT_TOPIC_ORDER.index(t) if t in DEFAULT_TOPIC_ORDER else 99)
    return hits[:limit]
