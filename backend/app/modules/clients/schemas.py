from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Phone = Annotated[str, Field(pattern=r"^\+?[0-9 ()-]{7,20}$")]


class ClientProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    full_name: str
    phone: str | None
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    pincode: str | None
    avatar_url: str | None = Field(default=None, description="Short-lived signed URL")


class ClientProfileUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    phone: Phone | None = None
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=2, max_length=120)
    state: str | None = Field(default=None, min_length=2, max_length=120)
    pincode: str | None = Field(default=None, pattern=r"^[A-Za-z0-9 -]{3,12}$")
