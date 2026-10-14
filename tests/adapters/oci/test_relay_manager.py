"""``RelayManager`` against a small stateful stand-in for the docker CLI (no engine needed)."""

from __future__ import annotations

import itertools
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from enginery.adapters.oci import image
from enginery.adapters.oci.admission import OciRunnerAdmission
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.relay import (
    EGRESS_NETWORK,
    RELAY_CONTAINER_NAME,
    RELAY_LABEL,
    RELAY_LIMITS,
    RELAY_RESTART_POLICY,
    RELAY_SPEC_LABEL,
    RELAY_TOKEN_MOUNT,
    RelayManager,
    RelayStatus,
    check_token_file,
    validate_upstream,
)
from enginery.domain.errors import (
    InvalidInputError,
    MissingPrerequisiteError,
    StaleEvidenceError,
)
from enginery.domain.execution_selection import HoldReason
from enginery.domain.runner import (
    MODEL_ACCESS_RELAY,
    ImageIdentity,
    RunnerProfile,
    RunnerSelection,
)

from .support import DOCKER_DESKTOP_INFO, IMAGE_ID

RELAY_IMAGE = "sha256:" + "c" * 64
TOKEN_CONTENT = "do-not-leak-this-gateway-token-0123456789"
INTERNAL = RunnerProfile().network


