"""Reject commands that run a shell interpreter, directly or through a known launcher.

The guard identifies the single program a command ultimately launches. It skips a
known launcher's own options (and the values those options take), ``VAR=value``
words for ``env``, and the launcher's leading operands (``timeout DURATION``,
``flock FILE``, ``taskset MASK``), then inspects the program that follows. A
program that is itself a known launcher is resolved recursively. Only the launched
program is compared with the shell-interpreter names, so ordinary arguments such as
``pytest -k sh`` or ``make -C build/sh`` are never mistaken for a shell.

The guard is fail-closed where a launcher's shape is ambiguous: an option the guard
does not know for a known launcher is rejected rather than guessed. It is not a
sandbox. A program the guard allows (for example ``python``) can still spawn a shell;
the guard only stops a manifest or request from naming a shell as the command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from enginery.domain.errors import InvalidInputError

SHELL_INTERPRETERS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "ash",
        "rbash",
        "csh",
        "tcsh",
        "fish",
        "cmd",
        "powershell",
        "pwsh",
    }
)

_NUMERIC_OPTION = re.compile(r"-\d+")
_MAX_DEPTH = 16


@dataclass(frozen=True, slots=True)
class _Launcher:
    """How to find the program a launcher runs."""

    values: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()
    operands: int = 0
    numeric_option: bool = False
    assignments: bool = False
    split_string: bool = False
    head: int = 1
    description: str = field(default="", compare=False)


def _names(*items: str) -> frozenset[str]:
    return frozenset(items)


_UV_VALUES = _names(
    "--with",
    "--with-requirements",
    "--with-editable",
    "--python",
    "-p",
    "--project",
    "--directory",
    "--env-file",
    "--extra",
    "--group",
    "--no-group",
    "--only-group",
    "--package",
    "--index",
    "--default-index",
    "--index-url",
    "--extra-index-url",
    "--find-links",
    "--index-strategy",
    "--resolution",
    "--prerelease",
    "--exclude-newer",
    "--config-file",
    "--cache-dir",
    "--keyring-provider",
    "--link-mode",
    "--python-platform",
    "--refresh-package",
    "--reinstall-package",
    "--upgrade-package",
    "--constraints",
    "--overrides",
    "--color",
    "-P",
    "-w",
)
_UV_FLAGS = _names(
    "--no-project",
    "--isolated",
    "--frozen",
    "--locked",
    "--no-sync",
    "--active",
    "--offline",
    "--no-dev",
    "--dev",
    "--all-extras",
    "--all-groups",
    "--no-editable",
    "--exact",
    "--no-env-file",
    "--no-cache",
    "--refresh",
    "--reinstall",
    "--upgrade",
    "--quiet",
    "-q",
    "--verbose",
    "-v",
    "--native-tls",
    "--no-progress",
    "--no-config",
    "--no-binary",
    "--no-build",
    "--script",
    "-s",
    "--module",
    "-m",
    "--no-managed-python",
    "--managed-python",
    "--no-python-downloads",
)
_NODE_VALUES = _names("-p", "--package", "-w", "--workspace", "--filter", "-F", "-C", "--dir")
_NODE_FLAGS = _names(
    "-y",
    "--yes",
    "--no",
    "--include-workspace-root",
    "-ws",
    "--workspaces",
    "-r",
    "--recursive",
    "--parallel",
    "--stream",
    "--workspace-root",
    "--no-bail",
    "--if-present",
)
_NODE_FORBIDDEN = _names("-c", "--call", "--shell-mode")

_LAUNCHERS: dict[str, _Launcher] = {
    "env": _Launcher(
        values=_names("-u", "--unset", "-C", "--chdir", "-P"),
        flags=_names("-i", "--ignore-environment", "-0", "--null", "-v", "--debug"),
        assignments=True,
        split_string=True,
    ),
    "nice": _Launcher(values=_names("-n", "--adjustment"), numeric_option=True),
    "nohup": _Launcher(),
    "exec": _Launcher(values=_names("-a"), flags=_names("-c", "-l")),
    "command": _Launcher(flags=_names("-p", "-v", "-V")),
    "builtin": _Launcher(),
    "time": _Launcher(
        values=_names("-f", "--format", "-o", "--output"),
        flags=_names("-p", "-v", "-l", "-a", "-q", "--portability", "--verbose", "--append"),
    ),
    "timeout": _Launcher(
        values=_names("-k", "--kill-after", "-s", "--signal"),
        flags=_names("--foreground", "--preserve-status", "-v", "--verbose"),
        operands=1,
    ),
    "stdbuf": _Launcher(values=_names("-i", "-o", "-e", "--input", "--output", "--error")),
    "setsid": _Launcher(flags=_names("-c", "--ctty", "-f", "--fork", "-w", "--wait")),
    "ionice": _Launcher(
        values=_names("-c", "--class", "-n", "--classdata", "-p", "--pid"),
        flags=_names("-t", "--ignore"),
    ),
    "chrt": _Launcher(
        flags=_names("-b", "-d", "-f", "-i", "-o", "-r", "-R", "-m", "-v"), operands=1
    ),
    "sudo": _Launcher(
        values=_names(
            "-u",
            "-g",
            "-C",
            "-D",
            "-h",
            "-p",
            "-r",
            "-t",
            "-U",
            "-T",
            "--user",
            "--group",
            "--preserve-env",
            "--chdir",
        ),
        flags=_names(
            "-E", "-H", "-n", "-S", "-b", "-k", "-K", "-P", "-A", "-B", "--non-interactive"
        ),
        forbidden=_names("-s", "-i", "--shell", "--login"),
    ),
    "doas": _Launcher(values=_names("-u", "-C"), flags=_names("-n"), forbidden=_names("-s")),
    "xargs": _Launcher(
        values=_names("-n", "-L", "-l", "-I", "-i", "-P", "-d", "-E", "-s", "-a", "--max-args"),
        flags=_names("-0", "-r", "-t", "-p", "-x", "--null", "--no-run-if-empty", "--verbose"),
    ),
    "unbuffer": _Launcher(flags=_names("-p")),
    "arch": _Launcher(
        values=_names("-arch"), flags=_names("-arm64", "-x86_64", "-i386", "-32", "-64", "-c")
    ),
    "busybox": _Launcher(),
    "toybox": _Launcher(),
    "flock": _Launcher(
        values=_names("-w", "--timeout", "-E", "--conflict-exit-code"),
        flags=_names(
            "-s",
            "--shared",
            "-x",
            "--exclusive",
            "-u",
            "--unlock",
            "-n",
            "--nonblock",
            "--nb",
            "-o",
            "--close",
            "-F",
            "--no-fork",
            "-e",
            "-v",
            "--verbose",
        ),
        forbidden=_names("-c", "--command"),
        operands=1,
    ),
    "taskset": _Launcher(flags=_names("-c", "--cpu-list", "-a", "--all-tasks"), operands=1),
    "caffeinate": _Launcher(values=_names("-t", "-w"), flags=_names("-d", "-i", "-m", "-s", "-u")),
    "sandbox-exec": _Launcher(values=_names("-f", "-n", "-p", "-D")),
    "uvx": _Launcher(
        values=_UV_VALUES - {"-w"}, flags=_UV_FLAGS - {"-s", "--script", "-m", "--module"}
    ),
    "npx": _Launcher(values=_NODE_VALUES, flags=_NODE_FLAGS, forbidden=_NODE_FORBIDDEN),
}

# Launchers whose program follows a fixed subcommand: (executable, subcommand words) -> spec.
_SUBCOMMAND_LAUNCHERS: dict[tuple[str, ...], _Launcher] = {
    ("uv", "run"): _Launcher(values=_UV_VALUES, flags=_UV_FLAGS, head=2),
    ("uv", "tool", "run"): _Launcher(
        values=_UV_VALUES - {"-w"}, flags=_UV_FLAGS - {"-s", "--script", "-m", "--module"}, head=3
    ),
    ("poetry", "run"): _Launcher(head=2),
    ("pipenv", "run"): _Launcher(head=2),
    ("pnpm", "exec"): _Launcher(
        values=_NODE_VALUES, flags=_NODE_FLAGS, forbidden=_NODE_FORBIDDEN, head=2
    ),
    ("npm", "exec"): _Launcher(
        values=_NODE_VALUES, flags=_NODE_FLAGS, forbidden=_NODE_FORBIDDEN, head=2
    ),
}

LAUNCHER_NAMES = frozenset(_LAUNCHERS) | frozenset(key[0] for key in _SUBCOMMAND_LAUNCHERS)


def program_name(token: str) -> str:
    """The lowercase program name of ``token`` with any directory and ``.exe`` removed."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def reject_shell_invocation(argv: tuple[str, ...] | list[str]) -> None:
    """Reject an argv whose launched program is a shell interpreter.

    Raises ``InvalidInputError`` when the program, resolved through any chain of known
    launchers, is a shell interpreter, when a launcher option that runs a shell is used,
    or when a known launcher is given an option this guard does not recognize.
    """
    _reject(list(argv), depth=0)


