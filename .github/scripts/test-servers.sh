#!/bin/sh
# Starts a set of test servers per IMAGE, for test runs that go at once, each against servers of
# its own (.github/scripts/with-server.sh N runs a command against set N): set N (from 0) is the
# compose `postgres` service on port 55432+N and its TLS-only `postgres-tls` on 55532+N, as
# compose project fastpg-test-N. `default` is docker-compose.yml's image. Run from the repo root,
# with Docker:
#   sh .github/scripts/test-servers.sh default default postgres:15.19-trixie
# FASTPG_TEST_PORT moves the first port (55432); the TLS ones stay 100 above.
# The first set starts alone: its TLS server writes the certificates (docker/tls/certs) that the
# others then reuse. The rest start at once.
set -eu

base=${FASTPG_TEST_PORT:-55432}

# Set $1 on image $2.
start() {
    image=$2
    [ "$image" != default ] || image=""
    FASTPG_PG_IMAGE=$image FASTPG_PG_PORT=$((base + $1)) FASTPG_TLS_PORT=$((base + 100 + $1)) \
        docker compose --project-name "fastpg-test-$1" --profile tls \
        up --detach --wait --quiet-pull postgres postgres-tls
}

n=0
pids=""
for image; do
    if [ "$n" -eq 0 ]; then
        start "$n" "$image"
    else
        start "$n" "$image" &
        pids="$pids $!"
    fi
    n=$((n + 1))
done
failed=0
for pid in $pids; do
    wait "$pid" || failed=1
done
exit "$failed"
