"""CLI entry point for pyeauidf."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, timedelta
from datetime import datetime as dt

from .client import EauIDFClient, EauIDFError, TimeStep


async def _async_main(args: argparse.Namespace) -> None:
    step_map = {
        "daily": TimeStep.DAILY,
        "weekly": TimeStep.WEEKLY,
        "monthly": TimeStep.MONTHLY,
    }

    async with EauIDFClient(args.username, args.password) as client:
        await client.login()

        end = dt.now(tz=UTC).date()
        start = end - timedelta(days=args.days)

        records = await client.get_daily_consumption(
            start_date=start,
            end_date=end,
            time_step=step_map[args.step],
        )

        if not records:
            print("No consumption data found for the given period.")
            return

        total_liters = sum(r.consumption_liters for r in records)

        print(f"Water consumption ({start} to {end}, {args.step}):\n")
        print(
            f"  {'Date':<12} {'Liters':>8}"
            f" {'Meter (m³)':>12} {'Est.':>5}",
        )
        print(f"  {'─' * 12} {'─' * 8} {'─' * 12} {'─' * 5}")
        for r in records:
            est = "yes" if r.is_estimated else ""
            print(
                f"  {r.date:%Y-%m-%d}"
                f"   {r.consumption_liters:7.0f}"
                f"  {r.meter_reading:11.3f}"
                f"  {est:>4}",
            )

        print(f"  {'─' * 12} {'─' * 8} {'─' * 12}")
        print(f"  {'Total':<12} {total_liters:7.0f}L")


def main() -> None:
    """Fetch and display water consumption data from L'eau d'Île-de-France."""
    parser = argparse.ArgumentParser(
        prog="pyeauidf",
        description="Fetch water consumption from L'eau d'Île-de-France",
    )
    parser.add_argument(
        "-u",
        "--username",
        default=os.environ.get("EAUIDF_USERNAME"),
        help="Account email (or set EAUIDF_USERNAME)",
    )
    parser.add_argument(
        "-p",
        "--password",
        default=os.environ.get("EAUIDF_PASSWORD"),
        help="Account password (or set EAUIDF_PASSWORD)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to fetch (default: 7)",
    )
    parser.add_argument(
        "--step",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="Time step (default: daily)",
    )
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error(
            "Credentials required. Use -u/-p flags or set "
            "EAUIDF_USERNAME and EAUIDF_PASSWORD environment variables.",
        )

    try:
        asyncio.run(_async_main(args))
    except EauIDFError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
