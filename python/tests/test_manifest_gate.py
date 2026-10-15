"""python/scripts/manifest_gate.py: waits for what a manifest names, then PUTs its bytes unchanged."""
import hashlib
import json
import os
import subprocess
import sys
import unittest

from http_fixture import reply, serve
from test_push_from_stdin import FakeRegistry

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "manifest_gate.py")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class ManifestGate(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.addCleanup(self.registry.server_close)
        self.addCleanup(self.registry.shutdown)
        self.layers = [bytes([n]) * 10 for n in range(4)]
        self.config = b'{"os": "linux"}'
        self.image = json.dumps({
            "schemaVersion": 2, "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"digest": digest(self.config), "size": len(self.config)},
            "layers": [{"digest": digest(layer), "size": 10} for layer in self.layers]}, indent=3).encode()

    def gate(self, raw, reference="1.0", wait="0", platform="linux/amd64"):
        env = {**os.environ, "REGISTRY_USER": "", "REGISTRY_PASSWORD": ""}
        return subprocess.run([sys.executable, SCRIPT, self.registry.url, "team/app", reference, wait, platform],
                              input=raw, capture_output=True, env=env, timeout=60)

    def test_missing_layers_are_named_by_number_and_nothing_is_pushed(self):
        for blob in (self.config, self.layers[0], self.layers[3]):
            self.registry.blobs[digest(blob)] = blob
        done = self.gate(self.image)
        self.assertEqual(done.returncode, 5)
        self.assertEqual(done.stderr.decode().strip(),
                         f"team/app:1.0 (linux/amd64) incomplete: missing layers 2 and 3 of 4. "
                         f"Check code {hashlib.sha256(self.image).hexdigest()[:8]}")
        self.assertEqual(self.registry.manifests, {})

    def test_a_missing_config_is_added_to_the_line(self):
        for blob in self.layers:
            self.registry.blobs[digest(blob)] = blob
        self.assertIn("missing no layers and the config blob", self.gate(self.image).stderr.decode())

    def test_a_complete_image_is_put_with_its_bytes_unchanged(self):
        for blob in (self.config, *self.layers):
            self.registry.blobs[digest(blob)] = blob
        done = self.gate(self.image)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registry.manifests["1.0"],
                         (self.image, "application/vnd.docker.distribution.manifest.v2+json"))

    def test_a_foreign_layer_is_not_waited_for(self):
        foreign = b"windows base layer"
        document = json.loads(self.image)
        document["layers"].append({"digest": digest(foreign), "size": len(foreign), "urls": ["https://mcr.example/base"],
                                   "mediaType": "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"})
        raw = json.dumps(document).encode()
        for blob in (self.config, *self.layers):
            self.registry.blobs[digest(blob)] = blob
        done = self.gate(raw)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registry.manifests["1.0"][0], raw)
        document["layers"][-1].pop("urls")  # without urls it is an ordinary layer the registry must hold
        self.assertEqual(self.gate(json.dumps(document).encode()).returncode, 5)

    def test_a_blob_that_arrives_during_the_wait_releases_the_manifest(self):
        import threading
        import time
        self.registry.blobs[digest(self.config)] = self.config
        for blob in self.layers[:3]:
            self.registry.blobs[digest(blob)] = blob
        threading.Timer(1.0, lambda: self.registry.blobs.update({digest(self.layers[3]): self.layers[3]})).start()
        started = time.time()
        done = self.gate(self.image, wait="20")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertLess(time.time() - started, 15)

    def test_an_index_waits_for_its_child_manifests(self):
        children = [b'{"child": 1}', b'{"child": 2}']
        index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                            "manifests": [{"digest": digest(c), "size": len(c)} for c in children]}).encode()
        self.registry.manifests[digest(children[0])] = (children[0], "x")
        done = self.gate(index, reference="multi", platform="")
        self.assertEqual(done.returncode, 5)
        self.assertIn("team/app:multi incomplete: missing platform manifests 2 of 2.", done.stderr.decode())
        self.registry.manifests[digest(children[1])] = (children[1], "x")
        self.assertEqual(self.gate(index, reference="multi").returncode, 0)
        self.assertEqual(self.registry.manifests["multi"][0], index)

    def test_a_repository_or_tag_that_is_not_a_plain_name_exits_15_without_a_request(self):
        for repository, reference in [("../admin", "1.0"), ("team/app?x", "1.0"), ("team/app", "../1.0"), ("team/app", "a?b")]:
            with self.subTest(repository=repository, reference=reference):
                with serve(lambda handler, seen: reply(handler, 200)) as (url, seen):
                    env = {**os.environ, "REGISTRY_USER": "", "REGISTRY_PASSWORD": ""}
                    done = subprocess.run([sys.executable, SCRIPT, url, repository, reference, "0"], input=self.image,
                                          capture_output=True, env=env, timeout=60)
                self.assertEqual(done.returncode, 15, done.stderr)
                self.assertEqual(seen.requests, [])

    def test_input_that_is_not_a_manifest_exits_14(self):
        self.assertEqual(self.gate(b"[1, 2]").returncode, 14)


if __name__ == "__main__":
    unittest.main()
