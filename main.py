"""FXShield - resilient asynchronous currency-conversion API.

This single-file version keeps the full application in one place so the project
is easy to read, run, and explain while still demonstrating retries, failover,
caching, circuit breakers, testing, Docker, and CI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------


def _get_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _get_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    APP_NAME = "FXShield"
    HTTP_TIMEOUT_SECONDS = _get_float("HTTP_TIMEOUT_SECONDS", 3.0)
    RETRIES_PER_PROVIDER = _get_int("RETRIES_PER_PROVIDER", 2)
    BACKOFF_BASE_SECONDS = _get_float("BACKOFF_BASE_SECONDS", 0.25)
    CACHE_TTL_SECONDS = _get_int("CACHE_TTL_SECONDS", 60)
    STALE_CACHE_SECONDS = _get_int("STALE_CACHE_SECONDS", 3600)
    CIRCUIT_FAILURE_THRESHOLD = _get_int("CIRCUIT_FAILURE_THRESHOLD", 3)
    CIRCUIT_COOLDOWN_SECONDS = _get_int("CIRCUIT_COOLDOWN_SECONDS", 30)
    SIMULATE_PRIMARY_FAILURE = _get_bool("SIMULATE_PRIMARY_FAILURE", False)


settings = Settings()


# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------


@dataclass
class CacheEntry:
    rate: Decimal
    provider: str
    stored_at: float


class TTLCache:
    """Small in-memory cache for fresh rates and stale fallback values."""

    def __init__(self, ttl_seconds: int, stale_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.stale_seconds = stale_seconds
        self._items: dict[str, CacheEntry] = {}

    def set(self, key: str, rate: Decimal, provider: str) -> None:
        self._items[key] = CacheEntry(
            rate=rate,
            provider=provider,
            stored_at=time.monotonic(),
        )

    def get_fresh(self, key: str) -> CacheEntry | None:
        entry = self._items.get(key)
        if entry is None:
            return None

        age = time.monotonic() - entry.stored_at
        return entry if age <= self.ttl_seconds else None

    def get_stale(self, key: str) -> CacheEntry | None:
        entry = self._items.get(key)
        if entry is None:
            return None

        age = time.monotonic() - entry.stored_at
        if age <= self.stale_seconds:
            return entry

        self._items.pop(key, None)
        return None

    def size(self) -> int:
        return len(self._items)


# -----------------------------------------------------------------------------
# Circuit breaker
# -----------------------------------------------------------------------------


class CircuitBreaker:
    """Stop calling a provider temporarily after repeated technical failures."""

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: int = 30) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.failure_count = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"

        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def allow_request(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, int | str]:
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "failure_threshold": self.failure_threshold,
        }


# -----------------------------------------------------------------------------
# External providers
# -----------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Raised when an upstream provider fails for a transient/technical reason."""


class UnsupportedCurrencyError(RuntimeError):
    """Raised when a provider rejects a requested currency or pair."""


async def frankfurter_rate(
    client: httpx.AsyncClient,
    base: str,
    quote: str,
) -> Decimal:
    """Get a rate from Frankfurter, the primary provider."""

    url = f"https://api.frankfurter.dev/v2/rate/{base}/{quote}"

    try:
        response = await client.get(url)

        if response.status_code in {400, 404, 422}:
            raise UnsupportedCurrencyError(
                f"Frankfurter does not support the requested pair {base}/{quote}."
            )

        response.raise_for_status()
        data = response.json()
        return Decimal(str(data["rate"]))
    except UnsupportedCurrencyError:
        raise
    except (
        httpx.TimeoutException,
        httpx.RequestError,
        httpx.HTTPStatusError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
    ) as exc:
        raise ProviderError(f"Frankfurter failed: {exc}") from exc


