#!/usr/bin/env python3
"""In-container attempt programs for the factory gate (standard library only, Python 3.11+).

``run_factory_gate.py`` copies this file into a worker's result directory and runs it as the
worker command inside the real isolated runner:

    python3 /out/factory_gate_probe.py <mode>

The attempt list of each mode is read from ``/out/params.json``, which the host puts in the
launch spec (the in-container supervisor writes it; no host path reaches ``/out``). Every
attempt is data (an identifier, the exact call made and the expectation), so the evidence that
the host records names precisely what was tried. This program only *attempts* and reports what it
observed; it never decides whether the platform passed. It prints one ``GATE_OBS <json>`` line
(the supervisor captures it into the result the host collects) and leaves a second copy in the
workspace, which the host reads back, so a killed supervisor cannot lose the observation.

Modes: read, write, escape, network, git, plant, relay, memory, pids, cpu, sleep, session,
objects, forge, spam, disk, bomb, hookcommit, listen.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import http.client
import json
import os
import re
import resource
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

PARAMS_PATH = "/out/params.json"
OBS_PATH = "/workspace/.git/gate-obs.json"
SELF_PATHS = frozenset({PARAMS_PATH, OBS_PATH, f"{OBS_PATH}.tmp", "/out/factory_gate_probe.py"})
WORKSPACE = "/workspace"

_SKIPPED_TREES = ("/proc", "/sys", "/dev")
_CONTENT_ROOTS = (
    "/workspace",
    "/out",
    "/home",
    "/tmp",
    "/mnt",
    "/media",
    "/srv",
    "/var",
    "/run",
    "/etc",
    "/opt",
    "/root",
    "/boot",
)
_MAX_SCANNED_FILE_BYTES = 2 * 1024 * 1024
_CREDENTIAL_KEY = re.compile(
    r"(^|_)(GH|GITHUB|SSH|AWS|AZURE|GOOGLE|GCP|GCLOUD|UV_PUBLISH|DOCKER|KUBE|GATEWAY|BROKER|"
    r"OPENAI|XAI|PYPI|AUTH)(_|$)|TOKEN|SECRET|PASSWORD|PASSPHRASE|API_KEY|APIKEY|CREDENTIAL|COOKIE",
    re.IGNORECASE,
)

REFUSAL_ERRNOS = frozenset(
    {
        "EROFS",
        "EACCES",
        "EPERM",
        "ENOENT",
        "ENOTDIR",
        "ENODEV",
        "ENXIO",
        "ECONNREFUSED",
        "ECONNRESET",
        "ENETUNREACH",
        "EHOSTUNREACH",
        "ENETDOWN",
        "EADDRNOTAVAIL",
        "EAFNOSUPPORT",
        "EPROTONOSUPPORT",
        "ENOSYS",
        "EOPNOTSUPP",
        "ELOOP",
    }
)
RESOLUTION_REFUSALS = frozenset(
    {
        "EAI_NONAME",
        "EAI_NODATA",
        "EAI_FAIL",
        "EAI_AGAIN",
        "EAI_FAMILY",
        "EAI_ADDRFAMILY",
        "EAI_SERVICE",
    }
)


def classify_failure(error: BaseException) -> tuple[str, str]:
    """``(outcome, name)`` for an exception an attempt raised.

    ``refused`` only for an explicit refusal outcome; a timeout or an unexpected exception is
    ``inconclusive``, so it can never satisfy an expectation of ``refused``.
    """
    if isinstance(error, socket.gaierror):
        code = {
            getattr(socket, key): key for key in RESOLUTION_REFUSALS if hasattr(socket, key)
        }.get(error.errno)
        if code is not None:
            return "refused", f"resolve:{code}"
        return "inconclusive", f"resolve:{error.errno}"
    if isinstance(error, TimeoutError | socket.timeout):
        return "inconclusive", "timeout"
    if isinstance(error, OSError):
        name = errno.errorcode.get(error.errno or 0)
        if name in REFUSAL_ERRNOS:
            return "refused", name
        return "inconclusive", name or type(error).__name__
    return "inconclusive", type(error).__name__


class Recorder:
    """Collects one entry per attempted operation."""

    def __init__(self) -> None:
        self.operations: list[dict[str, Any]] = []

    def add(
        self,
        op_id: str,
        command: str,
        expected: str,
        outcome: str,
        *,
        error: str | None = None,
        detail: object = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": op_id,
            "command": command,
            "expected": expected,
            "outcome": outcome,
            "error": error,
            "ok": outcome == expected,
        }
        if detail is not None:
            entry["detail"] = detail
        self.operations.append(entry)
        return entry

    def attempt(
        self,
        op_id: str,
        command: str,
        action: Callable[[], object],
        *,
        expected: str = "refused",
    ) -> dict[str, Any]:
        """Run ``action``.

        Completing is ``performed``. Only an explicit refusal (an errno in ``REFUSAL_ERRNOS``, or
        a name-resolution failure) is ``refused``; a timeout or any unexpected exception proves
        nothing about the boundary and is ``inconclusive`` (never counted as a pass).
        """
        try:
            result = action()
        except Exception as error:
            outcome, name = classify_failure(error)
            return self.add(op_id, command, expected, outcome, error=name)
        return self.add(op_id, command, expected, "performed", detail=_brief(result))


def _brief(value: object) -> object:
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "..."


def _load_params() -> dict[str, Any]:
    with open(PARAMS_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit("params must be an object")
    return data


def _emit(observation: dict[str, Any]) -> None:
    payload = json.dumps(observation, sort_keys=True, separators=(",", ":"))
    temporary = f"{OBS_PATH}.tmp"
    # A full or read-only workspace must not hide the copy on stdout.
    with contextlib.suppress(OSError):
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, OBS_PATH)
    print("GATE_OBS " + payload, flush=True)


def _identity() -> dict[str, Any]:
    status: dict[str, str] = {}
    with contextlib.suppress(OSError), open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            status[key.strip()] = value.strip()
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "euid": os.geteuid(),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "cap_eff": status.get("CapEff"),
        "cap_prm": status.get("CapPrm"),
        "cap_bnd": status.get("CapBnd"),
        "cap_inh": status.get("CapInh"),
        "cap_amb": status.get("CapAmb"),
        "no_new_privs": status.get("NoNewPrivs"),
        "seccomp": status.get("Seccomp"),
    }


def _find_name_terms(names: list[str]) -> list[str]:
    terms: list[str] = []
    for name in names:
        if terms:
            terms.append("-o")
        terms += ["-name", name]
    return terms


def _slurp(path: str, limit: int = 1) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


def _spit(path: str, data: bytes) -> int:
    with open(path, "wb") as handle:
        return handle.write(data)


def _sha256(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


# --- read ------------------------------------------------------------------------------------


def _walk_names(names: set[str]) -> tuple[int, int, list[str]]:
    directories = 0
    files = 0
    found: list[str] = []
    for root, subdirectories, filenames in os.walk("/", followlinks=False):
        subdirectories[:] = [
            name for name in subdirectories if os.path.join(root, name) not in _SKIPPED_TREES
        ]
        directories += 1
        for name in (*subdirectories, *filenames):
            if name in names:
                found.append(os.path.join(root, name))
        files += len(filenames)
    return directories, files, found


def _scan_contents(
    values: dict[str, bytes], roots: tuple[str, ...]
) -> tuple[int, dict[str, list[str]]]:
    scanned = 0
    hits: dict[str, list[str]] = {label: [] for label in values}
    for base in roots:
        if not os.path.isdir(base):
            continue
        for root, _subdirectories, filenames in os.walk(base, followlinks=False):
            for name in filenames:
                path = os.path.join(root, name)
                if path in SELF_PATHS:
                    continue
                try:
                    info = os.lstat(path)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SCANNED_FILE_BYTES:
                        continue
                    with open(path, "rb") as handle:
                        data = handle.read()
                except OSError:
                    continue
                scanned += 1
                for label, needle in values.items():
                    if needle in data:
                        hits[label].append(path)
    return scanned, {label: paths for label, paths in hits.items() if paths}


def mode_read(params: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder()
    for path in params["protected_paths"]:
        rec.attempt(f"read:{path}", f"open({path!r}, 'rb').read(1)", lambda p=path: _slurp(p))
        rec.attempt(f"stat:{path}", f"os.stat({path!r})", lambda p=path: os.stat(p))
    for path in params.get("allowed_reads", []):
        rec.attempt(
            f"control-read:{path}",
            f"open({path!r}, 'rb').read(1)",
            lambda p=path: _slurp(p),
            expected="performed",
        )
    names: dict[str, str] = params["canary_names"]
    values = {label: value.encode() for label, value in params["canary_values"].items()}
    control_name = params["control_name"]
    control_value = params["control_value"].encode()
    directories, files, found = _walk_names({*names.values(), control_name})
    control_paths = [path for path in found if os.path.basename(path) == control_name]
    foreign = [path for path in found if os.path.basename(path) != control_name]
    scanned, hits = _scan_contents({**values, "control": control_value}, _CONTENT_ROOTS)
    control_hits = hits.pop("control", [])
    expression = " -o ".join(f"-name {name!r}" for name in sorted(names.values()))
    find = subprocess.run(
        [
            "find",
            "/",
            "-xdev",
            "(",
            *_find_name_terms(sorted(names.values())),
            ")",
            "-not",
            "-path",
            "/proc/*",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    find_foreign = [line for line in find.stdout.splitlines() if line.strip()]

    environment_keys = sorted(os.environ)
    credential_keys = [key for key in environment_keys if _CREDENTIAL_KEY.search(key)]
    canary_in_env = sorted(
        label
        for label, value in params["canary_values"].items()
        if any(value in entry for entry in os.environ.values())
    )
    proc_values: dict[str, list[str]] = {}
    proc_keys: list[str] = []
    readable = unreadable = 0
    own = str(os.getpid())
    for entry in sorted(os.listdir("/proc")):
        if not entry.isdigit():
            continue
        for leaf in ("environ", "cmdline"):
            try:
                with open(f"/proc/{entry}/{leaf}", "rb") as handle:
                    data = handle.read()
            except OSError:
                if leaf == "environ":
                    unreadable += 1
                continue
            if leaf == "environ":
                readable += 1
                for item in data.split(b"\0"):
                    key = item.partition(b"=")[0].decode("utf-8", "replace")
                    if item and _CREDENTIAL_KEY.search(key):
                        proc_keys.append(f"{entry}:{key}")
            if entry == own and leaf == "cmdline":
                continue
            for label, needle in values.items():
                if needle in data:
                    proc_values.setdefault(label, []).append(f"/proc/{entry}/{leaf}")
    return {
        "identity": _identity(),
        "operations": rec.operations,
        "filename_search": {
            "walked_directories": directories,
            "walked_files": files,
            "foreign_matches": foreign,
            "control_matches": control_paths,
            "find_command": f"find / -xdev ( {expression} ) -not -path '/proc/*'",
            "find_exit": find.returncode,
            "find_foreign_matches": find_foreign,
        },
        "content_search": {
            "roots": list(_CONTENT_ROOTS),
            "scanned_files": scanned,
            "foreign_hits": hits,
            "control_hits": control_hits,
        },
        "environment": {
            "keys": environment_keys,
            "credential_shaped_keys": credential_keys,
            "canary_values_present": canary_in_env,
        },
        "proc_environ": {
            "environ_readable": readable,
            "environ_unreadable": unreadable,
            "credential_shaped_keys": proc_keys,
            "canary_hits": proc_values,
        },
    }


# --- write -----------------------------------------------------------------------------------


def _do_write(action: str, path: str) -> object:
    if action == "create":
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"gate-write-attempt\n")
        return "created"
    if action == "append":
        with open(path, "ab") as handle:
            handle.write(b"# gate-write-attempt\n")
        return "appended"
    if action == "truncate":
        with open(path, "wb") as handle:
            handle.write(b"")
        return "truncated"
    if action == "chmod":
        os.chmod(path, 0o777)
        return "chmod"
    if action == "unlink":
        os.unlink(path)
        return "unlinked"
    if action == "rename":
        os.rename(path, path + ".moved")
        return "renamed"
    if action == "mkdirs":
        os.makedirs(path, exist_ok=False)
        return "made"
    if action == "symlink":
        os.symlink("/etc/passwd", path)
        return "linked"
    if action == "link":
        os.link("/etc/passwd", path)
        return "linked"
    raise ValueError(f"unknown write action {action}")


def mode_write(params: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder()
    watched = {path: _sha256(path) for path in params.get("watched_files", [])}
    for item in params["refused"]:
        action, path = item["action"], item["path"]
        rec.attempt(
            item["id"],
            f"{action}: {path}",
            lambda a=action, p=path: _do_write(a, p),
        )
    for item in params["allowed"]:
        action, path = item["action"], item["path"]
        rec.attempt(
            item["id"],
            f"{action}: {path}",
            lambda a=action, p=path: _do_write(a, p),
            expected="performed",
        )
    for item in params.get("plants", []):
        path = item["path"]

        def plant(p: str = path, content: str = item["content"], mode: int = item["mode"]) -> str:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(p, mode)
            return "planted"

        rec.attempt(
            item["id"], f"write {path} mode {oct(item['mode'])}", plant, expected="performed"
        )
    after = {path: _sha256(path) for path in watched}
    return {
        "identity": _identity(),
        "operations": rec.operations,
        "watched_before": watched,
        "watched_after": after,
        "watched_unchanged": watched == after,
        "site_packages": _site_packages(),
        "site_packages_exist": {path: os.path.isdir(path) for path in _site_packages()},
    }


def _site_packages() -> list[str]:
    import site

    paths: list[str] = []
    with contextlib.suppress(Exception):
        paths.extend(site.getsitepackages())
    with contextlib.suppress(Exception):
        paths.append(site.getusersitepackages())
    return sorted({path for path in paths if path})


# --- escape ----------------------------------------------------------------------------------


def _libc() -> Any:
    return ctypes.CDLL(None, use_errno=True)


def _syscall_refused(
    rec: Recorder,
    op_id: str,
    command: str,
    call: Callable[[Any], int],
    *,
    failure: int = -1,
    also_refused: tuple[str, ...] = (),
) -> None:
    libc = _libc()
    ctypes.set_errno(0)
    result = call(libc)
    error = ctypes.get_errno()
    if result == failure:
        name = errno.errorcode.get(error, str(error))
        rec.add(
            op_id,
            command,
            "refused",
            "refused" if name in REFUSAL_ERRNOS or name in also_refused else "inconclusive",
            error=name,
        )
    else:
        rec.add(op_id, command, "refused", "performed", detail=result)


def _mountinfo() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            left, _, right = line.partition(" - ")
            fields = left.split()
            tail = right.split()
            if len(fields) < 6 or len(tail) < 2:
                continue
            entries.append(
                {
                    "mount_point": fields[4],
                    "options": fields[5],
                    "fstype": tail[0],
                    "source": tail[1],
                }
            )
    return entries


def _find_sockets() -> list[str]:
    """Unix sockets visible in the places a host socket would be mounted (bounded depth)."""
    found: list[str] = []
    for base in ("/var/run", "/run", "/tmp", "/home", "/mnt", "/opt", "/srv", "/media"):
        if not os.path.isdir(base):
            continue
        for root, subdirectories, filenames in os.walk(base, followlinks=False):
            if root.count("/") - base.count("/") >= 6:
                subdirectories[:] = []
            for name in (*subdirectories, *filenames):
                path = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    if stat.S_ISSOCK(os.lstat(path).st_mode):
                        found.append(path)
    for name in os.listdir("/"):
        path = f"/{name}"
        with contextlib.suppress(OSError):
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                found.append(path)
    return found


def _visible_processes() -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for entry in sorted(os.listdir("/proc"), key=lambda name: (not name.isdigit(), name)):
        if not entry.isdigit():
            continue
        uid = -1
        command = ""
        with contextlib.suppress(OSError):
            with open(f"/proc/{entry}/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                command = handle.read(80).replace(b"\0", b" ").decode("utf-8", "replace").strip()
        visible.append({"pid": int(entry), "uid": uid, "command": command[:80]})
    return visible


def mode_escape(params: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder()
    sockets_found = _find_sockets()
    for path in params["socket_paths"]:
        rec.attempt(f"socket-path:{path}", f"os.stat({path!r})", lambda p=path: os.stat(p))
    for item in params["host_mount_paths"]:
        path = item["path"]
        allowed_entries = set(item.get("allowed", []))
        command = f"os.listdir({path!r}) is absent or holds only {sorted(allowed_entries)}"
        try:
            unexpected = sorted(set(os.listdir(path)) - allowed_entries)
        except OSError as error:
            name = errno.errorcode.get(error.errno or 0, type(error).__name__)
            rec.add(f"host-mount:{path}", command, "empty", "empty", error=name)
            continue
        rec.add(
            f"host-mount:{path}",
            command,
            "empty",
            "empty" if not unexpected else "non-empty",
            detail=unexpected[:5] or None,
        )
    devices = sorted(os.listdir("/dev"))
    for path in params["device_paths"]:
        rec.attempt(f"device:{path}", f"open({path!r}, 'rb')", lambda p=path: _slurp(p))
    mounts = _mountinfo()
    _syscall_refused(
        rec,
        "mount-tmpfs",
        "mount('none', '/tmp', 'tmpfs', 0, None)",
        lambda libc: libc.mount(b"none", b"/tmp", b"tmpfs", 0, None),
    )
    _syscall_refused(
        rec,
        "mount-bind-root",
        "mount('/', '/tmp', None, MS_BIND, None)",
        lambda libc: libc.mount(b"/", b"/tmp", None, 4096, None),
    )
    _syscall_refused(
        rec,
        "mount-remount-rootfs-rw",
        "mount('none', '/', None, MS_REMOUNT, None)",
        lambda libc: libc.mount(b"none", b"/", None, 32, None),
    )
    _syscall_refused(
        rec, "unshare-mount-ns", "unshare(CLONE_NEWNS)", lambda libc: libc.unshare(0x00020000)
    )
    _syscall_refused(
        rec, "unshare-user-ns", "unshare(CLONE_NEWUSER)", lambda libc: libc.unshare(0x10000000)
    )
    _syscall_refused(
        rec, "unshare-pid-ns", "unshare(CLONE_NEWPID)", lambda libc: libc.unshare(0x20000000)
    )
    _syscall_refused(
        rec,
        "ptrace-foreign-pid",
        "ptrace(PTRACE_ATTACH, 4194303, 0, 0)  # a pid outside this container's namespace",
        lambda libc: libc.ptrace(16, 4194303, 0, 0),
        # The pid namespace hides every process outside the container: ESRCH is the explicit
        # outcome for a process that is not there to attach to.
        also_refused=("ESRCH",),
    )
    _syscall_refused(rec, "chroot", "chroot('/tmp')", lambda libc: libc.chroot(b"/tmp"))
    _syscall_refused(rec, "setuid-root", "setuid(0)", lambda libc: libc.setuid(0))
    _syscall_refused(
        rec,
        "sethostname",
        "sethostname('gate-escape', 11)",
        lambda libc: libc.sethostname(b"gate-escape", 11),
    )
    _syscall_refused(
        rec,
        "mknod-disk",
        "mknod('/tmp/gate-sda', S_IFBLK|0600, makedev(8, 0))",
        lambda libc: libc.mknod(b"/tmp/gate-sda", stat.S_IFBLK | 0o600, os.makedev(8, 0)),
    )
    rec.attempt(
        "raw-socket",
        "socket(AF_PACKET, SOCK_RAW)",
        lambda: socket.socket(socket.AF_PACKET, socket.SOCK_RAW),
    )
    rec.attempt(
        "sysrq-trigger",
        "open('/proc/sysrq-trigger', 'wb').write(b'h')",
        lambda: _spit("/proc/sysrq-trigger", b"h"),
    )
    rec.attempt(
        "kernel-hostname-write",
        "open('/proc/sys/kernel/hostname', 'wb')",
        lambda: _spit("/proc/sys/kernel/hostname", b""),
    )
    rec.attempt(
        "cgroup-release-agent",
        "open('/sys/fs/cgroup/release_agent', 'wb')",
        lambda: _spit("/sys/fs/cgroup/release_agent", b""),
    )
    visible = _visible_processes()
    foreign = [item for item in visible if item["uid"] != os.getuid()]
    rec.add(
        "proc-visible-processes",
        "read /proc/<pid>/status and cmdline for every visible process",
        "private",
        "private" if not foreign else "foreign-process-visible",
        detail={"processes": visible, "foreign": foreign},
    )
    return {
        "identity": _identity(),
        "operations": rec.operations,
        "sockets_found": sorted(sockets_found),
        "dev_entries": devices,
        "mountinfo": mounts,
        "rlimits": {"nproc": resource.getrlimit(resource.RLIMIT_NPROC)[0]},
        "docker_cli": subprocess.run(
            ["sh", "-c", "command -v docker || true"], capture_output=True, text=True, check=False
        ).stdout.strip(),
    }


# --- network ---------------------------------------------------------------------------------


def _tcp_attempt(host: str, port: int, timeout: float) -> tuple[str, str | None, str | None]:
    """``(outcome, error, address)``; ``connected`` only if the handshake finished.

    ``refused`` needs an explicit refusal on every address; a timeout or an unexpected error on
    any address makes the whole attempt ``inconclusive``.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        outcome, name = classify_failure(error)
        return outcome, name, None
    last: str = "no-address"
    inconclusive: str | None = None
    for family, kind, protocol, _name, address in infos:
        connection = socket.socket(family, kind, protocol)
        connection.settimeout(timeout)
        try:
            connection.connect(address)
            return "connected", None, str(address[0])
        except OSError as error:
            outcome, last = classify_failure(error)
            if outcome != "refused":
                inconclusive = last
        finally:
            connection.close()
    if inconclusive is not None:
        return "inconclusive", inconclusive, None
    return "refused", last, None


