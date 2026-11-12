import argparse
import json
import sys

from .metering_application import (
    configure_from_fixture,
    ingest_readings_fixture,
    list_pending,
    recheck_pending,
    run_check,
    show_check,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Metering evidence ingestion and station-gun reconciliation")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="import meter readings from a JSON fixture")
    ingest.add_argument("--fixture", required=True)
    ingest.add_argument("--database", required=True)

    configure = commands.add_parser("configure", help="replace attribution and tolerance config from a JSON fixture")
    configure.add_argument("--config", required=True)
    configure.add_argument("--database", required=True)

    check = commands.add_parser("check", help="run and persist a station-gun reconciliation for one period")
    check.add_argument("--database", required=True)
    check.add_argument("--station", required=True, help="station-level metering point id")
    check.add_argument("--period-start", required=True)
    check.add_argument("--period-end", required=True)

    recheck = commands.add_parser("recheck", help="re-run all periods marked as needing recheck")
    recheck.add_argument("--database", required=True)

    pending = commands.add_parser("pending", help="list periods marked as needing recheck")
    pending.add_argument("--database", required=True)

    show = commands.add_parser("show", help="show a stored check with participating readings")
    show.add_argument("--database", required=True)
    show.add_argument("--station", required=True)
    show.add_argument("--period-start", required=True)
    show.add_argument("--period-end", required=True)

    args = parser.parse_args()

    if args.command == "ingest":
        result = ingest_readings_fixture(args.fixture, args.database)
    elif args.command == "configure":
        result = configure_from_fixture(args.config, args.database)
    elif args.command == "check":
        result = run_check(args.database, args.station, args.period_start, args.period_end)
    elif args.command == "recheck":
        result = recheck_pending(args.database)
    elif args.command == "pending":
        result = list_pending(args.database)
    else:
        result = show_check(args.database, args.station, args.period_start, args.period_end)
        if result is None:
            print(json.dumps({"error": "check not found", "station": args.station}, ensure_ascii=False))
            sys.exit(1)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
