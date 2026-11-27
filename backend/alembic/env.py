from logging.config import fileConfig

from sqlalchemy import engine_from_config, text
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from database.models import Base
target_metadata = Base.metadata

def get_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL not set in .env")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    configuration = config.get_section(config.config_ini_section, {})
    url = get_url()
    configuration["sqlalchemy.url"] = url

    # Fail fast if the DB (e.g. the Supabase pooler) is unreachable at deploy
    # time. Without a connect_timeout, psycopg2/libpq can block on the TCP/SSL
    # handshake for minutes — long enough to blow Cloud Run's container-startup
    # window even though the Dockerfile now runs `alembic upgrade heads` as a
    # best-effort step that must NOT gate gunicorn. A short bounded connect (plus
    # TCP keepalives, matching database.py) makes `alembic upgrade heads` return
    # quickly on a network blip so gunicorn still comes up and the app self-heals.
    engine_kwargs = {"poolclass": pool.NullPool}
    if not url.startswith("sqlite"):
        engine_kwargs["connect_args"] = {
            "connect_timeout": 10,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        **engine_kwargs,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            # Disable statement timeout for migrations (crucial for large tables like 'activities')
            connection.execute(text("SET statement_timeout = 0"))
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
