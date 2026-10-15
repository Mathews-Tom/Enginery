from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from enginery.application.delivery_ports import PackageFile
from enginery.capabilities.closure import (
    MAX_FILE_BYTES,
    MAX_FILE_COUNT,
    MAX_TOTAL_BYTES,
    AssetKind,
    build_closure,
    build_closure_from_directory,
    is_license_file_name,
    is_license_like_name,
    is_notice_file_name,
    read_package_directory,
)
from enginery.capabilities.errors import (
    CapabilityAssetTypeError,
    CapabilityClosureLimitError,
    CapabilityManifestError,
    CapabilityPathError,
    CapabilitySymlinkError,
)
from tests.support.capabilities import package_files, pin_for, replace_file


def _write_tree(root: Path, files: Sequence[PackageFile]) -> None:
    for item in files:
        destination = root / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)
        destination.chmod(0o755 if item.executable else 0o644)


def _scripted(body: bytes = b"print('one')\n") -> tuple[PackageFile, ...]:
    return package_files("skill-a", extra={"scripts/run.py": body, "data/table.json": b"{}"})


def test_closure_records_every_file_with_kind_digest_and_size() -> None:
    closure = build_closure(_scripted())

    by_path = {record.path: record for record in closure.files}
    assert set(by_path) == {
        "SKILL.md",
        "capability.json",
        "data/table.json",
        "references/guide.md",
        "scripts/run.py",
    }
    assert by_path["SKILL.md"].kind is AssetKind.INSTRUCTION
    assert by_path["references/guide.md"].kind is AssetKind.REFERENCE
    assert by_path["scripts/run.py"].kind is AssetKind.SCRIPT
    assert by_path["data/table.json"].kind is AssetKind.DATA
    assert by_path["capability.json"].kind is AssetKind.METADATA
    assert by_path["scripts/run.py"].size == len(b"print('one')\n")


@pytest.mark.parametrize(
    "name",
    [
        "LICENSE",
        "license.md",
        "LICENCE.md",
        "LICENSE.MD",
        "COPYING.MD",
        "Copying.rst",
        "Notice.txt",
        "NOTICE",
    ],
)
def test_root_license_style_files_are_metadata_never_instructions(name: str) -> None:
    closure = build_closure(package_files("skill-a", extra={name: b"terms"}))

    assert {record.path: record.kind for record in closure.files}[name] is AssetKind.METADATA


@pytest.mark.parametrize(
    "name",
    ["LICENSE.md.txt", "LICENSES.md", "license-notes.md", "docs/LICENSE", "LICENSE.json", ""],
)
def test_other_names_are_not_license_files(name: str) -> None:
    assert not is_license_file_name(name)


_LICENSE_LIKE_NAMES = [
    "LICENSE-GPL.md",
    "LICENSE.gpl.md",
    "COPYING.LESSER.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE_GPL.md",
    "LICENSE.APACHE2",
    "LICENSE-BSD.txt",
    "NOTICE-THIRDPARTY.rst",
    "UNLICENSE.md",
    "LICENSES.md",
    "\uff2c\uff29\uff23\uff25\uff2e\uff33\uff25.md",
    "LICENSE-MIT",
    "LICENSE.MIT",
    "COPYING.LESSER",
    "UNLICENSE",
    "COPYRIGHT.txt",
    "license.md",
    "NOTICE",
]


@pytest.mark.parametrize("name", _LICENSE_LIKE_NAMES)
def test_every_license_like_root_name_is_metadata(name: str) -> None:
    assert is_license_like_name(name)
    closure = build_closure(package_files("skill-a", extra={name: b"terms"}))

    assert {record.path: record.kind for record in closure.files}[name] is AssetKind.METADATA


@pytest.mark.parametrize("name", ["README.md", "docs/LICENSE", "references/LICENSE.md", "SKILL.md"])
def test_other_names_are_not_license_like(name: str) -> None:
    assert not is_license_like_name(name)


def test_full_width_license_names_are_canonical_after_nfkc() -> None:
    assert is_license_file_name("\uff2c\uff29\uff23\uff25\uff2e\uff33\uff25.md")
    assert is_notice_file_name("\uff2e\uff2f\uff34\uff29\uff23\uff25")
    assert not is_notice_file_name("LICENSE")


def test_closure_digest_is_independent_of_file_order() -> None:
    files = _scripted()

    assert build_closure(files).digest == build_closure(tuple(reversed(files))).digest


def test_closure_digest_changes_when_a_sibling_script_changes() -> None:
    changed = replace_file(_scripted(), "scripts/run.py", b"print('two')\n")

    assert build_closure(_scripted()).digest != build_closure(changed).digest


def test_closure_digest_changes_when_a_reference_changes() -> None:
    changed = replace_file(_scripted(), "references/guide.md", b"edited")

    assert build_closure(_scripted()).digest != build_closure(changed).digest


def test_closure_digest_changes_when_a_data_file_changes() -> None:
    changed = replace_file(_scripted(), "data/table.json", b'{"a": 1}')

    assert build_closure(_scripted()).digest != build_closure(changed).digest


