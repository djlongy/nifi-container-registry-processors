"""PlanRegistryTransfer: one job list per request, manifests as the exact source bytes, nothing planned on a refusal."""
import base64
import hashlib
import json
import unittest

import nifi_stub
import fake_registry
from fake_registry import FakeRegistry, digest

import PlanRegistryTransfer as plan_module
from PlanRegistryTransfer import PlanRegistryTransfer, parse_reference
from container_registry_client import RegistryError


class FlowFile:
    def __init__(self, body=b"", **attributes):
        self.body, self.attributes = body, attributes

    def getContentsAsBytes(self):
        return self.body

    def getAttribute(self, name):
        return self.attributes.get(name)


def webhook(images):
    body = json.dumps({"registry": "registry.test", "images": images}).encode()
    return FlowFile(body, **{"X-Sha256": hashlib.sha256(body).hexdigest()})


class PlanBase(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        original = plan_module.RegistryClient
        plan_module.RegistryClient = lambda *args, **kwargs: self.registry
        self.addCleanup(setattr, plan_module, "RegistryClient", original)

    def plan(self, flowfile, **values):
        processor = PlanRegistryTransfer()
        processor.logger = nifi_stub.Logger()
        settings = {"Source Registry URL": "https://registry.test", "Allowed Repositories": "team/*"}
        settings.update(values)
        return processor.transform(nifi_stub.FakeContext(PlanRegistryTransfer.PROPERTIES, settings), flowfile)

    def jobs(self, flowfile, **values):
        result = self.plan(flowfile, **values)
        self.assertEqual(result.relationship, "success", result.attributes)
        return json.loads(result.contents)["jobs"], result

    def layered(self, repository, tag, markers, layer_type=fake_registry.LAYER):
        """An image with one config and a layer per marker."""
        config = json.dumps({"architecture": "amd64", "os": "linux", "marker": markers[0]}).encode()
        layers = [{"mediaType": layer_type, "size": len(m), "digest": self.registry.add_blob(repository, m.encode())}
                  for m in markers]
        manifest = {"schemaVersion": 2, "mediaType": fake_registry.MANIFEST,
                    "config": {"mediaType": fake_registry.CONFIG, "size": len(config),
                               "digest": self.registry.add_blob(repository, config)}, "layers": layers}
        return self.registry.add_manifest(repository, tag, manifest)


class SingleImage(PlanBase):
    def test_a_tag_plans_config_layers_and_the_manifest_with_its_exact_bytes(self):
        top = self.layered("team/app", "1.0", ["a", "b", "c"])
        jobs, result = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0", "transfer.id": "t-1"}))
        self.assertEqual([j["kind"] for j in jobs], ["config", "blob", "blob", "blob", "manifest"])
        self.assertEqual([(j["layer"], j["layers"]) for j in jobs[:4]], [(0, 3), (1, 3), (2, 3), (3, 3)])
        manifest = jobs[-1]
        raw = self.registry.manifests[("team/app", "1.0")][0]
        self.assertEqual(base64.b64decode(manifest["body"]), raw)
        self.assertEqual((manifest["digest"], manifest["tag"], manifest["reference"], manifest["size"]),
                         (top, "1.0", "1.0", len(raw)))
        self.assertEqual({j["check"] for j in jobs}, {top[7:15]})
        self.assertEqual({j["transfer"] for j in jobs}, {"t-1"})
        self.assertEqual(manifest["needs"], [j["digest"] for j in jobs[:4]])
        self.assertEqual(result.attributes["planner.job.count"], "5")
        self.assertEqual(jobs[1]["size"], 1)

    def test_a_digest_reference_has_no_tag(self):
        top = self.layered("team/app", None, ["a"])
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": f"team/app@{top}"}))
        self.assertEqual((jobs[-1]["tag"], jobs[-1]["reference"]), ("", top))

    def test_a_webhook_body_plans_every_image(self):
        first = self.layered("team/app", "1", ["a"])
        second = self.layered("team/other", "2", ["b"])
        jobs, _ = self.jobs(webhook([{"repository": "team/app", "tag": "1", "digest": first},
                                     {"repository": "team/other", "tag": "2", "digest": second}]))
        self.assertEqual([(j["repository"], j["kind"]) for j in jobs if j["kind"] == "manifest"],
                         [("team/app", "manifest"), ("team/other", "manifest")])

    def test_a_blob_shared_by_two_layers_is_planned_once(self):
        self.layered("team/app", "1.0", ["same", "same"])
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0"}))
        self.assertEqual([j["kind"] for j in jobs], ["config", "blob", "manifest"])

    def test_a_transfer_id_is_made_when_none_is_given(self):
        self.layered("team/app", "1.0", ["a"])
        jobs, result = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0"}))
        self.assertTrue(result.attributes["transfer.id"])
        self.assertEqual(jobs[0]["transfer"], result.attributes["transfer.id"])

    def test_a_foreign_layer_is_not_planned(self):
        foreign = "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"
        self.registry.add_image("team/app", "1.0", "win", urls=["https://example.invalid/x"], layer_type=foreign)
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0"}))
        self.assertEqual([j["kind"] for j in jobs], ["config", "manifest"])


class Indexes(PlanBase):
    def index(self):
        amd = self.layered("team/app", None, ["amd"])
        arm = self.registry.add_manifest("team/app", None, json.loads(self.registry.manifests[("team/app", amd)][0]) | {"annotations": {"a": "b"}})
        root = self.registry.add_manifest("team/app", "multi", {
            "schemaVersion": 2, "mediaType": fake_registry.INDEX,
            "manifests": [{"mediaType": fake_registry.MANIFEST, "digest": amd, "size": 1,
                           "platform": {"os": "linux", "architecture": "amd64"}},
                          {"mediaType": fake_registry.MANIFEST, "digest": arm, "size": 1,
                           "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}}]})
        return root, amd, arm

    def test_all_platforms_keep_the_index_last_with_the_tag(self):
        root, amd, arm = self.index()
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:multi"}))
        manifests = [j for j in jobs if j["kind"] in ("manifest", "index")]
        self.assertEqual([j["digest"] for j in manifests], [amd, arm, root])
        self.assertEqual([j["platform"] for j in manifests], ["linux/amd64", "linux/arm64/v8", "-"])
        self.assertEqual((manifests[-1]["kind"], manifests[-1]["tag"], manifests[-1]["needs"]), ("index", "multi", [amd, arm]))
        self.assertEqual(manifests[0]["tag"], "")
        self.assertEqual(base64.b64decode(manifests[-1]["body"]), self.registry.manifests[("team/app", root)][0])
        self.assertEqual({j["check"] for j in jobs}, {root[7:15]})

    def test_a_platform_selection_drops_the_index_and_tags_the_one_match(self):
        _root, amd, arm = self.index()
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:multi"}),
                            **{"Platform Selection": "linux/arm64"})
        manifests = [j for j in jobs if j["kind"] in ("manifest", "index")]
        self.assertEqual([(j["digest"], j["tag"]) for j in manifests], [(arm, "multi")])

    def test_a_selection_that_matches_nothing_fails(self):
        self.index()
        result = self.plan(FlowFile(**{"registry.image.reference": "team/app:multi"}), **{"Platform Selection": "windows/amd64"})
        self.assertEqual(result.relationship, "failure")
        self.assertIn("matches Platform Selection", result.attributes["registry.error"])

    def test_nesting_beyond_maximum_manifest_depth_fails(self):
        leaf = self.layered("team/app", None, ["a"])
        top, media = leaf, fake_registry.MANIFEST
        for n in range(3):
            top = self.registry.add_index("team/app", "deep" if n == 2 else None, [(top, media)])
            media = fake_registry.INDEX
        self.assertEqual(self.plan(FlowFile(**{"registry.image.reference": "team/app:deep"}),
                                   **{"Maximum Manifest Depth": "2"}).relationship, "failure")
        self.assertEqual(self.plan(FlowFile(**{"registry.image.reference": "team/app:deep"}),
                                   **{"Maximum Manifest Depth": "3"}).relationship, "success")


class Refusals(PlanBase):
    def refused(self, flowfile, message, **values):
        result = self.plan(flowfile, **values)
        self.assertEqual(result.relationship, "failure")
        self.assertIn(message, result.attributes["registry.error"])
        self.assertIsNone(result.contents)

    def test_a_repository_outside_the_allowlist(self):
        self.layered("other/app", "1", ["a"])
        self.refused(FlowFile(**{"registry.image.reference": "other/app:1"}), "not in Allowed Repositories")

    def test_a_tag_that_does_not_exist(self):
        self.refused(FlowFile(**{"registry.image.reference": "team/app:nope"}), "is not at any digest")

    def test_a_webhook_digest_that_is_not_the_tags_digest(self):
        self.layered("team/app", "1", ["a"])
        self.refused(webhook([{"repository": "team/app", "tag": "1", "digest": "sha256:" + "0" * 64}]), "is not at sha256:")

    def test_a_webhook_with_a_wrong_checksum(self):
        flowfile = webhook([])
        flowfile.attributes["X-Sha256"] = "bad"
        self.refused(flowfile, "X-Sha256")

    def test_a_missing_blob(self):
        self.layered("team/app", "1", ["a"])
        self.registry.blobs.clear()
        self.refused(FlowFile(**{"registry.image.reference": "team/app:1"}), "is missing")

    def test_more_jobs_than_the_maximum(self):
        self.layered("team/app", "1", ["a", "b"])
        self.refused(FlowFile(**{"registry.image.reference": "team/app:1"}), "Maximum Jobs Per Transfer",
                     **{"Maximum Jobs Per Transfer": "3"})

    def test_a_reference_that_is_neither_tag_nor_digest(self):
        for bad in ("team/app", "Team/App:1", "team/app@sha256:xyz", ""):
            with self.assertRaisesRegex(RegistryError, "not a repository:tag or repository@sha256:digest reference", msg=bad):
                parse_reference(bad)

    def test_a_reference_with_tag_and_digest(self):
        self.assertEqual(parse_reference("a/b:1@sha256:" + "a" * 64), ("a/b", "1", "sha256:" + "a" * 64))


class Resend(PlanBase):
    def test_named_layers_and_the_config_are_planned_without_a_manifest(self):
        top = self.layered("team/app", "1.0", ["a", "b", "c", "d"])
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0", "registry.layers": "2, 3",
                                        "registry.check": top[7:15]}))
        self.assertEqual([(j["kind"], j["layer"]) for j in jobs], [("config", 0), ("blob", 2), ("blob", 3)])

    def test_a_layer_repeated_at_two_positions_can_be_resent_by_either_position(self):
        top = self.layered("team/app", "1.0", ["a", "b", "a"])
        for position in ("1", "3"):
            jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0", "registry.layers": position,
                                            "registry.check": top[7:15]}))
            self.assertEqual([j["digest"] for j in jobs if j["kind"] == "blob"], [digest(b"a")], position)
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0", "registry.layers": "1,3",
                                        "registry.check": top[7:15]}))
        self.assertEqual(len([j for j in jobs if j["kind"] == "blob"]), 1, "the repeated blob is sent once")

    def test_a_repeated_layer_does_not_hide_the_layer_count(self):
        self.layered("team/app", "1.0", ["a", "b", "a"])
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:1.0"}))
        self.assertEqual({j["layers"] for j in jobs if j["kind"] != "manifest"}, {3})

    def test_a_moved_tag_is_refused_and_nothing_is_sent(self):
        self.layered("team/app", "1.0", ["a", "b"])
        result = self.plan(FlowFile(**{"registry.image.reference": "team/app:1.0", "registry.layers": "2",
                                       "registry.check": "deadbeef"}))
        self.assertEqual(result.relationship, "failure")
        self.assertIn("does not match", result.attributes["registry.error"])
        self.assertIsNone(result.contents)

    def test_layers_without_a_check_code_or_a_layer_that_does_not_exist_fail(self):
        top = self.layered("team/app", "1.0", ["a", "b"])
        for layers, check, message in (("2", "", "registry.check"), ("x", top[7:15], "layer numbers"),
                                       ("5", top[7:15], "no layer 5"), ("0", top[7:15], "from 1")):
            result = self.plan(FlowFile(**{"registry.image.reference": "team/app:1.0", "registry.layers": layers,
                                           "registry.check": check}))
            self.assertEqual(result.relationship, "failure", layers)
            self.assertIn(message, result.attributes["registry.error"])

    def test_the_platform_picks_which_manifest_the_numbers_count_in(self):
        root, amd, arm = Indexes.index(self)
        jobs, _ = self.jobs(FlowFile(**{"registry.image.reference": "team/app:multi", "registry.layers": "1",
                                        "registry.check": root[7:15], "registry.platform": "linux/arm64/v8"}))
        self.assertEqual([(j["kind"], j["platform"]) for j in jobs], [("config", "linux/arm64/v8"), ("blob", "linux/arm64/v8")])


if __name__ == "__main__":
    unittest.main()
