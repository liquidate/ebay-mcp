"""Value objects modelling the slice of the eBay Browse API we consume.

These are deliberately thin: they normalise the parts of eBay's verbose JSON we
care about into typed, comparable Python objects, and they know how to serialise
themselves back to plain dictionaries for the MCP tool responses. All parsing is
defensive -- eBay omits fields freely depending on the listing -- so a malformed
or sparse item degrades to ``None`` rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _to_float(value: Any) -> float | None:
    """Parse eBay's stringly-typed money/number fields into a float."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Money:
    """A monetary amount in a single currency."""

    value: float
    currency: str

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> Money | None:
        if not data:
            return None
        value = _to_float(data.get("value"))
        currency = data.get("currency")
        if value is None or not currency:
            return None
        return cls(value=value, currency=currency)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "currency": self.currency}


@dataclass(frozen=True, slots=True)
class Seller:
    """The seller of a listing, with feedback signals when available."""

    username: str | None
    feedback_percentage: float | None
    feedback_score: int | None

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> Seller | None:
        if not data:
            return None
        score = data.get("feedbackScore")
        valid_score = isinstance(score, (int, str)) and str(score).isdigit()
        return cls(
            username=data.get("username"),
            feedback_percentage=_to_float(data.get("feedbackPercentage")),
            feedback_score=int(score) if valid_score else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "feedback_percentage": self.feedback_percentage,
            "feedback_score": self.feedback_score,
        }


@dataclass(frozen=True, slots=True)
class Item:
    """A single eBay listing summary.

    ``price`` is the listed item price; ``shipping_cost`` is the lowest-cost
    shipping option to the buyer when eBay reports one (``None`` means the cost is
    unknown -- e.g. calculated shipping with no delivery postal code configured,
    or local pickup). :attr:`total_cost` combines the two and is the figure a
    buyer actually pays, which is what the deal/market analysis works from. When
    shipping is unknown, ``total_cost`` is ``None`` rather than the bare item
    price, so an unpriced shipping charge can't make a listing look cheaper.
    """

    item_id: str
    title: str
    price: Money | None
    condition: str | None
    condition_id: str | None
    seller: Seller | None
    shipping_cost: Money | None
    free_shipping: bool
    buying_options: tuple[str, ...]
    item_web_url: str | None
    item_location_country: str | None
    image_url: str | None
    categories: tuple[str, ...]
    # Detail-only fields: eBay returns these from ``getItem`` but not from search
    # summaries, so they stay empty/None for search results.
    aspects: tuple[tuple[str, str], ...] = ()
    short_description: str | None = None
    returns_accepted: bool | None = None
    return_period_days: int | None = None
    return_shipping_paid_by: str | None = None
    available_quantity: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Item:
        shipping_cost, free_shipping = _parse_shipping(data.get("shippingOptions"))
        image = data.get("image") or {}
        thumbs = data.get("thumbnailImages") or [{}]
        returns = data.get("returnTerms") or {}
        period = returns.get("returnPeriod") or {}
        availability = (data.get("estimatedAvailabilities") or [{}])[0] or {}
        quantity = availability.get("estimatedAvailableQuantity")
        return cls(
            item_id=data.get("itemId", ""),
            title=data.get("title", ""),
            price=Money.from_api(data.get("price")),
            condition=data.get("condition"),
            condition_id=data.get("conditionId"),
            seller=Seller.from_api(data.get("seller")),
            shipping_cost=shipping_cost,
            free_shipping=free_shipping,
            buying_options=tuple(data.get("buyingOptions") or ()),
            item_web_url=data.get("itemWebUrl"),
            item_location_country=(data.get("itemLocation") or {}).get("country"),
            image_url=image.get("imageUrl") or (thumbs[0] or {}).get("imageUrl"),
            categories=tuple(
                c.get("categoryName", "")
                for c in (data.get("categories") or [])
                if c.get("categoryName")
            ),
            aspects=tuple(
                (a["name"], str(a.get("value", "")))
                for a in (data.get("localizedAspects") or [])
                if a.get("name")
            ),
            short_description=data.get("shortDescription"),
            returns_accepted=returns.get("returnsAccepted"),
            return_period_days=(
                int(period["value"])
                if str(period.get("unit", "")).endswith("DAY")
                and str(period.get("value", "")).isdigit()
                else None
            ),
            return_shipping_paid_by=returns.get("returnShippingCostPayer"),
            available_quantity=quantity if isinstance(quantity, int) else None,
        )

    @property
    def total_cost(self) -> float | None:
        """Item price plus known shipping, in the item's currency."""

        if self.price is None or self.shipping_cost is None:
            return None
        return round(self.price.value + self.shipping_cost.value, 2)

    @property
    def currency(self) -> str | None:
        return self.price.currency if self.price else None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "item_id": self.item_id,
            "title": self.title,
            "price": self.price.to_dict() if self.price else None,
            "shipping_cost": self.shipping_cost.to_dict() if self.shipping_cost else None,
            "free_shipping": self.free_shipping,
            "total_cost": self.total_cost,
            "shipping_known": self.shipping_cost is not None,
            "currency": self.currency,
            "condition": self.condition,
            "buying_options": list(self.buying_options),
            "seller": self.seller.to_dict() if self.seller else None,
            "item_location_country": self.item_location_country,
            "categories": list(self.categories),
            "item_web_url": self.item_web_url,
            "image_url": self.image_url,
        }
        details = {
            "aspects": dict(self.aspects),
            "short_description": self.short_description,
            "returns_accepted": self.returns_accepted,
            "return_period_days": self.return_period_days,
            "return_shipping_paid_by": self.return_shipping_paid_by,
            "available_quantity": self.available_quantity,
        }
        # Search summaries carry none of these; only add the block when present.
        if any(v not in (None, {}) for v in details.values()):
            out["details"] = details
        return out


def _parse_shipping(options: list[dict[str, Any]] | None) -> tuple[Money | None, bool]:
    """Return the cheapest shipping cost and whether free shipping is offered.

    eBay can list several shipping options; we take the lowest fixed cost. A
    reported cost of exactly zero is treated as free shipping.
    """

    if not options:
        return None, False
    costs = [Money.from_api(opt.get("shippingCost")) for opt in options]
    costs = [c for c in costs if c is not None]
    if not costs:
        return None, False
    cheapest = min(costs, key=lambda m: m.value)
    return cheapest, cheapest.value == 0.0


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of search results plus eBay's reported total match count."""

    total: int
    items: tuple[Item, ...]
    limit: int
    offset: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SearchResult:
        summaries = data.get("itemSummaries") or []
        return cls(
            total=int(data.get("total", 0)),
            items=tuple(Item.from_api(s) for s in summaries),
            limit=int(data.get("limit", len(summaries))),
            offset=int(data.get("offset", 0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "returned": len(self.items),
            "limit": self.limit,
            "offset": self.offset,
            "items": [item.to_dict() for item in self.items],
        }