def test_closure_digest_changes_when_the_executable_bit_flips() -> None:
    plain = replace_file(_scripted(), "scripts/run.py", b"print('one')\n", executable=False)
    executable = replace_file(_scripted(), "scripts/run.py", b"print('one')\n", executable=True)

    assert build_closure(plain).digest != build_closure(executable).digest


def test_closure_digest_changes_when_a_file_is_added() -> None:
    extended = (*_scripted(), PackageFile("references/extra.md", b"more"))

    assert build_closure(_scripted()).digest != build_closure(extended).digest


def test_closure_digest_changes_when_a_dependency_pin_changes() -> None:
    first = package_files("skill-a", dependencies=[pin_for("dep-a", package_files("dep-a"))])
    second_dependency = package_files("dep-a", body=b"# different dependency\n")
    second = package_files("skill-a", dependencies=[pin_for("dep-a", second_dependency)])

    assert build_closure(first).digest != build_closure(second).digest


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "references/../../escape.md",
        "/etc/passwd.md",
        "./SKILL2.md",
        "references//guide2.md",
        "references/./guide2.md",
        "references\\guide2.md",
        "C:/windows.md",
        "bad\x00name.md",
        "caf\u0065\u0301.md",
        "",
        "a/" * 20 + "deep.md",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    files = (*package_files("skill-a"), PackageFile(path, b"payload"))

    with pytest.raises(CapabilityPathError):
        build_closure(files)


def test_duplicate_paths_are_rejected() -> None:
    files = (*package_files("skill-a"), PackageFile("SKILL.md", b"shadow"))

    with pytest.raises(CapabilityPathError, match="duplicate"):
        build_closure(files)


def test_case_colliding_paths_are_rejected() -> None:
    files = (*package_files("skill-a"), PackageFile("skill.md", b"shadow"))

    with pytest.raises(CapabilityPathError, match="case-colliding"):
        build_closure(files)


def test_a_path_that_is_both_file_and_directory_is_rejected() -> None:
    nested = PackageFile("references/guide.md/inner.md", b"under a file")
    files = (*package_files("skill-a"), nested)

    with pytest.raises(CapabilityPathError, match="both a file and a directory"):
        build_closure(files)


@pytest.mark.parametrize(
    "path",
    [
        "payload.exe",
        "run.sh",
        "scripts/run.rb",
        "scripts/tool",
        "references/diagram.png",
        "references/cache.pyc",
        "notes/extra.md",
        ".hidden",
        "SKILL.txt",
        "data/blob.bin",
    ],
)
def test_unreviewed_asset_types_are_rejected(path: str) -> None:
    files = (*package_files("skill-a"), PackageFile(path, b"payload"))

    with pytest.raises(CapabilityAssetTypeError):
        build_closure(files)


def test_an_executable_bit_on_a_non_script_is_rejected() -> None:
    files = (*package_files("skill-a"), PackageFile("references/tool.md", b"x", executable=True))

    with pytest.raises(CapabilityAssetTypeError, match="executable"):
        build_closure(files)


def test_a_package_without_a_manifest_is_rejected() -> None:
    files = tuple(item for item in package_files("skill-a") if item.path != "capability.json")

    with pytest.raises(CapabilityManifestError, match="no manifest"):
        build_closure(files)


def test_too_many_files_are_rejected() -> None:
    files = (
        *package_files("skill-a"),
        *(PackageFile(f"references/r{index}.md", b"x") for index in range(MAX_FILE_COUNT)),
    )

    with pytest.raises(CapabilityClosureLimitError, match="too many files"):
        build_closure(files)


def test_an_oversized_file_is_rejected() -> None:
    files = (*package_files("skill-a"), PackageFile("data/big.json", b"x" * (MAX_FILE_BYTES + 1)))

    with pytest.raises(CapabilityClosureLimitError, match="too large"):
        build_closure(files)


def test_an_oversized_package_is_rejected() -> None:
    chunk = b"x" * MAX_FILE_BYTES
    count = MAX_TOTAL_BYTES // MAX_FILE_BYTES + 1
    files = (
        *package_files("skill-a"),
        *(PackageFile(f"data/part{index}.json", chunk) for index in range(count)),
    )

    with pytest.raises(CapabilityClosureLimitError, match="package is too large"):
        build_closure(files)


def test_directory_closure_matches_the_in_memory_closure(tmp_path: Path) -> None:
    files = _scripted()
    _write_tree(tmp_path / "pkg", files)

    assert build_closure_from_directory(tmp_path / "pkg").digest == build_closure(files).digest


def test_directory_reader_reports_the_executable_bit(tmp_path: Path) -> None:
    executable = replace_file(_scripted(), "scripts/run.py", b"x", executable=True)
    _write_tree(tmp_path / "pkg", executable)

    read = {item.path: item for item in read_package_directory(tmp_path / "pkg")}

    assert read["scripts/run.py"].executable is True
    assert read["SKILL.md"].executable is False


