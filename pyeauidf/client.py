"""Client for L'eau d'Ile-de-France (SEDIF) water consumption API."""

from __future__ import annotations

import enum
import json
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import aiohttp
import certifi

BASE_URL = "https://connexion.leaudiledefrance.fr"
_INTERMEDIATE_CERT = Path(__file__).parent / "gandi_intermediate.pem"
AURA_URL = f"{BASE_URL}/s/sfsites/aura"
LOGIN_URL = f"{BASE_URL}/s/login/"

LOGIN_APP = "siteforce:loginApp2"
COMMUNITY_APP = "siteforce:communityApp"

_LOGIN_APP2_RE = re.compile(
    r'"APPLICATION@markup://siteforce:loginApp2"\s*:\s*"([^"]+)"',
)
_COMMUNITY_APP_RE = re.compile(
    r'"APPLICATION@markup://siteforce:communityApp"\s*:\s*"([^"]+)"',
)
_FWUID_RE = re.compile(r'"fwuid"\s*:\s*"([^"]+)"')

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)

_ESTIMATION_TRUTHY = frozenset(("true", "1", "yes"))


class TimeStep(enum.StrEnum):
    """Time granularity for consumption queries."""

    DAILY = "JOURNEE"
    WEEKLY = "SEMAINE"
    MONTHLY = "MOIS"


@dataclass
class ConsumptionRecord:
    """A single consumption measurement returned by the API."""

    date: datetime
    consumption_liters: float
    meter_reading: float
    is_estimated: bool

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ConsumptionRecord:
        """Parse a raw API record dict into a ConsumptionRecord."""
        return cls(
            date=datetime.strptime(
                raw["DATE_INDEX"],
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=UTC),
            consumption_liters=float(raw["CONSOMMATION"]) * 1000,
            meter_reading=float(raw["VALEUR_INDEX"]),
            is_estimated=str(raw.get("FLAG_ESTIMATION", "")).lower()
            in _ESTIMATION_TRUTHY,
        )


class EauIDFError(Exception):
    """Base exception for pyeauidf errors."""


class AuthenticationError(EauIDFError):
    """Raised when authentication with the portal fails."""


