import base64
import json
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
WRITE_SCOPE = 'pull,push'

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
    PROPERTIES = [SIDE, URL, USER, PASSWORD, SSL, REPOSITORY, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def getRelationships(self):
        return [Relationship(name, description) for name, description in [
            ("http", "Native InvokeHTTP; Original/Response returns here"),
            ("hash", "Native CryptographicHashContent; success returns here"),
            ("ready", "Verified content ready for the diode, or committed at the destination"),
            ("retry", "Penalised RetryFlowFile, then back here"),
            ("failure", "Preserved content for a disabled hold processor")]]

    @staticmethod
    def _result(relationship, attributes=None, contents=None):
        return FlowFileTransformResult(relationship, attributes=attributes or {}, contents=contents)

    @classmethod
    def _finish(cls, relationship, message=""):
        return cls._result(relationship, {STAGE: "", AUTH_HEADER: "", "registry.error": message})

    @classmethod
    def _retry(cls, message, dependencies=False):
        return cls._result("retry", {STAGE: "", AUTH_HEADER: "", "registry.error": message,
                                     "registry.retry.max": "120" if dependencies else "3"})

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

    def _http_failure(self, stage, status):
        if status == 0 or status in (401, 408, 429) or status >= 500:
            return self._retry(f"native {stage} failed (HTTP {status}); retry with fresh authentication")
        return self._finish("failure", f"native {stage} refused (HTTP {status})")

    def _manifest_committed(self, flowfile, facts, stage):
        observed = flowfile.getAttribute("resp.Docker-Content-Digest") or flowfile.getAttribute("resp.docker-content-digest")
        if observed != facts["digest"]:
            return self._finish("failure", "destination manifest digest differs from the planned job")
        reference = facts["reference"]
        if stage != "manifest-digest" or reference == facts["digest"]:
            return self._finish("ready")
        if not TAG.fullmatch(reference or ""):
            raise RegistryError("invalid manifest tag")
        return self._result("http", self._http("PUT", flowfile.getAttribute("registry.manifest.tag.url"),
            flowfile.getAttribute(AUTH_HEADER), facts[MEDIA_TYPE], "manifest-tag"))

    def _callback(self, side, stage, flowfile, facts):
        if stage == "verify":
            if flowfile.getSize() != int(facts["size"]) or flowfile.getAttribute("content_SHA-256") != facts["digest"][7:]:
                return self._finish("failure", "content digest or size differs from the planned job")
            return self._finish("ready")
        status = int(flowfile.getAttribute(HTTP_STATUS) or "0")
        if side == "Low" and stage == "fetch" and status == 200:
            return self._result("hash", {STAGE: "verify", AUTH_HEADER: ""})
        if side != "High" or status != 201:
            return self._http_failure(stage, status)
        if facts["kind"] in ("manifest", "index"):
            return self._manifest_committed(flowfile, facts, stage)
        return self._finish("ready")

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
            elif status != 200:
                raise RegistryError(f"manifest dependency HEAD failed (HTTP {status})")
        return dependencies, missing

    def _incomplete(self, facts, dependencies, missing):
        if facts["kind"] == "index":
            what = "platform manifests " + ",".join(str(n + 1) for n in missing) + f" of {len(dependencies)}"
        else:
            layers = [str(n) for n in missing if n]
            what = "layers " + (",".join(layers) or "none") + f" of {max(0, len(dependencies) - 1)}"
            if 0 in missing:
                what += " and the config blob"
        message = f"{facts['image']} ({facts['platform']}) incomplete: missing {what}. Check code {facts['check']}"
        self.logger.warn(message)
        return self._retry(message, dependencies=True)

    def _prepare_blob(self, client, repository, digest):
        response = client._send(repository, WRITE_SCOPE, "HEAD", f"{repository}/blobs/{digest}")
        status = response.status
        response.close()
        if status == 200:
            return self._finish("ready")
        if status != 404:
            raise RegistryError(f"destination blob HEAD failed (HTTP {status})")
        upload = client.start_upload(repository)
        if not client._guard(upload, "upload Location"):
            raise RegistryError("upload Location names a foreign host")
        url = upload + ("&" if "?" in upload else "?") + "digest=" + urllib.parse.quote(digest)
        return self._result("http", self._http("PUT", url, self._header(client, repository, WRITE_SCOPE),
                                               "application/octet-stream", "upload"))

    def _prepare_manifest(self, client, repository, facts):
        dependencies, missing = self._dependencies(client, repository, facts)
        if missing:
            return self._incomplete(facts, dependencies, missing)
        media_type = facts[MEDIA_TYPE]
        if media_type not in [t.strip() for t in MANIFEST_TYPES.split(",")]:
            raise RegistryError("unsupported manifest media type")
        attrs = self._http("PUT", f"{client.base}/v2/{repository}/manifests/{facts['digest']}",
                           self._header(client, repository, WRITE_SCOPE), media_type, "manifest-digest")
        attrs["registry.manifest.tag.url"] = f"{client.base}/v2/{repository}/manifests/{facts['reference']}"
        return self._result("http", attrs)

    def _request(self, context, side, flowfile, facts, attributes):
        def prop(descriptor):
            return evaluate(context.getProperty(descriptor), Attributes({})) or ""
        client = RegistryClient(prop(self.URL), prop(self.USER), prop(self.PASSWORD),
                                ca_der=trusted_der(context.getProperty(self.SSL)), timeout=int(prop(self.TIMEOUT)))
        repository, digest = facts["repository"], facts["digest"]
        if side == "Low":
            if not client.blob_exists(repository, digest):
                return self._finish("failure", "source blob is missing")
            # shortcut: payload redirects are refused; add guarded storage redirects when a registry requires them.
            http = self._http("GET", f"{client.base}/v2/{repository}/blobs/{digest}",
                              self._header(client, repository, "pull"), "application/octet-stream", "fetch")
            return self._result("http", {**attributes, **http})
        repository = prop(self.REPOSITORY) or repository
        if not REPOSITORY.fullmatch(repository):
            raise RegistryError("invalid target repository")
        if flowfile.getSize() != int(facts["size"]):
            return self._finish("failure", "received content size differs from the planned job")
        response = client._send(repository, WRITE_SCOPE, "GET", "")
        client._expect(response, "registry authentication", 200)
        response.close()
        if facts["kind"] in ("blob", "config"):
            return self._prepare_blob(client, repository, digest)
        return self._prepare_manifest(client, repository, facts)

    def transform(self, context, flowfile):
        side = context.getProperty(self.SIDE).getValue()
        stage = flowfile.getAttribute(STAGE) or ""
        attributes = {}
        try:
            if side == "Low" and not flowfile.getAttribute("registry.kind"):
                attributes, raw = self._job(flowfile)
                self._facts(flowfile, attributes)
                if raw is not None:
                    return self._result("hash", {**attributes, STAGE: "verify"}, raw)
            facts = self._facts(flowfile, attributes)
            if stage:
                return self._callback(side, stage, flowfile, facts)
            return self._request(context, side, flowfile, facts, attributes)
        except RegistryError as error:
            self.logger.warn(f"retrying: {error}")
            return self._retry(str(error))
        except (ValueError, KeyError, TypeError) as error:
            return self._finish("failure", f"invalid registry job: {type(error).__name__}")
