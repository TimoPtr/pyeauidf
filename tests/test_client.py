import re
from datetime import UTC, date, datetime

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from pyeauidf.client import (
    _USER_AGENT,
    BASE_URL,
    LOGIN_URL,
    AuthenticationError,
    ConsumptionData,
    ConsumptionRecord,
    EauIDFClient,
    EauIDFError,
)

AURA_URL_RE = re.compile(r"^https://connexion\.leaudiledefrance\.fr/s/sfsites/aura\b")

# ---------------------------------------------------------------------------
# ConsumptionRecord.from_api
# ---------------------------------------------------------------------------


def test_from_api_parses_fields() -> None:
    raw = {
        "DATE_INDEX": "2024-03-15 00:00:00",
        "CONSOMMATION": "0.123",
        "VALEUR_INDEX": "1234.567",
        "FLAG_ESTIMATION": "false",
    }
    record = ConsumptionRecord.from_api(raw)
    assert record.date == datetime(2024, 3, 15, tzinfo=UTC)
    assert record.consumption_liters == pytest.approx(123.0)
    assert record.meter_reading == pytest.approx(1234.567)
    assert record.is_estimated is False


@pytest.mark.parametrize("flag", ["true", "True", "TRUE", "1", "yes", "Yes"])
def test_from_api_estimated_truthy(flag: str) -> None:
    raw = {
        "DATE_INDEX": "2024-01-01 00:00:00",
        "CONSOMMATION": "0",
        "VALEUR_INDEX": "0",
        "FLAG_ESTIMATION": flag,
    }
    assert ConsumptionRecord.from_api(raw).is_estimated is True


@pytest.mark.parametrize("flag", ["false", "0", "no", "", None])
def test_from_api_estimated_falsy(flag: str | None) -> None:
    raw: dict[str, str] = {
        "DATE_INDEX": "2024-01-01 00:00:00",
        "CONSOMMATION": "0",
        "VALEUR_INDEX": "0",
    }
    if flag is not None:
        raw["FLAG_ESTIMATION"] = flag
    assert ConsumptionRecord.from_api(raw).is_estimated is False


def test_from_api_consumption_converts_m3_to_liters() -> None:
    raw = {
        "DATE_INDEX": "2024-01-01 00:00:00",
        "CONSOMMATION": "1.5",
        "VALEUR_INDEX": "0",
    }
    assert ConsumptionRecord.from_api(raw).consumption_liters == pytest.approx(
        1500.0,
    )


# ---------------------------------------------------------------------------
# _get_login_context
# ---------------------------------------------------------------------------

_LOGIN_HTML = """
<html><script>
var data = {"fwuid":"abc123","APPLICATION@markup://siteforce:loginApp2":"hash456"};
</script></html>
"""

_LOGIN_HTML_NO_FWUID = "<html><body>nothing here</body></html>"


@pytest.mark.asyncio
async def test_get_login_context_extracts_fwuid() -> None:
    async with EauIDFClient("user", "pass") as client:
        with aioresponses() as m:
            m.get(LOGIN_URL, body=_LOGIN_HTML, status=200)
            await client._get_login_context()
            assert client._fwuid == "abc123"
            assert client._app_loaded == {
                "APPLICATION@markup://siteforce:loginApp2": "hash456",
            }


@pytest.mark.asyncio
async def test_get_login_context_raises_without_fwuid() -> None:
    async with EauIDFClient("user", "pass") as client:
        with aioresponses() as m:
            m.get(LOGIN_URL, body=_LOGIN_HTML_NO_FWUID, status=200)
            with pytest.raises(AuthenticationError, match="fwuid"):
                await client._get_login_context()


# ---------------------------------------------------------------------------
# _extract_aura_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_aura_token_found() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._session.cookie_jar.update_cookies(
            {"__Host-ERIC-123": "token_value"},
            URL("https://connexion.leaudiledefrance.fr"),
        )
        assert client._extract_aura_token() == "token_value"


