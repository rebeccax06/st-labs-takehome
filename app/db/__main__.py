"""Run `python -m app.db` to initialize the configured local SQLite schema."""

from app.db.session import create_sqlite_engine, initialize_database


def main() -> None:
    engine = create_sqlite_engine()
    try:
        initialize_database(engine)
        print("CMDB database initialized (existing tables preserved).")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
