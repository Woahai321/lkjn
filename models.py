from pydantic import BaseModel, field_validator
from typing import Optional, List, Dict, Any, Literal

MediaType = Literal["movie", "tv"]

class Media(BaseModel):
    media_type: MediaType
    status: str
    imdbId: Optional[str] = None
    tmdbId: int
    tvdbId: Optional[int] = None

    @field_validator("imdbId", mode="after")
    @classmethod
    def stringify_imdb_id(cls, value: Any) -> Optional[str]:
        if value and isinstance(value, int):
            return f"tt{int(value):07d}"
        return value

    @field_validator("tvdbId", "tmdbId", mode="before")
    @classmethod
    def validate_ids(cls, value: Any) -> Optional[int]:
        if value in ("", None):
            return None  # Convert empty string to None
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return value

class OverseerrWebhook(BaseModel):
    notification_type: str
    event: str
    subject: str
    message: Optional[str] = None
    image: Optional[str] = None
    media: Media
    extra: List[Dict[str, Any]] = []