def _reject(argv: list[str], *, depth: int) -> None:
    if not argv:
        return
    if depth > _MAX_DEPTH:
        raise InvalidInputError("command nests launchers too deeply to resolve safely")
    name = program_name(argv[0])
    if name in SHELL_INTERPRETERS:
        raise InvalidInputError("a command cannot invoke a shell interpreter")
    launcher, head = _find_launcher(name, argv)
    if launcher is None:
        return
    program = _program_after(launcher, argv, head, label=" ".join(argv[:head]))
    _reject(program, depth=depth + 1)


def _find_launcher(name: str, argv: list[str]) -> tuple[_Launcher | None, int]:
    for words, subcommand_launcher in _SUBCOMMAND_LAUNCHERS.items():
        if name == words[0] and tuple(argv[1 : len(words)]) == words[1:]:
            return subcommand_launcher, subcommand_launcher.head
    launcher = _LAUNCHERS.get(name)
    return (launcher, launcher.head) if launcher is not None else (None, 0)


def _program_after(launcher: _Launcher, argv: list[str], head: int, *, label: str) -> list[str]:
    """The argv that starts at the program ``launcher`` runs."""
    rest = list(argv[head:])
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            index += 1
            break
        if launcher.assignments and "=" in token and not token.startswith("-"):
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            break
        name, has_value, value = token.partition("=")
        if name in launcher.forbidden:
            raise InvalidInputError(
                f"launcher option {name!r} of {label!r} runs a shell interpreter"
            )
        if launcher.split_string and (name == "--split-string" or token.startswith("-S")):
            if name == "--split-string" and has_value:
                words = value.split()
                rest[index : index + 1] = words
            elif token == "-S" or name == "--split-string":
                if index + 1 >= len(rest):
                    raise InvalidInputError(f"launcher option {name!r} of {label!r} needs a value")
                rest[index : index + 2] = rest[index + 1].split()
            else:
                rest[index : index + 1] = token[2:].split()
            continue
        if name in launcher.values:
            if not has_value and index + 1 >= len(rest):
                raise InvalidInputError(f"launcher option {name!r} of {label!r} needs a value")
            index += 1 if has_value else 2
            continue
        if name in launcher.flags:
            index += 1
            continue
        if not has_value and len(token) > 2 and token[:2] in launcher.values:
            index += 1
            continue
        if launcher.numeric_option and _NUMERIC_OPTION.fullmatch(token):
            index += 1
            continue
        raise InvalidInputError(
            f"unrecognized option {token!r} for launcher {label!r}; refusing to guess which "
            "program it runs (a shell interpreter cannot be ruled out)"
        )
    index += launcher.operands
    if index < len(rest) and rest[index].partition("=")[0] in launcher.forbidden:
        raise InvalidInputError(
            f"launcher option {rest[index]!r} of {label!r} runs a shell interpreter"
        )
    return rest[index:]


__all__ = ["LAUNCHER_NAMES", "SHELL_INTERPRETERS", "program_name", "reject_shell_invocation"]
