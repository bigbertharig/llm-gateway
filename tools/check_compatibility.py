#!/usr/bin/env python3
"""Check an optional four-repo tested set without making sibling repos mandatory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SCHEMA = "gpu-rig-compatible-set/1"
PRODUCTS = ("benchmarking", "llm-harness", "llm-gateway", "llm_orchestration")
ARTIFACTS = ("model-library", "tool-catalog")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{7,40}\Z")


def validate(manifest: dict) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported compatible-set schema")
    revisions = manifest.get("revisions")
    artifacts = manifest.get("artifacts")
    if not isinstance(revisions, dict) or set(revisions) != set(PRODUCTS):
        raise ValueError("compatible set must name all four repositories")
    if any(not isinstance(value, str) or not REVISION.fullmatch(value) for value in revisions.values()):
        raise ValueError("invalid tested revision")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACTS):
        raise ValueError("compatible set must name both artifact hashes")
    if any(not isinstance(value, str) or not SHA256.fullmatch(value) for value in artifacts.values()):
        raise ValueError("invalid tested artifact hash")


def assess(manifest: dict, revisions: dict[str, str], artifacts: dict[str, str],
           capabilities: list[str], live: bool) -> dict:
    """Only requested connected capabilities can block; standalone use needs no manifest."""
    validate(manifest)
    unknown = set(capabilities) - set(ARTIFACTS)
    if unknown:
        raise ValueError("unknown capability: " + ", ".join(sorted(unknown)))
    warnings = []
    for product, observed in revisions.items():
        if product not in PRODUCTS:
            raise ValueError("unknown product: " + product)
        if observed != manifest["revisions"][product]:
            warnings.append(f"{product} revision differs from tested set")
    status = {}
    for capability in capabilities:
        observed = artifacts.get(capability)
        expected = manifest["artifacts"][capability]
        if observed == expected:
            status[capability] = "compatible"
        elif observed is None:
            status[capability] = "unverified"
            warnings.append(f"{capability} artifact hash unavailable")
        else:
            status[capability] = "incompatible"
            warnings.append(f"{capability} artifact hash differs from tested set")
    blocked = sorted(name for name, state in status.items() if live and state != "compatible")
    return {"schema": SCHEMA, "mode": "live" if live else "planning",
            "warnings": warnings, "capabilities": status, "blocked": blocked,
            "standalone_allowed": True}


def artifact_hash(name: str, path: Path) -> str:
    if name == "tool-catalog":
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if name != "model-library":
        raise ValueError("unknown artifact: " + name)
    artifact = json.loads(path.read_text(encoding="utf-8"))
    if artifact.get("schema") != "gpu-rig-model-library/1":
        raise ValueError("unsupported model-library schema")
    digest = artifact.get("source_sha256")
    payload = {key: value for key, value in artifact.items() if key != "source_sha256"}
    calculated = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True).encode("utf-8")).hexdigest()
    if digest != calculated:
        raise ValueError("invalid model-library source hash")
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--revision", action="append", default=[], metavar="PRODUCT=SHA")
    parser.add_argument("--artifact", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--capability", action="append", default=[], choices=ARTIFACTS)
    parser.add_argument("--mode", choices=("planning", "live"), default="planning")
    args = parser.parse_args()
    try:
        revisions = dict(item.split("=", 1) for item in args.revision)
        paths = dict(item.split("=", 1) for item in args.artifact)
        if len(revisions) != len(args.revision) or len(paths) != len(args.artifact):
            raise ValueError("duplicate revision or artifact argument")
        observed = {name: artifact_hash(name, Path(path))
                    for name, path in paths.items()}
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = assess(manifest, revisions, observed, args.capability, args.mode == "live")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 2 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

