#!/usr/bin/env python3
"""Manifest gate for the layer-per-FlowFile flow. Pushes a manifest only once everything it names is in the registry.

usage: manifest_gate.py <registry URL> <repository> <tag or digest> <wait seconds> [platform]
stdin: the raw manifest or index bytes, exactly as the source registry served them.
env:   REGISTRY_USER, REGISTRY_PASSWORD (empty password: no credentials, for an open registry)
Polls HEAD for the config and every layer (image manifest) or every child manifest (index) for up to the wait, then
PUTs the stdin bytes unchanged, so the digest and tag are the source's. Needs push_from_stdin.py beside it.
Exit codes: 0 pushed, 5 still incomplete after the wait, 10 credentials refused, 14 manifest refused or not JSON,
15 bad arguments or network error. On 5, stderr holds the line an operator reads:
  <repo:tag> (<platform>) incomplete: missing layers 2 and 3 of 4. Check code 677a46bf
  <repo:tag> (<platform>) incomplete: missing platform manifests 1 of 2. Check code 677a46bf
The check code is the first 8 hex characters of the sha256 of the stdin bytes."""
import hashlib
import json
import os
import sys
import time

import push_from_stdin as push

ACCEPT = ("application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, "
          "application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json")
POLL = 3


def listing(numbers):
    words = [str(n) for n in numbers]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def needs(document):
    """[(kind, digest)] the manifest names: its config and layers, or its child manifests."""
    if "manifests" in document:
        return [("manifests", child["digest"]) for child in document["manifests"]]
    return [("blobs", document["config"]["digest"])] + [("blobs", layer["digest"]) for layer in document["layers"]]


def present(registry, repository, kind, digest):
    response, _ = registry.step("HEAD", f"{registry.prefix}/v2/{repository}/{kind}/{digest}", headers={"Accept": ACCEPT})
    return response.status == 200


def incomplete(document, missing, label, check):
    if "manifests" in document:
        numbers = [n for n, child in enumerate(document["manifests"], 1) if ("manifests", child["digest"]) in missing]
        return f"{label} incomplete: missing platform manifests {listing(numbers)} of {len(document['manifests'])}. Check code {check}"
    layers = document["layers"]
    numbers = [n for n, layer in enumerate(layers, 1) if ("blobs", layer["digest"]) in missing]
    extra = " and the config blob" if ("blobs", document["config"]["digest"]) in missing else ""
    what = f"layers {listing(numbers)} of {len(layers)}" if numbers else "no layers"
    return f"{label} incomplete: missing {what}{extra}. Check code {check}"


def main(argv):
    if len(argv) not in (5, 6):
        push.fail(15, "usage: manifest_gate.py <registry URL> <repository> <tag|digest> <wait seconds> [platform]")
    url, repository, reference = argv[1].rstrip("/"), argv[2], argv[3]
    try:
        wait = float(argv[4])
    except ValueError:
        push.fail(15, f"wait {argv[4]!r} is not a number of seconds")
    platform = argv[5] if len(argv) == 6 else ""
    raw = sys.stdin.buffer.read(push.LIMIT + 1)
    if len(raw) > push.LIMIT:
        push.fail(14, "manifest larger than 64 MiB")
    try:
        document = json.loads(raw)
        wanted = needs(document)
    except (ValueError, KeyError, TypeError):
        push.fail(14, "stdin is not an image manifest or index")
    parsed = push.target(url)
    headers = push.authorization(url, parsed, repository, os.environ.get("REGISTRY_USER", ""),
                                 os.environ.get("REGISTRY_PASSWORD", ""))
    registry = push.Registry(parsed, headers)
    deadline, missing = time.time() + wait, wanted
    while True:
        missing = [item for item in missing if not present(registry, repository, *item)]
        if not missing:
            break
        if time.time() >= deadline:
            label = f"{repository}:{reference}" + (f" ({platform})" if platform not in ("", "-") else "")
            push.fail(5, incomplete(document, missing, label, hashlib.sha256(raw).hexdigest()[:8]))
        time.sleep(POLL)
    push.put_manifest(registry, repository, reference, raw)
    print(json.dumps({"pushed": f"{repository}:{reference}", "bytes": len(raw)}))


if __name__ == "__main__":
    main(sys.argv)
