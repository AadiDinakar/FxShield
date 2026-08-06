from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from main import ProviderError, UnsupportedCurrencyError
from main import AllProvidersFailed, FXService, UnsupportedCurrency


@pytest.mark.asyncio
async def test_primary_provider_success(monkeypatch):
    primary = AsyncMock(return_value=Decimal("1.25"))
    secondary = AsyncMock(return_value=Decimal("9.99"))
    monkeypatch.setattr("main.frankfurter_rate", primary)
    monkeypatch.setattr("main.exchange_rate_api_rate", secondary)

    service = FXService(retries_per_provider=0)
    result = await service.get_rate("GBP", "EUR")

    assert result["rate"] == Decimal("1.25")
    assert result["provider"] == "frankfurter"
    assert result["cached"] is False
    primary.assert_awaited_once()
    secondary.assert_not_awaited()


@pytest.mark.asyncio
async def test_failover_to_secondary(monkeypatch):
    primary = AsyncMock(side_effect=ProviderError("primary is down"))
    secondary = AsyncMock(return_value=Decimal("1.20"))
    monkeypatch.setattr("main.frankfurter_rate", primary)
    monkeypatch.setattr("main.exchange_rate_api_rate", secondary)

    service = FXService(retries_per_provider=0)
    result = await service.get_rate("GBP", "EUR")

    assert result["rate"] == Decimal("1.20")
    assert result["provider"] == "exchange-rate-api"
    primary.assert_awaited_once()
    secondary.assert_awaited_once()


@pytest.mark.asyncio
async def test_cache_prevents_second_api_call(monkeypatch):
    primary = AsyncMock(return_value=Decimal("1.23"))
    monkeypatch.setattr("main.frankfurter_rate", primary)

    service = FXService(retries_per_provider=0, cache_ttl_seconds=60)
    first = await service.get_rate("GBP", "EUR")
    second = await service.get_rate("GBP", "EUR")

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["rate"] == Decimal("1.23")
    assert primary.await_count == 1


@pytest.mark.asyncio
async def test_retry_then_success(monkeypatch):
    primary = AsyncMock(side_effect=[ProviderError("temporary"), Decimal("1.22")])
    secondary = AsyncMock(return_value=Decimal("9.99"))
    monkeypatch.setattr("main.frankfurter_rate", primary)
    monkeypatch.setattr("main.exchange_rate_api_rate", secondary)

    service = FXService(retries_per_provider=1, backoff_base_seconds=0)
    result = await service.get_rate("GBP", "EUR")

    assert result["provider"] == "frankfurter"
    assert primary.await_count == 2
    secondary.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_cache_is_used_when_providers_fail(monkeypatch):
    failure = AsyncMock(side_effect=ProviderError("down"))
    monkeypatch.setattr("main.frankfurter_rate", failure)
    monkeypatch.setattr("main.exchange_rate_api_rate", failure)

    service = FXService(
        retries_per_provider=0,
        cache_ttl_seconds=0,
        stale_cache_seconds=60,
    )
    service.cache.set("GBP:EUR", Decimal("1.19"), "frankfurter")

    result = await service.get_rate("GBP", "EUR")

    assert result["rate"] == Decimal("1.19")
    assert result["cached"] is True
    assert result["stale"] is True


@pytest.mark.asyncio
async def test_unsupported_pair_from_both_providers(monkeypatch):
    unsupported = AsyncMock(side_effect=UnsupportedCurrencyError("unsupported"))
    monkeypatch.setattr("main.frankfurter_rate", unsupported)
    monkeypatch.setattr("main.exchange_rate_api_rate", unsupported)

    service = FXService(retries_per_provider=0)

    with pytest.raises(UnsupportedCurrency):
        await service.get_rate("ABC", "GBP")


@pytest.mark.asyncio
async def test_all_providers_fail_without_cache(monkeypatch):
    failure = AsyncMock(side_effect=ProviderError("down"))
    monkeypatch.setattr("main.frankfurter_rate", failure)
    monkeypatch.setattr("main.exchange_rate_api_rate", failure)

    service = FXService(retries_per_provider=0)

    with pytest.raises(AllProvidersFailed):
        await service.get_rate("GBP", "EUR")
