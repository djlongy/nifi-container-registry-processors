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
        self.stored = DIGEST

    def blob_exists(self, repository, digest):
        self.calls.append((repository, digest))
        return self.exists

    def manifest_digest(self, repository, reference):
        self.calls.append((repository, "manifest_digest", reference))
        return self.stored

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


def retry_flowfile(attributes, uuid="retry-card"):
    """RetryFlowFile 2.12.0 as the flow wires it: Retry Attribute registry.retries, Maximum Retries
    ${registry.retry.max}, Reuse Mode fail. Returns the relationship and the attributes it leaves."""
    attributes = dict(attributes)
    owner = attributes.get("registry.retries.uuid")
    if owner and owner != uuid:
        return "failure", attributes
    count = attributes.get("registry.retries")
    current = 1 if count is None else int(count) + 1
    if current > int(attributes["registry.retry.max"]):
        return "retries_exceeded", attributes
    attributes.update({"registry.retries": str(current), "registry.retries.uuid": uuid})
    return "retry", attributes


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
        self.assertEqual(result.relationship, "prepared")
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
        self.assertEqual(result.relationship, "unverified")
        self.assertEqual(result.contents, raw)

    def test_low_side_never_reads_a_three_gib_download(self):
        attrs = {**self.facts(), "registry.stage": "fetch", "invokehttp.status.code": "200", "content_SHA-256": "a" * 64}
        result = self.run_flow("Low", attrs)
        self.assertEqual(result.relationship, "unverified")
        self.assertIsNone(result.contents)
        self.assertEqual(result.attributes["registry.auth.header"], "")
        attrs.update(result.attributes)
        result = self.run_flow("Low", attrs)
        self.assertEqual(result.relationship, "complete")
        self.assertIsNone(result.contents)

    def test_wrong_digest_or_size_preserves_the_content_for_hold(self):
        for size, actual in [(3 * 1024 ** 3 - 1, "a" * 64), (3 * 1024 ** 3, "b" * 64)]:
            with self.subTest(size=size, digest=actual):
                attrs = {**self.facts(), "registry.stage": "verify", "content_SHA-256": actual}
                result = self.run_flow("Low", attrs, size=size)
                self.assertEqual(result.relationship, "failure")
                self.assertIsNone(result.contents)

    def test_a_followed_storage_redirect_leaves_no_signed_url_for_the_diode(self):
        signed = "https://storage.example/blob?X-Amz-Signature=secret"
        attrs = {**self.facts(), "registry.stage": "fetch", "invokehttp.status.code": "200", "invokehttp.response.url": signed}
        result = self.run_flow("Low", attrs)
        self.assertEqual(result.relationship, "unverified")
        self.assertEqual(result.attributes["invokehttp.response.url"], "")

    def test_high_side_dedupes_without_reading_content(self):
        self.registry.status = 200
        result = self.run_flow("High", self.facts())
        self.assertEqual(result.relationship, "complete")
        self.assertIsNone(result.contents)

    def test_upload_uses_post_then_native_put_and_keeps_the_location_query(self):
        result = self.run_flow("High", self.facts(), **{"Target Repository": "target/app"})
        self.assertEqual(result.relationship, "prepared")
        self.assertEqual(result.attributes["registry.http.method"], "PUT")
        self.assertIn("?_state=opaque&digest=sha256%3A", result.attributes["registry.http.url"])
        self.assertEqual(self.registry.calls[0][0], "target/app")
        self.assertIsNone(result.contents)

    def test_foreign_upload_location_never_receives_credentials(self):
        self.registry.foreign = True
        result = self.run_flow("High", self.facts())
        self.assertEqual(result.relationship, "retryable")
        self.assertNotIn("registry.http.url", result.attributes)
        self.assertEqual(result.attributes["registry.auth.header"], "")
        self.assertIn(("warn", "retrying: upload Location names a foreign host"), self.processor.logger.lines)

    def test_basic_credentials_are_not_published_in_attributes(self):
        self.registry.header = "Basic permanent-secret"
        for side in ("Low", "High"):
            with self.subTest(side=side):
                result = self.run_flow(side, self.facts())
                self.assertEqual(result.relationship, "prepared")
                self.assertEqual(result.attributes["registry.auth.header"], "")
                self.assertNotIn("permanent-secret", json.dumps(result.attributes))

    def test_http_retries_refresh_auth_but_permanent_refusals_hold(self):
        for status, expected in [(0, "retryable"), (401, "retryable"), (429, "retryable"), (503, "retryable"), (400, "failure"), (202, "failure")]:
            with self.subTest(status=status):
                result = self.run_flow("High", {**self.facts(), "registry.stage": "upload", "invokehttp.status.code": str(status)})
                self.assertEqual(result.relationship, expected)
                self.assertIsNone(result.contents)

    def test_missing_dependencies_report_human_fields_and_retry_without_content_reads(self):
        result = self.run_flow("High", self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST])))
        self.assertEqual(result.relationship, "waiting")
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
        self.assertEqual(early.relationship, "waiting")
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
        self.assertEqual(result.relationship, "waiting")

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
        self.assertEqual(self.run_flow("High", attrs).relationship, "complete")

    def test_a_forged_callback_cannot_redirect_the_tag_put_or_choose_its_credentials(self):
        attrs = {**self.facts(kind="manifest"), "registry.stage": "manifest-digest", "invokehttp.status.code": "201",
                 "resp.Docker-Content-Digest": DIGEST, "registry.auth.header": "Bearer attacker-chosen",
                 "registry.manifest.tag.url": "https://untrusted.example/capture"}
        result = self.run_flow("High", attrs, **{"Target Repository": "target/app"})
        self.assertEqual(result.relationship, "prepared")
        self.assertEqual(result.attributes["registry.http.url"], "https://registry.test/v2/target/app/manifests/latest")
        self.assertEqual(result.attributes["registry.auth.header"], "Bearer scoped-token")
        self.assertNotIn("untrusted", json.dumps(result.attributes))

    def test_a_blob_redirected_to_storage_counts_as_present_on_the_high_side(self):
        for status in (302, 307):
            with self.subTest(status=status):
                self.registry.status = status
                self.assertEqual(self.run_flow("High", self.facts()).relationship, "complete")
                manifest = self.run_flow("High", self.facts(kind="manifest"))
                self.assertEqual(manifest.relationship, "prepared")
                self.assertEqual(manifest.attributes["registry.stage"], "manifest-digest")

    def test_a_redirected_manifest_head_is_not_a_present_platform_manifest(self):
        self.registry.status = 307
        result = self.run_flow("High", self.facts(kind="index"))
        self.assertEqual(result.relationship, "retryable")

    def test_a_registry_that_omits_docker_content_digest_is_asked_for_the_stored_digest(self):
        base = {**self.facts(kind="manifest"), "registry.stage": "manifest-digest", "invokehttp.status.code": "201"}
        result = self.run_flow("High", base)
        self.assertEqual(result.attributes["registry.stage"], "manifest-tag")
        self.assertIn(("team/app", "manifest_digest", DIGEST), self.registry.calls)
        tagged = {**base, **result.attributes, "invokehttp.status.code": "201"}
        self.assertEqual(self.run_flow("High", tagged).relationship, "complete")
        self.assertIn(("team/app", "manifest_digest", "latest"), self.registry.calls)
        self.registry.stored = "sha256:" + "b" * 64
        self.assertEqual(self.run_flow("High", base).relationship, "failure")
        self.registry.stored = None
        self.assertEqual(self.run_flow("High", base).relationship, "failure")

    def test_a_changed_manifest_is_never_tagged(self):
        attrs = {**self.facts(kind="manifest"), "registry.stage": "manifest-digest", "invokehttp.status.code": "201",
                 "resp.Docker-Content-Digest": "sha256:" + "b" * 64}
        self.assertEqual(self.run_flow("High", attrs).relationship, "failure")

    def test_malformed_metadata_does_not_start_an_http_request(self):
        for change in [{"digest": "bad"}, {"repository": "../admin"}, {"size": "-1"}, {"kind": "other"}]:
            result = self.run_flow("High", self.facts(**change))
            self.assertNotEqual(result.relationship, "prepared")
        self.assertEqual(self.registry.calls, [])

    def test_metadata_network_failure_is_a_bounded_retry(self):
        self.registry.blob_exists = lambda *args: (_ for _ in ()).throw(RegistryError("network unavailable"))
        result = self.run_flow("Low", self.facts())
        self.assertEqual(result.relationship, "retryable")
        self.assertEqual(result.attributes["registry.retry.max"], "3")

    def test_a_manifest_that_waited_still_gets_its_error_retries(self):
        manifest = self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST]))
        attrs = dict(manifest)
        for _ in range(8):
            with patch("RouteRegistryTransfer.time.time", return_value=0.0):
                result = self.run_flow("High", attrs)
            self.assertEqual(result.relationship, "waiting")
            _, attrs = retry_flowfile({**attrs, **result.attributes})
            attrs["registry.wait.next"] = "0"
        self.registry.status = 200  # the layers arrived
        attrs = {**attrs, **self.run_flow("High", attrs).attributes, "registry.stage": "manifest-digest",
                 "invokehttp.status.code": "503"}
        for expected in ("retry", "retry", "retry", "retries_exceeded"):
            result = self.run_flow("High", attrs)
            self.assertEqual(result.relationship, "retryable")
            outcome, attrs = retry_flowfile({**attrs, **result.attributes})
            self.assertEqual(outcome, expected)
            attrs["registry.stage"], attrs["invokehttp.status.code"] = "manifest-digest", "503"

    def test_every_exit_of_a_side_clears_the_retry_owner_and_count(self):
        owned = {"registry.retries": "2", "registry.retries.uuid": "low-retry-card", "registry.retry.max": "3"}
        low = {**self.facts(), **owned, "registry.stage": "verify", "content_SHA-256": "a" * 64}
        high = {**self.facts(kind="manifest"), **owned, "registry.stage": "manifest-digest",
                "invokehttp.status.code": "201", "resp.Docker-Content-Digest": DIGEST}
        results = [self.run_flow("Low", {**low, "registry.stage": "fetch", "invokehttp.status.code": "200"}),
                   self.run_flow("Low", low, size=3 * 1024 ** 3),
                   self.run_flow("High", high),
                   self.run_flow("High", {**high, "registry.stage": "manifest-tag"}),
                   self.run_flow("High", {**self.facts(kind="manifest", needs=json.dumps([DIGEST, DIGEST])), **owned})]
        self.assertEqual([r.relationship for r in results], ["unverified", "complete", "prepared", "complete", "waiting"])
        del results[2]  # digest PUT to tag PUT: one step on the same side, so the count carries on
        for result in results:
            self.assertEqual(result.attributes["registry.retries.uuid"], "")
            self.assertEqual(result.attributes["registry.retries"], "0")

    def test_a_tag_put_that_keeps_failing_reaches_hold_and_carries_a_token(self):
        self.registry.status = 200
        attrs = {**self.facts(kind="manifest"), "registry.stage": "manifest-digest", "invokehttp.status.code": "201",
                 "resp.Docker-Content-Digest": DIGEST}
        outcomes = []
        for _ in range(6):
            result = self.run_flow("High", attrs)  # digest PUT committed: the tag PUT comes next
            self.assertEqual(result.relationship, "prepared")
            self.assertEqual(result.attributes["registry.auth.header"], "Bearer scoped-token")
            self.assertIn(("team/app", "pull,push", "GET", ""), self.registry.calls)  # authenticated first
            attrs = {**attrs, **result.attributes, "invokehttp.status.code": "401"}
            result = self.run_flow("High", attrs)  # the tag PUT answers 401
            self.assertEqual(result.relationship, "retryable")
            outcome, attrs = retry_flowfile({**attrs, **result.attributes})
            outcomes.append(outcome)
            if outcome != "retry":
                break
            attrs.update({"registry.stage": "manifest-digest", "invokehttp.status.code": "201"})
        self.assertEqual(outcomes, ["retry", "retry", "retry", "retries_exceeded"])

    def test_a_low_side_retry_does_not_fail_the_high_side_retry_card(self):
        low = {**self.facts(), "registry.stage": "fetch", "invokehttp.status.code": "503"}
        outcome, attrs = retry_flowfile({**low, **self.run_flow("Low", low).attributes})
        self.assertEqual(outcome, "retry")
        attrs.update({"registry.stage": "verify", "content_SHA-256": "a" * 64})
        sent = {**attrs, **self.run_flow("Low", attrs, size=3 * 1024 ** 3).attributes}
        high = {**sent, "invokehttp.status.code": "503", "registry.stage": "upload"}
        result = self.run_flow("High", high)
        self.assertEqual(result.relationship, "retryable")
        self.assertEqual(retry_flowfile({**high, **result.attributes}, uuid="high-retry-card")[0], "retry")


if __name__ == "__main__":
    unittest.main()
