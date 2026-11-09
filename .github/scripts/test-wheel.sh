#!/bin/sh
# Tests built wheels (.github/workflows/release.yml), each on the Python it's for, all Pythons at
# once (.github/scripts/parallel.sh), each against a test server of its own: the Nth PYTHON (from
# 0) against server N (.github/scripts/with-server.sh N: started by test-servers.sh, or by
# native-postgres.sh on port 55432+N; FASTPG_TEST_TLS=1 for the TLS-only ones). On each Python,
# the smoke test (tests/smoke.py) in a clean environment with just the wheel and its
# dependencies, then the test suite, serially, without the `timing` tests, against the wheel
# installed with the dev dependencies, in a venv of its own (.venv-<python>). Then, with
# FASTPG_TIMING_PYTHON set to one of the PYTHONs (e.g. 3.14), that one runs the `timing` tests
# (the ones that wait in real time for timeouts, keepalives and stalls) alone. pytest's own
# PYTEST_ADDOPTS applies, as for the suite run any other way. Run from the repo root, with uv:
#   sh .github/scripts/test-wheel.sh DIR PYTHON...
# e.g. `sh .github/scripts/test-wheel.sh dist 3.14 3.14t` tests the one cp314 wheel in dist on
# Python 3.14 against server 0, and the one cp314t (free-threaded) wheel on 3.14t against server
# 1, at once; uv finds or downloads each Python.
set -eu

# test-wheel.sh --one WHEEL PYTHON: one Python's part (run by parallel.sh, through with-server.sh).
if [ "$1" = --one ]; then
    set -x
    export UV_PYTHON="$3" UV_PROJECT_ENVIRONMENT=".venv-$3"
    uv run --isolated --no-project --with "$2" -- python tests/smoke.py
    uv sync --locked --no-install-project # the dev dependencies, without building fastpg
    uv pip install --python "$UV_PROJECT_ENVIRONMENT" --no-deps "$2"
    exec uv run --no-sync pytest -n0 -p no:cacheprovider -ra -m "not timing"
fi

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

n=0
timing=""
for python; do
    tag=cp$(echo "$python" | tr -d '.t') # 3.14t: cp314
    abi=$tag${python##*[0-9]}            # 3.14t: cp314t
    wheel=$(only "$dir"/fastpg-*-"$tag"-"$abi"-*.whl)
    [ "$python" != "${FASTPG_TIMING_PYTHON:-}" ] || timing=$n
    # As arguments of `set`, after the loop's own (which it has already taken).
    set -- "$@" "Python $python" \
        "sh .github/scripts/with-server.sh $n sh .github/scripts/test-wheel.sh --one '$wheel' $python"
    n=$((n + 1))
done
shift "$n"

status=0
sh .github/scripts/parallel.sh "$@" || status=$?
if [ -n "$timing" ]; then
    echo "The timing tests, on Python $FASTPG_TIMING_PYTHON"
    UV_PYTHON="$FASTPG_TIMING_PYTHON" UV_PROJECT_ENVIRONMENT=".venv-$FASTPG_TIMING_PYTHON" \
        sh .github/scripts/with-server.sh "$timing" uv run --no-sync pytest -ra -m timing \
        || status=$?
fi
exit "$status"
