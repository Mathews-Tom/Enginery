"""Reject commands that run a shell interpreter, directly or through a known launcher.

The guard identifies the single program a command ultimately launches. It skips a
known launcher's own options (and the values those options take), ``VAR=value``
words for ``env``, global options placed before a package-runner subcommand
(``uv --directory d run``), and the launcher's leading operands (``timeout
DURATION``, ``flock FILE``, ``taskset MASK``), then inspects the program that
follows. A program that is itself a known launcher is resolved recursively. Only the
launched program is compared with shell-interpreter names, so ordinary arguments
such as ``pytest -k sh`` or ``make -C build/sh`` are never mistaken for a shell.

The guard is fail-closed where a launcher's shape is ambiguous: an option the guard
does not know for a known launcher is rejected rather than guessed. It is not a
sandbox. A program the guard allows (for example ``python``) can still spawn a shell;
the guard only stops a manifest or request from naming a shell as the command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from enginery.domain.errors import InvalidInputError

_SHELL_NAME = re.compile(
    r"(sh|bash|zsh|dash|ash|rbash|ksh|ksh93|pdksh|mksh|oksh|posh|yash|csh|tcsh|fish|nu|nushell"
    r"|xonsh|elvish|cmd|powershell|pwsh)([-_.]?\d[\d.\-]*)?"
)
_NUMERIC_OPTION = re.compile(r"-\d+")
_MAX_DEPTH = 16


@dataclass(frozen=True, slots=True)
class _Launcher:
    """How to find the program a launcher runs."""

    values: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()
    attached_flags: frozenset[str] = frozenset()
    forbidden: frozenset[str] = frozenset()
    operands: int = 0
    numeric_option: bool = False
    assignments: bool = False
    split_string: bool = False


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
# `uvx` and `uv tool run` take the tool name as the program and `--from` as an option.
_UVX = _Launcher(
    values=(_UV_VALUES - {"-w"}) | {"--from"},
    flags=_UV_FLAGS - {"-s", "--script", "-m", "--module"},
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
_NODE_RUNNER = _Launcher(values=_NODE_VALUES, flags=_NODE_FLAGS, forbidden=_NODE_FORBIDDEN)

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
    # GNU xargs: -i, -l and -e take only an *attached* optional value (-i{}, -l5, -eEOF);
    # a bare -i/-l/-e is a flag and the next word is the program.
    "xargs": _Launcher(
        values=_names("-n", "-L", "-I", "-P", "-d", "-E", "-s", "-a", "--max-args"),
        flags=_names(
            "-0",
            "-r",
            "-t",
            "-p",
            "-x",
            "-i",
            "-l",
            "-e",
            "--null",
            "--no-run-if-empty",
            "--verbose",
        ),
        attached_flags=_names("-i", "-l", "-e"),
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
    "uvx": _UVX,
    "npx": _NODE_RUNNER,
}

# Package runners whose program follows global options and a subcommand.
_GLOBAL_OPTIONS: dict[str, _Launcher] = {
    "uv": _Launcher(
        values=_names("--directory", "--project", "--config-file", "--cache-dir", "--color"),
        flags=_names(
            "-q",
            "--quiet",
            "-v",
            "--verbose",
            "--offline",
            "--no-cache",
            "--native-tls",
            "--no-progress",
            "--no-config",
            "-V",
            "--version",
            "-h",
            "--help",
        ),
    ),
    "poetry": _Launcher(
        values=_names("-C", "--directory", "-P", "--project"),
        flags=_names(
            "-n",
            "--no-interaction",
            "-q",
            "--quiet",
            "-v",
            "-vv",
            "-vvv",
            "--ansi",
            "--no-ansi",
            "--no-plugins",
            "--no-cache",
            "-V",
            "--version",
            "-h",
            "--help",
        ),
    ),
    "pipenv": _Launcher(flags=_names("--verbose", "-v", "--quiet", "-q", "--version", "--help")),
    "npm": _Launcher(
        values=_names("--prefix", "--userconfig", "--registry", "--loglevel", "--cache"),
        flags=_names("--silent", "-s", "--version", "-v", "-h", "--help", "-g", "--global"),
    ),
    "pnpm": _Launcher(
        values=_names("--filter", "-F", "-C", "--dir", "--reporter", "--loglevel"),
        flags=_names(
            "-r",
            "--recursive",
            "-w",
            "--workspace-root",
            "--silent",
            "--version",
            "-v",
            "-h",
            "--help",
        ),
    ),
}
_SUBCOMMANDS: dict[str, dict[tuple[str, ...], _Launcher]] = {
    "uv": {
        ("run",): _Launcher(values=_UV_VALUES, flags=_UV_FLAGS),
        ("tool", "run"): _UVX,
    },
    "poetry": {("run",): _Launcher()},
    "pipenv": {("run",): _Launcher()},
    "pnpm": {("exec",): _NODE_RUNNER, ("dlx",): _NODE_RUNNER},
    "npm": {("exec",): _NODE_RUNNER, ("x",): _NODE_RUNNER},
}


def program_name(token: str) -> str:
    """The lowercase program name of ``token`` with any directory and ``.exe`` removed."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.removesuffix(".exe")


