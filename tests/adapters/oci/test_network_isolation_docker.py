"""Network separation on a real engine: who can reach the relay, and who can reach each other.

Skipped, by ``real_engine``, when no qualified container engine is reachable; everything else that
goes wrong on a reachable engine fails the test. The relay is the real packaged relay image run by
the real ``RelayManager`` under test-only names; its upstream is a loopback recording stub (no
gateway is contacted). Workers are launched by the real launcher on the stand-in worker image.

The assertions a qualification run must repeat against the production image:

* a validation container and a worker with ``model_access=none`` run with ``--network none`` and
  cannot resolve or connect to the relay (by name or by any of its addresses) while it is up, and
  the upstream sees zero requests from them;
* two concurrent workers with model access are on two different internal networks, each shared
  only with the relay, and neither can reach the other (by address or by name);
* every attempt network is gone afterwards and the relay is left on the egress network only.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from enginery.adapters.oci import image
from enginery.adapters.oci.engine import OciEngine
from enginery.adapters.oci.launcher import StopSignal, run_launcher
from enginery.adapters.oci.relay import RelayManager
from enginery.domain.errors import EngineryError
from enginery.domain.runner import ImageIdentity, LaunchSpec, PermissionMapping, RunnerProfile

from .real_engine import read_volume, write_volume_file
from .relay_support import RecordingUpstream, write_token
from .support import PERMISSIONS_WITHOUT_ARGUMENTS, make_spec

_PROBE = r"""
import json, socket, urllib.error, urllib.request

RELAY_ADDRESSES = {addresses!r}
out = {{}}


def http(method, url, body=None):
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={{"Content-Type": "application/json", "Authorization": "Bearer enginery-relay"}},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception as error:
        return type(error).__name__


def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close()
        return "connected"
    except Exception as error:
        return type(error).__name__


try:
    socket.getaddrinfo("enginery-relay", 8080)
    out["dns"] = "resolved"
except OSError:
    out["dns"] = "unresolved"
out["healthz"] = http("GET", "http://enginery-relay:8080/healthz")
out["messages"] = http(
    "POST",
    "http://enginery-relay:8080/v1/messages",
    json.dumps({{"model": "claude-sonnet-4-5", "messages": []}}).encode(),
)
for address in RELAY_ADDRESSES:
    out["address " + address] = connect(address, 8080)
print("PROBE " + json.dumps(out))
"""

_LISTEN = r"""
import os, socket, time

server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", 9999))
server.listen(8)
server.settimeout(0.5)
open("/workspace/ip.txt", "w").write(socket.gethostbyname(socket.gethostname()))
accepted = 0
deadline = time.monotonic() + 90
while time.monotonic() < deadline and not os.path.exists("/workspace/stop"):
    try:
        server.accept()[0].close()
        accepted += 1
    except OSError:
        pass
