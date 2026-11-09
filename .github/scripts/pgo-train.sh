#!/bin/sh
# Trains the profile for a profile-guided build (.github/workflows/release.yml). Installs the
# instrumented cp314 wheel in DIR (built with -Cprofile-generate) in a venv of its own
# (UV_PROJECT_ENVIRONMENT, .venv-pgo by default) with the dev and bench dependencies, then runs
# the training against test server N (.github/scripts/with-server.sh), each process writing its
# counts to RAW_DIR: the benchmark suite (benchmarks/) and bench/writes.py's fastpg methods.
# Between them they use every feature; a feature they leave out can get slower, not faster. On
# Python 3.14 only (the suite uses t-strings): one profile serves every Python's wheel. Extra
# arguments go to `uv sync`, e.g. --no-install-package psqlpy (no musl wheel). Run from the repo
# root, with uv:
#   sh .github/scripts/pgo-train.sh DIR RAW_DIR N [UV_SYNC_ARG]...
set -eu

dir=$1
raw=$2
n=$3
shift 3

# The one file of the glob it's given; fails unless there's exactly one.
only() {
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "Expected one wheel, got: $*" >&2
        exit 1
    fi
    echo "$1"
}

wheel=$(only "$dir"/fastpg-*-cp314-cp314-*.whl)
mkdir -p "$raw"
export UV_PYTHON=3.14 UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.venv-pgo}"
LLVM_PROFILE_FILE="$(cd "$raw" && pwd)/fastpg-%p-%m.profraw"
export LLVM_PROFILE_FILE
set -x
uv sync --locked --no-install-project --group bench "$@"
uv pip install --python "$UV_PROJECT_ENVIRONMENT" --no-deps "$wheel"
sh .github/scripts/with-server.sh "$n" uv run --no-sync pytest benchmarks -p no:cacheprovider -q \
    --codspeed --codspeed-mode walltime --codspeed-warmup-time 0.1 --codspeed-max-time 0.5 \
    --timeout 600
sh .github/scripts/with-server.sh "$n" uv run --no-sync yeet ./bench/writes.py -m 'fastpg*' \
    -r 1000 -r 10000 --repeat 2
rm -rf .codspeed
