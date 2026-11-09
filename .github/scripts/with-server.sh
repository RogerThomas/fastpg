#!/bin/sh
# Runs a command against test server N: the one on port 55432+N (set N of
# .github/scripts/test-servers.sh, or a native-postgres.sh on that port) as FASTPG_DSN, and with
# FASTPG_TEST_TLS=1 its TLS-only one on 55532+N as FASTPG_TLS_DSN and FASTPG_TLS_CA. Run from the
# repo root:
#   sh .github/scripts/with-server.sh 1 uv run --no-sync pytest
# FASTPG_TEST_PORT moves the first port, as for test-servers.sh.
set -eu

n=$1
shift
port=$((${FASTPG_TEST_PORT:-55432} + n))
export FASTPG_DSN="postgresql://fastpg:fastpg@127.0.0.1:$port/fastpg"
if [ "${FASTPG_TEST_TLS:-}" = 1 ]; then
    # localhost: the name in the server's certificate.
    export FASTPG_TLS_DSN="postgresql://fastpg:fastpg@localhost:$((port + 100))/fastpg"
    export FASTPG_TLS_CA=docker/tls/certs/ca.crt
fi
exec "$@"
