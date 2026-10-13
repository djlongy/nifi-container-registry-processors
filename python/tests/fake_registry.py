"""An in-memory registry with the client surface that plan_image, write_export and push_export use."""
import hashlib
import io
import json

import nifi_stub  # noqa: F401  (puts container_registry on sys.path)
from container_registry_client import RegistryError

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
CONFIG = "application/vnd.oci.image.config.v1+json"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    host = "registry.test"

    def __init__(self):
        self.blobs = {}       # (repository, digest) -> bytes
        self.manifests = {}   # (repository, reference) -> (bytes, media type)
        self.puts = []        # (repository, reference) in order

    # -- building ----------------------------------------------------------------------------
    def add_blob(self, repository, data):
        self.blobs[(repository, digest(data))] = data
        return digest(data)

    def add_image(self, repository, tag, marker, urls=None, layer_type=LAYER):
        config = json.dumps({"architecture": "amd64", "os": "linux", "marker": marker}).encode()
        layer = marker.encode()
        layer_descriptor = {"mediaType": layer_type, "size": len(layer), "digest": self.add_blob(repository, layer)}
        if urls:
            layer_descriptor["urls"] = urls
        manifest = {"schemaVersion": 2, "mediaType": MANIFEST,
                    "config": {"mediaType": CONFIG, "size": len(config), "digest": self.add_blob(repository, config)},
                    "layers": [layer_descriptor]}
        return self.add_manifest(repository, tag, manifest)

    def add_index(self, repository, tag, children):
        """children: [(digest, media type)]"""
        manifest = {"schemaVersion": 2, "mediaType": INDEX,
                    "manifests": [{"mediaType": t, "digest": d, "size": len(self.manifests[(repository, d)][0])}
                                  for d, t in children]}
        return self.add_manifest(repository, tag, manifest)

    def add_manifest(self, repository, tag, manifest):
        raw = json.dumps(manifest).encode()
        self.manifests[(repository, digest(raw))] = (raw, manifest["mediaType"])
        if tag:
            self.manifests[(repository, tag)] = (raw, manifest["mediaType"])
        return digest(raw)

    # -- the client surface ------------------------------------------------------------------
    def manifest_digest(self, repository, reference):
        found = self.manifests.get((repository, reference))
        return digest(found[0]) if found else None

    def get_manifest(self, repository, reference):
        found = self.manifests.get((repository, reference))
        if not found:
            raise RegistryError(f"no manifest {repository}:{reference}")
        return found

    def blob_exists(self, repository, blob):
        return (repository, blob) in self.blobs

    def open_blob(self, repository, blob):
        return io.BytesIO(self.blobs[(repository, blob)])

    def start_upload(self, repository, blob=None, mount_from=None):
        if mount_from and (mount_from, blob) in self.blobs:
            self.blobs[(repository, blob)] = self.blobs[(mount_from, blob)]
            return None
        return f"upload:{repository}"

    def upload_blob(self, repository, blob, stream, size, upload=None):
        data = stream.read(size) if size else b""
        self.blobs[(repository, blob)] = data

    def put_manifest(self, repository, reference, manifest, media_type):
        for descriptor in json.loads(manifest).get("manifests") or []:
            if (repository, descriptor["digest"]) not in self.manifests:
                raise RegistryError(f"push manifest {repository}:{reference}: HTTP 400 (missing {descriptor['digest']})")
        self.manifests[(repository, reference)] = (manifest, media_type)
        self.manifests[(repository, digest(manifest))] = (manifest, media_type)
        self.puts.append((repository, reference))
