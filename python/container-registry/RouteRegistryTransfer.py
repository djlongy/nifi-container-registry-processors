import base64
import json
import time
import urllib.parse

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from container_registry_client import (DIGEST, MANIFEST_TYPES, REPOSITORY, TAG, RegistryClient, RegistryError,
                                       trusted_der)
from nifi_attributes import Attributes, evaluate


STAGE = 'registry.stage'
AUTH_HEADER = 'registry.auth.header'
HTTP_STATUS = 'invokehttp.status.code'
REGISTRY_PREFIX = 'registry.'
MEDIA_TYPE = 'media.type'
ERROR = 'registry.error'
RETRY_MAX = 'registry.retry.max'
RETRIES, RETRIES_UUID = 'registry.retries', 'registry.retries.uuid'  # RetryFlowFile's Retry Attribute and its '.uuid'
ERROR_RETRIES = 3
WAIT_SINCE, WAIT_CHECKS, WAIT_NEXT = 'registry.wait.since', 'registry.wait.checks', 'registry.wait.next'
WAIT_RETRIES = '1000000'  # RetryFlowFile's count never ends a wait; Stale After Hours does
FIRST_CHECK, LAST_CHECK = 30, 1800  # seconds between dependency checks: doubles from 30 s up to 30 min
WRITE_SCOPE = 'pull,push'
BLOB_PRESENT = (200, 302, 307)  # a blob HEAD that redirects to object storage still means the blob exists