def _done(
    argv: Sequence[str], returncode: int = 0, *, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(tuple(argv), returncode, stdout, stderr)


class FakeRelayDocker:
    """Answers the commands the relay manager issues; ``create`` is parsed, never executed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.networks: dict[str, bool] = {}
        self.network_labels: dict[str, dict[str, str]] = {}
        self.foreign_members: dict[str, list[str]] = {}
        self.images: dict[str, dict[str, str]] = {
            RELAY_IMAGE: {
                image.RELAY_IMAGE_LABEL: "1",
                image.RECIPE_DIGEST_LABEL: image.relay_recipe_digest(),
            }
        }
        self.container: dict[str, Any] | None = None
        self.health_returncode = 0
        self.info: dict[str, Any] = dict(DOCKER_DESKTOP_INFO)
        self.tamper: Callable[[dict[str, Any]], None] | None = None

    def engine(self) -> OciEngine:
        return OciEngine(runner=self)

    def verbs(self) -> list[str]:
        return [" ".join(call[1:3]) for call in self.calls]

    def calls_of(self, *verb: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[1 : 1 + len(verb)] == verb]

    def __call__(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        input: str | None = None,  # noqa: A002
    ) -> subprocess.CompletedProcess[str]:
        del timeout, input
        call = tuple(argv)
        self.calls.append(call)
        args = call[1:]
        match args[0]:
            case "info":
                return _done(call, stdout=json.dumps(self.info))
            case "network":
                return self._network(call, args)
            case "image":
                return self._image(call, args)
            case "create":
                return self._create(call, args)
            case "container":
                return self._inspect(call, args[2])
            case "start":
                assert self.container is not None
                self.container["State"]["Running"] = True
                return _done(call)
            case "rm":
                self.container = None
                return _done(call)
            case "exec":
                return _done(
                    call,
                    self.health_returncode,
                    stderr="" if not self.health_returncode else "connection refused\n",
                )
            case "logs":
                return _done(call, stdout="relay: listening\n")
        raise AssertionError(f"fake docker does not model {args}")

    def _network(
        self, call: tuple[str, ...], args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        if args[1] == "inspect":
            name = args[2]
            if name not in self.networks:
                return _done(
                    call, 1, stderr=f"Error response from daemon: network {name} not found"
                )
            members = list(self.foreign_members.get(name, []))
            if self.container is not None and name in self.container["NetworkSettings"]["Networks"]:
                members.append(RELAY_CONTAINER_NAME)
            document = {
                "Name": name,
                "Internal": self.networks[name],
                "Labels": self.network_labels.get(name, {}),
                "Containers": {f"id{index}": {"Name": item} for index, item in enumerate(members)},
            }
            return _done(call, stdout=json.dumps([document]))
        if args[1] == "rm":
            if args[2] not in self.networks:
                return _done(
                    call, 1, stderr=f"Error response from daemon: network {args[2]} not found"
                )
            attached = (
                self.container is not None
                and args[2] in (self.container["NetworkSettings"]["Networks"])
            )
            if attached or self.foreign_members.get(args[2]):
                return _done(
                    call,
                    1,
                    stderr=f"Error response from daemon: network {args[2]} has active endpoints",
                )
            del self.networks[args[2]]
            self.network_labels.pop(args[2], None)
            return _done(call)
        if args[1] == "create":
            self.networks[args[-1]] = "--internal" in args
            self.network_labels[args[-1]] = dict(
                item.split("=", 1) for flag, item in itertools.pairwise(args) if flag == "--label"
            )
            return _done(call)
        if args[1] == "connect":
            assert self.container is not None
            rest = list(args[2:])
            alias = None
            if rest[0] == "--alias":
                alias, rest = rest[1], rest[2:]
            network, name = rest
            if name != RELAY_CONTAINER_NAME:
                return _done(
                    call, 1, stderr=f"Error response from daemon: No such container: {name}"
                )
            self.container["NetworkSettings"]["Networks"][network] = {
                "Aliases": [] if alias is None else [alias]
            }
            return _done(call)
        if args[1] == "disconnect":
            rest = [item for item in args[2:] if item != "--force"]
            network, name = rest
            if self.container is None or name != RELAY_CONTAINER_NAME:
                return _done(
                    call, 1, stderr=f"Error response from daemon: No such container: {name}"
                )
            if network not in self.container["NetworkSettings"]["Networks"]:
                return _done(
                    call,
                    1,
                    stderr=(
                        f"Error response from daemon: container {name} is not connected "
                        f"to network {network}"
                    ),
                )
            del self.container["NetworkSettings"]["Networks"][network]
            return _done(call)
        raise AssertionError(args)

    def _image(
        self, call: tuple[str, ...], args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        reference = args[-1]
        if reference not in self.images:
            return _done(call, 1, stderr=f"Error response from daemon: No such image: {reference}")
        return _done(call, stdout=f"{reference}\n{json.dumps(self.images[reference])}")

    def _inspect(self, call: tuple[str, ...], name: str) -> subprocess.CompletedProcess[str]:
        if self.container is None or name != RELAY_CONTAINER_NAME:
            return _done(call, 1, stderr=f"Error response from daemon: No such container: {name}")
        return _done(call, stdout=json.dumps([self.container]))

    def _create(
        self, call: tuple[str, ...], args: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        assert self.container is None, "create over an existing container"
        values: dict[str, list[str]] = {}
        index = 1
        while index < len(args) - 1:
            flag = args[index]
            if flag in ("--read-only", "--init"):
                values.setdefault(flag, []).append("")
                index += 1
                continue
            values.setdefault(flag, []).append(args[index + 1])
            index += 2
        image_id = args[-1]
        labels = dict(item.split("=", 1) for item in values.get("--label", []))
        memory = values["--memory"][0]
        cpus = float(values["--cpus"][0])
        container: dict[str, Any] = {
            "Name": "/" + values["--name"][0],
            "Image": image_id,
            "State": {"Running": False, "Restarting": False},
            "Config": {
                "User": values["--user"][0],
                "Labels": labels,
                "Env": values.get("-e", []),
                "Image": image_id,
            },
            "HostConfig": {
                "CapDrop": values.get("--cap-drop", []),
                "CapAdd": None,
                "SecurityOpt": [f"{opt}:true" for opt in values.get("--security-opt", [])],
                "Privileged": False,
                "ReadonlyRootfs": "--read-only" in values,
                "Memory": int(memory.removesuffix("m")) * 1024 * 1024,
                "MemorySwap": int(values["--memory-swap"][0].removesuffix("m")) * 1024 * 1024,
                "NanoCpus": round(cpus * 1_000_000_000),
                "PidsLimit": int(values["--pids-limit"][0]),
                "NetworkMode": values["--network"][0],
                "Binds": values.get("-v", []),
                "Tmpfs": {
                    item.split(":", 1)[0]: item.split(":", 1)[1] for item in values["--tmpfs"]
                },
                "Devices": [],
                "ExtraHosts": values.get("--add-host", []),
                "RestartPolicy": {"Name": values["--restart"][0]},
            },
            "NetworkSettings": {
                "Networks": {values["--network"][0]: {"Aliases": values.get("--network-alias", [])}}
            },
        }
        if self.tamper is not None:
            self.tamper(container)
        self.container = container
        return _done(call, stdout="deadbeef\n")


@pytest.fixture
def fake() -> FakeRelayDocker:
    return FakeRelayDocker()


@pytest.fixture
def token(tmp_path: Path) -> Path:
    path = tmp_path / "gateway-token"
    path.write_text(TOKEN_CONTENT + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _manager(fake: FakeRelayDocker) -> RelayManager:
    return RelayManager(fake.engine(), sleep=lambda _seconds: None)


def _up(manager: RelayManager, token: Path, **kwargs: Any) -> RelayStatus:
    options: dict[str, Any] = {
        "relay_image": RELAY_IMAGE,
        "upstream": "http://host.docker.internal:47400",
        "token_file": token,
        "wait_seconds": 2,
    }
    return manager.up(**{**options, **kwargs})


# --- creation ---------------------------------------------------------------------------------


def test_up_creates_a_hardened_relay_on_the_egress_network_and_waits_until_it_is_healthy(
    fake: FakeRelayDocker, token: Path
) -> None:
    status = _up(_manager(fake), token)

    assert status.healthy and status.running and status.present
    assert status.image_id == RELAY_IMAGE
    assert status.networks == (EGRESS_NETWORK,)
    assert status.upstream == "http://host.docker.internal:47400"
    assert fake.networks == {EGRESS_NETWORK: False}
    (create,) = fake.calls_of("create")
    for pair in (
        ("--user", "65532:65532"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory", "256m"),
        ("--memory-swap", "256m"),
        ("--cpus", "1"),
        ("--pids-limit", "64"),
        ("--network", EGRESS_NETWORK),
        ("--restart", "unless-stopped"),
        ("--name", RELAY_CONTAINER_NAME),
        ("--label", f"{RELAY_LABEL}=1"),
        ("-v", f"{token}:{RELAY_TOKEN_MOUNT}:ro"),
        ("-e", "RELAY_UPSTREAM=http://host.docker.internal:47400"),
        ("-e", f"RELAY_TOKEN_FILE={RELAY_TOKEN_MOUNT}"),
        ("-e", "RELAY_LISTEN_PORT=8080"),
    ):
        assert pair in list(itertools.pairwise(create)), pair
    assert "--read-only" in create
    assert create[-1] == RELAY_IMAGE
    assert [value for flag, value in itertools.pairwise(create) if flag == "-v"] == [
        f"{token}:{RELAY_TOKEN_MOUNT}:ro"
    ]
    assert fake.calls_of("network", "connect") == []  # no shared worker network, no later join
    for forbidden in ("--privileged", "--pid", "--device", "--cap-add", "--publish", "-p"):
        assert forbidden not in create
    verbs = [call[1] for call in fake.calls]
    assert verbs.index("create") < verbs.index("start")


def test_a_rootless_engine_runs_the_relay_as_mapped_root_so_it_can_read_the_private_token(
    fake: FakeRelayDocker, token: Path
) -> None:
    fake.info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=rootless"]

    status = _up(_manager(fake), token)

    assert status.healthy
    (create,) = fake.calls_of("create")
    assert ("--user", "0:0") in list(itertools.pairwise(create))
    assert "--cap-drop" in create and "--read-only" in create
    assert fake.container is not None
    assert fake.container["Config"]["User"] == "0:0"


def test_the_host_gateway_alias_is_added_only_for_host_docker_internal(
    fake: FakeRelayDocker, token: Path
) -> None:
    _up(_manager(fake), token)
    (create,) = fake.calls_of("create")
    assert ("--add-host", "host.docker.internal:host-gateway") in list(itertools.pairwise(create))

    other = FakeRelayDocker()
    _up(_manager(other), token, upstream="http://192.168.5.2:47400")
    assert "--add-host" not in other.calls_of("create")[0]


def test_the_token_file_is_never_read_and_its_content_reaches_no_command(
    fake: FakeRelayDocker, token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    real_open = open

    def watching_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", watching_open)
    for name in ("read_text", "read_bytes"):
        original = getattr(Path, name)

        def guarded(self: Path, *args: Any, _original: Any = original, **kwargs: Any) -> Any:
            assert self != token, "the token file was read"
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)

    _up(_manager(fake), token)

    assert str(token) not in opened
    assert all(TOKEN_CONTENT not in " ".join(call) for call in fake.calls)


def test_an_existing_matching_relay_is_left_alone(fake: FakeRelayDocker, token: Path) -> None:
    manager = _manager(fake)
    _up(manager, token)
    before = len(fake.calls)

    status = _up(manager, token)

    assert status.healthy
    assert (
        not fake.calls_of(
            "create",
        )
        or len(fake.calls_of("create")) == 1
    )
    assert fake.calls_of("rm") == []
    assert len(fake.calls) > before


@pytest.mark.parametrize(
    "change",
    [{"upstream": "http://host.docker.internal:47401"}, {"recreate": True}],
)
def test_a_changed_spec_or_recreate_replaces_the_relay(
    fake: FakeRelayDocker, token: Path, change: dict[str, Any]
) -> None:
    manager = _manager(fake)
    _up(manager, token)

    _up(manager, token, **change)

    assert len(fake.calls_of("create")) == 2
    assert len(fake.calls_of("rm")) == 1


def test_a_same_named_container_that_is_not_the_relay_is_never_replaced(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    del fake.container["Config"]["Labels"][RELAY_LABEL]

    with pytest.raises(MissingPrerequisiteError, match="is not the relay"):
        _up(manager, token)
    with pytest.raises(MissingPrerequisiteError, match="is not the relay"):
        manager.down()

    assert fake.calls_of("rm") == []
    assert fake.container is not None
    assert manager.status().healthy is False


def test_down_removes_the_relay_and_its_egress_network_and_is_idempotent(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert EGRESS_NETWORK in fake.networks

    assert manager.down().present is False
    assert EGRESS_NETWORK not in fake.networks
    removals = fake.calls_of("network", "rm")
    assert [call[-1] for call in removals] == [EGRESS_NETWORK]
    assert manager.down().present is False  # nothing left to remove, and no error
    assert fake.calls_of("network", "rm") == removals
    assert manager.status().detail == "the model access relay container does not exist"

    # The next ``up`` makes the network again.
    assert _up(manager, token).healthy
    assert EGRESS_NETWORK in fake.networks


def test_down_leaves_an_egress_network_another_container_still_uses(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    fake.foreign_members[EGRESS_NETWORK] = ["someone-elses-container"]

    assert manager.down().present is False

    assert EGRESS_NETWORK in fake.networks
    assert fake.calls_of("network", "rm") == []


def test_down_leaves_an_egress_network_the_runner_did_not_create(
    fake: FakeRelayDocker, token: Path
) -> None:
    fake.networks[EGRESS_NETWORK] = False  # an operator's own network: no runner label
    manager = _manager(fake)
    _up(manager, token)

    assert manager.down().present is False

    assert EGRESS_NETWORK in fake.networks
    assert fake.calls_of("network", "rm") == []


def test_down_without_a_relay_or_network_changes_nothing(fake: FakeRelayDocker) -> None:
    assert _manager(fake).down().present is False
    assert fake.calls_of("network", "rm") == []


# --- refusals ---------------------------------------------------------------------------------


def test_an_unhealthy_relay_is_removed_and_reported_with_its_logs(
    fake: FakeRelayDocker, token: Path
) -> None:
    fake.health_returncode = 1

    with pytest.raises(MissingPrerequisiteError, match="did not become healthy") as caught:
        _up(_manager(fake), token)

    assert "listening" in str(caught.value.details["logs"])
    assert fake.container is None


@pytest.mark.parametrize(
    "tamper, fragment",
    [
        (lambda c: c["HostConfig"].update(ReadonlyRootfs=False), "writable root filesystem"),
        (lambda c: c["HostConfig"].update(CapDrop=[]), "does not drop every capability"),
        (lambda c: c["HostConfig"].update(Privileged=True), "is privileged"),
        (lambda c: c["HostConfig"].update(SecurityOpt=[]), "no-new-privileges"),
        (lambda c: c["Config"].update(User=""), "not 65532:65532"),
        (lambda c: c["HostConfig"].update(Memory=0), "memory limit"),
        (lambda c: c["HostConfig"].update(PidsLimit=0), "pids limit"),
        (
            lambda c: c["HostConfig"].update(Binds=["/:/host:rw"]),
            "unexpected bind mount",
        ),
        (lambda c: c["HostConfig"].update(Binds=[]), "does not mount the gateway token"),
        (
            lambda c: c["HostConfig"].update(
                Devices=[{"PathOnHost": "/dev/kvm", "PathInContainer": "/dev/kvm"}]
            ),
            "maps host devices",
        ),
    ],
)
def test_a_container_the_engine_did_not_harden_is_refused_and_removed(
    fake: FakeRelayDocker,
    token: Path,
    tamper: Callable[[dict[str, Any]], None],
    fragment: str,
) -> None:
    fake.tamper = tamper

    with pytest.raises(MissingPrerequisiteError, match="not hardened") as caught:
        _up(_manager(fake), token)

    assert fragment in str(caught.value)
    assert fake.container is None
    assert fake.calls_of("start") == []


def test_status_reports_a_relay_that_lost_its_egress_network(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    del fake.container["NetworkSettings"]["Networks"][EGRESS_NETWORK]

    status = manager.status()

    assert not status.healthy
    assert EGRESS_NETWORK in status.detail


def test_status_reports_a_stopped_relay(fake: FakeRelayDocker, token: Path) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    fake.container["State"]["Running"] = False

    status = manager.status()

    assert (status.present, status.running, status.healthy) == (True, False, False)
    assert status.detail == "the relay container is not running"


def test_an_egress_network_that_is_internal_is_refused(fake: FakeRelayDocker, token: Path) -> None:
    fake.networks[EGRESS_NETWORK] = True

    with pytest.raises(MissingPrerequisiteError, match="is internal"):
        _up(_manager(fake), token)
    assert fake.calls_of("create") == []


def test_a_missing_or_stale_relay_image_is_refused_before_anything_is_created(
    fake: FakeRelayDocker, token: Path
) -> None:
    with pytest.raises(MissingPrerequisiteError, match="cannot be inspected"):
        _up(_manager(fake), token, relay_image="sha256:" + "d" * 64)

    fake.images[RELAY_IMAGE][image.RECIPE_DIGEST_LABEL] = "sha256:" + "e" * 64
    with pytest.raises(StaleEvidenceError, match="built from recipe"):
        _up(_manager(fake), token)

    fake.images[RELAY_IMAGE] = {image.RUNNER_IMAGE_LABEL: "1"}
    with pytest.raises(MissingPrerequisiteError, match="not an Enginery relay image"):
        _up(_manager(fake), token)
    assert fake.calls_of("create") == []


# --- the token file -------------------------------------------------------------------------


def test_a_private_regular_file_is_accepted(token: Path) -> None:
    assert check_token_file(token) == token


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o400, 0o700, 0o666])
def test_a_token_file_must_be_mode_0600(token: Path, mode: int) -> None:
    token.chmod(mode)
    with pytest.raises(MissingPrerequisiteError, match="mode 0600"):
        check_token_file(token)


def test_a_symlink_is_refused(token: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(MissingPrerequisiteError, match="symbolic link"):
        check_token_file(link)


def test_a_directory_and_a_missing_file_are_refused(tmp_path: Path) -> None:
    with pytest.raises(MissingPrerequisiteError, match="not a regular file"):
        check_token_file(tmp_path)
    with pytest.raises(MissingPrerequisiteError, match="cannot be inspected"):
        check_token_file(tmp_path / "absent")


def test_a_file_owned_by_someone_else_is_refused(
    token: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(token).st_uid + 1)
    with pytest.raises(MissingPrerequisiteError, match="not owned by the invoking user"):
        check_token_file(token)


@pytest.mark.parametrize("size", [0, 4097])
def test_an_empty_or_oversized_token_file_is_refused(tmp_path: Path, size: int) -> None:
    path = tmp_path / "t"
    path.write_bytes(b"x" * size)
    path.chmod(0o600)
    with pytest.raises(MissingPrerequisiteError, match="single token"):
        check_token_file(path)


def test_a_relative_or_unmountable_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidInputError, match="absolute"):
        check_token_file(Path("relative-token"))
    with pytest.raises(InvalidInputError, match="cannot be bind-mounted"):
        check_token_file(tmp_path / "a:b")


def test_a_bad_token_file_or_upstream_stops_up_before_the_engine_is_touched(
    fake: FakeRelayDocker, token: Path
) -> None:
    token.chmod(0o644)
    with pytest.raises(MissingPrerequisiteError):
        _up(_manager(fake), token)
    token.chmod(0o600)
    with pytest.raises(InvalidInputError):
        _up(_manager(fake), token, upstream="ftp://gateway")
    assert fake.calls == []


@pytest.mark.parametrize(
    "upstream",
    ["", "gateway:80", "ftp://gateway", "http://", "http://g/v1", "http://g?x=1", "http://u:p@g"],
)
def test_a_malformed_upstream_is_refused(upstream: str) -> None:
    with pytest.raises(InvalidInputError):
        validate_upstream(upstream)


def test_a_valid_upstream_reports_scheme_host_and_port() -> None:
    assert validate_upstream("http://127.0.0.1:47400") == ("http", "127.0.0.1", 47400)
    assert validate_upstream("https://gw.example/") == ("https", "gw.example", 443)


def test_the_relay_limits_are_the_documented_small_ones() -> None:
    assert (RELAY_LIMITS.memory_mib, RELAY_LIMITS.pids, RELAY_LIMITS.cpus) == (256, 64, 1.0)


# --- admission --------------------------------------------------------------------------------


def _healthy_status() -> RelayStatus:
    return RelayStatus(True, True, True, RELAY_IMAGE, (EGRESS_NETWORK,), "u", "ok")


def _admit(
    status: RelayStatus | Exception,
    *,
    model_access: str = MODEL_ACCESS_RELAY,
    verify_relay: Callable[[OciEngine, str], ImageIdentity] | None = None,
) -> Any:
    from .test_admission import _fake_with_runner_image

    fake = _fake_with_runner_image()

    def relay_status(_engine: OciEngine, _profile: RunnerProfile) -> RelayStatus:
        if isinstance(status, Exception):
            raise status
        return status

    def verified(_engine: OciEngine, image_id: str) -> ImageIdentity:
        return ImageIdentity(image_id=image_id, recipe_digest=image.relay_recipe_digest())

    admission = OciRunnerAdmission(
        engine_factory=lambda context: fake.engine(context=context),
        relay_status=relay_status,
        verify_relay=verify_relay or verified,
    )
    selection = RunnerSelection.isolated(
        image=IMAGE_ID, profile=RunnerProfile(), model_access=model_access
    )
    return admission.admit(selection)


def test_admission_with_a_healthy_relay_admits() -> None:
    decision = _admit(_healthy_status())

    assert decision.hold is None
    assert decision.engine is not None


def test_admission_without_model_access_never_asks_about_the_relay() -> None:
    decision = _admit(RuntimeError("must not be consulted"), model_access="none")

    assert decision.hold is None


@pytest.mark.parametrize(
    "status",
    [
        RelayStatus(
            False, False, False, None, (), None, "the model access relay container does not exist"
        ),
        RelayStatus(True, False, False, RELAY_IMAGE, (), "u", "the relay container is not running"),
        RelayStatus(True, True, False, RELAY_IMAGE, (), "u", "the relay does not answer"),
        RelayStatus(True, True, True, None, (), "u", "ok"),
    ],
)
def test_an_unhealthy_or_absent_relay_holds_the_run(status: RelayStatus) -> None:
    decision = _admit(status)

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "not run in trusted-local mode" in decision.hold.message
    assert "relay" in decision.hold.message


def test_a_relay_built_from_another_recipe_is_a_fingerprint_drift_hold() -> None:
    def stale(_engine: OciEngine, image_id: str) -> ImageIdentity:
        raise StaleEvidenceError("old", details={"image_id": image_id})

    decision = _admit(_healthy_status(), verify_relay=stale)

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_FINGERPRINT_DRIFT


def test_an_engine_error_while_asking_about_the_relay_holds_the_run() -> None:
    decision = _admit(MissingPrerequisiteError("engine went away"))

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED


def test_the_spec_label_changes_with_every_input(fake: FakeRelayDocker, token: Path) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    first = fake.container["Config"]["Labels"][RELAY_SPEC_LABEL]
    _up(manager, token, upstream="http://host.docker.internal:47402")
    assert fake.container is not None
    assert fake.container["Config"]["Labels"][RELAY_SPEC_LABEL] != first


def test_a_rootless_engine_resolves_the_host_name_itself_and_gets_no_host_gateway_alias(
    fake: FakeRelayDocker, token: Path
) -> None:
    fake.info["OperatingSystem"] = "Ubuntu 24.04.5 LTS"
    fake.info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=rootless"]

    _up(_manager(fake), token)

    assert "--add-host" not in fake.calls_of("create")[0]


def test_plain_docker_runs_the_relay_as_the_token_owner_so_the_private_file_is_readable(
    fake: FakeRelayDocker, token: Path
) -> None:
    import os

    fake.info["OperatingSystem"] = "Ubuntu 24.04.5 LTS"
    fake.info["SecurityOptions"] = ["name=seccomp,profile=builtin", "name=cgroupns"]

    _up(_manager(fake), token)

    (create,) = fake.calls_of("create")
    assert ("--user", f"{os.geteuid()}:{os.getegid()}") in list(itertools.pairwise(create))
    assert os.geteuid() != 0


# --- the relay's networks: egress at rest, an attempt's own network while it runs ------------

ATTEMPT_NETWORK = f"{INTERNAL}-0123456789abcdef01234567"


def _fake_with_network(fake: FakeRelayDocker, name: str) -> None:
    fake.networks[name] = True


def test_up_never_creates_a_shared_worker_network(fake: FakeRelayDocker, token: Path) -> None:
    _up(_manager(fake), token)

    assert INTERNAL not in fake.networks
    assert [call[-1] for call in fake.calls_of("network", "create")] == [EGRESS_NETWORK]


def test_the_relay_is_created_on_the_egress_network_only_with_a_restart_policy(
    fake: FakeRelayDocker, token: Path
) -> None:
    _up(_manager(fake), token)

    (create,) = fake.calls_of("create")
    pairs = list(itertools.pairwise(create))
    assert ("--network", EGRESS_NETWORK) in pairs
    assert ("--restart", RELAY_RESTART_POLICY) in pairs
    assert RELAY_RESTART_POLICY == "unless-stopped"
    assert "--network-alias" not in create
    assert fake.container is not None
    assert sorted(fake.container["NetworkSettings"]["Networks"]) == [EGRESS_NETWORK]


def test_attach_connects_the_running_relay_to_an_attempt_network_under_its_alias(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    _fake_with_network(fake, ATTEMPT_NETWORK)

    manager.attach(ATTEMPT_NETWORK)

    assert fake.calls_of("network", "connect") == [
        (
            "docker",
            "network",
            "connect",
            "--alias",
            "enginery-relay",
            ATTEMPT_NETWORK,
            "enginery-relay",
        )
    ]
    status = manager.status()
    assert status.healthy, status.detail
    assert status.networks == (EGRESS_NETWORK, ATTEMPT_NETWORK)


def test_detach_disconnects_the_relay_and_leaves_it_on_the_egress_network(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    _fake_with_network(fake, ATTEMPT_NETWORK)
    manager.attach(ATTEMPT_NETWORK)

    manager.detach(ATTEMPT_NETWORK)
    manager.detach(ATTEMPT_NETWORK)  # idempotent

    assert manager.status().networks == (EGRESS_NETWORK,)
    assert fake.calls_of("network", "disconnect")[0] == (
        "docker",
        "network",
        "disconnect",
        "--force",
        ATTEMPT_NETWORK,
        "enginery-relay",
    )


@pytest.mark.parametrize("network", [EGRESS_NETWORK, INTERNAL, "bridge", "none", "other-net"])
def test_the_relay_is_only_ever_attached_to_an_attempt_network(
    fake: FakeRelayDocker, token: Path, network: str
) -> None:
    manager = _manager(fake)
    _up(manager, token)

    with pytest.raises(InvalidInputError, match="attempt's own network"):
        manager.attach(network)
    with pytest.raises(InvalidInputError, match="attempt's own network"):
        manager.detach(network)

    assert fake.calls_of("network", "connect") == []
    assert fake.calls_of("network", "disconnect") == []


def test_attach_refuses_a_missing_stopped_or_restarting_relay(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    with pytest.raises(MissingPrerequisiteError, match="does not exist"):
        manager.attach(ATTEMPT_NETWORK)
    _up(manager, token)
    assert fake.container is not None
    fake.container["State"]["Running"] = False
    with pytest.raises(MissingPrerequisiteError, match="not running"):
        manager.attach(ATTEMPT_NETWORK)
    fake.container["State"].update(Running=True, Restarting=True)
    with pytest.raises(MissingPrerequisiteError, match="not running"):
        manager.attach(ATTEMPT_NETWORK)

    assert fake.calls_of("network", "connect") == []


def test_attach_refuses_a_container_that_is_not_the_relay(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    del fake.container["Config"]["Labels"][RELAY_LABEL]

    with pytest.raises(MissingPrerequisiteError, match="is not the relay"):
        manager.attach(ATTEMPT_NETWORK)

    assert fake.calls_of("network", "connect") == []


@pytest.mark.parametrize("network", [INTERNAL, "bridge", "enginery-other-net"])
def test_status_reports_a_relay_on_a_shared_or_foreign_network(
    fake: FakeRelayDocker, token: Path, network: str
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    fake.container["NetworkSettings"]["Networks"][network] = {}

    status = manager.status()

    assert not status.healthy
    assert network in status.detail
    assert "neither the egress network nor an attempt's own network" in status.detail


def test_status_does_not_require_any_internal_network(fake: FakeRelayDocker, token: Path) -> None:
    manager = _manager(fake)
    _up(manager, token)

    status = manager.status()

    assert status.healthy
    assert status.networks == (EGRESS_NETWORK,)


def test_status_reports_a_relay_without_its_restart_policy(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    fake.container["HostConfig"]["RestartPolicy"] = {"Name": "no"}
    probes = len(fake.calls_of("exec"))

    status = manager.status()

    assert not status.healthy
    assert "restart policy 'no'" in status.detail
    assert len(fake.calls_of("exec")) == probes  # no health probe on a relay that stays down


def test_status_reports_a_restarting_relay_as_unhealthy_without_probing_it(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    fake.container["State"].update(Running=True, Restarting=True)
    probes = len(fake.calls_of("exec"))

    status = manager.status()

    assert (status.present, status.healthy) == (True, False)
    assert "restarting" in status.detail
    assert len(fake.calls_of("exec")) == probes


def test_an_unhealthy_relay_holds_admission_until_it_answers_again(
    fake: FakeRelayDocker, token: Path
) -> None:
    manager = _manager(fake)
    _up(manager, token)
    assert fake.container is not None
    fake.container["State"].update(Running=True, Restarting=True)

    decision = _admit(manager.status())

    assert decision.hold is not None
    assert decision.hold.reason is HoldReason.RUNNER_UNQUALIFIED
    assert "restarting" in decision.hold.message

    fake.container["State"].update(Running=True, Restarting=False)
    assert _admit(manager.status()).hold is None
