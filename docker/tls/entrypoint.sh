#!/bin/sh
# Entry point of the TLS-only PostgreSQL used by tests/test_tls.py (compose service
# `postgres-tls`, profile `tls`; see notes/tls.md for running it by hand).
#
# On first start it creates a throwaway CA, a server certificate for `localhost` and a
# client certificate for the role `fastpg_cert` in /certs (a bind mount: tests read the CA
# and client files from there; nothing in it is committed). The CA key is ECDSA P-384 signing
# with SHA-384, so SCRAM channel binding exercises a non-SHA-256 `tls-server-end-point` hash.
# The server's files are then copied to where PostgreSQL accepts them (owned by postgres,
# key mode 0600), and the official image's entry point starts the server with TLS on.
set -eu

certs=/certs
own=/etc/postgresql-tls

if [ ! -f "$certs/ca.crt" ]; then
    cd "$certs"
    openssl req -x509 -new -nodes -days 30 -sha384 \
        -newkey ec -pkeyopt ec_paramgen_curve:P-384 \
        -subj "/CN=fastpg test CA" -keyout ca.key -out ca.crt
    openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -subj "/CN=localhost" -keyout server.key -out server.csr
    printf '%s\n' "subjectAltName=DNS:localhost,DNS:postgres-tls" \
        "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
        "extendedKeyUsage=serverAuth" > server.ext
    openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days 30 -sha384 -extfile server.ext -out server.crt
    openssl req -new -nodes -newkey ec -pkeyopt ec_paramgen_curve:P-256 \
        -subj "/CN=fastpg_cert" -keyout client.key -out client.csr
    printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
        "extendedKeyUsage=clientAuth" > client.ext
    openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
        -days 30 -sha384 -extfile client.ext -out client.crt
    rm -f ./*.csr ./*.ext ./*.srl
    # Throwaway test credentials, readable by the (host) test user.
    chmod 0644 ./*
fi

install -d -o postgres -g postgres -m 0700 "$own"
install -o postgres -g postgres -m 0600 "$certs/server.key" "$own/server.key"
install -o postgres -g postgres -m 0644 "$certs/server.crt" "$certs/ca.crt" \
    /tls/pg_hba.conf "$own/"

exec docker-entrypoint.sh postgres \
    -c ssl=on \
    -c ssl_cert_file="$own/server.crt" \
    -c ssl_key_file="$own/server.key" \
    -c ssl_ca_file="$own/ca.crt" \
    -c hba_file="$own/pg_hba.conf" \
    "$@"
