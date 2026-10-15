import base64
import hashlib
import json
import unittest
from unittest.mock import patch

import nifi_stub
import RouteRegistryTransfer as module
from RouteRegistryTransfer import RouteRegistryTransfer
from container_registry_client import RegistryError

DIGEST = "sha256:" + "a" * 64
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


class FlowFile:
    def __init__(self, attributes=None, body=None, size=3 * 1024 ** 3):
        self.attributes, self.body, self.size = attributes or {}, body, size

    def getAttribute(self, name):
        return self.attributes.get(name)

    def getSize(self):
        return self.size

    def getContentsAsBytes(self):
        if self.body is None:
            raise AssertionError("a controller read streamed content")
        return self.body


class Response:
    def __init__(self, status):
        self.status = status

    def close(self):
        pass


class Registry:
    base = "https://registry.test"

    def __init__(self):
        self.status, self.exists = 404, True
        self.upload = self.base + "/v2/team/app/blobs/uploads/id?_state=opaque"
        self.foreign, self.calls, self.header = False, [], "Bearer scoped-token"

    def blob_exists(self, repository, digest):
        self.calls.append((repository, digest))
        return self.exists

    def _send(self, repository, actions, method, path, headers=None):
        self.calls.append((repository, actions, method, path))
        return Response(self.status if path else 200)

    def _with_auth(self, key, headers):
        return {"Authorization": self.header}

    def _expect(self, response, label, *codes):
        if response.status not in codes:
            raise RegistryError(label)

    def _guard(self, url, label):
        return not self.foreign

    def start_upload(self, repository):
        return self.upload