print("ACCEPTED %d" % accepted)
"""

_VALIDATION = PermissionMapping(
    harness="validation",
    name="validation-container-only",
    arguments=(),
    unconditional_bypass=False,
    rationale="no harness runs during validation",
)


@dataclass
class Stack:
    engine: OciEngine
    manager: RelayManager
    profile: RunnerProfile
    upstream: RecordingUpstream
    relay_name: str
    egress: str
    relay_addresses: list[str]


@pytest.fixture(scope="module")
def relay_image(real_engine: OciEngine) -> str:
    return image.build_relay_image(real_engine).image_id


@pytest.fixture
def stack(
    real_engine: OciEngine, real_profile: RunnerProfile, relay_image: str, tmp_path: Path
) -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:10]
    egress = f"enginery-test-egress-{suffix}"
    relay_name = f"enginery-relay-test-{suffix}"
    manager = RelayManager(
        real_engine, profile=real_profile, egress_network=egress, container_name=relay_name
    )
    upstream = RecordingUpstream(host="0.0.0.0")  # bound wide so the engine can reach it
    upstream.start()
    token_file = write_token(tmp_path / "gateway-token")
    try:
        status = manager.up(
            relay_image=relay_image,
            upstream=f"http://host.docker.internal:{upstream.port}",
            token_file=token_file,
        )
        assert status.healthy, status.detail
        endpoints = real_engine.container_inspect(relay_name)["NetworkSettings"]["Networks"]
        addresses = [str(entry["IPAddress"]) for entry in endpoints.values() if entry["IPAddress"]]
        yield Stack(real_engine, manager, real_profile, upstream, relay_name, egress, addresses)
    finally:
        upstream.stop()
        with contextlib.suppress(Exception):
            manager.down()
        with contextlib.suppress(Exception):
            real_engine.run_cli(("network", "rm", egress))


def _standin(_engine: OciEngine, image_id: str) -> ImageIdentity:
    return ImageIdentity(image_id=image_id, recipe_digest="sha256:" + "d" * 64)


def _spec(
    root: Path,
    stack: Stack,
    operation_id: str,
    standin_image: str,
    *,
    relay: bool,
    harness: str = "omp",
    permissions: PermissionMapping = PERMISSIONS_WITHOUT_ARGUMENTS,
) -> LaunchSpec:
    root.mkdir(mode=0o700, exist_ok=True)
    return make_spec(
        root,
        operation_id=operation_id,
        image=standin_image,
        profile=stack.profile,
        harness=harness,
        permissions=permissions,
        time_budget_seconds=120,
        relay_container=stack.relay_name if relay else None,
    )


def _launch(spec: LaunchSpec, stack: Stack, script: str, **kwargs: object) -> tuple[int, str]:
    stream = io.StringIO()
    code = run_launcher(
        spec,
        ("python3", "-c", script),
        engine=stack.engine,
        verify=_standin,
        stderr=stream,
        **kwargs,  # type: ignore[arg-type]
    )
    return code, stream.getvalue()


def _output(spec: LaunchSpec) -> str:
    return str(json.loads((Path(spec.out_path) / spec.result_name).read_text("utf-8"))["output"])


def _workspace_file(stack: Stack, spec: LaunchSpec, name: str) -> str:
    """Text of ``name`` in the running worker's workspace volume; empty while it is unreadable.

    The volume is reached through the attempt's store container, which is not up until the
    launcher has created and started it (the engine then streams nothing, or reports no
    container).
    """
    try:
        content = read_volume(stack.engine, spec, "/workspace/.").get(name, b"")
    except (EngineryError, tarfile.ReadError):
        return ""
    return content.decode("utf-8").strip()


def _probe(spec: LaunchSpec, stack: Stack) -> dict[str, object]:
    code, stderr = _launch(spec, stack, _PROBE.format(addresses=stack.relay_addresses))
    assert code == 0, stderr
    line = next(row for row in _output(spec).splitlines() if row.startswith("PROBE "))
    return dict(json.loads(line.removeprefix("PROBE ")))


def test_a_validation_container_and_a_none_mode_worker_cannot_reach_the_relay_while_it_is_up(
    stack: Stack, real_operation: Callable[[], str], standin_image: str, tmp_path: Path
) -> None:
    assert stack.relay_addresses, "the relay must have an address to try"
    validation = _spec(
        tmp_path / "validation",
        stack,
        real_operation(),
        standin_image,
        relay=False,
        harness="validation",
        permissions=_VALIDATION,
    )
    none_mode = _spec(tmp_path / "none", stack, real_operation(), standin_image, relay=False)

    for spec in (validation, none_mode):
        seen = _probe(spec, stack)

        assert seen["dns"] == "unresolved", spec.harness
        assert seen["healthz"] not in (200, 403, 404, 503), (spec.harness, seen)
        assert seen["messages"] not in (200, 403, 404, 503), (spec.harness, seen)
        for key, value in seen.items():
            if key.startswith("address "):
                assert value != "connected", (spec.harness, key)
        receipt = json.loads(Path(spec.receipt_path).read_text("utf-8"))
        assert receipt["effective"]["network_mode"] == "none"
        assert receipt["effective"]["networks"] == ["none"]

    # The relay was up the whole time and nothing from either container reached the upstream.
    assert stack.manager.status().healthy
    assert stack.upstream.forwarded() == []

    # Control: a worker that was granted model access does reach it, through its own network.
    granted = _spec(tmp_path / "granted", stack, real_operation(), standin_image, relay=True)
    seen = _probe(granted, stack)
    assert seen["dns"] == "resolved"
    assert seen["healthz"] == 200
    assert seen["messages"] == 200
    assert [(row.method, row.path) for row in stack.upstream.forwarded()] == [
        ("POST", "/v1/messages")
    ]
    # ... and not through the relay's address on any network it does not share.
    for key, value in seen.items():
        if key.startswith("address "):
            assert value != "connected", key


def test_two_concurrent_model_access_workers_cannot_reach_each_other(
    stack: Stack, real_operation: Callable[[], str], standin_image: str, tmp_path: Path
) -> None:
    first = _spec(tmp_path / "first", stack, real_operation(), standin_image, relay=True)
    second = _spec(tmp_path / "second", stack, real_operation(), standin_image, relay=True)
    assert first.attempt_network != second.attempt_network
    results: dict[str, tuple[int, str]] = {}
    listener = threading.Thread(
        target=lambda: results.update(first=_launch(first, stack, _LISTEN)), daemon=True
    )
    listener.start()
    try:
        deadline = time.monotonic() + 120
        # The worker's workspace is a volume the host cannot see: read it through the store
        # container. The worker writes the file in place: wait for content, not just the file.
        first_address = ""
        while not first_address and time.monotonic() < deadline:
            first_address = _workspace_file(stack, first, "ip.txt")
            if not first_address:
                time.sleep(0.2)
        assert first_address

        # The second worker waits (holding its network open) until the host has inspected both
        # networks, then tries to reach the first one.
        wait = (
            "import os, time\n"
            "limit = time.monotonic() + 90\n"
            "while not os.path.exists('/workspace/checked') and time.monotonic() < limit:\n"
            "    time.sleep(0.2)\n"
        )
        attack = wait + _PROBE.format(addresses=[first_address])
        attack += (
            f"\nprint('NAME ' + connect({first.container_name!r}, 9999))\n"
            f"print('PEER ' + connect({first_address!r}, 9999))\n"
        )
        runner = threading.Thread(
            target=lambda: results.update(second=_launch(second, stack, attack)), daemon=True
        )
        runner.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            second_info = stack.engine.network_info(second.attempt_network)
            if second_info is not None and second.container_name in second_info.containers:
                break
            time.sleep(0.2)
        # While both run: two networks, each shared only with its own worker and the relay.
        networks = {first.attempt_network, second.attempt_network}
        assert networks <= set(stack.engine.list_attempt_networks())
        for spec in (first, second):
            info = stack.engine.network_info(spec.attempt_network)
            assert info is not None and info.internal
            assert info.containers == tuple(sorted((stack.relay_name, spec.container_name)))
        write_volume_file(stack.engine, second, "checked", b"ok")
        runner.join(timeout=150)
    finally:
        with contextlib.suppress(EngineryError):  # the listener is already gone if it faulted
            write_volume_file(stack.engine, first, "stop", b"stop")
        listener.join(timeout=150)

    assert results["second"][0] == 0, results["second"][1]
    output = _output(second)
    assert "NAME gaierror" in output  # the other worker's name does not resolve on this network
    assert "NAME connected" not in output and "PEER connected" not in output
    probe = json.loads(next(r for r in output.splitlines() if r.startswith("PROBE "))[6:])
    assert probe["healthz"] == 200 and probe["messages"] == 200  # each still reaches the relay
    assert f"address {first_address}" in probe and probe[f"address {first_address}"] != "connected"
    # The first worker's listener was never connected to by the second.
    assert "ACCEPTED 0" in _output(first)


def test_the_attempt_network_and_the_relays_attachment_are_gone_afterwards(
    stack: Stack, real_operation: Callable[[], str], standin_image: str, tmp_path: Path
) -> None:
    spec = _spec(tmp_path / "attempt", stack, real_operation(), standin_image, relay=True)
    attached: list[tuple[str, ...]] = []

    watcher = threading.Event()

    def watch() -> None:
        while not watcher.is_set():
            info = stack.engine.network_info(spec.attempt_network)
            if info is not None and spec.container_name in info.containers:
                attached.append(info.containers)
                return
            time.sleep(0.1)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        code, stderr = _launch(spec, stack, "import time; time.sleep(3); print('done')")
    finally:
        watcher.set()
        thread.join(timeout=10)

    assert code == 0, stderr
    assert attached and set(attached[0]) == {stack.relay_name, spec.container_name}
    assert stack.engine.network_info(spec.attempt_network) is None
    assert spec.attempt_network not in stack.engine.list_attempt_networks()
    assert not stack.engine.container_exists(spec.container_name)
    relay = stack.engine.container_inspect(stack.relay_name)
    assert sorted(relay["NetworkSettings"]["Networks"]) == [stack.egress]
    assert stack.manager.status().healthy


def test_a_stopped_launcher_leaves_no_attempt_network(
    stack: Stack, real_operation: Callable[[], str], standin_image: str, tmp_path: Path
) -> None:
    spec = _spec(tmp_path / "stopped", stack, real_operation(), standin_image, relay=True)
    stop = StopSignal()
    thread = threading.Thread(
        target=lambda: _launch(spec, stack, "import time; time.sleep(60)", stop=stop), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        info = stack.engine.network_info(spec.attempt_network)
        if info is not None and spec.container_name in info.containers:
            break
        time.sleep(0.2)
    stop.request(15)
    thread.join(timeout=60)

    assert stack.engine.network_info(spec.attempt_network) is None
    assert not stack.engine.container_exists(spec.container_name)
    relay = stack.engine.container_inspect(stack.relay_name)
    assert sorted(relay["NetworkSettings"]["Networks"]) == [stack.egress]