def _default_gateway() -> str | None:
    try:
        with open("/proc/net/route", encoding="utf-8") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    raw = bytes.fromhex(fields[2])
                    return ".".join(str(octet) for octet in raw[::-1])
    except (OSError, ValueError, StopIteration):
        return None
    return None


def _read_text(path: str, limit: int = 4000) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError as error:
        return f"unreadable: {errno.errorcode.get(error.errno or 0, type(error).__name__)}"


def mode_network(params: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder()
    timeout = float(params.get("timeout", 4.0))
    gateway = _default_gateway()
    targets: list[dict[str, Any]] = list(params["targets"])
    if gateway is not None:
        for port in params.get("gateway_ports", []):
            targets.append({"id": f"default-gateway:{port}", "host": gateway, "port": port})
    for target in targets:
        outcome, error, address = _tcp_attempt(target["host"], int(target["port"]), timeout)
        rec.add(
            f"tcp:{target['id']}",
            f"socket.create_connection(({target['host']!r}, {target['port']}), timeout={timeout})",
            "refused",
            outcome,
            error=error,
            detail={"address": address},
        )
    for name in params["dns_names"]:

        def lookup(n: str = name) -> object:
            return socket.getaddrinfo(n, 443)[0][4][0]

        rec.attempt(f"dns:{name}", f"socket.getaddrinfo({name!r}, 443)", lookup)
    for host in params["udp_dns_servers"]:

        def udp(h: str = host) -> bytes:
            query = (
                b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                b"\x07example\x03com\x00\x00\x01\x00\x01"
            )
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            try:
                sock.sendto(query, (h, 53))
                return sock.recv(512)
            finally:
                sock.close()

        rec.attempt(f"udp-dns:{host}", f"udp query example.com via {host}:53", udp)
    for item in params.get("http_posts", []):

        def post(i: dict[str, Any] = item) -> object:
            connection = http.client.HTTPConnection(i["host"], int(i["port"]), timeout=timeout)
            try:
                connection.request("POST", i["path"], body=b"{}", headers={"X-Gate": "attempt"})
                return connection.getresponse().status
            finally:
                connection.close()

        rec.attempt(
            item["id"],
            f"POST http://{item['host']}:{item['port']}{item['path']}",
            post,
        )
    # Control: the attempt code really opens sockets and connects.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    outcome, error, _ = _tcp_attempt("127.0.0.1", server.getsockname()[1], timeout)
    server.close()
    rec.add(
        "control:loopback-connect",
        "connect to a listener opened inside the container on 127.0.0.1",
        "connected",
        outcome,
        error=error,
    )
    time.sleep(float(params.get("hold", 0)))
    return {
        "identity": _identity(),
        "operations": rec.operations,
        "default_gateway": gateway,
        "route": _read_text("/proc/net/route"),
        "interfaces": _read_text("/proc/net/dev"),
        "resolv_conf": _read_text("/etc/resolv.conf"),
        "hosts": _read_text("/etc/hosts"),
    }


# --- git -------------------------------------------------------------------------------------


def _git(
    arguments: list[str], *, timeout: float = 60.0, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
    return subprocess.run(
        ["git", *arguments],
        check=False,
        capture_output=True,
        text=True,
        cwd=WORKSPACE,
        env=environment,
        input=stdin,
        timeout=timeout,
    )


def _command_text(arguments: list[str]) -> str:
    return "git " + " ".join(arguments)


GIT_REFUSAL_SIGNATURES = (
    "could not resolve host",
    "name or service not known",
    "temporary failure in name resolution",
    "network is unreachable",
    "connection refused",
    "failed to connect",
    "no route to host",
    "could not read username",
    "could not read password",
    "terminal prompts disabled",
    "permission denied",
    "authentication failed",
    "host key verification failed",
    "does not appear to be a git repository",
    "no such file or directory",
    "read-only file system",
    "not permitted",
    "protocol not supported",
    "transport 'ssh' not allowed",
    "transport 'git' not allowed",
    "transport 'file' not allowed",
)
"""Messages git prints when the sandbox, not git itself, refused the operation."""


def git_failure_outcome(returncode: int, stderr: str) -> tuple[str, str]:
    """``(outcome, error)`` for a finished git command.

    Success is ``performed``. A failure is ``refused`` only when its stderr carries a specific
    refusal signature; any other non-zero exit is ``inconclusive`` (git may have failed for a
    reason that says nothing about the boundary), so it can never satisfy ``refused``.
    """
    if returncode == 0:
        return "performed", ""
    text = stderr.lower()
    for signature in GIT_REFUSAL_SIGNATURES:
        if signature in text:
            return "refused", f"exit {returncode}: {signature}"
    return "inconclusive", f"exit {returncode}: no refusal signature"


def _git_attempt(
    rec: Recorder, op_id: str, arguments: list[str], *, stdin: str | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        completed = _git(arguments, stdin=stdin, timeout=40.0)
    except (OSError, subprocess.TimeoutExpired) as error:
        outcome, name = classify_failure(error)
        if isinstance(error, subprocess.TimeoutExpired):
            outcome, name = "inconclusive", "timeout"
        rec.add(op_id, _command_text(arguments), "refused", outcome, error=name)
        return None
    outcome, error_text = git_failure_outcome(completed.returncode, completed.stderr)
    rec.add(
        op_id,
        _command_text(arguments),
        "refused",
        outcome,
        error=None if outcome == "performed" else error_text,
        detail=(completed.stderr.strip().splitlines() or [""])[-1][:160],
    )
    return completed


def mode_git(params: dict[str, Any]) -> dict[str, Any]:
    rec = Recorder()
    branch = params["branch"]
    home = os.environ.get("HOME", "/home/worker")
    remote_listing = _git(["remote", "-v"])
    positive: dict[str, Any] = {"remote_listing": remote_listing.stdout.strip()}
    positive["checkout"] = _git(["checkout", "-q", "-b", branch]).returncode
    with open(os.path.join(WORKSPACE, "gate-change.txt"), "w", encoding="utf-8") as handle:
        handle.write(params["change_text"])
    for step in (
        ["add", "gate-change.txt"],
        ["commit", "-q", "-m", "gate: worker commits locally"],
    ):
        completed = _git(step)
        positive[" ".join(step)] = completed.returncode
    head = _git(["rev-parse", "HEAD"]).stdout.strip()
    positive["head"] = head
    for index, url in enumerate(params["push_targets"]):
        _git_attempt(
            rec, f"git-push:{index}:{url}", ["push", url, f"HEAD:refs/heads/gate-attack-{index}"]
        )
    for index, url in enumerate(params["other_remote_targets"]):
        _git_attempt(rec, f"git-remote-op:{index}:{url}", ["ls-remote", url])
    _git_attempt(
        rec,
        "git-credential-fill",
        ["credential", "fill"],
        stdin="protocol=https\nhost=github.com\n\n",
    )
    for path in params["credential_paths"]:
        resolved = path.replace("~", home, 1) if path.startswith("~") else path
        rec.attempt(
            f"cred-read:{path}", f"open({resolved!r}).read()", lambda p=resolved: _slurp(p, 4096)
        )
    for name in ("gh", "ssh", "ssh-agent", "hub", "docker", "sudo", "curl", "wget"):
        found = subprocess.run(
            ["sh", "-c", f"command -v {name} || true"], capture_output=True, text=True, check=False
        ).stdout.strip()
        rec.add(
            f"which:{name}",
            f"command -v {name}",
            "absent",
            "absent" if not found else "present",
            detail=found or None,
        )
    for item in params["https_targets"]:

        def fetch(i: dict[str, str] = item) -> object:
            connection = http.client.HTTPSConnection(i["host"], timeout=6)
            try:
                connection.request(
                    i["method"],
                    i["path"],
                    body=b"{}",
                    headers={"Authorization": "token gate-fake", "User-Agent": "gate"},
                )
                return connection.getresponse().status
            finally:
                connection.close()

        rec.attempt(
            item["id"], f"https {item['method']} https://{item['host']}{item['path']}", fetch
        )
    for item in params["broker_posts"]:

        def post(i: dict[str, Any] = item) -> object:
            connection = http.client.HTTPConnection(i["host"], int(i["port"]), timeout=5)
            try:
                connection.request("POST", i["path"], body=b"{}", headers={"X-Gate": "attempt"})
                return connection.getresponse().status
            finally:
                connection.close()

        rec.attempt(item["id"], f"POST http://{item['host']}:{item['port']}{item['path']}", post)
    global_config = _git(["config", "--global", "--list"])
    return {
        "identity": _identity(),
        "operations": rec.operations,
        "positive": positive,
        "git_global_config": global_config.stdout.strip()[:400],
        "git_local_config_keys": sorted(
            {
                line.split("=", 1)[0]
                for line in _git(["config", "--local", "--list"]).stdout.splitlines()
            }
        ),
    }


def mode_plant(params: dict[str, Any]) -> dict[str, Any]:
    """A hostile candidate: hooks, conftest, config and tests, committed and uncommitted."""
    rec = Recorder()
    branch = params["branch"]
    _git(["checkout", "-q", "-b", branch])
    for item in params["committed"]:
        path = os.path.join(WORKSPACE, item["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(item["content"])
        os.chmod(path, item.get("mode", 0o644))
        rec.add(f"commit-file:{item['path']}", f"write {item['path']}", "performed", "performed")
    _git(["add", "-A"])
    commit = _git(["commit", "-q", "-m", "gate: hostile candidate"])
    rec.add(
        "commit", "git commit", "performed", "performed" if commit.returncode == 0 else "refused"
    )
    for item in params["plants"]:
        path = (
            item["path"] if item["path"].startswith("/") else os.path.join(WORKSPACE, item["path"])
        )
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(item["content"])
            os.chmod(path, item.get("mode", 0o644))
            rec.add(f"plant:{item['path']}", f"write {item['path']}", "performed", "performed")
        except OSError as error:
            rec.add(
                f"plant:{item['path']}",
                f"write {item['path']}",
                "performed",
                "refused",
                error=errno.errorcode.get(error.errno or 0, "OSError"),
            )
    for key, value in params["git_config"].items():
        completed = _git(["config", "--local", key, value])
        rec.add(
            f"git-config:{key}",
            f"git config --local {key} <value>",
            "performed",
            "performed" if completed.returncode == 0 else "refused",
        )
    for item in params["leftovers"]:
        path = os.path.join(WORKSPACE, item["path"])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(item["content"])
        rec.add(f"leftover:{item['path']}", f"write {item['path']}", "performed", "performed")
    head = _git(["rev-parse", "HEAD"]).stdout.strip()
    return {"identity": _identity(), "operations": rec.operations, "head": head}


# --- relay -----------------------------------------------------------------------------------


def _http_case(base: str, case: dict[str, Any]) -> dict[str, Any]:
    host, _, port = base.removeprefix("http://").partition(":")
    result: dict[str, Any] = {"id": case["id"], "request": f"{case['method']} {case['path']}"}
    body = case.get("body")
    raw_body = None if body is None else body.encode("utf-8")
    try:
        if case.get("chunked"):
            sock = socket.create_connection((host, int(port)), timeout=20)
            try:
                head = (
                    f"{case['method']} {case['path']} HTTP/1.1\r\nHost: {host}\r\n"
                    "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                )
                payload = raw_body or b""
                sock.sendall(
                    head.encode() + f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
                )
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            finally:
                sock.close()
            status_line = data.split(b"\r\n", 1)[0].decode("latin-1")
            parts = status_line.split()
            result["status"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            result["body"] = data.partition(b"\r\n\r\n")[2][:2000].decode("utf-8", "replace")
            return result
        connection = http.client.HTTPConnection(host, int(port), timeout=30)
        try:
            headers = dict(case.get("headers", {}))
            if raw_body is not None:
                headers.setdefault("Content-Type", "application/json")
            connection.request(case["method"], case["path"], body=raw_body, headers=headers)
            response = connection.getresponse()
            payload = response.read(1024 * 1024)
            result["status"] = response.status
            result["content_type"] = response.getheader("Content-Type")
            result["body"] = payload[:4000].decode("utf-8", "replace")
            result["body_bytes"] = len(payload)
        finally:
            connection.close()
    except (OSError, http.client.HTTPException) as error:
        result["status"] = None
        result["error"] = type(error).__name__
    return result


def mode_relay(params: dict[str, Any]) -> dict[str, Any]:
    base = params["relay_url"]
    results = [_http_case(base, case) for case in params["cases"]]
    env_view = {
        key: ("set" if os.environ.get(key) else "unset") for key in params.get("env_keys", [])
    }
    return {"identity": _identity(), "results": results, "env": env_view}


# --- resources -------------------------------------------------------------------------------


def _cgroup_file(name: str) -> str | None:
    try:
        with open(f"/sys/fs/cgroup/{name}", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _cgroup_snapshot() -> dict[str, Any]:
    return {
        name: _cgroup_file(name)
        for name in (
            "memory.max",
            "memory.swap.max",
            "memory.current",
            "memory.events",
            "pids.max",
            "pids.current",
            "pids.events",
            "cpu.max",
            "cpu.stat",
        )
    }


def mode_memory(params: dict[str, Any]) -> dict[str, Any]:
    target_mib = int(params["allocate_mib"])
    chunk_mib = 8
    held: list[bytearray] = []
    print(json.dumps({"event": "memory-start", "cgroup": _cgroup_snapshot()}), flush=True)
    allocated = 0
    while allocated < target_mib:
        block = bytearray(chunk_mib * 1024 * 1024)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        held.append(block)
        allocated += chunk_mib
        print(json.dumps({"event": "allocated_mib", "value": allocated}), flush=True)
    # Reaching this line means the limit was not enforced.
    return {"identity": _identity(), "reached_target": True, "allocated_mib": allocated}


def mode_pids(params: dict[str, Any]) -> dict[str, Any]:
    wanted = int(params["fork_attempts"])
    children: list[int] = []
    failure: str | None = None
    for _ in range(wanted):
        try:
            pid = os.fork()
        except OSError as error:
            failure = errno.errorcode.get(error.errno or 0, "OSError")
            break
        if pid == 0:
            time.sleep(30)
            os._exit(0)
        children.append(pid)
    snapshot = _cgroup_snapshot()
    for pid in children:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    for pid in children:
        with contextlib.suppress(OSError):
            os.waitpid(pid, 0)
    return {
        "identity": _identity(),
        "fork_attempts": wanted,
        "children_created": len(children),
        "fork_failure": failure,
        "cgroup_at_peak": snapshot,
        "cgroup_after": _cgroup_snapshot(),
    }


def _cpu_stat() -> dict[str, int]:
    stat_text = _cgroup_file("cpu.stat") or ""
    values: dict[str, int] = {}
    for line in stat_text.splitlines():
        key, _, value = line.partition(" ")
        if value.isdigit():
            values[key] = int(value)
    return values


def mode_cpu(params: dict[str, Any]) -> dict[str, Any]:
    seconds = float(params["seconds"])
    before = _cpu_stat()
    wall_start = time.monotonic()
    deadline = wall_start + seconds
    counter = 0
    while time.monotonic() < deadline:
        counter += 1
        for _ in range(20000):
            counter += 1
    wall = time.monotonic() - wall_start
    after = _cpu_stat()
    used = (after.get("usage_usec", 0) - before.get("usage_usec", 0)) / 1_000_000
    return {
        "identity": _identity(),
        "wall_seconds": wall,
        "cpu_seconds": used,
        "cores_used": used / wall if wall else None,
        "cgroup_cpu_max": _cgroup_file("cpu.max"),
        "throttled_periods": after.get("nr_throttled", 0) - before.get("nr_throttled", 0),
        "throttled_usec": after.get("throttled_usec", 0) - before.get("throttled_usec", 0),
        "stat_before": before,
        "stat_after": after,
    }


def mode_sleep(params: dict[str, Any]) -> dict[str, Any]:
    """Hold the container open past its wall-time budget; never returns by itself."""
    if params.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(["sleep", "600"])
    print(json.dumps({"event": "sleep-started", "child": child.pid}), flush=True)
    time.sleep(600)
    return {"identity": _identity(), "returned": True}


def mode_session(params: dict[str, Any]) -> dict[str, Any]:
    """Report the environment a harness session will see (key names only)."""
    return {
        "identity": _identity(),
        "env_keys": sorted(os.environ),
        "models_yml_present": os.path.exists(params.get("models_yml", "/nonexistent")),
    }


# --- review modes: object store, result forgery, logs, disk, bombs, hooks, peers ---------------


def mode_objects(params: dict[str, Any]) -> dict[str, Any]:
    """What the clone's object store, refs, stashes and tags hold, as the worker sees them."""
    present = {
        label: _git(["cat-file", "-e", oid]).returncode == 0
        for label, oid in params["oids"].items()
    }
    listed = _git(["cat-file", "--batch-all-objects", "--batch-check"]).stdout.splitlines()
    unreachable = _git(["fsck", "--unreachable", "--no-reflogs", "--no-progress"])
    return {
        "identity": _identity(),
        "oids_present": present,
        "object_ids": sorted(line.split()[0] for line in listed if line.strip()),
        "unreachable_report": (unreachable.stdout + unreachable.stderr).splitlines()[:50],
        "refs": _git(["for-each-ref", "--format=%(refname)"]).stdout.split(),
        "stash_list": _git(["stash", "list"]).stdout,
        "tags": _git(["tag", "-l"]).stdout.split(),
        "branches": _git(["branch", "-a", "--format=%(refname)"]).stdout.split(),
        "alternates_present": os.path.exists(
            os.path.join(WORKSPACE, ".git/objects/info/alternates")
        ),
        "operations": [],
    }


def mode_forge(params: dict[str, Any]) -> dict[str, Any]:
    """Plant a hostile result file, then kill the supervisor so nothing overwrites it."""
    result = "/out/result.json"
    kind = params["kind"]
    supervisor = os.getppid()
    existed_at_start = os.path.lexists(result)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(result)
    if kind == "symlink":
        # The link points at a file inside the result volume that holds a recognisable canary.
        with open(params["target"], "w", encoding="utf-8") as canary:
            canary.write(params["canary"])
        os.symlink(params["target"], result)
    elif kind == "fifo":
        os.mkfifo(result)
    elif kind == "replay":
        with open(result, "w", encoding="utf-8") as target:
            target.write(params["stale_result"])
    else:
        raise SystemExit(f"unknown forgery {kind!r}")
    try:
        with open(f"/proc/{supervisor}/cmdline", "rb") as handle:
            nonce_readable = b"--nonce" in handle.read()
    except OSError:
        nonce_readable = False
    _emit(
        {
            "identity": _identity(),
            "kind": kind,
            "result_existed_at_start": existed_at_start,
            "supervisor_pid": supervisor,
            "supervisor_nonce_readable_from_worker": nonce_readable,
            "operations": [],
            "mode": "forge",
        }
    )
    os.kill(supervisor, signal.SIGKILL)
    time.sleep(30)
    return {"identity": _identity(), "operations": []}


def mode_spam(params: dict[str, Any]) -> dict[str, Any]:
    """Write ``mib`` MiB to the supervisor-captured stdout/stderr and to the container's own
    stdout/stderr (the log driver's input, reached through pid 1's descriptors)."""
    line = "x" * 1023 + "\n"
    container_streams = []
    for descriptor in (1, 2):
        with contextlib.suppress(OSError):
            container_streams.append(
                open(f"/proc/1/fd/{descriptor}", "w", encoding="utf-8")  # noqa: SIM115
            )
    for _ in range(int(params["mib"]) * 1024):
        sys.stdout.write(line)
        sys.stderr.write(line)
        for stream in container_streams:
            stream.write(line)
    for stream in (sys.stdout, sys.stderr, *container_streams):
        stream.flush()
    return {
        "identity": _identity(),
        "operations": [],
        "spammed_mib": int(params["mib"]),
        "container_log_streams_opened": len(container_streams),
    }


def _volume_stats(path: str) -> dict[str, int]:
    """What the kernel reports for the filesystem holding ``path`` (the capped volume's own
    numbers: size, free space, inode total and free)."""
    stat_result = os.statvfs(path)
    return {
        "total_bytes": stat_result.f_blocks * stat_result.f_frsize,
        "used_bytes": (stat_result.f_blocks - stat_result.f_bfree) * stat_result.f_frsize,
        "free_bytes": stat_result.f_bavail * stat_result.f_frsize,
        "inodes_total": stat_result.f_files,
        "inodes_used": stat_result.f_files - stat_result.f_ffree,
    }


def _tree_usage(root: str) -> dict[str, int]:
    """Allocated bytes (blocks actually held, not apparent size) and entries under ``root``."""
    allocated = apparent = entries = 0
    for directory, directories, files in os.walk(root):
        for name in (*directories, *files):
            entries += 1
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                allocated += info.st_blocks * 512
                apparent += info.st_size
    return {"allocated_bytes": allocated, "apparent_bytes": apparent, "entries": entries}


def _fill_bytes(root: str, params: dict[str, Any]) -> dict[str, Any]:
    chunk = b"\x5a" * (int(params.get("chunk_kib", 512)) * 1024)
    limit = int(params["max_mib"]) * 1024 * 1024
    written = files = 0
    failure: str | None = None
    try:
        while written < limit:
            with open(os.path.join(root, f"f{files}"), "wb") as handle:
                handle.write(chunk)
            written += len(chunk)
            files += 1
    except OSError as error:
        failure = errno.errorcode.get(error.errno or 0, type(error).__name__)
    return {"files_written": files, "bytes_written": written, "failure": failure}


def _fill_entries(root: str, params: dict[str, Any]) -> dict[str, Any]:
    created = 0
    failure: str | None = None
    try:
        for index in range(int(params["max_entries"])):
            os.close(os.open(os.path.join(root, f"i{index}"), os.O_WRONLY | os.O_CREAT, 0o600))
            created += 1
    except OSError as error:
        failure = errno.errorcode.get(error.errno or 0, type(error).__name__)
    return {"entries_created": created, "failure": failure}


def _exit_report(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """How a command the probe ran ended: its code, the signal that ended it, its stderr tail."""
    code = completed.returncode
    ended_by: str | None = None
    if code < 0:
        try:
            ended_by = signal.Signals(-code).name
        except ValueError:
            ended_by = f"signal {-code}"
    return {"exit": code, "signal": ended_by, "stderr": completed.stderr.strip()[-160:]}


def _fallocate_cli(path: str, mib: int) -> dict[str, Any]:
    """``fallocate -l <mib>M <path>`` as a separate process, so a signal ends it, not the probe."""
    try:
        completed = subprocess.run(
            ["fallocate", "-l", f"{mib}M", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"error": type(error).__name__}
    return _exit_report(completed)


def _fallocate_child(root: str, index: int, params: dict[str, Any], write_end: int) -> None:
    """One burst process: a ``fallocate`` (command line) below the per-file limit, then chunked
    ``posix_fallocate`` calls into new files until the first refusal. Reports one JSON object and
    exits."""
    report: dict[str, Any] = {"index": index}
    big_path = os.path.join(root, f"big{index}")
    report["big_fallocate"] = _fallocate_cli(big_path, int(params["big_mib"]))
    chunk = int(params["chunk_mib"]) * 1024 * 1024
    chunks = 0
    failure: str | None = None
    try:
        for number in range(int(params["max_chunks"])):
            descriptor = os.open(
                os.path.join(root, f"c{index}-{number}"), os.O_WRONLY | os.O_CREAT, 0o600
            )
            try:
                os.posix_fallocate(descriptor, 0, chunk)
                chunks += 1
            finally:
                os.close(descriptor)
    except OSError as error:
        failure = errno.errorcode.get(error.errno or 0, type(error).__name__)
    report.update(chunks_allocated=chunks, failure=failure)
    os.write(write_end, (json.dumps(report) + "\n").encode())


def _fallocate_burst(root: str, params: dict[str, Any]) -> dict[str, Any]:
    pipes: list[tuple[int, int]] = []
    pids: list[int] = []
    for index in range(int(params["procs"])):
        read_end, write_end = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_end)
            try:
                _fallocate_child(root, index, params, write_end)
            finally:
                os._exit(0)
        os.close(write_end)
        pipes.append((read_end, write_end))
        pids.append(pid)
    reports: list[dict[str, Any]] = []
    for (read_end, _), pid in zip(pipes, pids, strict=True):
        with os.fdopen(read_end, encoding="utf-8") as handle:
            for line in handle:
                with contextlib.suppress(ValueError):
                    reports.append(json.loads(line))
        os.waitpid(pid, 0)
    return {"processes": int(params["procs"]), "reports": reports}


def _file_size_limit(root: str, params: dict[str, Any]) -> dict[str, Any]:
    """One allocation below the per-file limit (``RLIMIT_FSIZE``) and one above it that still fits
    the space left on the volume after the first, so that only the per-file limit can be what
    stops the second; the free space before each allocation is recorded."""
    report: dict[str, Any] = {"rlimit_fsize": list(resource.getrlimit(resource.RLIMIT_FSIZE))}
    for label in ("under", "over"):
        path = os.path.join(root, f"{label}_limit")
        mib = int(params[f"{label}_mib"])
        # The free space just before the allocation: a request larger than it would end with
        # ENOSPC, so the observation carries it for the verdict to rule that out.
        free_before = _volume_stats(root)["free_bytes"]
        outcome: dict[str, Any] = {
            "requested_mib": mib,
            "free_bytes_before": free_before,
            **_fallocate_cli(path, mib),
        }
        try:
            info = os.lstat(path)
            outcome["allocated_bytes"] = info.st_blocks * 512
            outcome["apparent_bytes"] = info.st_size
        except OSError:
            outcome["allocated_bytes"] = None
        report[f"{label}_limit"] = outcome
    return report


def mode_disk(params: dict[str, Any]) -> dict[str, Any]:
    """Outgrow a capped volume in bytes, in entries or with a parallel ``fallocate`` burst, or
    outgrow the per-file size limit alone (``filesize``).

    Reports what the process saw (errno names) and what the kernel reports for the volume
    (``statvfs``), before and after. ``cleanup`` removes the fill afterwards so a full result
    volume cannot stop the supervisor from writing the result.
    """
    where = params["where"]
    root = os.path.join(where, "gate-fill")
    os.makedirs(root, exist_ok=True)
    if params.get("plant_links"):
        # A symlink and a FIFO in the volume. Read back to the host as data, the symlink comes
        # back as the same link (never followed) and the FIFO is skipped; the result volume
        # reads back neither.
        os.symlink("/etc/passwd", os.path.join(where, "gate-link"))
        os.mkfifo(os.path.join(where, "gate-fifo"))
    before = _volume_stats(where)
    kind = params["kind"]
    if kind == "bytes":
        outcome = _fill_bytes(root, params)
    elif kind == "entries":
        outcome = _fill_entries(root, params)
    elif kind == "burst":
        outcome = _fallocate_burst(root, params)
    elif kind == "filesize":
        outcome = _file_size_limit(root, params)
    else:
        raise SystemExit(f"unknown disk kind {kind!r}")
    after = _volume_stats(where)
    usage = _tree_usage(root)
    if params.get("cleanup"):
        shutil.rmtree(root, ignore_errors=True)
    return {
        "identity": _identity(),
        "operations": [],
        "kind": kind,
        "where": where,
        "volume_before": before,
        "volume_after": after,
        "fill_tree": usage,
        "fill": outcome,
    }


def mode_bomb(params: dict[str, Any]) -> dict[str, Any]:
    """Commit one tiny blob under ``paths`` different paths: a few KiB of pack, a huge tree."""
    head = _git(["rev-parse", "HEAD"]).stdout.strip()
    lines = [
        "blob",
        "mark :1",
        "data 5",
        "bomb",
        "",
        f"commit refs/heads/{params['branch']}",
        "committer Gate <gate@enginery.invalid> 0 +0000",
        "data 4",
        "bomb",
        f"from {head}",
    ]
    directories = int(params.get("dirs", 100))
    for index in range(int(params["paths"])):
        lines.append(f"M 100644 :1 bomb{index % directories}/f{index}")
    lines.append("")
    completed = _git(["fast-import", "--quiet"], stdin="\n".join(lines), timeout=300.0)
    _git(["checkout", "-q", "--force", params["branch"]], timeout=300.0)
    return {
        "identity": _identity(),
        "operations": [],
        "fast_import_exit": completed.returncode,
        "head": _git(["rev-parse", "HEAD"]).stdout.strip(),
    }


def mode_hookcommit(params: dict[str, Any]) -> dict[str, Any]:
    """Commit twice with a planted ``pre-commit`` hook: with the worker environment, then without
    its command-scoped git configuration (the control: the hook can run).

    The hook appends a line to ``/workspace/.git/gate-hook-ran.log`` each time it runs; the
    workspace comes back to the host when the worker exits, so the host reads the log itself.
    """
    log = "/workspace/.git/gate-hook-ran.log"
    hook = os.path.join(WORKSPACE, ".git/hooks/pre-commit")
    os.makedirs(os.path.dirname(hook), exist_ok=True)
    with open(hook, "w", encoding="utf-8") as handle:
        handle.write(f"#!/bin/sh\necho ran >> {log}\nexit 0\n")
    os.chmod(hook, 0o755)

    def runs() -> int:
        try:
            with open(log, encoding="utf-8") as handle:
                return len(handle.read().splitlines())
        except OSError:
            return 0

    env_keys = sorted(key for key in os.environ if key.startswith("GIT_CONFIG"))
    # The attack the environment defeats: repository configuration that re-enables the hook.
    _git(["config", "--local", "core.hooksPath", os.path.dirname(hook)])
    with open(os.path.join(WORKSPACE, "one.txt"), "w", encoding="utf-8") as handle:
        handle.write("one\n")
    _git(["add", "one.txt"])
    first = _git(["commit", "-q", "-m", "with the worker environment"])
    runs_after_first = runs()
    stripped = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG")}
    with open(os.path.join(WORKSPACE, "two.txt"), "w", encoding="utf-8") as handle:
        handle.write("two\n")
    _git(["add", "two.txt"])
    second = subprocess.run(
        # The system config is disabled for this control, so safe.directory (which the image sets
        # system-wide for the differently-owned bind mount) is given on the command line.
        [
            "git",
            "-c",
            "safe.directory=*",
            "commit",
            "-q",
            "-m",
            "without the command-scoped configuration",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=WORKSPACE,
        env={**stripped, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )
    return {
        "identity": _identity(),
        "operations": [],
        "git_config_env_keys": env_keys,
        "hooks_path_env_value": os.environ.get("GIT_CONFIG_VALUE_0"),
        "first_commit_exit": first.returncode,
        "hook_runs_after_commit_with_worker_environment": runs_after_first,
        "second_commit_exit": second.returncode,
        "hook_runs_after_commit_without_command_scoped_config": runs(),
        "head": _git(["rev-parse", "HEAD"]).stdout.strip(),
    }


def mode_listen(params: dict[str, Any]) -> dict[str, Any]:
    """Listen on every interface for ``seconds`` and report who connected."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", int(params["port"])))
    server.listen(8)
    server.settimeout(1.0)
    accepted: list[str] = []
    # The host watches the container's own log for this line (pid 1's stdout), then connects.
    with contextlib.suppress(OSError), open("/proc/1/fd/1", "w", encoding="utf-8") as stream:
        stream.write("GATE_READY\n")
    deadline = time.monotonic() + float(params["seconds"])
    stopped = False
    while time.monotonic() < deadline and not stopped:
        try:
            connection, peer = server.accept()
        except TimeoutError:
            continue
        # A peer that sends ``stop`` ends the listen early; any other connection is recorded.
        connection.settimeout(0.5)
        try:
            stopped = connection.recv(16).startswith(b"stop")
        except OSError:
            stopped = False
        if not stopped:
            accepted.append(peer[0])
        connection.close()
    server.close()
    return {"identity": _identity(), "operations": [], "accepted_peers": accepted}


MODES: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "read": mode_read,
    "write": mode_write,
    "escape": mode_escape,
    "network": mode_network,
    "git": mode_git,
    "plant": mode_plant,
    "relay": mode_relay,
    "memory": mode_memory,
    "pids": mode_pids,
    "cpu": mode_cpu,
    "sleep": mode_sleep,
    "session": mode_session,
    "objects": mode_objects,
    "forge": mode_forge,
    "spam": mode_spam,
    "disk": mode_disk,
    "bomb": mode_bomb,
    "hookcommit": mode_hookcommit,
    "listen": mode_listen,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in MODES:
        print(f"usage: factory_gate_probe.py {{{','.join(MODES)}}}", file=sys.stderr)
        return 2
    params = _load_params()
    observation = MODES[argv[1]](params)
    observation["mode"] = argv[1]
    _emit(observation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