@pytest.mark.asyncio
async def test_extract_aura_token_missing() -> None:
    async with EauIDFClient("user", "pass") as client:
        assert client._extract_aura_token() is None


# ---------------------------------------------------------------------------
# _build_aura_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_aura_context_defaults() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._fwuid = "fw1"
        ctx = client._build_aura_context()
        assert ctx["mode"] == "PROD"
        assert ctx["fwuid"] == "fw1"
        assert ctx["loaded"] == {}


@pytest.mark.asyncio
async def test_build_aura_context_with_loaded() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._fwuid = "fw1"
        client._app_loaded = {
            "APP@markup://siteforce:communityApp": "hash1",
        }
        ctx = client._build_aura_context()
        assert ctx["loaded"] == {
            "APP@markup://siteforce:communityApp": "hash1",
        }


# ---------------------------------------------------------------------------
# _apex_action error handling
# ---------------------------------------------------------------------------

_AURA_RESPONSE_FAILED = {
    "actions": [
        {"state": "ERROR", "error": [{"message": "Something broke"}]},
    ],
}

_AURA_RESPONSE_SUCCESS = {
    "actions": [
        {"state": "SUCCESS", "returnValue": {"returnValue": ["contract-1"]}},
    ],
}


@pytest.mark.asyncio
async def test_apex_action_raises_on_error_state() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._authenticated = True
        client._fwuid = "fw1"
        with aioresponses() as m:
            m.post(
                AURA_URL_RE,
                payload=_AURA_RESPONSE_FAILED,
                status=200,
                repeat=True,
            )
            with pytest.raises(EauIDFError, match="failed"):
                await client._apex_action("SomeClass", "someMethod")


@pytest.mark.asyncio
async def test_apex_action_unwraps_nested_return_value() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._authenticated = True
        client._fwuid = "fw1"
        with aioresponses() as m:
            m.post(
                AURA_URL_RE,
                payload=_AURA_RESPONSE_SUCCESS,
                status=200,
                repeat=True,
            )
            result = await client._apex_action("SomeClass", "someMethod")
            assert result == ["contract-1"]


# ---------------------------------------------------------------------------
# login flow
# ---------------------------------------------------------------------------

_COMMUNITY_HTML = """
<html><script>
var data = {"fwuid":"fw_community","APPLICATION@markup://siteforce:communityApp":"comm_hash"};
</script></html>
"""

_LOGIN_AURA_RESPONSE = {
    "actions": [{"state": "SUCCESS", "returnValue": None}],
    "events": [
        {
            "descriptor": "markup://aura:clientRedirect",
            "attributes": {
                "values": {"url": "https://example.com/frontdoor"},
            },
        },
    ],
}


@pytest.mark.asyncio
async def test_login_success() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._session.cookie_jar.update_cookies(
            {"__Host-ERIC-abc": "csrf_token"},
            URL("https://connexion.leaudiledefrance.fr"),
        )
        with aioresponses() as m:
            m.get(LOGIN_URL, body=_LOGIN_HTML, status=200)
            m.post(
                AURA_URL_RE,
                payload=_LOGIN_AURA_RESPONSE,
                status=200,
                repeat=True,
            )
            m.get(
                "https://example.com/frontdoor",
                body="",
                status=200,
            )
            m.get(f"{BASE_URL}/s/", body=_COMMUNITY_HTML, status=200)

            await client.login()

            assert client._authenticated is True
            assert client._fwuid == "fw_community"
            assert client._aura_token == "csrf_token"  # noqa: S105


@pytest.mark.asyncio
async def test_login_raises_on_action_failure() -> None:
    async with EauIDFClient("user", "pass") as client:
        with aioresponses() as m:
            m.get(LOGIN_URL, body=_LOGIN_HTML, status=200)
            m.post(
                AURA_URL_RE,
                payload={
                    "actions": [{"state": "ERROR", "error": []}],
                    "events": [],
                },
                status=200,
                repeat=True,
            )

            with pytest.raises(AuthenticationError, match="Login failed"):
                await client.login()


