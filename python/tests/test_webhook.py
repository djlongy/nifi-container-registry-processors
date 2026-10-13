"""ExportContainerImages: every refusal exports nothing, and a good webhook exports one tar."""
import hashlib
import json
import os
import pathlib
import tarfile
import tempfile
import unittest

import nifi_stub
from fake_registry import FakeRegistry

import ExportContainerImages as export_module
from ExportContainerImages import ExportContainerImages


class FlowFile:
    def __init__(self, body, sha="auto"):
        self.body = body
        self.sha = hashlib.sha256(body).hexdigest() if sha == "auto" else sha

    def getContentsAsBytes(self):
        return self.body

    def getAttribute(self, name):
        return self.sha if name == "X-Sha256" else None


def webhook(_registry, images, **fields):
    fields.setdefault("registry", "registry.test")
    body = {"images": images, **fields}
    return json.dumps(body).encode()


class Export(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.registry = FakeRegistry()
        self.digest = self.registry.add_image("team/app", "1.0", "one")
        self.image = {"repository": "team/app", "tag": "1.0", "digest": self.digest}
        original = export_module.RegistryClient
        export_module.RegistryClient = lambda *args, **kwargs: self.registry
        self.addCleanup(setattr, export_module, "RegistryClient", original)

    def run_export(self, flowfile, **values):
        processor = ExportContainerImages()
        processor.logger = nifi_stub.Logger()
        settings = {"Registry URL": "https://registry.test", "Repositories": "team/*", "Output Directory": self.directory}
        settings.update(values)
        context = nifi_stub.FakeContext(ExportContainerImages.PROPERTIES, settings)
        return processor.transform(context, flowfile)

    def refused(self, flowfile, message, **values):
        result = self.run_export(flowfile, **values)
        self.assertEqual(result.relationship, "failure")
        self.assertIn(message, result.attributes["registry.error"])
        self.assertEqual(os.listdir(self.directory), [], "nothing is exported")

    def test_a_good_webhook_exports_one_tar(self):
        self.registry.add_image("team/other", "2", "two")
        other = {"repository": "team/other", "tag": "2", "digest": self.registry.manifest_digest("team/other", "2")}
        result = self.run_export(FlowFile(webhook(self.registry, [self.image, other])))
        self.assertEqual(result.relationship, "success")
        attributes = result.attributes
        tars = os.listdir(self.directory)
        self.assertEqual(tars, [attributes["filename"]])
        path = os.path.join(self.directory, tars[0])
        self.assertEqual(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest(), attributes["X-Sha256"])
        self.assertEqual(attributes["X-Artifact-Type"], "container-images")
        self.assertEqual(attributes["registry.host"], "registry.test")
        self.assertEqual(attributes["registry.images"].splitlines(),
                         [f"team/app:1.0@{self.digest}", f"team/other:2@{other['digest']}"])
        self.assertEqual(attributes["file.size"], str(os.path.getsize(path)))
        with tarfile.open(path) as tar:
            index = json.load(tar.extractfile("./export.json"))
        self.assertEqual([i["target"] for i in index["images"]], ["team/app", "team/other"])

    def test_a_missing_or_wrong_sha_is_refused(self):
        body = webhook(self.registry, [self.image])
        self.refused(FlowFile(body, sha=None), "X-Sha256")
        self.refused(FlowFile(body, sha="0" * 64), "X-Sha256")
        self.refused(FlowFile(body, sha=hashlib.sha256(body).hexdigest().upper()), "X-Sha256")

    def test_a_body_of_the_wrong_shape_is_refused(self):
        for body in (b"not json", b"[]", b'{"registry": "registry.test"}', b'{"registry": "registry.test", "images": []}',
                     b'{"registry": "registry.test", "images": [{"repository": "team/app"}]}',
                     b'{"registry": "registry.test", "images": [{"repository": 1, "tag": "1", "digest": "d"}]}'):
            with self.subTest(body=body):
                self.refused(FlowFile(body), "the body is not JSON")

    def test_another_registry_host_is_refused(self):
        self.refused(FlowFile(webhook(self.registry, [self.image], registry="evil.test")), "evil.test")

    def test_the_port_443_is_the_same_host(self):
        result = self.run_export(FlowFile(webhook(self.registry, [self.image], registry="registry.test:443")))
        self.assertEqual(result.relationship, "success")

    def test_a_repository_outside_the_allowlist_is_refused(self):
        self.registry.add_image("secret/app", "1", "x")
        image = {"repository": "secret/app", "tag": "1", "digest": self.registry.manifest_digest("secret/app", "1")}
        self.refused(FlowFile(webhook(self.registry, [self.image, image])), "secret/app is not in Repositories")

    def test_an_exact_entry_matches_only_itself(self):
        self.refused(FlowFile(webhook(self.registry, [self.image])), "team/app is not", **{"Repositories": "team/ap"})
        self.assertEqual(self.run_export(FlowFile(webhook(self.registry, [self.image])),
                                         Repositories="x/y\nteam/app").relationship, "success")

    def test_a_bad_repository_tag_or_digest_is_refused(self):
        for bad in ({**self.image, "repository": "team/../app"}, {**self.image, "tag": "-x"},
                    {**self.image, "digest": "sha256:xyz"}, {**self.image, "tag": "a b"}):
            with self.subTest(image=bad):
                self.refused(FlowFile(webhook(self.registry, [bad])), "not a valid repository, tag and digest",
                             Repositories="*")

    def test_a_digest_that_is_not_the_registrys_now_is_refused(self):
        self.refused(FlowFile(webhook(self.registry, [{**self.image, "digest": "sha256:" + "0" * 64}])),
                     "is not at sha256:")

    def test_a_tag_gone_from_the_registry_is_refused(self):
        self.refused(FlowFile(webhook(self.registry, [{**self.image, "tag": "9.9"}])), "is not at")

    def test_one_bad_image_exports_none(self):
        self.registry.add_image("team/other", "2", "two")
        bad = {"repository": "team/other", "tag": "2", "digest": "sha256:" + "1" * 64}
        self.refused(FlowFile(webhook(self.registry, [self.image, bad])), "team/other:2")

    def test_over_max_export_size_is_refused(self):
        self.refused(FlowFile(webhook(self.registry, [self.image])), "Max Export Size", **{"Max Export Size": "1 B"})


if __name__ == "__main__":
    unittest.main()
