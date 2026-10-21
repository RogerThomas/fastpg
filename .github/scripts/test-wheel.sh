#!/bin/sh
# Tests built wheels (.github/workflows/release.yml), each on the Python it's for: the smoke test
# (tests/smoke.py) in a clean environment with just the wheel and its dependencies, then the test
# suite against the wheel installed with the dev dependencies, in a venv of its own
# (.venv-<python>). Run from the repo root, with uv, and FASTPG_DSN set (the test database, which
# the smoke test needs too; FASTPG_TLS_DSN and FASTPG_TLS_CA for the TLS tests, and pytest's own
# PYTEST_ADDOPTS, as for the suite run any other way):
#   sh .github/scripts/test-wheel.sh DIR PYTHON...
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
        uv sync --locked --no-install-project # the dev dependencies, without building fastpg
        uv pip install --python "$UV_PROJECT_ENVIRONMENT" --no-deps "$wheel"
        uv run --no-sync pytest -ra
    )
done
