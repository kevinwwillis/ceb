"""pyproject.toml is what `pip install` reads; check the two things a plain venv install needs."""
import pathlib
import re

import pytest

tomllib = pytest.importorskip("tomllib")
PYPROJECT = tomllib.loads((pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text())


def test_license_string_needs_setuptools_77():
    if isinstance(PYPROJECT["project"].get("license"), str):
        req = next(r for r in PYPROJECT["build-system"]["requires"] if r.startswith("setuptools"))
        version = tuple(int(x) for x in re.search(r">=\s*([\d.]+)", req).group(1).split("."))
        assert version >= (77,), req


def test_pygobject_is_not_a_hard_pip_dependency():
    deps = [d.lower() for d in PYPROJECT["project"]["dependencies"]]
    assert not any(d.startswith("pygobject") for d in deps)
    assert any(d.lower().startswith("pygobject") for d in PYPROJECT["project"]["optional-dependencies"]["gi"])