def _build_ssl_context() -> ssl.SSLContext:
    """Create an SSL context with the missing Gandi intermediate cert."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.load_verify_locations(cafile=str(_INTERMEDIATE_CERT))
    return ctx


_SSL_CONTEXT = _build_ssl_context()


class EauIDFClient:
    """Client to fetch water consumption data from L'eau d'Ile-de-France."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the client with credentials and an optional session."""
        self._username = username
        self._password = password
        self._external_session = session is not None
        self._session = session or aiohttp.ClientSession(
            headers={
                "User-Agent": _USER_AGENT,
                "Origin": BASE_URL,
            },
        )
        self._fwuid: str | None = None
        self._aura_token: str | None = None
        self._app_loaded: dict[str, str] | None = None
        self._authenticated = False

    async def _get_login_context(self) -> None:
        """Fetch the login page to extract fwuid and app context."""
        async with self._session.get(LOGIN_URL, ssl=_SSL_CONTEXT) as resp:
            resp.raise_for_status()
            html = await resp.text()

        match = _FWUID_RE.search(html)
        if match:
            self._fwuid = match.group(1)

        match = _LOGIN_APP2_RE.search(html)
        if match:
            self._app_loaded = {
                "APPLICATION@markup://siteforce:loginApp2": match.group(1),
            }

        if not self._fwuid:
            msg = "Could not extract fwuid from login page"
            raise AuthenticationError(msg)

    def _extract_aura_token(self) -> str | None:
        """Extract the CSRF token from the __Host-ERIC cookie."""
        for cookie in self._session.cookie_jar:
            if "ERIC" in cookie.key:
                return cookie.value
        return None

    def _build_aura_context(
        self,
        app: str = COMMUNITY_APP,
    ) -> dict[str, Any]:
        loaded = dict(self._app_loaded) if self._app_loaded else {}
        return {
            "mode": "PROD",
            "fwuid": self._fwuid,
            "app": app,
            "loaded": loaded,
            "dn": [],
            "globals": {},
            "uad": True,
        }

    async def _aura_call_raw(
        self,
        actions: list[dict[str, Any]],
        app: str = COMMUNITY_APP,
        page_uri: str = "/espace-particuliers/s/",
    ) -> dict[str, Any]:
        """Make an Aura API call and return the full JSON response."""
        descriptors = []
        for a in actions:
            desc = a.get("descriptor", "")
            if "ApexActionController" in desc:
                descriptors.append("aura.ApexAction.execute=1")
            else:
                short = desc.split("/")[-1].replace("ACTION$", ".")
                descriptors.append(f"other.{short}=1")

        query_parts = ["r=0"]
        seen: set[str] = set()
        for d in descriptors:
            if d not in seen:
                query_parts.append(d)
                seen.add(d)

        url = f"{AURA_URL}?{'&'.join(query_parts)}"

        data = aiohttp.FormData()
        data.add_field("message", json.dumps({"actions": actions}))
        data.add_field(
            "aura.context",
            json.dumps(self._build_aura_context(app)),
        )
        data.add_field("aura.pageURI", page_uri)
        data.add_field("aura.token", self._aura_token or "undefined")

        headers = {
            "Content-Type": (
                "application/x-www-form-urlencoded; charset=UTF-8"
            ),
        }
        async with self._session.post(
            url,
            data=data,
            headers=headers,
            ssl=_SSL_CONTEXT,
        ) as resp:
            resp.raise_for_status()
            result: dict[str, Any] = await resp.json(
                content_type=None,
            )

        ctx = result.get("context", {})
        if ctx.get("fwuid"):
            self._fwuid = ctx["fwuid"]
        loaded = ctx.get("loaded")
        if loaded:
            self._app_loaded = loaded

        return result

    async def _aura_call(
        self,
        actions: list[dict[str, Any]],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Make an Aura API call and return just the action results."""
        raw = await self._aura_call_raw(actions, **kwargs)
        result: list[dict[str, Any]] = raw.get("actions", [])
        return result

    async def _apex_action(
        self,
        classname: str,
        method: str,
        params: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Call a single Apex action and return its returnValue."""
        action_params: dict[str, Any] = {
            "namespace": "",
            "classname": classname,
            "method": method,
            "cacheable": False,
            "isContinuation": False,
        }
        if params:
            action_params["params"] = params

        action = {
            "id": "1;a",
            "descriptor": "aura://ApexActionController/ACTION$execute",
            "callingDescriptor": "UNKNOWN",
            "params": action_params,
        }

        results = await self._aura_call([action], **kwargs)
        if not results:
            msg = f"No response for {classname}.{method}"
            raise EauIDFError(msg)

        result = results[0]
        if result.get("state") != "SUCCESS":
            error = result.get("error", [])
            msg = f"{classname}.{method} failed: {error}"
            raise EauIDFError(msg)

        rv = result.get("returnValue", {})
        if isinstance(rv, dict) and "returnValue" in rv:
            return rv["returnValue"]
        return rv

    async def login(self) -> None:
        """Authenticate with the L'eau d'Ile-de-France portal."""
        await self._get_login_context()

        action = {
            "id": "1;a",
            "descriptor": (
                "apex://LightningLoginFormController/ACTION$login"
            ),
            "callingDescriptor": "UNKNOWN",
            "params": {
                "username": self._username,
                "password": self._password,
                "startUrl": "/espace-particuliers/s/",
            },
        }

        response = await self._aura_call_raw(
            [action],
            app=LOGIN_APP,
            page_uri="/espace-particuliers/s/login",
        )

        actions = response.get("actions", [])
        if actions:
            result = actions[0]
            if result.get("state") != "SUCCESS":
                msg = f"Login failed: {result.get('error', [])}"
                raise AuthenticationError(msg)
            return_value = result.get("returnValue")
            if isinstance(return_value, str) and return_value:
                msg = f"Login failed: {return_value}"
                raise AuthenticationError(msg)

        await self._complete_login(response)

    async def _complete_login(
        self,
        login_response: dict[str, Any],
    ) -> None:
        """Follow the frontdoor redirect and extract session tokens."""
        events = login_response.get("events", [])
        redirect_url = None
        for event in events:
            if event.get("descriptor") == "markup://aura:clientRedirect":
                redirect_url = event["attributes"]["values"]["url"]
                break

        if not redirect_url:
            msg = "No redirect URL in login response"
            raise AuthenticationError(msg)

        async with self._session.get(
            redirect_url,
            ssl=_SSL_CONTEXT,
        ) as resp:
            resp.raise_for_status()

        async with self._session.get(
            f"{BASE_URL}/s/",
            ssl=_SSL_CONTEXT,
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()

        self._aura_token = self._extract_aura_token()
        if not self._aura_token:
            msg = "Could not extract CSRF token after login"
            raise AuthenticationError(msg)

        match = _COMMUNITY_APP_RE.search(html)
        if match:
            self._app_loaded = {
                "APPLICATION@markup://siteforce:communityApp": (
                    match.group(1)
                ),
            }

        match = _FWUID_RE.search(html)
        if match:
            self._fwuid = match.group(1)

        self._authenticated = True

    async def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            await self.login()

    async def get_contracts(self) -> list[str]:
        """Get list of active contract IDs."""
        await self._ensure_authenticated()
        result = await self._apex_action(
            "LTN009_ICL_ContratsGroupements",
            "listCurrentUserActiveContrats",
        )
        if isinstance(result, list):
            return result
        return []

    async def get_contract_details(
        self,
        contract_id: str,
    ) -> dict[str, Any]:
        """Get details for a contract including meter number and PDS ID."""
        await self._ensure_authenticated()
        result: dict[str, Any] = await self._apex_action(
            "LTN008_ICL_ContratDetails",
            "getContratDetails",
            params={"contratId": contract_id},
        )
        return result

    async def get_daily_consumption(
        self,
        contract_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        time_step: TimeStep = TimeStep.DAILY,
    ) -> list[ConsumptionRecord]:
        """Fetch water consumption history."""
        await self._ensure_authenticated()

        if end_date is None:
            end_date = datetime.now(tz=UTC).date()
        if start_date is None:
            start_date = end_date - timedelta(days=90)

        if contract_id is None:
            contracts = await self.get_contracts()
            if not contracts:
                msg = "No active contracts found"
                raise EauIDFError(msg)
            contract_id = contracts[0]

        details = await self.get_contract_details(contract_id)
        compte_info = details.get("compteInfo", [])
        if not compte_info:
            msg = "No meter information found for contract"
            raise EauIDFError(msg)

        meter = compte_info[0]
        numero_compteur = meter["ELEMB"]
        id_pds = meter["ELEMA"]

        result = await self._apex_action(
            "LTN015_ICL_ContratConsoHisto",
            "getData",
            params={
                "contractId": contract_id,
                "TYPE_PAS": time_step.value,
                "DATE_DEBUT": start_date.isoformat(),
                "DATE_FIN": end_date.isoformat(),
                "NUMERO_COMPTEUR": numero_compteur,
                "ID_PDS": id_pds,
            },
            page_uri="/espace-particuliers/s/historique",
        )

        data = result.get("data", {})
        records = [
            ConsumptionRecord.from_api(raw)
            for raw in data.get("CONSOMMATION", [])
        ]

        return records

    async def close(self) -> None:
        """Close the HTTP session (only if we created it)."""
        if not self._external_session:
            await self._session.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
