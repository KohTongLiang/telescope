"""Configuration: filesystem paths and the source registry."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

Tier = Literal["first_party", "trade", "enthusiast", "aggregator", "community"]
Axis = Literal["business", "development", "releases", "platform", "general", "community"]

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


class Source(BaseModel):
    """One feed in the registry."""

    id: str
    name: str
    url: str
    tier: Tier = "enthusiast"
    axis: Axis = "general"
    enabled: bool = True
    max_age_hours: int = 48


class RankWeights(BaseModel):
    """Ranking terms. Every one is tunable; none is decided by a model."""

    breadth: float = 1.0
    tier: float = 0.8
    recency: float = 1.2
    keywords: float = 0.9
    watchlist: float = 1.5
    first_party: float = 0.4


# Mirrors config/digest.yaml. Kept here as well so a missing or partial config
# file still produces a sensible digest.
DEFAULT_KEYWORD_WEIGHTS: dict[str, float] = {
    "delay": 1.2,
    "delayed": 1.2,
    "delays": 1.2,
    "layoff": 1.5,
    "layoffs": 1.5,
    "redundancies": 1.4,
    "shut down": 1.5,
    "shutting down": 1.5,
    "closure": 1.4,
    "acquired": 1.3,
    "acquisition": 1.3,
    "merger": 1.2,
    "lawsuit": 1.1,
    "strike": 1.3,
    "union": 1.2,
    "cancelled": 1.1,
    "canceled": 1.1,
    "funding": 1.1,
    "bankruptcy": 1.4,
    "early access": 0.8,
    "release date": 0.9,
    "out now": 0.7,
    "exclusive": 0.7,
    "review": 0.3,
}

DEFAULT_DEMOTE_KEYWORDS: dict[str, float] = {
    "rumor": 0.9,
    "rumour": 0.9,
    "reportedly": 0.6,
    "leak": 0.7,
    "leaked": 0.7,
    "deal": 0.4,
    "sale": 0.5,
    "% off": 1.0,
    "discount": 0.6,
    "best games": 0.8,
    "where to buy": 0.9,
    "how to": 0.5,
    "guide": 0.5,
    "tierlist": 0.8,
    "tier list": 0.8,
    "patch notes": 0.4,
}


class ClusteringConfig(BaseModel):
    """Thresholds for merging the same story across outlets."""

    # Containment: the fraction of the shorter headline's content tokens that
    # appear in the longer one. Chosen over Jaccard because Jaccard punishes the
    # length differences that separate two write-ups of one event.
    containment_threshold: float = 0.5
    # A source repeating itself is normal (updates, live blogs), so it has to
    # be near-identical before two of its own items merge. Lowered from 0.85:
    # that gate blocked an outlet's own "…[update: delayed]" follow-up from
    # merging with its original story at containment 0.67, and an outlet
    # restating its own story is the most certain duplicate signal there is.
    same_source_containment_threshold: float = 0.6
    window_hours: int = 48
    # Candidate generation is bounded by document frequency. The index bound is
    # deliberately looser than the distinctive bound, so anything the acceptance
    # rule would allow is guaranteed to be proposed as a candidate.
    candidate_token_df_ratio: float = 0.10
    # A shared token appearing in more items than this fraction of the window is
    # too common to prove two headlines are about the same thing.
    distinctive_token_df_ratio: float = 0.05


class DigestConfig(BaseModel):
    output_dir: str | None = None
    # Optional notification target, POSTed a small JSON summary after the digest
    # is written. Off by default; a failure is reported, never fatal.
    webhook_url: str | None = None
    timezone: str = "Asia/Singapore"
    window_hours_default: int = 24
    max_top_stories: int = 5
    long_tail_limit: int = 40
    # Per-topic cap. Overflow is not dropped: it falls through to "Everything
    # else", so a broad topic cannot bury the rest of the digest.
    topic_limit: int = 8
    quiet_day_threshold: int = 3
    summary_chars: int = 260
    recency_half_life_hours: float = 18.0
    weights: RankWeights = Field(default_factory=RankWeights)
    clustering: ClusteringConfig = Field(default_factory=ClusteringConfig)
    keyword_weights: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_KEYWORD_WEIGHTS)
    )
    demote_keywords: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_DEMOTE_KEYWORDS)
    )


class Watchlist(BaseModel):
    """Entities and topics that earn a ranking boost and their own section."""

    entities: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class Settings:
    """Resolved paths, with environment overrides so tests can stay sandboxed.

    ``TELESCOPE_HOME`` relocates mutable state (database, caches) without moving
    the code. ``TELESCOPE_CONFIG_DIR`` relocates the source registry.
    """

    def __init__(
        self,
        *,
        project_root: str | Path | None = None,
        data_root: str | Path | None = None,
        config_dir: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root or PROJECT_ROOT)
        self.data_root = Path(
            data_root or os.environ.get("TELESCOPE_HOME") or self.project_root
        )
        self.config_dir = Path(
            config_dir
            or os.environ.get("TELESCOPE_CONFIG_DIR")
            or (self.project_root / "config")
        )

    @classmethod
    def load(cls) -> "Settings":
        return cls()

    @property
    def db_path(self) -> Path:
        return self.data_root / "var" / "telescope.db"

    @property
    def sources_path(self) -> Path:
        return self.config_dir / "sources.yaml"

    def load_sources(self) -> list[Source]:
        """Read and validate the source registry."""
        path = self.sources_path
        if not path.exists():
            raise ConfigError(f"source registry not found: {path}")

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

        if not isinstance(raw, list):
            raise ConfigError(f"{path} must contain a YAML list of sources")

        sources: list[Source] = []
        seen: set[str] = set()
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ConfigError(f"{path} entry #{index} is not a mapping")
            try:
                source = Source(**entry)
            except ValidationError as exc:
                raise ConfigError(f"{path} entry #{index} is invalid: {exc}") from exc
            if source.id in seen:
                raise ConfigError(f"{path} has a duplicate source id: {source.id}")
            seen.add(source.id)
            sources.append(source)
        return sources

    # -- digest configuration ---------------------------------------------

    @property
    def digest_config_path(self) -> Path:
        return self.config_dir / "digest.yaml"

    @property
    def watchlist_path(self) -> Path:
        return self.config_dir / "watchlist.yaml"

    def _read_yaml_mapping(self, path: Path) -> dict:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a YAML mapping")
        return raw

    def load_digest_config(self) -> DigestConfig:
        path = self.digest_config_path
        if path.exists():
            try:
                config = DigestConfig(**self._read_yaml_mapping(path))
            except ValidationError as exc:
                raise ConfigError(f"{path} is invalid: {exc}") from exc
        else:
            config = DigestConfig()

        # Tests and one-off runs point the output somewhere disposable.
        override = os.environ.get("TELESCOPE_DIGEST_DIR")
        if override:
            config.output_dir = override
        return config

    def load_watchlist(self) -> Watchlist:
        path = self.watchlist_path
        if not path.exists():
            return Watchlist()
        try:
            return Watchlist(**self._read_yaml_mapping(path))
        except ValidationError as exc:
            raise ConfigError(f"{path} is invalid: {exc}") from exc

    @property
    def digests_dir(self) -> Path | None:
        """Where readable digests go. The only output that syncs to Drive."""
        output_dir = self.load_digest_config().output_dir
        return Path(output_dir).expanduser() if output_dir else None