class RouteRegistryTransfer(FlowFileTransform):
    """Metadata decisions around native InvokeHTTP and hashing; never read downloaded content."""

    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.5.1"
        description = "Prepare registry jobs and route native downloads/uploads, retries and failures. Never reads blob content."
        tags = ["registry", "container", "streaming", "diode"]

    SIDE = PropertyDescriptor(name="Transfer Side", description="Low prepares downloads; High prepares uploads.",
                              required=True, default_value="Low", allowable_values=["Low", "High"])
    URL = PropertyDescriptor(name="Registry URL", description="Registry origin or API base URL; never comes from a webhook.",
                             required=True, validators=[StandardValidators.URL_VALIDATOR],
                             expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    USER = PropertyDescriptor(name="Registry Username", description="Registry user for metadata and token requests.",
                              expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = PropertyDescriptor(name="Registry Password", description="Registry password or robot token, kept in a sensitive parameter.", sensitive=True)
    SSL = PropertyDescriptor(name="SSL Context Service", description="Truststore CAs added to the Python system trust store.",
                             controller_service_definition="org.apache.nifi.ssl.SSLContextProvider")
    REPOSITORY = PropertyDescriptor(name="Target Repository", description="High-side repository override; empty keeps the source repository.",
                                    expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    TIMEOUT = PropertyDescriptor(name="Request Timeout", description="Metadata request timeout in seconds; native payload timeouts are separate.",
                                required=True, default_value="60",
                                validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])
    STALE = PropertyDescriptor(name="Stale After Hours",
                               description="High side: how long a manifest may wait for its layers or platform manifests "
                                           "before it goes to stale with a report of what is missing.",
                               required=True, default_value="24", validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])
    PROPERTIES = [SIDE, URL, USER, PASSWORD, SSL, REPOSITORY, TIMEOUT, STALE]

    def __init__(self, **kwargs):
        super().__init__()

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def getRelationships(self):
        return [Relationship(name, description) for name, description in [
            ("prepared", "A registry request is prepared in registry.http.*. Connect to InvokeHTTP (low: FetchBlob, "
                         "high: UploadContent) and connect its Original, Response, Retry, No Retry and Failure back here"),
            ("unverified", "Low side: downloaded content whose digest is not checked yet. Connect to "
                           "CryptographicHashContent and its success back here. High side: never used, connect to Hold"),
            ("complete", "This side is finished. Low: content verified, connect to the diode. "
                         "High: stored in the registry, auto-terminate"),
            ("retryable", "A transient error (network, authentication, HTTP 408, 429 or 5xx), recoverable. "
                          "Connect to RetryFlowFile and its retry back here; registry.error says why"),
            ("waiting", "High side: a manifest whose layers or platform manifests have not all arrived. Connect to "
                        "RetryFlowFile with retry back here; it is checked again at a doubling interval up to 30 min"),
            ("stale", "High side: a manifest still waiting after Stale After Hours. Connect to a disabled processor "
                      "to keep it for a resend; low side never uses it, auto-terminate"),
            ("failure", "A permanent condition: refused by the registry, wrong digest or size, or a malformed job. "
                       "Content is kept; connect to a disabled hold processor")]]

    @staticmethod
    def _result(relationship, attributes=None, contents=None):
        return FlowFileTransformResult(relationship, attributes=attributes or {}, contents=contents)

    # A new retry episode: the count restarts and the RetryFlowFile that owned it is forgotten. A leftover
    # '.uuid' from the low side's RetryFlowFile makes the high side's (Reuse Mode fail) send the FlowFile to
    # failure, and a count left by a long wait would use up the retries of the next error at once. RetryFlowFile
    # reads "" as a number and fails, so the count restarts at "0", and a blank '.uuid' is ignored.
    FRESH_RETRIES = {RETRIES: "0", RETRIES_UUID: "", RETRY_MAX: ""}

    @classmethod
    def _finish(cls, relationship, message=""):
        return cls._result(relationship, {STAGE: "", AUTH_HEADER: "", ERROR: message, **cls.FRESH_RETRIES})

    @classmethod
    def _retry(cls, flowfile, message):
        """ERROR_RETRIES more tries counted from the count the FlowFile has now. The limit set by the first error
        stays for the rest of the episode, so repeated errors still end in retries_exceeded."""
        limit = flowfile.getAttribute(RETRY_MAX)
        if limit in (None, "", WAIT_RETRIES) or not limit.isdigit():
            count = flowfile.getAttribute(RETRIES) or "0"
            limit = str((int(count) if count.isdigit() else 0) + ERROR_RETRIES)
        return cls._result("retryable", {STAGE: "", AUTH_HEADER: "", ERROR: message, RETRY_MAX: limit})

    @staticmethod
    def _http(method, url, header, media_type, stage):
        return {"registry.http.method": method, "registry.http.url": url, AUTH_HEADER: header,
                "registry.http.media.type": media_type, STAGE: stage,
                HTTP_STATUS: "", "invokehttp.java.exception.message": "",
                "resp.Docker-Content-Digest": "", "resp.docker-content-digest": ""}

    @staticmethod
    def _facts(flowfile, attributes):
        facts = {key: attributes.get(REGISTRY_PREFIX + key, flowfile.getAttribute(REGISTRY_PREFIX + key)) for key in
                 ("kind", "repository", "digest", "size", MEDIA_TYPE, "reference", "needs", "image", "platform", "check")}
        if facts["kind"] not in ("blob", "config", "manifest", "index"):
            raise RegistryError("unknown registry job kind")
        if not REPOSITORY.fullmatch(facts["repository"] or "") or not DIGEST.fullmatch(facts["digest"] or ""):
            raise RegistryError("invalid registry repository or digest")
        if facts["size"] is None or not facts["size"].isdigit():
            raise RegistryError("invalid registry job size")
        return facts

    def _job(self, flowfile):
        if flowfile.getSize() > 64 * 1024 * 1024:
            raise RegistryError("registry job exceeds 64 MiB")
        job = json.loads(flowfile.getContentsAsBytes())  # Only the planner's small JSON job, before any download.
        attrs = {REGISTRY_PREFIX + key: str(job[value]) for key, value in {
            "kind": "kind", "repository": "repository", "digest": "digest", "size": "size",
            MEDIA_TYPE: "mediaType", "reference": "reference", "image": "image", "platform": "platform", "check": "check"}.items()}
        attrs["registry.needs"] = json.dumps(job["needs"])
        attrs["registry.layer.count"] = str(job.get("layers", 0))
        attrs[STAGE] = ""
        raw = base64.b64decode(job["body"], validate=True) if job["kind"] in ("manifest", "index") else None
        return attrs, raw

    @staticmethod
    def _header(client, repository, actions):
        """The Authorization value for the native InvokeHTTP: a scoped Bearer token, or empty. Basic credentials
        are never written to an attribute: for a Basic registry, set them on InvokeHTTP's sensitive Request
        Username and Request Password instead."""
        header = client._with_auth((repository, actions, None), {}).get("Authorization", "")
        return header if header.startswith("Bearer ") else ""

    def _http_failure(self, flowfile, stage, status):
        if status == 0 or status in (401, 408, 429) or status >= 500:
            return self._retry(flowfile, f"native {stage} failed (HTTP {status}); retry with fresh authentication")
        return self._finish("failure", f"native {stage} refused (HTTP {status})")

    def _manifest_committed(self, context, flowfile, facts, stage):
        reference = facts["reference"]
        client, repository = self._destination(context, facts)
        observed = flowfile.getAttribute("resp.Docker-Content-Digest") or flowfile.getAttribute("resp.docker-content-digest")
        if not observed:  # the header is optional in the distribution spec: ask the registry what it stored
            observed = client.manifest_digest(repository, facts["digest"] if stage == "manifest-digest" else reference)
        if observed != facts["digest"]:
            return self._finish("failure", "destination manifest digest differs from the planned job")
        if stage != "manifest-digest" or reference == facts["digest"]:
            return self._finish("complete")
        if not TAG.fullmatch(reference or ""):
            raise RegistryError("invalid manifest tag")
        response = client._send(repository, WRITE_SCOPE, "GET", "")  # a scoped token for the tag PUT
        client._expect(response, "registry authentication", 200)
        response.close()
        # The retry count carries on from the digest PUT: a tag PUT that keeps failing reaches Hold.
        return self._result("prepared", self._http("PUT", f"{client.base}/v2/{repository}/manifests/{reference}",
            self._header(client, repository, WRITE_SCOPE), facts[MEDIA_TYPE], "manifest-tag"))

    def _callback(self, context, side, stage, flowfile, facts):
        if stage == "verify":
            if flowfile.getSize() != int(facts["size"]) or flowfile.getAttribute("content_SHA-256") != facts["digest"][7:]:
                return self._finish("failure", "content digest or size differs from the planned job")
            return self._finish("complete")
        status = int(flowfile.getAttribute(HTTP_STATUS) or "0")
        if side == "Low" and stage == "fetch" and status == 200:
            return self._result("unverified", {STAGE: "verify", AUTH_HEADER: "", **self.FRESH_RETRIES})
        if side != "High" or status != 201:
            return self._http_failure(flowfile, stage, status)
        if facts["kind"] in ("manifest", "index"):
            return self._manifest_committed(context, flowfile, facts, stage)
        return self._finish("complete")

    @staticmethod
    def _dependencies(client, repository, facts):
        dependencies = json.loads(facts["needs"])
        if not isinstance(dependencies, list) or len(dependencies) > 1000:
            raise RegistryError("invalid manifest dependencies")
        path = "manifests" if facts["kind"] == "index" else "blobs"
        missing = []
        for n, dependency in enumerate(dependencies):
            if not isinstance(dependency, str) or not DIGEST.fullmatch(dependency):
                raise RegistryError("invalid dependency digest")
            response = client._send(repository, WRITE_SCOPE, "HEAD", f"{repository}/{path}/{dependency}",
                                    {"Accept": MANIFEST_TYPES})
            status = response.status
            response.close()
            if status == 404:
                missing.append(n)
            elif status not in (BLOB_PRESENT if path == "blobs" else (200,)):
                raise RegistryError(f"manifest dependency HEAD failed (HTTP {status})")
        return dependencies, missing

    @staticmethod
    def _wait_pending(flowfile):
        """A waiting manifest whose next dependency check is not due: back to RetryFlowFile without a registry call."""
        due = float(flowfile.getAttribute(WAIT_NEXT) or 0)
        if time.time() < due:
            return FlowFileTransformResult("waiting", attributes={**RouteRegistryTransfer.FRESH_RETRIES, RETRY_MAX: WAIT_RETRIES})
        return None

    def _incomplete(self, flowfile, facts, dependencies, missing, stale_hours):
        if facts["kind"] == "index":
            what = "platform manifests " + ",".join(str(n + 1) for n in missing) + f" of {len(dependencies)}"
        else:
            layers = [str(n) for n in missing if n]
            what = "layers " + (",".join(layers) or "none") + f" of {max(0, len(dependencies) - 1)}"
            if 0 in missing:
                what += " and the config blob"
        message = f"{facts['image']} ({facts['platform']}) incomplete: missing {what}. Check code {facts['check']}"
        now = time.time()
        since = float(flowfile.getAttribute(WAIT_SINCE) or now)
        checks = int(flowfile.getAttribute(WAIT_CHECKS) or 0) + 1
        if now - since >= stale_hours * 3600:
            self.logger.warn(f"{message}; waited {stale_hours} h, moved to stale")
            # Cleared so that a stale manifest replayed after a resend starts a fresh wait.
            return self._result("stale", {STAGE: "", AUTH_HEADER: "", ERROR: message, **self.FRESH_RETRIES,
                                          WAIT_SINCE: "", WAIT_CHECKS: "", WAIT_NEXT: ""})
        delay = min(FIRST_CHECK * 2 ** (checks - 1), LAST_CHECK)
        return self._result("waiting", {STAGE: "", AUTH_HEADER: "", ERROR: message,
                                      **self.FRESH_RETRIES, RETRY_MAX: WAIT_RETRIES, WAIT_SINCE: str(since),
                                      WAIT_CHECKS: str(checks), WAIT_NEXT: str(now + delay)})

    def _prepare_blob(self, client, repository, digest):
        response = client._send(repository, WRITE_SCOPE, "HEAD", f"{repository}/blobs/{digest}")
        status = response.status
        response.close()
        if status in BLOB_PRESENT:
            return self._finish("complete")
        if status != 404:
            raise RegistryError(f"destination blob HEAD failed (HTTP {status})")
        upload = client.start_upload(repository)
        if not client._guard(upload, "upload Location"):
            raise RegistryError("upload Location names a foreign host")
        url = upload + ("&" if "?" in upload else "?") + "digest=" + urllib.parse.quote(digest)
        return self._result("prepared", self._http("PUT", url, self._header(client, repository, WRITE_SCOPE),
                                               "application/octet-stream", "upload"))

    def _prepare_manifest(self, client, repository, facts, flowfile, stale_hours):
        dependencies, missing = self._dependencies(client, repository, facts)
        if missing:
            return self._incomplete(flowfile, facts, dependencies, missing, stale_hours)
        media_type = facts[MEDIA_TYPE]
        if media_type not in [t.strip() for t in MANIFEST_TYPES.split(",")]:
            raise RegistryError("unsupported manifest media type")
        attrs = self._http("PUT", f"{client.base}/v2/{repository}/manifests/{facts['digest']}",
                           self._header(client, repository, WRITE_SCOPE), media_type, "manifest-digest")
        return self._result("prepared", attrs)

    def _prop(self, context, descriptor):
        return evaluate(context.getProperty(descriptor), Attributes({})) or ""

    def _client(self, context):
        prop = lambda descriptor: self._prop(context, descriptor)
        return RegistryClient(prop(self.URL), prop(self.USER), prop(self.PASSWORD),
                              ca_der=trusted_der(context.getProperty(self.SSL)), timeout=int(prop(self.TIMEOUT)))

    def _target_repository(self, context, facts):
        repository = self._prop(context, self.REPOSITORY) or facts["repository"]
        if not REPOSITORY.fullmatch(repository):
            raise RegistryError("invalid target repository")
        return repository

    def _destination(self, context, facts):
        """The configured registry and the repository a High-side request writes to. Every URL and credential of a
        native request is rebuilt from these, never read back from an attribute a job or a callback carries."""
        return self._client(context), self._target_repository(context, facts)

    def _request(self, context, side, flowfile, facts, attributes):
        prop = lambda descriptor: self._prop(context, descriptor)
        client = self._client(context)
        repository, digest = facts["repository"], facts["digest"]
        if side == "Low":
            if not client.blob_exists(repository, digest):
                return self._finish("failure", "source blob is missing")
            # shortcut: payload redirects are refused; add guarded storage redirects when a registry requires them.
            http = self._http("GET", f"{client.base}/v2/{repository}/blobs/{digest}",
                              self._header(client, repository, "pull"), "application/octet-stream", "fetch")
            return self._result("prepared", {**attributes, **http})
        if facts["kind"] in ("manifest", "index"):
            pending = self._wait_pending(flowfile)
            if pending:
                return pending
        repository = self._target_repository(context, facts)
        if flowfile.getSize() != int(facts["size"]):
            return self._finish("failure", "received content size differs from the planned job")
        response = client._send(repository, WRITE_SCOPE, "GET", "")
        client._expect(response, "registry authentication", 200)
        response.close()
        if facts["kind"] in ("blob", "config"):
            return self._prepare_blob(client, repository, digest)
        return self._prepare_manifest(client, repository, facts, flowfile, int(prop(self.STALE)))

    def transform(self, context, flowfile):
        side = context.getProperty(self.SIDE).getValue()
        stage = flowfile.getAttribute(STAGE) or ""
        attributes = {}
        try:
            if side == "Low" and not flowfile.getAttribute("registry.kind"):
                attributes, raw = self._job(flowfile)
                self._facts(flowfile, attributes)
                if raw is not None:
                    return self._result("unverified", {**attributes, STAGE: "verify"}, raw)
            facts = self._facts(flowfile, attributes)
            if stage:
                return self._callback(context, side, stage, flowfile, facts)
            return self._request(context, side, flowfile, facts, attributes)
        except RegistryError as error:
            self.logger.warn(f"retrying: {error}")
            return self._retry(flowfile, str(error))
        except (ValueError, KeyError, TypeError) as error:
            return self._finish("failure", f"invalid registry job: {type(error).__name__}")
