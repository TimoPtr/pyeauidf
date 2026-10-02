# Copyright (c) 2026 Timothy (TimoPtr)
"""Python client for L'eau d'Île-de-France (SEDIF) water consumption data."""

from .client import (
    ConsumptionData,
    ConsumptionRecord,
    Contract,
    EauIDFClient,
    TimeStep,
)

__all__ = [
    "ConsumptionData",
    "ConsumptionRecord",
    "Contract",
    "EauIDFClient",
    "TimeStep",
]
