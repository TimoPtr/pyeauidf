"""Client for L'eau d'Ile-de-France (SEDIF) water consumption API."""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import certifi
import requests


BASE_URL = "https://connexion.leaudiledefrance.fr/espace-particuliers"
_INTERMEDIATE_CERT = Path(__file__).parent / "gandi_intermediate.pem"
AURA_URL = f"{BASE_URL}/s/sfsites/aura"
LOGIN_URL = f"{BASE_URL}/s/login/"

LOGIN_APP = "siteforce:loginApp2"
COMMUNITY_APP = "siteforce:communityApp"


class TimeStep(str, Enum):
    DAILY = "JOURNEE"
    WEEKLY = "SEMAINE"
    MONTHLY = "MOIS"


@dataclass
class ConsumptionRecord:
    date: datetime
    consumption_liters: float
    meter_reading: float
    is_estimated: bool

    @classmethod
    def from_api(cls, raw: dict) -> ConsumptionRecord:
        return cls(
            date=datetime.strptime(raw["DATE_INDEX"], "%Y-%m-%d %H:%M:%S"),
            consumption_liters=float(raw["CONSOMMATION"]) * 1000,
            meter_reading=float(raw["VALEUR_INDEX"]),
            is_estimated=str(raw.get("FLAG_ESTIMATION", "")).lower() in ("true", "1", "yes"),
        )


class EauIDFError(Exception):
    pass


class AuthenticationError(EauIDFError):
    pass


def _build_ca_bundle() -> str:
    """Create a CA bundle with the missing Gandi intermediate cert."""
    intermediate = _INTERMEDIATE_CERT.read_text()
    roots = Path(certifi.where()).read_text()
    bundle = tempfile.NamedTemporaryFile(
        suffix=".pem", prefix="pyeauidf_ca_", delete=False
    )
    bundle.write((intermediate + "\n" + roots).encode())
    bundle.close()
    return bundle.name


_CA_BUNDLE = _build_ca_bundle()


