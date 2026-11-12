#!/bin/sh
# Create the least-privilege runtime role and the DDL-capable migration role.
#
# Runs once on first initialisation of the Postgres volume
# (/docker-entrypoint-initdb.d). CI runs the same script against its service
# container, so the role split is exercised there too.
#
# The app role can only DML (SELECT/INSERT/UPDATE/DELETE) on tables the
# migration role owns; it cannot create tables, indexes or types.
set -eu

: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${APP_DB_USER:?APP_DB_USER is required}"
: "${APP_DB_PASSWORD:?APP_DB_PASSWORD is required}"
: "${MIGRATE_DB_USER:?MIGRATE_DB_USER is required}"
: "${MIGRATE_DB_PASSWORD:?MIGRATE_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  -v db_name="$POSTGRES_DB" \
  -v app_user="$APP_DB_USER" \
  -v app_password="$APP_DB_PASSWORD" \
  -v migrate_user="$MIGRATE_DB_USER" \
  -v migrate_password="$MIGRATE_DB_PASSWORD" <<'SQL'
-- The migration role is not a superuser, so the extension is installed here by
-- the bootstrap superuser. `CREATE EXTENSION IF NOT EXISTS` in the first
-- migration then becomes a no-op.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE :"migrate_user" LOGIN PASSWORD :'migrate_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- The migrator owns the schema and therefore may run DDL.
ALTER SCHEMA public OWNER TO :"migrate_user";

-- Nobody gets schema-wide CREATE implicitly; the app role is DML-only.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_user";

GRANT CONNECT ON DATABASE :"db_name" TO :"app_user", :"migrate_user";

-- Objects created by future migrations (run as the migrator) are automatically
-- usable by the app role.
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrate_user" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrate_user" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_user";
SQL
