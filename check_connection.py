import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

load_dotenv()

from config import Config


def check_db_connection():
    """Run a minimal query and return whether the configured database is reachable."""
    print("--- Starting database connection check ---")

    db_uri = Config.SQLALCHEMY_DATABASE_URI
    db_url = make_url(db_uri)
    target = db_url.database or "(default)"
    if db_url.host:
        target = f"{db_url.host}:{db_url.port or '(default)'}/{target}"
    print(f"Target: {target} | Driver: {db_url.drivername}")

    engine_opts = getattr(Config, "SQLALCHEMY_ENGINE_OPTIONS", {})
    engine = None
    try:
        engine = create_engine(db_uri, **engine_opts)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        print("Database connection OK.")
        return True
    except Exception as exc:
        print("Database connection failed.")
        print(f"Error detail: {exc}")
        return False
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(0 if check_db_connection() else 1)