class EauIDFClient:
    """Client to fetch water consumption data from L'eau d'Ile-de-France."""

    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            "Origin": "https://connexion.leaudiledefrance.fr",
        })
        # The server doesn't send the intermediate CA cert, so we use
        # a custom CA bundle: certifi roots + the bundled Gandi intermediate.
        self._session.verify = _CA_BUNDLE
        self._fwuid: str | None = None
        self._aura_token: str | None = None
        self._app_loaded: dict | None = None
        self._authenticated = False

    def _get_login_context(self) -> None:
        """Fetch the login page to extract fwuid and app context."""
        resp = self._session.get(LOGIN_URL)
        resp.raise_for_status()
        html = resp.text

        # Extract fwuid from the page scripts
        match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', html)
        if match:
            self._fwuid = match.group(1)

        # Extract the loginApp2 loaded hash
        match = re.search(
            r'"APPLICATION@markup://siteforce:loginApp2"\s*:\s*"([^"]+)"', html
        )
        if match:
            self._app_loaded = {
                "APPLICATION@markup://siteforce:loginApp2": match.group(1)
            }

        if not self._fwuid:
            raise AuthenticationError("Could not extract fwuid from login page")

    def _extract_aura_token(self) -> str | None:
        """Extract the CSRF token from the __Host-ERIC cookie set by Salesforce."""
        for cookie in self._session.cookies:
            if "ERIC" in cookie.name:
                return cookie.value
        return None

    def _build_aura_context(self, app: str = COMMUNITY_APP) -> dict:
        loaded_key = f"APPLICATION@markup://{app}"
        loaded = {}
        if self._app_loaded:
            # Reuse whatever loaded hash we have
            for k, v in self._app_loaded.items():
                loaded[k] = v
        return {
            "mode": "PROD",
            "fwuid": self._fwuid,
            "app": app,
            "loaded": loaded,
            "dn": [],
            "globals": {},
            "uad": True,
        }

    def _aura_call_raw(
        self,
        actions: list[dict],
        app: str = COMMUNITY_APP,
        page_uri: str = "/espace-particuliers/s/",
    ) -> dict:
        """Make an Aura API call and return the full JSON response."""
        # Build query string descriptor hints
        descriptors = []
        for a in actions:
            desc = a.get("descriptor", "")
            if "ApexActionController" in desc:
                descriptors.append("aura.ApexAction.execute=1")
            else:
                short = desc.split("/")[-1].replace("ACTION$", ".")
                descriptors.append(f"other.{short}=1")

        query_parts = ["r=0"]
        seen = set()
        for d in descriptors:
            if d not in seen:
                query_parts.append(d)
                seen.add(d)

        url = f"{AURA_URL}?{'&'.join(query_parts)}"

        resp = self._session.post(
            url,
            data={
                "message": json.dumps({"actions": actions}),
                "aura.context": json.dumps(self._build_aura_context(app)),
                "aura.pageURI": page_uri,
                "aura.token": self._aura_token or "undefined",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        resp.raise_for_status()

        result = resp.json()

        # Update context if returned
        ctx = result.get("context", {})
        if ctx.get("fwuid"):
            self._fwuid = ctx["fwuid"]
        loaded = ctx.get("loaded")
        if loaded:
            self._app_loaded = loaded

        return result

    def _aura_call(
        self,
        actions: list[dict],
        **kwargs: Any,
    ) -> list[dict]:
        """Make an Aura API call and return just the action results."""
        return self._aura_call_raw(actions, **kwargs).get("actions", [])

    def _apex_action(
        self,
        classname: str,
        method: str,
        params: dict | None = None,
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

        results = self._aura_call([action], **kwargs)
        if not results:
            raise EauIDFError(f"No response for {classname}.{method}")

        result = results[0]
        if result.get("state") != "SUCCESS":
            error = result.get("error", [])
            raise EauIDFError(
                f"{classname}.{method} failed: {error}"
            )

        rv = result.get("returnValue", {})
        # Apex actions wrap in an extra returnValue
        if isinstance(rv, dict) and "returnValue" in rv:
            return rv["returnValue"]
        return rv

    def login(self) -> None:
        """Authenticate with the L'eau d'Ile-de-France portal."""
        self._get_login_context()

        # Call the Aura login action
        action = {
            "id": "1;a",
            "descriptor": "apex://LightningLoginFormController/ACTION$login",
            "callingDescriptor": "UNKNOWN",
            "params": {
                "username": self._username,
                "password": self._password,
                "startUrl": "/espace-particuliers/s/",
            },
        }

        response = self._aura_call_raw(
            [action],
            app=LOGIN_APP,
            page_uri="/espace-particuliers/s/login",
        )

        actions = response.get("actions", [])
        if actions:
            result = actions[0]
            if result.get("state") != "SUCCESS":
                raise AuthenticationError(f"Login failed: {result.get('error', [])}")
            # returnValue is an error message on failure, None on success
            return_value = result.get("returnValue")
            if isinstance(return_value, str) and return_value:
                raise AuthenticationError(f"Login failed: {return_value}")

        self._complete_login(response)

    def _complete_login(self, login_response: dict) -> None:
        """Follow the frontdoor redirect and extract session tokens."""
        # Extract the redirect URL from aura:clientRedirect event
        events = login_response.get("events", [])
        redirect_url = None
        for event in events:
            if event.get("descriptor") == "markup://aura:clientRedirect":
                redirect_url = event["attributes"]["values"]["url"]
                break

        if not redirect_url:
            raise AuthenticationError("No redirect URL in login response")

        # Follow frontdoor.jsp to establish session cookies (sid, etc.)
        resp = self._session.get(redirect_url)
        resp.raise_for_status()

        # Load the authenticated community page to get the app context
        resp = self._session.get(f"{BASE_URL}/s/")
        resp.raise_for_status()
        html = resp.text

        # Extract the CSRF token from the __Host-ERIC cookie
        self._aura_token = self._extract_aura_token()
        if not self._aura_token:
            raise AuthenticationError("Could not extract CSRF token after login")

        # Extract communityApp loaded hash
        match = re.search(
            r'"APPLICATION@markup://siteforce:communityApp"\s*:\s*"([^"]+)"', html
        )
        if match:
            self._app_loaded = {
                "APPLICATION@markup://siteforce:communityApp": match.group(1)
            }

        # Update fwuid if available
        match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', html)
        if match:
            self._fwuid = match.group(1)

        self._authenticated = True

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            self.login()

    def get_contracts(self) -> list[str]:
        """Get list of active contract IDs."""
        self._ensure_authenticated()
        result = self._apex_action(
            "LTN009_ICL_ContratsGroupements",
            "listCurrentUserActiveContrats",
        )
        if isinstance(result, list):
            return result
        return []

    def get_contract_details(self, contract_id: str) -> dict:
        """Get details for a contract including meter number and PDS ID."""
        self._ensure_authenticated()
        return self._apex_action(
            "LTN008_ICL_ContratDetails",
            "getContratDetails",
            params={"contratId": contract_id},
        )

    def get_daily_consumption(
        self,
        contract_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        time_step: TimeStep = TimeStep.DAILY,
    ) -> list[ConsumptionRecord]:
        """
        Fetch water consumption history.

        If contract_id is not provided, uses the first active contract.
        Defaults to last 90 days if dates are not specified.
        """
        self._ensure_authenticated()

        if end_date is None:
            end_date = date.today()
        if start_date is None:
            start_date = end_date - timedelta(days=90)

        # Get contract ID if not provided
        if contract_id is None:
            contracts = self.get_contracts()
            if not contracts:
                raise EauIDFError("No active contracts found")
            contract_id = contracts[0]

        # Get contract details for meter number and PDS ID
        details = self.get_contract_details(contract_id)
        compte_info = details.get("compteInfo", [])
        if not compte_info:
            raise EauIDFError("No meter information found for contract")

        meter = compte_info[0]
        numero_compteur = meter["ELEMB"]
        id_pds = meter["ELEMA"]

        # Fetch consumption data
        result = self._apex_action(
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

        records = []
        data = result.get("data", {})
        for raw in data.get("CONSOMMATION", []):
            records.append(ConsumptionRecord.from_api(raw))

        return records

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> EauIDFClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
