from typing import ClassVar
from decimal import Decimal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    price: Decimal = Field(gt=0, lt=1_000_000)
    currency: str = Field(pattern=r"^[A-Z]{3}$", default="GBP")
    in_stock: bool
    url: HttpUrl
    image_url: HttpUrl | None = None
    VOLATILE: ClassVar[set[str]] = {"price", "in_stock"}