async def exchange_rate_api_rate(
    client: httpx.AsyncClient,
    base: str,
    quote: str,
) -> Decimal:
    """Get a rate from ExchangeRate-API, the secondary provider."""

    url = f"https://open.er-api.com/v6/latest/{base}"

    try:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        if data.get("result") != "success":
            error_type = str(data.get("error-type", "unknown"))
            if error_type == "unsupported-code":
                raise UnsupportedCurrencyError(f"Unsupported base currency: {base}.")
            raise ProviderError(f"ExchangeRate-API returned error: {error_type}")

        rates = data.get("rates", {})
        if quote not in rates:
            raise UnsupportedCurrencyError(f"Unsupported quote currency: {quote}.")

        return Decimal(str(rates[quote]))
    except (ProviderError, UnsupportedCurrencyError):
        raise
    except (
        httpx.TimeoutException,
        httpx.RequestError,
        httpx.HTTPStatusError,
        KeyError,
        TypeError,
        ValueError,
        InvalidOperation,
    ) as exc:
        raise ProviderError(f"ExchangeRate-API failed: {exc}") from exc


# -----------------------------------------------------------------------------
# Service logic
# -----------------------------------------------------------------------------


logger = logging.getLogger(__name__)


class AllProvidersFailed(RuntimeError):
    """Raised when no live provider and no stale cache can serve a rate."""


class UnsupportedCurrency(RuntimeError):
    """Raised when the available providers reject the requested currencies."""


