import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    pubsub_topic: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(pubsub_topic=os.environ.get("SPEND_CAP_PUBSUB_TOPIC") or None)
