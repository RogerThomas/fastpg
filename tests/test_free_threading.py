"""fastpg on free-threaded CPython (3.14t, 3.15t): the extension declares `gil_used = true`,
since it hasn't been audited for running without the GIL, so importing it turns the GIL back on.
Checked in a fresh interpreter, since this one has imported fastpg already.
"""

import os
import subprocess
import sys
import sysconfig

import pytest

_IMPORT = """
import sys

import fastpg

print(sys._is_gil_enabled())
"""


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason="not a free-threaded build",
)
def test_free_threaded_build_enables_the_gil_on_import() -> None:
    environment = {name: value for name, value in os.environ.items() if name != "PYTHON_GIL"}

    result = subprocess.run(  # noqa: S603 - our own interpreter and code
        [sys.executable, "-c", _IMPORT],
        capture_output=True,
        encoding="utf-8",
        check=True,
        env=environment,
    )

    assert result.stdout == "True\n"
    assert "fastpg._native" in result.stderr