class TransferController(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        patcher = patch.object(module, "RegistryClient", return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.processor = RouteRegistryTransfer()
        self.processor.logger = nifi_stub.Logger()

    def facts(self, **changes):
        facts = {"kind": "blob", "repository": "team/app", "digest": DIGEST, "size": str(3 * 1024 ** 3),
                 "media.type": MANIFEST, "reference": "latest", "needs": json.dumps([DIGEST]),
                 "image": "team/app:latest", "platform": "linux/amd64", "check": "1234abcd"}
        facts.update(changes)
        return {"registry." + key: value for key, value in facts.items()}

    def run_flow(self, side, attributes, body=None, size=3 * 1024 ** 3, **properties):
        context = nifi_stub.FakeContext(RouteRegistryTransfer.PROPERTIES,
            {"Transfer Side": side, "Registry URL": "https://registry.test", **properties})
        return self.processor.transform(context, FlowFile(attributes, body, size))

    def test_job_batches_attributes_and_auth_without_a_retry_hop(self):
        job = {"kind": "blob", "repository": "team/app", "digest": DIGEST, "size": 3 * 1024 ** 3,
               "mediaType": MANIFEST, "reference": DIGEST, "needs": [], "image": "team/app:latest",
               "platform": "linux/amd64", "check": "1234abcd"}
        body = json.dumps(job).encode()
        result = self.run_flow("Low", {}, body, len(body))
        self.assertEqual(result.relationship, "http")
        self.assertEqual(result.attributes["registry.digest"], DIGEST)
        self.assertEqual(result.attributes["registry.http.method"], "GET")
        self.assertIsNone(result.contents)

    def test_manifest_job_decodes_exact_bytes_before_native_hashing(self):
        raw = b'{ "schemaVersion": 2 }\n'
        job = {"kind": "manifest", "repository": "team/app", "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
               "size": len(raw), "mediaType": MANIFEST, "reference": "latest", "needs": [], "image": "team/app:latest",
               "platform": "linux/amd64", "check": "1234abcd", "body": base64.b64encode(raw).decode()}
        body = json.dumps(job).encode()
        result = self.run_flow("Low", {}, body, len(body))
        self.assertEqual(result.relationship, "hash")
        self.assertEqual(result.contents, raw)

    def test_low_side_never_reads_a_three_gib_download(self):
        attrs = {**self.facts(), "registry.stage": "fetch", "invokehttp.status.code": "200", "content_SHA-256": "a" * 64}
        result = self.run_flow("Low", attrs)
        self.assertEqual(result.relationship, "hash")
        self.assertIsNone(result.contents)
        self.assertEqual(result.attributes["registry.auth.header"], "")
        attrs.update(result.attributes)
        result = self.run_flow("Low", attrs)
        self.assertEqual(result.relationship, "ready")
        self.assertIsNone(result.contents)

    def test_wrong_digest_or_size_preserves_the_content_for_hold(self):
        for size, actual in [(3 * 1024 ** 3 - 1, "a" * 64), (3 * 1024 ** 3, "b" * 64)]:
            with self.subTest(size=size, digest=actual):
                attrs = {**self.facts(), "registry.stage": "verify", "content_SHA-256": actual}
                result = self.run_flow("Low", attrs, size=size)
                self.assertEqual(result.relationship, "failure")
                self.assertIsNone(result.contents)

    def test_high_side_dedupes_without_reading_content(self):
        self.registry.status = 200
        result = self.run_flow("High", self.facts())
        self.assertEqual(result.relationship, "ready")
        self.assertIsNone(result.contents)

    def test_upload_uses_post_then_native_put_and_keeps_the_location_query(self):
        result = self.run_flow("High", self.facts(), **{"Target Repository": "target/app"})
        self.assertEqual(result.relationship, "http")
        self.assertEqual(result.attributes["registry.http.method"], "PUT")
        self.assertIn("?_state=opaque&digest=sha256%3A", result.attributes["registry.http.url"])
        self.assertEqual(self.registry.calls[0][0], "target/app")
        self.assertIsNone(result.contents)

    def test_foreign_upload_location_never_receives_credentials(self):
        self.registry.foreign = True
        result = self.run_flow("High", self.facts())
        self.assertEqual(result.relationship, "retry")
        self.assertNotIn("registry.http.url", result.attributes)
        self.assertEqual(result.attributes["registry.auth.header"], "")
        self.assertIn(("warn", "retrying: upload Location names a foreign host"), self.processor.logger.lines)

    def test_basic_credentials_are_not_published_in_attributes(self):
        self.registry.header = "Basic permanent-secret"
        for side in ("Low", "High"):
            with self.subTest(side=side):
                result = self.run_flow(side, self.facts())
                self.assertEqual(result.relationship, "http")
                self.assertEqual(result.attributes["registry.auth.header"], "")
                self.assertNotIn("permanent-secret", json.dumps(result.attributes))

    def test_http_retries_refresh_auth_but_permanent_refusals_hold(self):
        for status, expected in [(0, "retry"), (401, "retry"), (429, "retry"), (503, "retry"), (400, "failure"), (202, "failure")]:
            with self.subTest(status=status):
                result = self.run_flow("High", {**self.facts(), "registry.stage": "upload", "invokehttp.status.code": str(status)})
                self.assertEqual(result.relationship, expected)
                self.assertIsNone(result.contents)

    def test_missing_dependencies_report_human_fields_and_retry_without_content_reads(self):
        result = self.run_flow("High", self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST])))
        self.assertEqual(result.relationship, "retry")
        self.assertEqual(result.attributes["registry.retry.max"], "1000000")
        self.assertEqual(self.processor.logger.lines, [])  # waiting is normal: no warning until stale
        self.assertIn("team/app:latest (linux/amd64) incomplete: missing layers 1 of 1 and the config blob", result.attributes["registry.error"])
        self.assertIn("Check code 1234abcd", result.attributes["registry.error"])

    def test_waiting_manifest_checks_on_a_doubling_interval_and_skips_the_registry_until_due(self):
        manifest = self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST]))
        with patch("RouteRegistryTransfer.time.time", return_value=1000.0):
            first = self.run_flow("High", manifest)
        self.assertEqual(float(first.attributes["registry.wait.next"]), 1030.0)
        waiting = {**manifest, **first.attributes}
        self.registry.calls.clear()
        with patch("RouteRegistryTransfer.time.time", return_value=1020.0):
            early = self.run_flow("High", waiting)
        self.assertEqual(early.relationship, "retry")
        self.assertEqual(self.registry.calls, [])  # not due: no request at all
        nexts = []
        for check, now in enumerate([1030.0, 2000.0, 9000.0, 20000.0, 40000.0, 60000.0], start=2):
            with patch("RouteRegistryTransfer.time.time", return_value=now):
                result = self.run_flow("High", waiting)
            waiting = {**waiting, **result.attributes}
            self.assertEqual(waiting["registry.wait.checks"], str(check))
            nexts.append(float(waiting["registry.wait.next"]) - now)
        self.assertEqual(nexts, [60, 120, 240, 480, 960, 1800])  # doubles, capped at 30 min

    def test_manifest_goes_stale_after_the_configured_hours_with_one_warning_and_a_fresh_wait_on_replay(self):
        manifest = {**self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST])),
                    "registry.wait.since": "0", "registry.wait.checks": "40", "registry.wait.next": "0"}
        with patch("RouteRegistryTransfer.time.time", return_value=2 * 3600.0):
            result = self.run_flow("High", manifest, **{"Stale After Hours": "2"})
        self.assertEqual(result.relationship, "stale")
        self.assertIn("missing layers 1 of 1 and the config blob. Check code 1234abcd", result.attributes["registry.error"])
        self.assertEqual(result.attributes["registry.wait.since"], "")
        self.assertEqual(len(self.processor.logger.lines), 1)
        self.assertIn("moved to stale", self.processor.logger.lines[0][1])
        with patch("RouteRegistryTransfer.time.time", return_value=2 * 3600.0 - 1):
            result = self.run_flow("High", manifest, **{"Stale After Hours": "2"})
        self.assertEqual(result.relationship, "retry")

    def test_manifest_is_committed_by_digest_before_its_tag(self):
        self.registry.status = 200
        result = self.run_flow("High", self.facts(kind="manifest"))
        self.assertEqual(result.attributes["registry.stage"], "manifest-digest")
        attrs = {**self.facts(kind="manifest"), **result.attributes, "invokehttp.status.code": "201", "resp.Docker-Content-Digest": DIGEST}
        result = self.run_flow("High", attrs)
        self.assertEqual(result.attributes["registry.stage"], "manifest-tag")
        self.assertEqual(result.attributes["registry.http.url"], "https://registry.test/v2/team/app/manifests/latest")
        attrs.update(result.attributes)
        attrs.update({"invokehttp.status.code": "201", "resp.docker-content-digest": DIGEST})
        self.assertEqual(self.run_flow("High", attrs).relationship, "ready")

    def test_a_changed_manifest_is_never_tagged(self):
        attrs = {**self.facts(kind="manifest"), "registry.stage": "manifest-digest", "invokehttp.status.code": "201",
                 "resp.Docker-Content-Digest": "sha256:" + "b" * 64}
        self.assertEqual(self.run_flow("High", attrs).relationship, "failure")

    def test_malformed_metadata_does_not_start_an_http_request(self):
        for change in [{"digest": "bad"}, {"repository": "../admin"}, {"size": "-1"}, {"kind": "other"}]:
            result = self.run_flow("High", self.facts(**change))
            self.assertNotEqual(result.relationship, "http")
        self.assertEqual(self.registry.calls, [])

    def test_metadata_network_failure_is_a_bounded_retry(self):
        self.registry.blob_exists = lambda *args: (_ for _ in ()).throw(RegistryError("network unavailable"))
        result = self.run_flow("Low", self.facts())
        self.assertEqual(result.relationship, "retry")
        self.assertEqual(result.attributes["registry.retry.max"], "3")


if __name__ == "__main__":
    unittest.main()