@pytest.mark.asyncio
async def test_login_raises_when_no_redirect() -> None:
    async with EauIDFClient("user", "pass") as client:
        with aioresponses() as m:
            m.get(LOGIN_URL, body=_LOGIN_HTML, status=200)
            m.post(
                AURA_URL_RE,
                payload={
                    "actions": [
                        {"state": "SUCCESS", "returnValue": None},
                    ],
                    "events": [],
                },
                status=200,
                repeat=True,
            )

            with pytest.raises(AuthenticationError, match="redirect"):
                await client.login()


# ---------------------------------------------------------------------------
# Headers with external session
# ---------------------------------------------------------------------------


def _get_request_headers(
    mock: aioresponses,
    method: str,
    url_pattern: re.Pattern[str],
) -> dict[str, str]:
    """Extract headers from the first matching request in the mock."""
    for (req_method, req_url), calls in mock.requests.items():
        if req_method == method and url_pattern.search(str(req_url)):
            return dict(calls[0].kwargs.get("headers", {}))
    msg = f"No {method} request matching {url_pattern.pattern}"
    raise AssertionError(msg)


@pytest.mark.asyncio
async def test_external_session_sends_origin_and_user_agent_on_get() -> None:
    external = aiohttp.ClientSession()
    try:
        async with EauIDFClient("user", "pass", session=external) as client:
            with aioresponses() as m:
                m.get(LOGIN_URL, body=_LOGIN_HTML, status=200)
                await client._get_login_context()

                headers = _get_request_headers(
                    m,
                    "GET",
                    re.compile(re.escape(LOGIN_URL)),
                )
                assert headers.get("User-Agent") == _USER_AGENT
                assert headers.get("Origin") == BASE_URL
    finally:
        await external.close()


@pytest.mark.asyncio
async def test_external_session_sends_origin_and_user_agent_on_post() -> None:
    external = aiohttp.ClientSession()
    try:
        async with EauIDFClient("user", "pass", session=external) as client:
            client._authenticated = True
            client._fwuid = "fw1"
            with aioresponses() as m:
                m.post(
                    AURA_URL_RE,
                    payload=_AURA_RESPONSE_SUCCESS,
                    status=200,
                    repeat=True,
                )
                await client._apex_action("SomeClass", "someMethod")

                headers = _get_request_headers(m, "POST", AURA_URL_RE)
                assert headers.get("User-Agent") == _USER_AGENT
                assert headers.get("Origin") == BASE_URL
    finally:
        await external.close()


# ---------------------------------------------------------------------------
# get_daily_consumption → ConsumptionData
# ---------------------------------------------------------------------------

_CONTRACTS_RESPONSE = {
    "actions": [
        {"state": "SUCCESS", "returnValue": {"returnValue": ["contract-1"]}},
    ],
}

_CONTRACT_DETAILS_RESPONSE = {
    "actions": [
        {
            "state": "SUCCESS",
            "returnValue": {
                "returnValue": {
                    "compteInfo": [{"ELEMB": "CTR-001", "ELEMA": "PDS-001"}],
                },
            },
        },
    ],
}

_GET_DATA_RESPONSE = {
    "actions": [
        {
            "state": "SUCCESS",
            "returnValue": {
                "returnValue": {
                    "prixMoyenEau": 4.2345,
                    "data": {
                        "CONSOMMATION": [
                            {
                                "DATE_INDEX": "2024-03-15 00:00:00",
                                "CONSOMMATION": "0.150",
                                "VALEUR_INDEX": "100.000",
                                "FLAG_ESTIMATION": "false",
                            },
                            {
                                "DATE_INDEX": "2024-03-16 00:00:00",
                                "CONSOMMATION": "0.200",
                                "VALEUR_INDEX": "100.200",
                                "FLAG_ESTIMATION": "true",
                            },
                        ],
                        "CONSOMMATION_MAX": 0.250,
                        "CONSOMMATION_MOYENNE": 0.175,
                        "DATE_CONSOMMATION_MAX": "2024-03-16",
                    },
                },
            },
        },
    ],
}