def test_directory_reader_rejects_a_symlinked_file(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write_tree(package, package_files("skill-a"))
    secret = tmp_path / "secret.md"
    secret.write_text("outside the package", encoding="utf-8")
    (package / "references" / "leak.md").symlink_to(secret)

    with pytest.raises(CapabilitySymlinkError, match="symlink"):
        build_closure_from_directory(package)


def test_directory_reader_rejects_a_symlinked_directory(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write_tree(package, package_files("skill-a"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.md").write_text("outside", encoding="utf-8")
    (package / "data").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CapabilitySymlinkError, match="symlink"):
        build_closure_from_directory(package)


def test_directory_reader_rejects_a_symlinked_package_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    _write_tree(real, package_files("skill-a"))
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(CapabilitySymlinkError, match="directory is a symlink"):
        build_closure_from_directory(link)


def test_directory_reader_rejects_a_hardlinked_file(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write_tree(package, package_files("skill-a"))
    shared = tmp_path / "shared.md"
    shared.write_text("shared inode", encoding="utf-8")
    os.link(shared, package / "references" / "linked.md")

    with pytest.raises(CapabilitySymlinkError, match="hardlinked"):
        build_closure_from_directory(package)


def test_directory_reader_rejects_a_fifo(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write_tree(package, package_files("skill-a"))
    os.mkfifo(package / "references" / "pipe.md")

    with pytest.raises(CapabilitySymlinkError, match="non-regular"):
        build_closure_from_directory(package)


def test_directory_reader_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(CapabilityPathError, match="cannot be read"):
        read_package_directory(tmp_path / "missing")


def test_directory_reader_rejects_an_unreviewed_asset_on_disk(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    _write_tree(package, package_files("skill-a"))
    (package / "scripts").mkdir()
    (package / "scripts" / "payload.rb").write_text("puts 1", encoding="utf-8")

    with pytest.raises(CapabilityAssetTypeError):
        build_closure_from_directory(package)


@pytest.mark.parametrize(
    "name",
    ["LI\u200bCENSE.md", "LIC\u00adENSE.md", "dir/LICENSE\u2028.md", "a\u2029b.md"],
)
def test_format_and_separator_characters_in_a_path_are_rejected(name: str) -> None:
    with pytest.raises(CapabilityPathError):
        build_closure((*package_files("skill-a"), PackageFile(name, b"payload")))


@pytest.mark.parametrize("name", ["L\u0406CENSE.md", "L\u0456cense", "\u0441opying"])
def test_a_non_ascii_root_name_is_license_like_and_rejected(name: str) -> None:
    assert is_license_like_name(name)
    with pytest.raises(CapabilityAssetTypeError):
        build_closure((*package_files("skill-a"), PackageFile(name, b"payload")))


@pytest.mark.parametrize(
    "name", ["notice.so", "copyright.bin", "license-helper.sh", "licensed.pth", "LICENSE.md.sh"]
)
def test_license_like_names_with_executable_or_binary_shapes_are_rejected(name: str) -> None:
    with pytest.raises(CapabilityAssetTypeError):
        build_closure((*package_files("skill-a"), PackageFile(name, b"text")))


@pytest.mark.parametrize(
    "name",
    [
        *(f"license{ext}" for ext in (".pyw", ".ksh", ".command", ".lua", ".vbs", ".html")),
        *(f"license{ext}" for ext in (".svg", ".jsx", ".desktop", ".fish", ".csh", ".tcl")),
        "LICENSE.md.md",
        "LICENSE-MIT-extra.md",
        "LICENSE.mit.apache.bsd",
        "LICENSE-2.0.txt",
    ],
)
def test_license_like_names_outside_the_closed_word_list_are_rejected(name: str) -> None:
    with pytest.raises(CapabilityAssetTypeError):
        build_closure((*package_files("skill-a"), PackageFile(name, b"text")))


@pytest.mark.parametrize(
    "name", ["LICENSE-MIT", "COPYING.LESSER", "THIRD_PARTY_NOTICES.md", "LICENSE.APACHE2"]
)
def test_license_like_names_from_the_closed_word_list_are_metadata(name: str) -> None:
    closure = build_closure((*package_files("skill-a"), PackageFile(name, b"text")))

    assert {record.path: record.kind for record in closure.files}[name] is AssetKind.METADATA


@pytest.mark.parametrize("name", ["LICENSE", "LICENSE.md", "NOTICE.txt", "COPYING.LESSER"])
def test_license_like_files_must_be_utf8_text(name: str) -> None:
    with pytest.raises(CapabilityAssetTypeError):
        build_closure((*package_files("skill-a"), PackageFile(name, b"\xff\xfe\x00bad")))


@pytest.mark.parametrize("name", ["EULA.md", "LEGAL.txt", "TERMS", "PATENTS.md"])
def test_eula_legal_terms_and_patent_names_are_license_like(name: str) -> None:
    assert is_license_like_name(name)
    closure = build_closure((*package_files("skill-a"), PackageFile(name, b"terms")))

    assert {record.path: record.kind for record in closure.files}[name] is AssetKind.METADATA
