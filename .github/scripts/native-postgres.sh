#!/bin/sh
# Starts a PostgreSQL of the runner's own (no Docker: the macOS runners have none), set up like
# the compose `postgres` service: user, password and database `fastpg`, SCRAM over TCP, the
# schema and seed of docker/initdb, and its server flags (some tests depend on them, e.g. on
# work_mem). The DSN is then postgresql://fastpg:fastpg@127.0.0.1:PORT/fastpg. Run from the repo
# root, with PostgreSQL's bin directory on PATH:
#   sh .github/scripts/native-postgres.sh DATA_DIR PORT
set -eu

data=$1
port=$2

pwfile=$(mktemp)
trap 'rm -f "$pwfile"' EXIT
printf 'fastpg\n' > "$pwfile"
initdb --pgdata "$data" --username fastpg --pwfile "$pwfile" --encoding UTF8 --locale C \
    --auth-local trust --auth-host scram-sha-256 > /dev/null

# docker-compose.yml's flags, on TCP only, and in UTC as the image is.
flags="port=$port listen_addresses=127.0.0.1 unix_socket_directories= timezone=UTC
    max_connections=1000 superuser_reserved_connections=5 shared_buffers=2GB
    effective_cache_size=6GB work_mem=32MB maintenance_work_mem=512MB max_worker_processes=16
    max_parallel_workers=8 jit=off fsync=off synchronous_commit=off full_page_writes=off
    wal_level=minimal max_wal_senders=0 max_wal_size=4GB"
# On one line: pg_ctl runs the server through a shell, which would end the command at a newline.
options=""
for flag in $flags; do
    options="$options -c $flag"
done
pg_ctl --pgdata "$data" --log "$data/server.log" --wait --options "$options" start \
    || { cat "$data/server.log"; exit 1; }

export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=fastpg PGPASSWORD=fastpg
createdb fastpg
for script in docker/initdb/*.sql; do
    psql --quiet --no-psqlrc --set ON_ERROR_STOP=1 --dbname fastpg --file "$script"
done