@pytest.mark.asyncio
async def test_get_daily_consumption_returns_consumption_data() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._authenticated = True
        client._fwuid = "fw1"
        with aioresponses() as m:
            m.post(AURA_URL_RE, payload=_CONTRACTS_RESPONSE, status=200)
            m.post(AURA_URL_RE, payload=_CONTRACT_DETAILS_RESPONSE, status=200)
            m.post(AURA_URL_RE, payload=_GET_DATA_RESPONSE, status=200)

            result = await client.get_daily_consumption(
                start_date=date(2024, 3, 15),
                end_date=date(2024, 3, 17),
            )

            assert isinstance(result, ConsumptionData)
            assert len(result.records) == 2
            assert result.records[0].consumption_liters == pytest.approx(150.0)
            assert result.records[1].is_estimated is True
            assert result.price_per_m3 == pytest.approx(4.2345)


@pytest.mark.asyncio
async def test_get_daily_consumption_no_contracts_raises() -> None:
    async with EauIDFClient("user", "pass") as client:
        client._authenticated = True
        client._fwuid = "fw1"
        with aioresponses() as m:
            m.post(
                AURA_URL_RE,
                payload={
                    "actions": [
                        {"state": "SUCCESS", "returnValue": {"returnValue": []}},
                    ],
                },
                status=200,
            )

            with pytest.raises(EauIDFError, match="No active contracts"):
                await client.get_daily_consumption()


@pytest.mark.asyncio
async def test_get_daily_consumption_handles_missing_max_date() -> None:
    response = {
        "actions": [
            {
                "state": "SUCCESS",
                "returnValue": {
                    "returnValue": {
                        "prixMoyenEau": 3.5,
                        "data": {
                            "CONSOMMATION": [],
                            "CONSOMMATION_MAX": 0,
                            "CONSOMMATION_MOYENNE": 0,
                        },
                    },
                },
            },
        ],
    }
    async with EauIDFClient("user", "pass") as client:
        client._authenticated = True
        client._fwuid = "fw1"
        with aioresponses() as m:
            m.post(AURA_URL_RE, payload=_CONTRACTS_RESPONSE, status=200)
            m.post(AURA_URL_RE, payload=_CONTRACT_DETAILS_RESPONSE, status=200)
            m.post(AURA_URL_RE, payload=response, status=200)

            result = await client.get_daily_consumption(
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 2),
            )

            assert result.records == []
            assert result.price_per_m3 == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# ConsumptionData.daily_cost / total_cost
# ---------------------------------------------------------------------------


def test_daily_cost_computes_from_price_and_liters() -> None:
    record = ConsumptionRecord(
        date=datetime(2024, 5, 14, tzinfo=UTC),
        consumption_liters=103.0,
        meter_reading=100.0,
        is_estimated=False,
    )
    data = ConsumptionData(
        records=[record],
        price_per_m3=4.6602,
    )
    assert data.daily_cost(record) == pytest.approx(0.48, abs=0.005)


def test_total_cost_sums_all_records() -> None:
    records = [
        ConsumptionRecord(
            date=datetime(2024, 5, 14, tzinfo=UTC),
            consumption_liters=150.0,
            meter_reading=100.0,
            is_estimated=False,
        ),
        ConsumptionRecord(
            date=datetime(2024, 5, 15, tzinfo=UTC),
            consumption_liters=200.0,
            meter_reading=100.2,
            is_estimated=False,
        ),
    ]
    data = ConsumptionData(
        records=records,
        price_per_m3=4.0,
    )
    # (0.150 + 0.200) * 4.0 = 1.40
    assert data.total_cost == pytest.approx(1.40)
