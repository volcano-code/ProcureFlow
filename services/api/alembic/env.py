from logging.config import fileConfig
from alembic import context
from sqlalchemy import create_engine, pool
from procureflow.config import Settings
from procureflow.db import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    # Tests may provide a connection confined to a disposable schema.
    migrate(config.attributes["connection"])
else:
    engine = create_engine(Settings().database_url, poolclass=pool.NullPool, hide_parameters=True)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
