#!/bin/sh
# Tests built wheels (.github/workflows/release.yml), each on the Python it's for: the smoke test
# (tests/smoke.py) in a clean environment with just the wheel and its dependencies, then the test
# suite against the wheel installed with the dev dependencies, in a venv of its own
# (.venv-<python>). Run from the repo root, with uv, and FASTPG_DSN set (the test database, which
# the smoke test needs too; FASTPG_TLS_DSN and FASTPG_TLS_CA for the TLS tests, and pytest's own
# PYTEST_ADDOPTS, as for the suite run any other way):
#   sh .github/scripts/test-wheel.sh DIR PYTHON...
# With FASTPG_TEST_OWN_SERVERS=1 (where Docker is available), the suite ignores those servers and
# runs in parallel on throwaway ones of its own, a set per worker, as `task test` does; the smoke
# test still uses FASTPG_DSN.
# With FASTPG_TIMING_PYTHON set (e.g. 3.14), only that Python runs the `timing` tests (the ones
# that wait in real time for timeouts, keepalives and stalls); the others run the rest of the
# suite. Unset, every Python runs them all.
# With FASTPG_FULL_SUITE_PYTHON set (e.g. 3.14), only that Python runs the test suite; the others
# run just the smoke test. Unset, every Python runs the suite.
# e.g. `sh .github/scripts/test-wheel.sh dist 3.14 3.14t` tests the one cp314 wheel in dist on
# Python 3.14, then the one cp314t (free-threaded) wheel on 3.14t; uv finds or downloads each.
set -eux

: "${FASTPG_DSN:?FASTPG_DSN must name the test database}"
dir=$1
shift

# The one file of the glob it's given; fails unless there's exactly one.
only() {
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "Expected one wheel, got: $*" >&2
        exit 1
    fi
    echo "$1"
}

for python; do
    tag=cp$(echo "$python" | tr -d '.t') # 3.14t: cp314
    abi=$tag${python##*[0-9]}            # 3.14t: cp314t
    wheel=$(only "$dir"/fastpg-*-"$tag"-"$abi"-*.whl)
    (
        export UV_PYTHON="$python" UV_PROJECT_ENVIRONMENT=".venv-$python"
        uv run --isolated --no-project --with "$wheel" -- python tests/smoke.py
        if [ -n "${FASTPG_FULL_SUITE_PYTHON:-}" ] && [ "$python" != "$FASTPG_FULL_SUITE_PYTHON" ]; then
            exit 0 # this subshell: on to the next Python
        fi
        uv sync --locked --no-install-project # the dev dependencies, without building fastpg
        uv pip install --python "$UV_PROJECT_ENVIRONMENT" --no-deps "$wheel"
        timing=true
        if [ -n "${FASTPG_TIMING_PYTHON:-}" ] && [ "$python" != "$FASTPG_TIMING_PYTHON" ]; then
            timing=false
        fi
        if [ "${FASTPG_TEST_OWN_SERVERS:-}" = 1 ]; then
            # As `task test`: in parallel, then the `timing` tests alone.
            env -u FASTPG_DSN -u FASTPG_TLS_DSN -u FASTPG_TLS_CA \
                uv run --no-sync pytest -ra -n auto --maxprocesses 8 --dist loadfile -m "not timing"
            if [ "$timing" = true ]; then
                env -u FASTPG_DSN -u FASTPG_TLS_DSN -u FASTPG_TLS_CA \
                    uv run --no-sync pytest -ra -m timing
            fi
        elif [ "$timing" = true ]; then
            uv run --no-sync pytest -ra
        else
            uv run --no-sync pytest -ra -m "not timing"
        fi
    )
done