def is_shell_name(name: str) -> bool:
    """Whether a normalized program name is a shell interpreter, including versioned names."""
    return _SHELL_NAME.fullmatch(name) is not None


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
    if is_shell_name(name):
        raise InvalidInputError("a command cannot invoke a shell interpreter")
    launcher, head = _find_launcher(name, argv)
    if launcher is None:
        return
    program = _program_after(launcher, argv, head, label=" ".join(argv[:head]))
    _reject(program, depth=depth + 1)


def _find_launcher(name: str, argv: list[str]) -> tuple[_Launcher | None, int]:
    """The launcher spec for ``argv`` and how many leading tokens are the launcher itself."""
    subcommands = _SUBCOMMANDS.get(name)
    if subcommands is not None:
        rest = list(argv[1:])
        skipped = _skip_options(_GLOBAL_OPTIONS[name], rest, label=name)
        for words, subcommand_launcher in subcommands.items():
            if tuple(rest[skipped : skipped + len(words)]) == words:
                return subcommand_launcher, 1 + skipped + len(words)
        return None, 0
    launcher = _LAUNCHERS.get(name)
    return (launcher, 1) if launcher is not None else (None, 0)


def _program_after(launcher: _Launcher, argv: list[str], head: int, *, label: str) -> list[str]:
    """The argv that starts at the program ``launcher`` runs."""
    rest = list(argv[head:])
    index = _skip_options(launcher, rest, label=label)
    index += launcher.operands
    if index < len(rest) and rest[index].partition("=")[0] in launcher.forbidden:
        raise InvalidInputError(
            f"launcher option {rest[index]!r} of {label!r} runs a shell interpreter"
        )
    return rest[index:]


def _skip_options(launcher: _Launcher, rest: list[str], *, label: str) -> int:
    """Index in ``rest`` of the first token that is not one of the launcher's options.

    ``rest`` is edited in place only to expand ``env -S`` strings into words.
    """
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            return index + 1
        if launcher.assignments and "=" in token and not token.startswith("-"):
            index += 1
            continue
        if not token.startswith("-") or token == "-":
            return index
        name, has_value, value = token.partition("=")
        if name in launcher.forbidden:
            raise InvalidInputError(
                f"launcher option {name!r} of {label!r} runs a shell interpreter"
            )
        if launcher.split_string and (name == "--split-string" or token.startswith("-S")):
            if name == "--split-string" and has_value:
                rest[index : index + 1] = value.split()
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
        if (
            not has_value
            and len(token) > 2
            and token[:2] in launcher.values | launcher.attached_flags
        ):
            index += 1
            continue
        if launcher.numeric_option and _NUMERIC_OPTION.fullmatch(token):
            index += 1
            continue
        raise InvalidInputError(
            f"unrecognized option {token!r} for launcher {label!r}; refusing to guess which "
            "program it runs"
        )
    return index


__all__ = ["is_shell_name", "program_name", "reject_shell_invocation"]
