#!yeet
"""Build the documentation and publish agent-readable Markdown artifacts."""

import inspect
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from yeetr import Opt

import fastpg


@dataclass(frozen=True, slots=True)
class _AgentPage:
    title: str
    filename: str
    body: str


def _clean_docstring(value: str | None) -> str:
    if value is None:
        return ""
    return inspect.cleandoc(value)


def _signature_markdown(name: str, symbol: object) -> str:
    if not callable(symbol):
        return ""
    try:
        signature = inspect.signature(symbol)
    except NameError, TypeError, ValueError:
        return ""
    prefix = "async def " if inspect.iscoroutinefunction(symbol) else ""
    return f"```python\n{prefix}{name}{signature}\n```"


def _is_public_member(name: str, member: object) -> bool:
    if name.startswith("_"):
        return False
    return inspect.isfunction(member) or isinstance(member, property)


def _member_markdown(class_name: str, name: str, member: object) -> str:
    target = member.fget if isinstance(member, property) else member
    lines = [f"### `{class_name}.{name}`", ""]
    if isinstance(member, property):
        lines.append("Property.")
    else:
        lines.append(_signature_markdown(name, target))
    docstring = _clean_docstring(getattr(target, "__doc__", None))
    if docstring:
        lines.extend(["", docstring])
    return "\n".join(lines)


def _is_private_class(cls: type) -> bool:
    return cls.__name__.startswith("_")


def _documented_members(cls: type) -> dict[str, object]:
    """``cls``'s own members, plus those it inherits from private (undocumented) bases."""
    members: dict[str, object] = {}
    for base in reversed(cls.__mro__):
        if base is cls or _is_private_class(base):
            members.update(vars(base))
    return members


def _public_bases(cls: type) -> list[str]:
    """``cls``'s base names for the docs, with a private base replaced by its own bases."""
    names: list[str] = []
    for base in cls.__bases__:
        if _is_private_class(base):
            names.extend(_public_bases(base))
        else:
            names.append(base.__name__)
    return names


def _class_members_markdown(cls: type) -> list[str]:
    return [
        _member_markdown(cls.__name__, name, member)
        for name, member in _documented_members(cls).items()
        if _is_public_member(name, member)
    ]


def _symbol_markdown(name: str, symbol: object) -> str:
    if not inspect.isclass(symbol) and not callable(symbol):
        return f"## `{name}`\n\n```python\n{name} = {symbol!r}\n```"
    lines = [f"## `{name}`", "", _signature_markdown(name, symbol)]
    docstring = _clean_docstring(getattr(symbol, "__doc__", None))
    if docstring:
        lines.extend(["", docstring])
    if inspect.isclass(symbol):
        bases = ", ".join(dict.fromkeys(_public_bases(symbol)))
        lines.extend(["", f"Bases: `{bases}`", *_class_members_markdown(symbol)])
    return "\n\n".join(line for line in lines if line != "")


def _api_markdown() -> str:
    parts = ["# API reference", "", "Public API exported by `fastpg`.", ""]
    for name in fastpg.__all__:
        parts.extend([_symbol_markdown(name, getattr(fastpg, name)), ""])
    return "\n".join(parts).rstrip() + "\n"


def _page_source(root: Path) -> list[_AgentPage]:
    readme = root / "README.md"
    return [
        _AgentPage("Home", "home.md", readme.read_text(encoding="utf-8")),
        _AgentPage("API reference", "api-reference.md", _api_markdown()),
    ]


def _directory_markdown(pages: list[_AgentPage]) -> str:
    lines = [
        "# fastpg agent documentation",
        "",
        "Raw Markdown documentation for agents and other text-first readers.",
        "",
        "- [All documentation](all.md)",
    ]
    lines.extend(f"- [{page.title}]({page.filename})" for page in pages)
    return "\n".join(lines) + "\n"


def _all_markdown(pages: list[_AgentPage]) -> str:
    blocks = ["# fastpg documentation", ""]
    for page in pages:
        blocks.extend([f"# {page.title}", "", page.body.rstrip(), ""])
    return "\n".join(blocks).rstrip() + "\n"


def _write_agent_docs(root: Path) -> None:
    output_dir = root / "site" / "agents"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    pages = _page_source(root)
    (output_dir / "index.html").write_text(
        (
            '<!doctype html><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0; url=index.md">'
            '<link rel="canonical" href="index.md"><a href="index.md">index.md</a>\n'
        ),
        encoding="utf-8",
    )
    (output_dir / "index.md").write_text(_directory_markdown(pages), encoding="utf-8")
    (output_dir / "all.md").write_text(_all_markdown(pages), encoding="utf-8")
    for page in pages:
        (output_dir / page.filename).write_text(page.body.rstrip() + "\n", encoding="utf-8")


def main(
    *,
    config_file: Annotated[Path, Opt(alias="f")] = Path("mkdocs.yml"),
    clean: Annotated[bool, Opt(alias="c")] = False,
    strict: Annotated[bool, Opt(alias="s")] = False,
) -> None:
    """Build the site with zensical, then write the agent docs into site/agents."""
    root = Path(__file__).resolve().parent.parent
    flags = [flag for flag, enabled in (("--clean", clean), ("--strict", strict)) if enabled]
    # zensical's CLI, not `zensical.build`: its stub types `options` as a bare `dict`, and the
    # CLI keeps this script type-checkable when the `docs` group isn't installed.
    command = [sys.executable, "-m", "zensical", "build", "-f", str(root / config_file), *flags]
    subprocess.run(command, check=True, cwd=root)  # noqa: S603 - fixed argv, no shell
    _write_agent_docs(root)
