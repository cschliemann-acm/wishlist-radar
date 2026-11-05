"""Marketplace connectors. Each one turns a search query into a list of Listings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Listing:
    listing_id: str
    title: str
    price: float | None
    currency: str
    url: str
    image_url: str = ""
    size_text: str = ""
    condition: str = ""
    listed_at: float | None = None  # unix time, if the marketplace reports it


@dataclass
class SourceStatus:
    ok: bool
    message: str


class Source(Protocol):
    key: str
    label: str
    experimental: bool

    def status(self) -> SourceStatus: ...

    def search(self, query: str, *, max_price: float | None, limit: int = 50) -> list[Listing]: ...