class FXService:
    def __init__(
        self,
        *,
        timeout_seconds: float = settings.HTTP_TIMEOUT_SECONDS,
        retries_per_provider: int = settings.RETRIES_PER_PROVIDER,
        backoff_base_seconds: float = settings.BACKOFF_BASE_SECONDS,
        cache_ttl_seconds: int = settings.CACHE_TTL_SECONDS,
        stale_cache_seconds: int = settings.STALE_CACHE_SECONDS,
        failure_threshold: int = settings.CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: int = settings.CIRCUIT_COOLDOWN_SECONDS,
        simulate_primary_failure: bool = settings.SIMULATE_PRIMARY_FAILURE,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries_per_provider = retries_per_provider
        self.backoff_base_seconds = backoff_base_seconds
        self.simulate_primary_failure = simulate_primary_failure

        self.cache = TTLCache(cache_ttl_seconds, stale_cache_seconds)
        self.breakers = {
            "frankfurter": CircuitBreaker(failure_threshold, cooldown_seconds),
            "exchange-rate-api": CircuitBreaker(failure_threshold, cooldown_seconds),
        }

    async def get_rate(self, base: str, quote: str) -> dict[str, object]:
        base = base.upper().strip()
        quote = quote.upper().strip()

        if base == quote:
            return {
                "rate": Decimal("1"),
                "provider": "identity",
                "cached": False,
                "stale": False,
            }

        cache_key = f"{base}:{quote}"
        fresh = self.cache.get_fresh(cache_key)
        if fresh is not None:
            logger.info("Fresh cache hit for %s", cache_key)
            return {
                "rate": fresh.rate,
                "provider": fresh.provider,
                "cached": True,
                "stale": False,
            }

        providers = [
            ("frankfurter", frankfurter_rate),
            ("exchange-rate-api", exchange_rate_api_rate),
        ]

        attempted_providers = 0
        unsupported_providers = 0
        transient_failure_seen = False

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            for provider_name, provider_func in providers:
                breaker = self.breakers[provider_name]

                if not breaker.allow_request():
                    logger.warning("Circuit open; skipping provider=%s", provider_name)
                    transient_failure_seen = True
                    continue

                attempted_providers += 1

                try:
                    for attempt in range(self.retries_per_provider + 1):
                        try:
                            if provider_name == "frankfurter" and self.simulate_primary_failure:
                                raise ProviderError("Simulated primary provider failure")

                            logger.info(
                                "Calling provider=%s attempt=%s pair=%s",
                                provider_name,
                                attempt + 1,
                                cache_key,
                            )
                            rate = await provider_func(client, base, quote)
                            breaker.record_success()
                            self.cache.set(cache_key, rate, provider_name)

                            return {
                                "rate": rate,
                                "provider": provider_name,
                                "cached": False,
                                "stale": False,
                            }
                        except UnsupportedCurrencyError:
                            unsupported_providers += 1
                            logger.info(
                                "Provider rejected currency pair provider=%s pair=%s",
                                provider_name,
                                cache_key,
                            )
                            break
                        except ProviderError as exc:
                            transient_failure_seen = True
                            logger.warning(
                                "Provider error provider=%s attempt=%s error=%s",
                                provider_name,
                                attempt + 1,
                                exc,
                            )
                            if attempt < self.retries_per_provider:
                                delay = self.backoff_base_seconds * (2**attempt)
                                await asyncio.sleep(delay)
                            else:
                                breaker.record_failure()
                except Exception:
                    logger.exception("Unexpected provider error provider=%s", provider_name)
                    transient_failure_seen = True
                    breaker.record_failure()

        if (
            attempted_providers == len(providers)
            and unsupported_providers == len(providers)
            and not transient_failure_seen
        ):
            raise UnsupportedCurrency(f"Unsupported currency code or pair: {base}/{quote}.")

        stale = self.cache.get_stale(cache_key)
        if stale is not None:
            logger.warning("Serving stale cache for %s because live providers failed", cache_key)
            return {
                "rate": stale.rate,
                "provider": stale.provider,
                "cached": True,
                "stale": True,
            }

        raise AllProvidersFailed(
            f"Could not get exchange rate for {base}/{quote} from any provider."
        )

    def health(self) -> dict[str, object]:
        return {
            "status": "healthy",
            "cache_entries": self.cache.size(),
            "providers": {
                name: breaker.snapshot() for name, breaker in self.breakers.items()
            },
        }


# -----------------------------------------------------------------------------
# FastAPI application
# -----------------------------------------------------------------------------


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(
    title="FXShield",
    version="2.1.0",
    description=(
        "A resilient asynchronous currency-conversion API with provider failover, "
        "retries, caching and circuit breakers."
    ),
)

fx_service = FXService()


class RateResponse(BaseModel):
    base: str
    quote: str
    rate: float
    provider: str
    cached: bool
    stale: bool


class ConversionResponse(BaseModel):
    from_currency: str
    to_currency: str
    amount: float
    rate: float
    converted_amount: float
    provider: str
    cached: bool
    stale: bool
    attribution: str | None = None


def _currency_code(value: str) -> str:
    value = value.upper().strip()
    if len(value) != 3 or not value.isalpha():
        raise HTTPException(
            status_code=400,
            detail="Currency codes must be three letters, for example GBP, EUR or USD.",
        )

    # ISO 4217 reserves XXX for "no currency" and XTS for testing.
    if value in {"XXX", "XTS"}:
        raise HTTPException(status_code=400, detail=f"Unsupported currency code: {value}.")

    return value


def _money(value: Decimal) -> float:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "name": settings.APP_NAME,
        "message": "FXShield is running. Open /docs for interactive API documentation.",
        "endpoints": [
            "/health",
            "/rates/GBP/EUR",
            "/convert?from=GBP&to=EUR&amount=100",
        ],
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return fx_service.health()


@app.get("/rates/{base}/{quote}", response_model=RateResponse)
async def rate(
    base: str = Path(..., examples=["GBP"]),
    quote: str = Path(..., examples=["EUR"]),
) -> RateResponse:
    base = _currency_code(base)
    quote = _currency_code(quote)

    try:
        result = await fx_service.get_rate(base, quote)
    except UnsupportedCurrency as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AllProvidersFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return RateResponse(
        base=base,
        quote=quote,
        rate=float(result["rate"]),
        provider=str(result["provider"]),
        cached=bool(result["cached"]),
        stale=bool(result["stale"]),
    )


@app.get("/convert", response_model=ConversionResponse)
async def convert(
    from_currency: str = Query(
        "GBP", alias="from", description="Three-letter source currency code"
    ),
    to_currency: str = Query(
        "EUR", alias="to", description="Three-letter target currency code"
    ),
    amount: Decimal = Query(Decimal("100"), gt=0, description="Positive amount to convert"),
) -> ConversionResponse:
    source = _currency_code(from_currency)
    target = _currency_code(to_currency)

    try:
        result = await fx_service.get_rate(source, target)
    except UnsupportedCurrency as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AllProvidersFailed as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    rate_value = Decimal(str(result["rate"]))
    converted = amount * rate_value
    provider = str(result["provider"])

    attribution = None
    if provider == "exchange-rate-api":
        attribution = "Rates By Exchange Rate API — https://www.exchangerate-api.com"

    return ConversionResponse(
        from_currency=source,
        to_currency=target,
        amount=_money(amount),
        rate=float(rate_value),
        converted_amount=_money(converted),
        provider=provider,
        cached=bool(result["cached"]),
        stale=bool(result["stale"]),
        attribution=attribution,
    )
