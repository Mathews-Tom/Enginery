"""Check emitted commands against the parser rather than a spelling allowlist."""

from __future__ import annotations

import argparse
import ast
import re
import shlex
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from enginery.application.human_decisions import decision_commands
from enginery.cli import factory_behavioral
from enginery.cli.config_binding import _custody_repair
from enginery.cli.factory_collect import _auth_repair, paused_hold
from enginery.cli.factory_control import drain
from enginery.cli.factory_diagnostics import capability_resolution, trust_mode_resolution
from enginery.cli.factory_run import (
    _hold_with_repair,
    _project_command,
    _review_ingress_command,
    changed_hold,
    factory_repairs,
    workspace_documents,
)
from enginery.cli.factory_setup import _capability_commands, runner_resolution_command
from enginery.cli.main import _build_parser
from enginery.domain.errors import HumanActionRequiredError
from enginery.engine.runtime import CoordinatorRuntime
from enginery.ledger.service import LedgerService
from enginery.workflows.stage1 import Stage1RunService, _exhausted_behavioral_repair
from tests.cli.test_stage1 import _request

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "enginery"
_PREFIX = re.compile(r"(?<![\w./-])(?P<executable>enginery(?:-[a-z][a-z0-9-]*)?)\s+")


def _children(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _assert_path(root: argparse.ArgumentParser, tokens: Sequence[str], source: str) -> None:
    """Validate paths, leaf options and positional capacity without executing."""
    parser = root
    index = 0
    while index < len(tokens) and (children := _children(parser)) is not None:
        token = tokens[index]
        if token == "VALUE":
            return  # The group suffix is computed at runtime, not statically known.
        if "|" in token:
            for choice in token.split("|"):
                _assert_path(root, [*tokens[:index], choice, *tokens[index + 1 :]], source)
            return
        if token.startswith("-"):
            assert token in parser._option_string_actions, (source, token, parser.prog)
            return
        assert token in children.choices, (source, tuple(tokens), parser.prog)
        parser = children.choices[token]
        index += 1
    for token in tokens[index:]:
        if token.startswith("--"):
            assert token.partition("=")[0] in parser._option_string_actions, (
                source,
                token,
                parser.prog,
            )
    positional_count = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            option, separator, _ = token.partition("=")
            assert option in parser._option_string_actions, (source, token, parser.prog)
            action = parser._option_string_actions[option]
            index += 1
            if not separator and action.nargs != 0:
                if action.nargs in ("?", "*", "+"):
                    while index < len(tokens) and not tokens[index].startswith("--"):
                        index += 1
                else:
                    index += 1 if action.nargs is None else int(action.nargs)
            continue
        positional_count += 1
        index += 1
    capacity = 0
    for action in parser._actions:
        if action.option_strings or isinstance(action, argparse._SubParsersAction):
            continue
        if action.nargs in ("*", "+", argparse.REMAINDER):
            return
        capacity += 1 if action.nargs in (None, "?") else int(action.nargs)
    assert positional_count <= capacity, (source, tuple(tokens), parser.prog, positional_count)


def _constant_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return str(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant_text(node.left), _constant_text(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            value = _constant_text(part.value if isinstance(part, ast.FormattedValue) else part)
            parts.append("VALUE" if value is None else value)
        return "".join(parts)
    return None


def _constant_prefixes(tree: ast.AST) -> Iterator[tuple[list[str], int]]:
    """Extract static shell/argv commands, including parameterized argv helpers.

    Known residuals:
    - .format() templates.
    - Prose without the executable prefix.
    - Dynamic-left concatenations like x + "enginery " + "...".
    - Non-literal starred helper arguments stop positional expansion at an unknown value.
    - Helper discovery is per-module and calls must use bare names; cross-module and
      attribute calls, and helpers with *argv or keyword-only command parameters,
      are not followed.
    """
    # Discover argv-building helpers rather than maintaining a helper-name allowlist.
    helpers = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        parameters = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
        for candidate in ast.walk(function):
            if (
                isinstance(candidate, (ast.List, ast.Tuple))
                and len(candidate.elts) > 1
                and _constant_text(candidate.elts[0]) == "enginery"
                and isinstance(candidate.elts[1], ast.Name)
                and candidate.elts[1].id in parameters
            ):
                helpers[function.name] = (function.args, candidate.elts[1:])
                break

    def expand_arguments(arguments: list[ast.expr]) -> Iterator[ast.expr]:
        for argument in arguments:
            if isinstance(argument, ast.Starred):
                if isinstance(argument.value, (ast.Tuple, ast.List)):
                    for expanded in expand_arguments(argument.value.elts):
                        yield expanded
                        if isinstance(expanded, ast.Starred):
                            return
                else:
                    yield argument
                    return
            else:
                yield argument

    def visit(node: ast.AST) -> Iterator[tuple[list[str], int]]:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = helpers.get(node.func.id)
            if helper is not None:
                signature, template = helper
                parameters = [arg.arg for arg in (*signature.posonlyargs, *signature.args)]
                arguments = list(expand_arguments(node.args))
                bound = dict(zip(parameters, arguments, strict=False))
                bound.update(
                    (keyword.arg, keyword.value)
                    for keyword in node.keywords
                    if keyword.arg is not None
                )
                varargs = arguments[len(parameters) :]
                tokens = []
                for part in template:
                    if (
                        isinstance(part, ast.Starred)
                        and isinstance(part.value, ast.Name)
                        and signature.vararg is not None
                        and part.value.id == signature.vararg.arg
                    ):
                        tokens.extend(_constant_text(arg) or "VALUE" for arg in varargs)
                    elif isinstance(part, ast.Name) and part.id in bound:
                        tokens.append(_constant_text(bound[part.id]) or "VALUE")
                    else:
                        tokens.append(_constant_text(part) or "VALUE")
                assert tokens[0] != "VALUE", (node.lineno, "dynamic command in argv helper")
                yield tokens, node.lineno
                return
        text = _constant_text(node)
        if text is not None:
            for match in _PREFIX.finditer(text):
                remainder = text[match.end() :]
                if remainder.startswith("worker:"):
                    continue
                assert match.group("executable") == "enginery", (node.lineno, text)
                lexer = shlex.shlex(remainder, posix=True, punctuation_chars=";&`")
                lexer.whitespace_split = True
                tokens = []
                try:
                    for token in lexer:
                        if token[0] in ";&`" or token in {
                            "then",
                            "for",
                            "and",
                            "again",
                            "first",
                            "requires",
                            "using",
                            "after",
                            "with",
                        }:
                            break
                        tokens.append(token.rstrip(".,:"))
                        if token.endswith((".", ",")):
                            break
                except ValueError:
                    # A literal fragment may end inside a quoted dynamic value.
                    pass
                if tokens:
                    yield tokens, node.lineno
            for part in ast.walk(node):
                if isinstance(part, ast.FormattedValue):
                    yield from visit(part.value)
            return
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = _constant_text(node.elts[0])
            if first == "enginery":
                tokens = []
                for part in node.elts[1:]:
                    value = _constant_text(part)
                    if value is None:
                        break
                    tokens.append(value)
                if tokens:
                    yield tokens, node.lineno
                return
            if first is not None:
                assert not first.startswith("enginery-"), (node.lineno, first)
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    yield from visit(tree)


def test_all_source_command_prefixes_resolve() -> None:
    root = _build_parser()
    found = 0
    for path in sorted(_SOURCE.rglob("*.py")):
        for tokens, line in _constant_prefixes(ast.parse(path.read_text(encoding="utf-8"))):
            _assert_path(root, tokens, f"{path.relative_to(_SOURCE)}:{line}")
            found += 1
    assert found > 0


def test_source_guard_rejects_unknown_paths() -> None:
    tree = ast.parse('repair = "enginery nonexistent-command inspect"')
    with pytest.raises(AssertionError):
        for tokens, line in _constant_prefixes(tree):
            _assert_path(_build_parser(), tokens, f"synthetic:{line}")


@pytest.mark.parametrize(
    "expression",
    [
        '"enginery " + "nonexistent-command inspect"',
        'f"enginery {"nonexistent-command"} inspect"',
        'shlex.join(["enginery", "nonexistent-command", "inspect"])',
        '["enginery", "nonexistent-command", unknown]',
        '"enginery pause --nonexistent-option --project P"',
        '"enginery status RUN EXTRA"',
    ],
)
def test_source_guard_folds_expressions_and_checks_leaf_arguments(expression: str) -> None:
    with pytest.raises(AssertionError):
        for tokens, line in _constant_prefixes(ast.parse("repair = " + expression)):
            _assert_path(_build_parser(), tokens, f"synthetic:{line}")


@pytest.mark.parametrize(
    "arguments",
    [
        '"nonexistent-command", run_id',
        '"pause", "--nonexistent-option"',
        '"status", run_id, extra',
    ],
)
def test_source_guard_checks_argv_helper_calls(arguments: str) -> None:
    tree = ast.parse(
        "def repair_command(project, command, *arguments):\n"
        '    return shlex.join(["enginery", command, *arguments, "--project", str(project)])\n'
        f"repair = repair_command(project, {arguments})\n"
    )
    with pytest.raises(AssertionError):
        for tokens, line in _constant_prefixes(tree):
            _assert_path(_build_parser(), tokens, f"synthetic:{line}")


def test_source_guard_preserves_dynamic_argv_helper_arguments() -> None:
    tree = ast.parse(
        "def repair_command(project, command, *arguments):\n"
        '    return shlex.join(["enginery", command, *arguments, "--project", str(project)])\n'
        'repair = repair_command(project, "run", issue, "--supersede")\n'
    )
    commands = list(_constant_prefixes(tree))
    assert (["run", "VALUE", "--supersede", "--project", "VALUE"], 3) in commands
    for tokens, line in commands:
        _assert_path(_build_parser(), tokens, f"synthetic:{line}")


@pytest.mark.parametrize(
    "expression",
    [
        """f"use {repair_command(project, 'nonexistent-command', issue)}" """,
        """f"{branch}, then {repair_command(project, 'pause', '--nonexistent-option')}" """,
        """"use " + f"{repair_command(project, 'nonexistent-command', issue)}" """,
        """f"{branch}, then " + f"{repair_command(project, 'pause', '--nonexistent-option')}" """,
        """f"use {issue:{repair_command(project, 'nonexistent-command', issue)}}" """,
        'repair_command(project, "status", *(issue, "--nonexistent-option"))',
        'repair_command(project, "status", *[issue, "--nonexistent-option"])',
        'repair_command(project, *("status", issue, "--nonexistent-option"))',
    ],
)
def test_source_guard_checks_nested_and_starred_helper_calls(expression: str) -> None:
    tree = ast.parse(
        "def repair_command(project, command, *arguments):\n"
        '    return shlex.join(["enginery", command, *arguments, "--project", str(project)])\n'
        f"repair = {expression}\n"
    )
    with pytest.raises(AssertionError):
        for tokens, line in _constant_prefixes(tree):
            _assert_path(_build_parser(), tokens, f"synthetic:{line}")


def test_source_guard_expands_literal_starred_command_slot() -> None:
    tree = ast.parse(
        "def repair_command(project, command, *arguments):\n"
        '    return shlex.join(["enginery", command, *arguments, "--project", str(project)])\n'
        'repair = repair_command(project, *("status", issue))\n'
    )
    commands = list(_constant_prefixes(tree))
    assert (["status", "VALUE", "--project", "VALUE"], 3) in commands
    for tokens, line in commands:
        _assert_path(_build_parser(), tokens, f"synthetic:{line}")


def test_source_guard_stops_at_unknown_starred_helper_arguments() -> None:
    tree = ast.parse(
        "def repair_command(project, command, *arguments):\n"
        '    return shlex.join(["enginery", command, *arguments, "--project", str(project)])\n'
        'repair = repair_command(project, "status", *arguments, "--nonexistent-option")\n'
    )
    assert (["status", "VALUE", "--project", "VALUE"], 3) in list(_constant_prefixes(tree))


def _commands(text: str) -> Iterator[list[str]]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=";&")
    lexer.whitespace_split = True
    chunk: list[str] = []
    for token in lexer:
        if token in {";", "&&"}:
            if chunk and chunk[0] == "enginery":
                yield chunk[1:]
            chunk = []
        else:
            chunk.append(token)
    if chunk and chunk[0] == "enginery":
        yield chunk[1:]


def _parse_emitted(root: argparse.ArgumentParser, text: str) -> int:
    parsed = 0
    for argv in _commands(text):
        replacements = {
            "<trusted-local|isolated-oci>": "trusted-local",
            "RUN": "sample-run",
            "<run-id>": "sample-run",
            "REPORT.json": "report.json",
        }
        argv = [replacements.get(token, token) for token in argv]
        _assert_path(root, argv, text)
        parser = root
        for token in argv:
            children = _children(parser)
            if children is None:
                break
            assert token in children.choices, text
            parser = children.choices[token]
        assert _children(parser) is None, ("emitted a group, not a command", text)
        try:
            root.parse_args(argv)
        except SystemExit as error:
            pytest.fail(f"emitted command failed to parse ({error.code}): {text}")
        parsed += 1
    assert parsed, ("builder did not produce a command", text)
    return parsed


def test_command_builders_emit_parseable_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project with spaces; and punctuation"
    project.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = _build_parser()
    commands = []
    for database in (None, str(tmp_path / "ledger with spaces.sqlite")):
        for action in ("pull_request.open", "human_wait.validate.approve"):
            commands.extend(
                decision_commands("sample-run", str(project), action, database=database)
            )
    commands.extend(_capability_commands("example", "1.0.0"))
    commands.append(capability_resolution("example", None))
    commands.append(trust_mode_resolution(project / "missing.toml"))
    for context in (None, "operator engine"):
        compiled = SimpleNamespace(runner=SimpleNamespace(docker_context=context))
        for command in ("build-image", "status --image sha256:" + "a" * 64):
            commands.append(runner_resolution_command(compiled, command))
    commands.append(changed_hold(project, "owner/repo#1", "sample-run")["resolve"])
    commands.append(factory_repairs(project, [{"prerequisite": "dispatch"}])[0]["resolve"])
    document, _ = drain(project, wait_seconds=0, stop=False)
    commands.append(str(document["reconcile"]))
    commands.extend(hold["resolve"] for hold in paused_hold(project))
    commands.append(_auth_repair(project, "gh").split("; ", 1)[1])
    for command, arguments in (
        ("collect", ("--dry-run",)),
        ("run", ("owner/repo#1",)),
        ("run", ("owner/repo#1", "--supersede")),
        ("continue", ("sample-run",)),
        ("status", ("superseding-run",)),
    ):
        commands.append(_project_command(project, command, *arguments))
    commands.append(_review_ingress_command(project, "sample-run", 1))
    for reason in ("configuration_drift", "runner_unqualified", "other"):
        repair = str(
            _hold_with_repair(tmp_path / "repair-project", {"reason": reason}, "sample-run")[
                "resolve"
            ]
        )
        # This builder interleaves prose with commands; only shell commands are parsed.
        commands.extend(part for part in repair.split("; ") if part.startswith("enginery "))
    request = _request(tmp_path)
    with LedgerService.open(tmp_path / "engine.sqlite") as ledger:
        service = Stage1RunService(
            runtime=CoordinatorRuntime(ledger, owner="factory"), ledger=ledger
        )
        commands.extend(service._github_commands(request))
        for result in ("fail", "indeterminate"):
            with monkeypatch.context() as patch:
                patch.setattr(
                    factory_behavioral,
                    "execute_behavioral",
                    lambda *args, result=result, **kwargs: {
                        "result": result,
                        "holds": ["observer unavailable"],
                    },
                )
                with pytest.raises(HumanActionRequiredError) as raised:
                    factory_behavioral.verify_for_stage1(ledger, request, "a" * 40)
            repair = str(raised.value.details["repair"])
            for match in _PREFIX.finditer(repair):
                commands.append(repair[match.start() :].split("; ", 1)[0])
        review_command, _ = decision_commands("sample-run", str(project), "pull_request.open")
        repair = _exhausted_behavioral_repair(review_command)
        snippets = list(_constant_prefixes(ast.parse(f"repair = {repair!r}")))
        assert len(snippets) == 4
        for tokens, line in snippets:
            _assert_path(root, tokens, f"exhausted-repair:{line}")
            # The repair names request-building synopses, not fully bound argv.
            # Supply their required inputs before checking the real parser.
            if tokens[:2] == ["engine", "build-request"]:
                tokens.extend(
                    [
                        "--output",
                        "request.json",
                        "--external-reference",
                        "owner/repo#1",
                        "--repository",
                        "owner/repo",
                        "--repository-path",
                        str(project),
                        "--base-revision",
                        "a" * 40,
                        "--workspace-path",
                        str(project / "workspace"),
                        "--artifact-root",
                        str(project),
                    ]
                )
            elif tokens[:2] == ["engine", "start"]:
                tokens.extend(["--database", str(tmp_path / "engine.sqlite"), "--owner", "factory"])
            commands.append(shlex.join(["enginery", *tokens]))
        with monkeypatch.context() as patch:
            patch.setattr(
                CoordinatorRuntime,
                "list_workspace_reservations",
                lambda _: (
                    SimpleNamespace(
                        status="retained",
                        repository_id="owner/repo",
                        run_id="sample-run",
                        workspace_path=project / "workspace",
                    ),
                ),
            )
            commands.extend(str(row["resolve"]) for row in workspace_documents(project, ledger))
    # The custody builder also prefixes a separate filesystem repair; parse its CLI part.
    custody_request = SimpleNamespace(
        run=SimpleNamespace(id="fr-sample"),
        repository_path=project,
        work_snapshot=request.work_snapshot,
    )
    commands.append(
        _custody_repair(custody_request, tmp_path / "request.json").split("; then ", 1)[1]
    )
    for command in commands:
        _parse_emitted(root, command)
