# FXShield

FXShield is a resilient asynchronous currency-conversion API built with Python and FastAPI. It uses two external exchange-rate providers and is designed to keep serving requests when an upstream provider is slow or unavailable.

## What the project demonstrates

- FastAPI REST API development
- asynchronous HTTP requests with `httpx`
- primary/secondary provider failover
- retries with exponential backoff
- in-memory TTL caching and stale-cache fallback
- circuit-breaker behaviour for repeated provider failures
- input/error handling for invalid or unsupported currencies
- automated tests with `pytest`
- Docker containerisation
- GitHub Actions CI

The project intentionally does **not** include Terraform, Jenkins, or deployment shell scripts. The focus is backend engineering, reliability, testing, and CI.

## Project structure

```text
FXShield/
├── main.py                 # complete FastAPI application and resilience logic
├── tests/                  # automated tests
├── requirements.txt        # runtime dependencies
├── requirements-dev.txt    # testing and linting dependencies
├── Dockerfile
├── .env.example
└── .github/workflows/ci.yml
```

The application is intentionally kept in a single `main.py` file for a small portfolio project. In a larger production system, the configuration, provider adapters, cache, service logic, and API routes would normally be split into modules.

## Architecture

```text
Client
  |
  v
FXShield / FastAPI
  |
  +--> fresh cache? ------> return cached rate
  |
  v
Frankfurter (primary)
  |
  +--> success -----------> cache + return
  |
  +--> temporary failure -> retry with backoff
  |
  v
ExchangeRate-API (secondary)
  |
  +--> success -----------> cache + return
  |
  +--> failure -----------> stale cache if available
                              |
                              +--> otherwise HTTP 503
```

Repeated technical failures can open a provider's circuit breaker temporarily. Unsupported currency input is treated as a client error instead of a provider outage.

## Run locally

Requires Python 3.12+.

```bash
python -m venv .venv
```

Linux/macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements-dev.txt
```

Run tests:

```bash
pytest -q tests
```

Start the API:

```bash
uvicorn main:app --reload
```

Open Swagger documentation:

```text
http://127.0.0.1:8000/docs
```

## Endpoints

```text
GET /
GET /health
GET /rates/{base}/{quote}
GET /convert?from=GBP&to=EUR&amount=100
```

Examples:

```bash
curl "http://127.0.0.1:8000/rates/USD/GBP"
curl "http://127.0.0.1:8000/convert?from=EUR&to=JPY&amount=100"
```

`XXX` and `XTS` are rejected immediately. Other three-letter codes are passed to the providers; if both providers reject the pair as unsupported, FXShield returns HTTP 400.

## Demonstrate failover

Linux/macOS:

```bash
export SIMULATE_PRIMARY_FAILURE=true
uvicorn main:app --reload
```

Windows PowerShell:

```powershell
$env:SIMULATE_PRIMARY_FAILURE="true"
uvicorn main:app --reload
```

Then make a normal conversion request. FXShield will intentionally fail the primary provider and try the secondary provider.

## Docker

Build:

```bash
docker build -t fxshield .
```

Run:

```bash
docker run --rm -p 8000:8000 fxshield
```

## Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and pull requests. It:

1. installs dependencies
2. runs Ruff linting
3. runs pytest
4. builds the Docker image

## External data sources

FXShield does not generate exchange rates itself. It requests rates from:

- Frankfurter (primary): `https://api.frankfurter.dev`
- ExchangeRate-API Open Access (secondary): `https://open.er-api.com`

When the secondary provider supplies a rate, the API response includes the required ExchangeRate-API attribution.
