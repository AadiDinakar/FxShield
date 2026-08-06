from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from main import app, fx_service
from main import UnsupportedCurrency

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_convert_endpoint(monkeypatch):
    fake = AsyncMock(
        return_value={
            "rate": Decimal("1.20"),
            "provider": "frankfurter",
            "cached": False,
            "stale": False,
        }
    )
    monkeypatch.setattr(fx_service, "get_rate", fake)

    response = client.get("/convert?from=GBP&to=EUR&amount=100")

    assert response.status_code == 200
    body = response.json()
    assert body["from_currency"] == "GBP"
    assert body["to_currency"] == "EUR"
    assert body["converted_amount"] == 120.0


def test_invalid_currency_format():
    response = client.get("/rates/GB/EUR")
    assert response.status_code == 400


def test_xxx_currency_is_rejected():
    response = client.get("/convert?from=XXX&to=GBP&amount=100")
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported currency code: XXX."


def test_provider_rejected_currency_returns_400(monkeypatch):
    fake = AsyncMock(side_effect=UnsupportedCurrency("Unsupported currency code or pair: ABC/GBP."))
    monkeypatch.setattr(fx_service, "get_rate", fake)

    response = client.get("/rates/ABC/GBP")

    assert response.status_code == 400
