from datetime import datetime

import pytest
import responses as rsps

from pyeauidf.client import (
    AURA_URL,
    BASE_URL,
    LOGIN_URL,
    AuthenticationError,
    ConsumptionRecord,
    EauIDFClient,
    EauIDFError,
)

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
    assert record.date == datetime(2024, 3, 15)
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
    raw: dict[str, str] = {"DATE_INDEX": "2024-01-01 00:00:00", "CONSOMMATION": "0", "VALEUR_INDEX": "0"}
    if flag is not None:
        raw["FLAG_ESTIMATION"] = flag
    assert ConsumptionRecord.from_api(raw).is_estimated is False


def test_from_api_consumption_converts_m3_to_liters() -> None:
    raw = {"DATE_INDEX": "2024-01-01 00:00:00", "CONSOMMATION": "1.5", "VALEUR_INDEX": "0"}
    assert ConsumptionRecord.from_api(raw).consumption_liters == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# _get_login_context
# ---------------------------------------------------------------------------

_LOGIN_HTML = """
<html><script>
var data = {"fwuid":"abc123","APPLICATION@markup://siteforce:loginApp2":"hash456"};
</script></html>
"""

_LOGIN_HTML_NO_FWUID = "<html><body>nothing here</body></html>"


@rsps.activate
def test_get_login_context_extracts_fwuid() -> None:
    rsps.add(rsps.GET, LOGIN_URL, body=_LOGIN_HTML, status=200)
    client = EauIDFClient("user", "pass")
    client._get_login_context()
    assert client._fwuid == "abc123"
    assert client._app_loaded == {"APPLICATION@markup://siteforce:loginApp2": "hash456"}


@rsps.activate
def test_get_login_context_raises_without_fwuid() -> None:
    rsps.add(rsps.GET, LOGIN_URL, body=_LOGIN_HTML_NO_FWUID, status=200)
    client = EauIDFClient("user", "pass")
    with pytest.raises(AuthenticationError, match="fwuid"):
        client._get_login_context()


# ---------------------------------------------------------------------------
# _extract_aura_token
# ---------------------------------------------------------------------------


def test_extract_aura_token_found() -> None:
    client = EauIDFClient("user", "pass")
    client._session.cookies.set("__Host-ERIC-123", "token_value", domain="connexion.leaudiledefrance.fr")
    assert client._extract_aura_token() == "token_value"


def test_extract_aura_token_missing() -> None:
    client = EauIDFClient("user", "pass")
    assert client._extract_aura_token() is None


# ---------------------------------------------------------------------------
# _build_aura_context
# ---------------------------------------------------------------------------


def test_build_aura_context_defaults() -> None:
    client = EauIDFClient("user", "pass")
    client._fwuid = "fw1"
    ctx = client._build_aura_context()
    assert ctx["mode"] == "PROD"
    assert ctx["fwuid"] == "fw1"
    assert ctx["loaded"] == {}


def test_build_aura_context_with_loaded() -> None:
    client = EauIDFClient("user", "pass")
    client._fwuid = "fw1"
    client._app_loaded = {"APP@markup://siteforce:communityApp": "hash1"}
    ctx = client._build_aura_context()
    assert ctx["loaded"] == {"APP@markup://siteforce:communityApp": "hash1"}


# ---------------------------------------------------------------------------
# _apex_action error handling
# ---------------------------------------------------------------------------

_AURA_RESPONSE_FAILED = {
    "actions": [{"state": "ERROR", "error": [{"message": "Something broke"}]}],
}

_AURA_RESPONSE_SUCCESS = {
    "actions": [{"state": "SUCCESS", "returnValue": {"returnValue": ["contract-1"]}}],
}


@rsps.activate
def test_apex_action_raises_on_error_state() -> None:
    rsps.add(rsps.POST, AURA_URL, json=_AURA_RESPONSE_FAILED, status=200)
    client = EauIDFClient("user", "pass")
    client._authenticated = True
    client._fwuid = "fw1"
    with pytest.raises(EauIDFError, match="failed"):
        client._apex_action("SomeClass", "someMethod")


@rsps.activate
def test_apex_action_unwraps_nested_return_value() -> None:
    rsps.add(rsps.POST, AURA_URL, json=_AURA_RESPONSE_SUCCESS, status=200)
    client = EauIDFClient("user", "pass")
    client._authenticated = True
    client._fwuid = "fw1"
    result = client._apex_action("SomeClass", "someMethod")
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
            "attributes": {"values": {"url": "https://example.com/frontdoor"}},
        }
    ],
}


@rsps.activate
def test_login_success() -> None:
    rsps.add(rsps.GET, LOGIN_URL, body=_LOGIN_HTML, status=200)
    rsps.add(rsps.POST, AURA_URL, json=_LOGIN_AURA_RESPONSE, status=200)
    rsps.add(rsps.GET, "https://example.com/frontdoor", body="", status=200)
    rsps.add(rsps.GET, f"{BASE_URL}/s/", body=_COMMUNITY_HTML, status=200)

    client = EauIDFClient("user", "pass")
    client._session.cookies.set("__Host-ERIC-abc", "csrf_token", domain="connexion.leaudiledefrance.fr")
    client.login()

    assert client._authenticated is True
    assert client._fwuid == "fw_community"
    assert client._aura_token == "csrf_token"


@rsps.activate
def test_login_raises_on_action_failure() -> None:
    rsps.add(rsps.GET, LOGIN_URL, body=_LOGIN_HTML, status=200)
    rsps.add(
        rsps.POST,
        AURA_URL,
        json={"actions": [{"state": "ERROR", "error": []}], "events": []},
        status=200,
    )

    client = EauIDFClient("user", "pass")
    with pytest.raises(AuthenticationError, match="Login failed"):
        client.login()


@rsps.activate
def test_login_raises_when_no_redirect() -> None:
    rsps.add(rsps.GET, LOGIN_URL, body=_LOGIN_HTML, status=200)
    rsps.add(
        rsps.POST,
        AURA_URL,
        json={"actions": [{"state": "SUCCESS", "returnValue": None}], "events": []},
        status=200,
    )

    client = EauIDFClient("user", "pass")
    with pytest.raises(AuthenticationError, match="redirect"):
        client.login()
