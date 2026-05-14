# pyeauidf

Python client for [L'eau d'Île-de-France](https://connexion.leaudiledefrance.fr) (SEDIF) water consumption data.

Fetches daily, weekly, or monthly water consumption history directly via the Salesforce Aura API — no browser or Selenium required.

## Disclaimer

This integration relies on scraping the SEDIF customer portal. It is not based on an official API, so any change to the website's structure or authentication flow may break it without notice.

This integration was built with the help of [Claude](https://claude.ai) (Anthropic).

## Home Assistant

A ready-to-use Home Assistant integration built on top of this library is available: [ha_eauidf](https://github.com/TimoPtr/ha_eauidf).

## Installation

```bash
pip install .
```

Requires Python 3.10+ and `aiohttp`.

## Usage

### CLI

```bash
# Last 7 days (default)
pyeauidf -u email@example.com -p password

# Custom number of days
pyeauidf -u email@example.com -p password --days 30

# Weekly aggregation
pyeauidf -u email@example.com -p password --days 90 --step weekly

# Credentials via environment variables
export EAUIDF_USERNAME=email@example.com
export EAUIDF_PASSWORD=password
pyeauidf
```

### Python

```python
import asyncio
from pyeauidf import EauIDFClient
from pyeauidf.client import TimeStep
from datetime import date, timedelta

async def main():
    async with EauIDFClient("email@example.com", "password") as client:
        await client.login()

        # Daily consumption (last 90 days by default)
        records = await client.get_daily_consumption()
        for r in records:
            print(f"{r.date:%Y-%m-%d}: {r.consumption_liters:.0f}L")

        # Weekly or monthly
        records = await client.get_daily_consumption(time_step=TimeStep.WEEKLY)

        # Custom date range
        records = await client.get_daily_consumption(
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 1),
        )

asyncio.run(main())
```

### Data model

Each `ConsumptionRecord` contains:

| Field | Type | Description |
|---|---|---|
| `date` | `datetime` | Timestamp of the reading |
| `consumption_liters` | `float` | Water consumed (liters) |
| `meter_reading` | `float` | Cumulative meter reading (m³) |
| `is_estimated` | `bool` | Whether the value is estimated |

## How it works

The library authenticates against the Salesforce Experience Cloud portal that powers L'eau d'Île-de-France, then calls the same Aura API endpoints that the website uses:

1. Login via `LightningLoginFormController`
2. Follow `frontdoor.jsp` to establish session cookies
3. Call Apex actions (`LTN015_ICL_ContratConsoHisto.getData`) for consumption data
