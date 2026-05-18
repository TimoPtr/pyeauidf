"""Python client for L'eau d'Île-de-France (SEDIF) water consumption data."""

from .client import ConsumptionData, ConsumptionRecord, EauIDFClient, TimeStep

__all__ = ["ConsumptionData", "ConsumptionRecord", "EauIDFClient", "TimeStep"]
