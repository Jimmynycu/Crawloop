from typing import ClassVar
from pydantic import BaseModel, ConfigDict, HttpUrl


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    author: str
    tags: list[str] = []
    url: HttpUrl | None = None
    VOLATILE: ClassVar[set[str]] = set()
