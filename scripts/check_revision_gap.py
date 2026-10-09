"""Print a read-only, counts-first snapshot for ; never fetch external data."""

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from load_forecast.eco2mix import connect_database


def main() -> None:
    statements = Path(__file__).with_name("revision_gap_counts.sql").read_text().split(";")
    try:
        with connect_database() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            for statement in statements:
                if not statement.strip():
                    continue
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                    print(" | ".join(column.name for column in cursor.description))
                    for row in cursor:
                        print(" | ".join(str(value) for value in row))
                    print()
    except (psycopg.Error, KeyError, ValueError):
        raise SystemExit("Counts failed; check database availability and environment.") from None


if __name__ == "__main__":
    main()
