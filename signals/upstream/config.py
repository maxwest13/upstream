import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def parse_tier_minimums(spec: str) -> dict:
    """'inner:20,middle:5,outer:3' -> {'inner': 20, 'middle': 5, 'outer': 3}.
    Smaller sources need smaller bars before they count toward breadth."""
    out = {}
    for part in spec.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = int(v)
    return out


@dataclass(frozen=True)
class Settings:
    database_url: str
    min_mentions_24h: int
    min_authors_12h: int
    min_tier_mentions_24h: dict
    breakout_velocity: float
    min_batch_count: int
    include_words: bool
    max_mentions_per_author: float
    telegram_token: str
    telegram_chat: str
    api_token: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ["DATABASE_URL"],
            min_mentions_24h=_int("MIN_MENTIONS_24H", 40),
            min_authors_12h=_int("MIN_AUTHORS_12H", 15),
            min_tier_mentions_24h=parse_tier_minimums(
                os.environ.get("MIN_TIER_MENTIONS_24H", "inner:20,middle:5,outer:3")),
            breakout_velocity=_float("BREAKOUT_VELOCITY", 1.5),
            min_batch_count=_int("MIN_BATCH_COUNT", 3),
            include_words=os.environ.get("INCLUDE_WORDS", "true").lower() == "true",
            max_mentions_per_author=_float("MAX_MENTIONS_PER_AUTHOR", 3.0),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat=os.environ.get("TELEGRAM_CHAT_ID", ""),
            api_token=os.environ.get("API_TOKEN", ""),
        )
