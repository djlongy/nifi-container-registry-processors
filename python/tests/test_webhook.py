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
    def __init__(self, body, sha="auto", **headers):
        self.body = body
        self.headers = headers
        self.sha = hashlib.sha256(body).hexdigest() if sha == "auto" else sha

    def getContentsAsBytes(self):
        return self.body

    def getAttribute(self, name):
        return self.sha if name == "X-Sha256" else self.headers.get(name)


def webhook(_registry, images, **fields):
    fields.setdefault("registry", "registry.test")
    body = {"images": images, **fields}
    return json.dumps(body).encode()


class ExportBase(unittest.TestCase):
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
        settings = {"Source Registry URL": "https://registry.test", "Allowed Repositories": "team/*", "Tar Output Directory": self.directory}
        settings.update(values)
        context = nifi_stub.FakeContext(ExportContainerImages.PROPERTIES, settings)
        return processor.transform(context, flowfile)

    def refused(self, flowfile, message, **values):
        result = self.run_export(flowfile, **values)
        self.assertEqual(result.relationship, "failure")
        self.assertIn(message, result.attributes["registry.error"])
        self.assertEqual(os.listdir(self.directory), [], "nothing is exported")


class Export(ExportBase):
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
        self.refused(FlowFile(webhook(self.registry, [self.image, image])), "secret/app is not in Allowed Repositories")

    def test_an_exact_entry_matches_only_itself(self):
        self.refused(FlowFile(webhook(self.registry, [self.image])), "team/app is not", **{"Allowed Repositories": "team/ap"})
        self.assertEqual(self.run_export(FlowFile(webhook(self.registry, [self.image])),
                                         **{"Allowed Repositories": "x/y\nteam/app"}).relationship, "success")

    def test_a_bad_repository_tag_or_digest_is_refused(self):
        for bad in ({**self.image, "repository": "team/../app"}, {**self.image, "tag": "-x"},
                    {**self.image, "digest": "sha256:xyz"}, {**self.image, "tag": "a b"}):
            with self.subTest(image=bad):
                self.refused(FlowFile(webhook(self.registry, [bad])), "not a valid repository, tag and digest",
                             **{"Allowed Repositories": "*"})

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


class StructuredAttributes(ExportBase):
    def setUp(self):
        super().setUp()
        self.registry.add_image("team/other", "2", "two")
        self.other = {"repository": "team/other", "tag": "2",
                      "digest": self.registry.manifest_digest("team/other", "2")}

    def test_every_image_has_numbered_attributes_in_body_order(self):
        attributes = self.run_export(FlowFile(webhook(self.registry, [self.image, self.other]))).attributes
        self.assertEqual(attributes["registry.image.count"], "2")
        for n, image in enumerate([self.image, self.other]):
            for field in ("repository", "tag", "digest"):
                self.assertEqual(attributes[f"registry.image.{n}.{field}"], image[field])
        self.assertEqual(attributes["registry.images"].split("\n"),
                         [f"{i['repository']}:{i['tag']}@{i['digest']}" for i in (self.image, self.other)])

    def test_ci_headers_are_never_read_or_overwritten(self):
        headers = {"X-CI-Project": "g/p", "X-CI-Pipeline-Url": "https://ci.example/1", "X-CI-Commit-Sha": "abc",
                   "X-Image-Count": "99", "X-Registry": "evil.example"}
        result = self.run_export(FlowFile(webhook(self.registry, [self.image]), **headers))
        self.assertEqual(result.relationship, "success")
        self.assertEqual(result.attributes["registry.image.count"], "1")
        self.assertEqual(result.attributes["registry.host"], "registry.test")
        self.assertFalse(set(headers) & set(result.attributes), "the incoming attributes are left to NiFi to keep")


class TarPerImage(StructuredAttributes):
    def per_image(self, flowfile, **values):
        return self.run_export(flowfile, **{"Tar Per Image": "true", **values})

    def test_default_is_one_tar(self):
        self.assertEqual(ExportContainerImages.TAR_PER_IMAGE.default_value, "false")

    def test_one_tar_and_one_line_per_image(self):
        result = self.per_image(FlowFile(webhook(self.registry, [self.image, self.other])))
        self.assertEqual(result.relationship, "success")
        lines = [json.loads(line) for line in result.contents.decode().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(sorted(os.listdir(self.directory)), sorted(line["filename"] for line in lines))
        self.assertEqual(result.attributes["registry.tar.count"], "2")
        self.assertEqual(result.attributes["registry.image.count"], "2")
        for line, image in zip(lines, [self.image, self.other]):
            self.assertEqual((line["registry.repository"], line["registry.tag"], line["registry.digest"]),
                             (image["repository"], image["tag"], image["digest"]))
            self.assertEqual(line["registry.host"], "registry.test")
            self.assertEqual(line["registry.image.count"], "1")
            self.assertEqual(line["registry.image.0.digest"], image["digest"])
            path = os.path.join(self.directory, line["filename"])
            self.assertEqual(hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest(), line["X-Sha256"])
            with tarfile.open(path) as tar:
                exported = json.load(tar.extractfile("./export.json"))["images"]
            self.assertEqual([i["digest"] for i in exported], [image["digest"]])

    def test_one_bad_image_exports_nothing(self):
        bad = {**self.other, "digest": "sha256:" + "0" * 64}
        self.refused(FlowFile(webhook(self.registry, [self.image, bad])), "is not at", **{"Tar Per Image": "true"})

    def test_a_failed_write_removes_the_tars_already_written(self):
        real, calls = export_module.write_export, []

        def flaky(client, path, images):
            calls.append(path)
            if len(calls) == 2:
                raise RuntimeError("disk full")
            return real(client, path, images)
        export_module.write_export = flaky
        self.addCleanup(setattr, export_module, "write_export", real)
        self.refused(FlowFile(webhook(self.registry, [self.image, self.other])), "disk full", **{"Tar Per Image": "true"})

    def test_max_export_size_applies_to_each_tar(self):
        from container_registry_client import plan_image
        sizes = [sum(plan_image(self.registry, i["repository"], i["digest"])[2].values()) for i in (self.image, self.other)]
        body = webhook(self.registry, [self.image, self.other])
        limit = {"Max Export Size": f"{max(sizes)} B"}
        self.refused(FlowFile(body), "over Max Export Size", **limit)
        self.assertEqual(self.per_image(FlowFile(body), **limit).relationship, "success")


class SharedLayers(ExportBase):
    def test_a_layer_shared_by_two_images_counts_once_in_one_bundle(self):
        from container_registry_client import bundle_bytes, plan_image
        layer = self.registry.add_blob("team/app", b"x" * 1000)
        images = []
        for name in ("a", "b"):
            config = json.dumps({"marker": name}).encode()
            manifest = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "size": len(config),
                                   "digest": self.registry.add_blob("team/app", config)},
                        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar", "size": 1000, "digest": layer}]}
            images.append({"repository": "team/app", "tag": name, "digest": self.registry.add_manifest("team/app", name, manifest)})
        blobs = [plan_image(self.registry, "team/app", i["digest"])[2] for i in images]
        distinct = bundle_bytes(blobs)
        self.assertLess(distinct, sum(sum(b.values()) for b in blobs))
        body = FlowFile(webhook(self.registry, images))
        self.assertEqual(self.run_export(body, **{"Max Export Size": f"{distinct} B"}).relationship, "success")
        for name in os.listdir(self.directory):
            os.remove(os.path.join(self.directory, name))
        self.refused(body, "over Max Export Size", **{"Max Export Size": f"{distinct - 1} B"})
