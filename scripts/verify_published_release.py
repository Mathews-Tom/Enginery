#!/usr/bin/env python3
"""Read-only post-publication verification for a real, already-published release.

Never publishes anything. Confirms a GitHub Release and a PyPI project
version both exist for the given tag/version, that the GitHub Release
targets the intended commit and is not a draft, and that every named
build artifact's `sha256` hash matches both PyPI's reported digest and
the bytes actually downloadable from the GitHub Release asset.

Requires `--confirm-published` as an explicit acknowledgment that this
verifies a real, already-published, effectively irreversible public
destination -- matching this repository's established opt-in discipline
for scripts that touch a real external destination.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request

_DEFAULT_PYPI_JSON_API_BASE = "https://pypi.org/pypi"


class VerificationError(RuntimeError):
    """A fatal, fail-closed post-publication verification failure."""


def _run_gh(args: list[str]) -> str:
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise VerificationError(f"'gh {' '.join(args)}' failed: {result.stderr.strip()}")
    return result.stdout


def _fetch_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        data: bytes = response.read()
    return data


def _resolve_tag_commit(*, repository: str, tag_name: str) -> str:
    """Resolve an annotated or lightweight tag ref to the commit sha it targets."""
    raw = _run_gh(["api", f"repos/{repository}/git/ref/tags/{tag_name}"])
    ref_object = json.loads(raw)["object"]
    if ref_object["type"] == "commit":
        return str(ref_object["sha"])
    if ref_object["type"] == "tag":
        tag_raw = _run_gh(["api", f"repos/{repository}/git/tags/{ref_object['sha']}"])
        tag_object = json.loads(tag_raw)["object"]
        if tag_object["type"] != "commit":
            raise VerificationError(f"tag {tag_name!r} does not ultimately point to a commit")
        return str(tag_object["sha"])
    raise VerificationError(f"unexpected tag object type {ref_object['type']!r} for {tag_name!r}")


def _verify_github_release(
    *, repository: str, tag_name: str, target_commitish: str, artifact_hashes: dict[str, str]
) -> dict[str, object]:
    raw = _run_gh(
        [
            "release",
            "view",
            tag_name,
            "--repo",
            repository,
            "--json",
            "tagName,isDraft,isPrerelease,assets,url",
        ]
    )
    release = json.loads(raw)

    if release.get("tagName") != tag_name:
        raise VerificationError(
            f"GitHub release tagName is {release.get('tagName')!r}, expected {tag_name!r}"
        )

    # `targetCommitish` on `gh release view` only reflects the exact commit when
    # GitHub itself creates the tag as part of release creation; for a release
    # built from an already-existing tag (this repository's convention: the tag
    # is created and pushed before the release), GitHub reports the default
    # branch name there instead. Resolve the tag ref itself -- which is
    # immutable once pushed -- to the commit it actually targets.
    observed_commit = _resolve_tag_commit(repository=repository, tag_name=tag_name)
    if observed_commit != target_commitish:
        raise VerificationError(
            "the tag does not resolve to the expected commit: "
            f"expected {target_commitish!r}, observed {observed_commit!r}"
        )
    if release.get("isDraft"):
        raise VerificationError("GitHub release is still a draft, not published")

    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    for filename, expected_sha256 in artifact_hashes.items():
        asset = assets.get(filename)
        if asset is None:
            raise VerificationError(
                f"GitHub release has no asset named {filename!r}; found {sorted(assets)}"
            )
        reported_digest = asset.get("digest")
        if reported_digest == f"sha256:{expected_sha256}":
            continue
        if reported_digest not in (None, ""):
            raise VerificationError(
                f"GitHub release asset {filename!r} digest mismatch: "
                f"expected sha256:{expected_sha256}, observed {reported_digest!r}"
            )
        download_url = asset.get("url")
        if not download_url:
            raise VerificationError(f"GitHub release asset {filename!r} has no download URL")
        try:
            observed_bytes = _fetch_url(download_url)
        except urllib.error.URLError as error:
            raise VerificationError(
                f"failed to download GitHub release asset {filename!r}: {error}"
            ) from error
        observed_sha256 = hashlib.sha256(observed_bytes).hexdigest()
        if observed_sha256 != expected_sha256:
            raise VerificationError(
                f"GitHub release asset {filename!r} sha256 mismatch: "
                f"expected {expected_sha256}, observed {observed_sha256}"
            )

    return {
        "repository": repository,
        "tag_name": tag_name,
        "target_commitish": target_commitish,
        "url": release.get("url"),
        "assets_verified": sorted(artifact_hashes),
    }


def _verify_pypi(
    *, project_name: str, version: str, artifact_hashes: dict[str, str], json_api_base: str
) -> dict[str, object]:
    url = f"{json_api_base}/{project_name}/{version}/json"
    try:
        raw = _fetch_url(url)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise VerificationError(f"PyPI does not yet report {project_name} {version}") from error
        raise VerificationError(f"PyPI JSON API request failed: {error}") from error
    except urllib.error.URLError as error:
        raise VerificationError(f"PyPI JSON API request failed: {error}") from error

    payload = json.loads(raw)
    reported_digests = {
        entry.get("digests", {}).get("sha256")
        for entry in payload.get("urls", [])
        if isinstance(entry, dict)
    }
    missing = [
        filename
        for filename, expected in artifact_hashes.items()
        if expected not in reported_digests
    ]
    if missing:
        raise VerificationError(f"PyPI does not report a matching sha256 digest for: {missing}")

    return {
        "project_name": project_name,
        "version": version,
        "json_api_base": json_api_base,
        "artifacts_verified": sorted(artifact_hashes),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-published",
        action="store_true",
        required=True,
        help="required acknowledgment that this verifies a real, already-published destination",
    )
    parser.add_argument("--repository", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--tag-name", required=True)
    parser.add_argument("--target-commitish", required=True)
    parser.add_argument("--project-name", required=True, help="PyPI project name")
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="FILENAME=SHA256",
        help="one built artifact filename and its expected sha256, repeatable",
    )
    parser.add_argument(
        "--pypi-json-api-base",
        default=_DEFAULT_PYPI_JSON_API_BASE,
        help=f"PyPI JSON API base (default: {_DEFAULT_PYPI_JSON_API_BASE})",
    )
    return parser


def _parse_artifacts(raw_artifacts: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    for raw in raw_artifacts:
        filename, separator, sha256 = raw.partition("=")
        if not separator or not filename or not sha256:
            raise VerificationError(f"--artifact must be FILENAME=SHA256, got {raw!r}")
        artifacts[filename] = sha256
    return artifacts


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        artifact_hashes = _parse_artifacts(args.artifact)
        github_evidence = _verify_github_release(
            repository=args.repository,
            tag_name=args.tag_name,
            target_commitish=args.target_commitish,
            artifact_hashes=artifact_hashes,
        )
        pypi_evidence = _verify_pypi(
            project_name=args.project_name,
            version=args.version,
            artifact_hashes=artifact_hashes,
            json_api_base=args.pypi_json_api_base,
        )
    except VerificationError as error:
        print(f"PUBLISHED-RELEASE VERIFICATION FAILED: {error}", file=sys.stderr)
        return 1

    evidence = {"github_release": github_evidence, "pypi": pypi_evidence}
    print(json.dumps(evidence, indent=2, sort_keys=True))
    print(f"PASS verify-published-release version={args.version} tag={args.tag_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
