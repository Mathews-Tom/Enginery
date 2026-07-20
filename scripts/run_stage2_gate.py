#!/usr/bin/env python3
"""Opt-in Stage 2 gate: verify real GitHub Release and PyPI destinations.

Read-only: verifies an already-published release exists at both
destinations with the expected commit and artifact digest, and prints
the Stage 2 release evidence digest. Never publishes anything itself.

Requires ``--fixture-distribution`` to run at all, so it can never fire
as a side effect of an ordinary CI invocation -- verifying a real,
already-published public resource is deliberately opt-in, matching the
same discipline the M9 gate-G1 pilot used for its own live provider
mutation.
"""

from __future__ import annotations

import argparse
import json
import sys

from enginery.adapters.github import GitHubAdapterConfig, GitHubReleaseAdapter, GitHubReleaseRequest
from enginery.adapters.pypi import PyPiAdapter, PyPiAdapterConfig
from enginery.application.delivery_ports import PublicationReceipt
from enginery.domain.digests import Digest
from enginery.domain.errors import EngineryError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify a published Stage 2 fixture release.")
    parser.add_argument(
        "--fixture-distribution",
        action="store_true",
        required=True,
        help="Required acknowledgment that this verifies a real, already-published destination.",
    )
    parser.add_argument("--repository", required=True, help="owner/name of the GitHub repository")
    parser.add_argument("--tag-name", required=True)
    parser.add_argument("--target-commitish", required=True)
    parser.add_argument("--project-name", required=True, help="PyPI/TestPyPI project name")
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True, help="hex sha256 digest of the published wheel")
    parser.add_argument(
        "--json-api-base",
        default="https://test.pypi.org/pypi",
        help="PyPI JSON API base (default: test.pypi.org)",
    )
    parser.add_argument(
        "--index-url",
        default="https://test.pypi.org/simple/",
        help="index URL (for reference only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    digest = Digest(algorithm="sha256", hex_value=args.sha256)

    github_config = GitHubAdapterConfig(
        repository=args.repository, credential_reference="operator-authenticated-gh-cli"
    )
    github_release = GitHubReleaseAdapter(github_config)
    github_receipt = PublicationReceipt(
        destination="github-release", version=args.version, artifact_digest=digest
    )
    github_release.stage(
        digest,
        GitHubReleaseRequest(
            tag_name=args.tag_name,
            target_commitish=args.target_commitish,
            name=args.tag_name,
            body="verification-only staging; publish() is never called by this script",
        ),
    )

    pypi_config = PyPiAdapterConfig(
        project_name=args.project_name,
        index_url=args.index_url,
        publish_url="https://unused.invalid/",  # verify() never publishes; publish_url is unused
        json_api_base=args.json_api_base,
    )
    pypi = PyPiAdapter(pypi_config)
    pypi_receipt = PublicationReceipt(
        destination="pypi", version=args.version, artifact_digest=digest
    )

    try:
        verified_github = github_release.verify(github_receipt)
        verified_pypi = pypi.verify(pypi_receipt)
    except EngineryError as error:
        print(f"Stage 2 gate FAILED: {error}", file=sys.stderr)
        return 1

    evidence = {
        "repository": args.repository,
        "tag_name": args.tag_name,
        "target_commitish": args.target_commitish,
        "project_name": args.project_name,
        "version": args.version,
        "artifact_digest": str(digest),
        "github_release_verified": verified_github.destination == "github-release",
        "pypi_verified": verified_pypi.destination == "pypi",
    }
    evidence_digest = Digest.of_json(evidence)
    rendered = {**evidence, "evidence_digest": str(evidence_digest)}
    print(json.dumps(rendered, indent=2, sort_keys=True))
    print(f"Stage 2 release evidence digest: {evidence_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
