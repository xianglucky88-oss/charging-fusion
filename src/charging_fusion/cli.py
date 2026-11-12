import argparse
import json

from .application import reconstruct_database, run_fixture, run_url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest fixture/HTTP evidence, or reconstruct charging fragments from SQLite"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fixture", help="local JSON list fixture to ingest")
    source.add_argument("--url", help="HTTP(S) JSON endpoint to ingest")
    source.add_argument(
        "--reconstruct",
        metavar="DATABASE",
        help="rebuild fragments from an existing SQLite evidence database",
    )
    parser.add_argument("--database", help="SQLite database (required for ingest modes)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--raw-dir", help="directory for immutable raw HTTP responses")
    parser.add_argument("--records-key", help="dot-separated JSON key containing the record list")
    parser.add_argument(
        "--continuity-gap-seconds",
        type=int,
        default=900,
        help="active samples farther apart than this are separate fragments (default 900)",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="reconstruct without writing a new segment build",
    )
    args = parser.parse_args()

    if args.reconstruct:
        result = reconstruct_database(
            args.reconstruct,
            continuity_gap_seconds=args.continuity_gap_seconds,
            persist=not args.no_persist,
        )
    else:
        if not args.database:
            parser.error("--database is required when ingesting")
        if args.fixture:
            result = run_fixture(args.fixture, args.database)
        else:
            result = run_url(
                args.url,
                args.database,
                timeout=args.timeout,
                raw_dir=args.raw_dir,
                records_key=args.records_key,
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
