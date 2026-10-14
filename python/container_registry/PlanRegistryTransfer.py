import base64
import json
import re
import uuid

from nifiapi.documentation import use_case
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import (DIGEST, FOREIGN_LAYERS, REPOSITORY, TAG, RegistryClient, RegistryError,
                                       _manifest_type, allowed, trusted_der, webhook_images)

REFERENCE = re.compile(r"(?P<repository>[^:@]+)(?::(?P<tag>[^@]+))?(?:@(?P<digest>[^@]+))?")


def parse_reference(reference):
    """(repository, tag, digest) of repository:tag, repository@sha256:..., or repository:tag@sha256:...;
    tag and digest are "" when absent."""
    found = REFERENCE.fullmatch((reference or "").strip())
    repository, tag, digest = (found.group("repository"), found.group("tag") or "", found.group("digest") or "") if found else ("", "", "")
    if not (REPOSITORY.fullmatch(repository) and (tag or digest) and (not tag or TAG.fullmatch(tag))
            and (not digest or DIGEST.fullmatch(digest))):
        raise RegistryError(f"not a repository:tag or repository@sha256:digest reference: {reference!r}")
    return repository, tag, digest


def platform_of(descriptor):
    """os/architecture[/variant] of an index entry, "-" when the entry names none."""
    data = descriptor.get("platform") or {}
    parts = [data.get("os"), data.get("architecture"), data.get("variant")]
    return "/".join(p for p in parts if p) or "-"


class Plan:
    """The jobs for one image: blobs and configs first, then manifests deepest first, the top manifest or index last."""

    def __init__(self, client, repository, tag, top, transfer, selection, max_depth, budget):
        self.client, self.repository, self.tag, self.top = client, repository, tag, top
        self.check = top[7:15]
        self.transfer, self.selection, self.max_depth, self.budget = transfer, selection, max_depth, budget
        self.blobs, self.manifests = {}, {}

    def job(self, kind, digest, size, media_type, platform, needs, **extra):
        if len(self.blobs) + len(self.manifests) >= self.budget:
            raise RegistryError(f"{self.repository}:{self.tag or self.top} needs more than Maximum Jobs Per Transfer ({self.budget})")
        return {"kind": kind, "repository": self.repository, "digest": digest, "size": size, "mediaType": media_type,
                "tag": "", "reference": digest, "layer": 0, "layers": 0, "platform": platform, "check": self.check,
                "transfer": self.transfer, "needs": needs, **extra}

    def wanted(self, platform):
        return not self.selection or any(platform == s or platform.startswith(s + "/") for s in self.selection)

    def walk(self, digest, raw, content_type, platform, depth):
        document = json.loads(raw)
        media_type = _manifest_type(raw, content_type)
        body = base64.b64encode(raw).decode()
        if "manifests" in document:
            if depth >= self.max_depth:
                raise RegistryError(f"{self.repository}@{self.top}: indexes nested deeper than Maximum Manifest Depth ({self.max_depth})")
            children = [(c, platform_of(c)) for c in document["manifests"]]
            for child, child_platform in children:
                if not DIGEST.fullmatch(child.get("digest", "")):
                    raise RegistryError(f"unsupported child digest {child.get('digest')}")
                if self.wanted(child_platform) and child["digest"] not in self.manifests:
                    child_raw, child_type = self.client.get_manifest(self.repository, child["digest"])
                    self.walk(child["digest"], child_raw, child_type, child_platform, depth + 1)
            if not self.selection:  # a filtered copy cannot keep the index: it names platforms that are not sent
                needs = [c["digest"] for c, p in children]
                self.manifests[digest] = self.job("index", digest, len(raw), media_type, "-", needs, body=body)
            return
        config, layers = document["config"], document.get("layers") or []
        self.add_blob("config", config["digest"], config["size"], config["mediaType"], platform, 0, len(layers))
        needs = [config["digest"]]
        for n, layer in enumerate(layers, 1):
            if layer.get("urls") and layer.get("mediaType") in FOREIGN_LAYERS:
                continue  # a foreign layer is served from its urls, never from the registry
            self.add_blob("blob", layer["digest"], layer["size"], layer["mediaType"], platform, n, len(layers))
            needs.append(layer["digest"])
        self.manifests[digest] = self.job("manifest", digest, len(raw), media_type, platform, needs, body=body)

    def add_blob(self, kind, digest, size, media_type, platform, n, m):
        if not DIGEST.fullmatch(digest):
            raise RegistryError(f"unsupported descriptor digest {digest}")
        if digest in self.blobs:
            return
        if not self.client.blob_exists(self.repository, digest):
            raise RegistryError(f"blob {digest} is missing")
        self.blobs[digest] = self.job(kind, digest, int(size), media_type, platform, [], layer=n, layers=m)

    def jobs(self):
        raw, content_type = self.client.get_manifest(self.repository, self.top)
        self.walk(self.top, raw, content_type, "-", 0)
        manifests = list(self.manifests.values())
        if not manifests:
            raise RegistryError(f"no platform of {self.repository}:{self.tag or self.top} matches Platform Selection")
        if self.selection:
            if len(manifests) == 1 and self.tag:  # one platform: the tag follows it, with that manifest's own digest
                manifests[0]["tag"] = manifests[0]["reference"] = self.tag
        else:
            manifests[-1]["tag"] = self.tag
            manifests[-1]["reference"] = self.tag or self.top
        return list(self.blobs.values()) + manifests


@use_case(description="Plan the transfer of container images between registries as a list of jobs, one per layer and manifest.",
          configuration="""ListenHTTP or GenerateFlowFile with registry.image.reference and transfer.id, then this processor with
          Source Registry URL and Allowed Repositories, then SplitJson on $.jobs. Native processors move every blob.""",
          keywords=["container", "diode", "registry", "plan", "layer"])
class PlanRegistryTransfer(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.5.0"
        description = ("Plans the transfer of container images: reads the manifests and indexes the FlowFile names from the "
                       "source registry and emits one JSON job list, {\"transfer.id\", \"jobs\"}, with a job per config, "
                       "layer, manifest and index. It never reads a blob. Split $.jobs with SplitJson and let native "
                       "processors download, hand over and upload each blob; manifests travel inside their job as base64 "
                       "of the exact source bytes, so digests and tags stay the source's. Attributes registry.layers, registry.check "
                       "and registry.platform resend named layers only.")
        tags = ["container", "image", "oci", "docker", "registry", "plan", "diode", "layer"]

    REGISTRY_URL = PropertyDescriptor(
        name="Source Registry URL",
        description=(
            "Registry to read manifests from, such as https://registry.example.com, or "
            "https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the "
            "URL. Required, no default. Environment variables allowed: ${REGISTRY_URL}."),
        required=True,
        validators=[StandardValidators.URL_VALIDATOR],
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    USERNAME = PropertyDescriptor(
        name="Source Registry Username",
        description=(
            "User for the source registry. Empty (default): anonymous access, which works only on a "
            "registry that allows it. Environment variables allowed: ${REGISTRY_USER}."),
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = PropertyDescriptor(
        name="Source Registry Password",
        description=(
            "Password or access token for the source registry. Empty (default): none sent. Sensitive:"
            " reference a parameter, such as #{registry_password}."),
        sensitive=True)
    SSL_CONTEXT = PropertyDescriptor(
        name="SSL Context Service",
        description=(
            "A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it "
            "does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty "
            "(default): only those are trusted. Its keystore is ignored."),
        controller_service_definition="org.apache.nifi.ssl.SSLContextProvider")
    REPOSITORIES = PropertyDescriptor(
        name="Allowed Repositories",
        description=(
            "Allowlist, not the selection: the FlowFile picks the images and each must be in here. "
            "Comma or new line separated: team/app, or team/* for a prefix, * for all. Required. "
            "Any other repository fails the FlowFile."),
        required=True,
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PLATFORMS = PropertyDescriptor(
        name="Platform Selection",
        description=(
            "all (default): every platform, index kept, so its digest is the source's. Or platforms such as "
            "linux/amd64, linux/arm64/v8 (comma or new line separated): only those are planned, without the "
            "index; a single match gets the tag, on its own digest."),
        required=True,
        default_value="all",
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    MAX_DEPTH = PropertyDescriptor(
        name="Maximum Manifest Depth",
        description="Most indexes nested in one another before an image is refused as malformed. Default 16.",
        required=True,
        default_value="16",
        validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])
    MAX_JOBS = PropertyDescriptor(
        name="Maximum Jobs Per Transfer",
        description=(
            "Most jobs (configs, layers, manifests and indexes, a blob shared by platforms counted once) "
            "one image may plan. More fails the FlowFile. Default 1000."),
        required=True,
        default_value="1000",
        validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])
    TIMEOUT = PropertyDescriptor(
        name="Request Timeout",
        description=(
            "Seconds a registry read may stall before it fails. Default 60. Only manifests and "
            "HEAD checks are read here, never blobs."),
        required=True,
        default_value="60",
        validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, REPOSITORIES, PLATFORMS, MAX_DEPTH, MAX_JOBS, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "One JSON job list, {\"transfer.id\", \"jobs\"}, for SplitJson on $.jobs"),
                Relationship("failure", "The request was refused or planning failed; the registry.error attribute says why. No job was planned")]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor):
        value = evaluate(context.getProperty(descriptor), Attributes({}))
        return value.strip() if value else None

    def _images(self, flowfile, entries, host):
        """[(repository, tag, digest)] the FlowFile names: registry.image.reference, else a webhook body."""
        reference = flowfile.getAttribute("registry.image.reference")
        if not reference:
            return webhook_images(flowfile.getContentsAsBytes(), flowfile.getAttribute("X-Sha256"), host, entries)
        repository, tag, digest = parse_reference(reference)
        if not allowed(repository, entries):
            raise RegistryError(f"{repository} is not in Allowed Repositories")
        return [(repository, tag, digest)]

    @staticmethod
    def _resend(flowfile):
        """{"layers", "check", "platform"} when the FlowFile asks for named layers again, else None."""
        text = flowfile.getAttribute("registry.layers")
        if not text:
            return None
        try:
            layers = {int(n) for n in re.split(r"[,\s]+", text.strip()) if n}
        except ValueError:
            raise RegistryError(f"registry.layers must be layer numbers such as 2,3: {text!r}")
        check = (flowfile.getAttribute("registry.check") or "").strip()
        if not layers or min(layers) < 1 or not check:
            raise RegistryError("registry.layers needs numbers from 1 and the registry.check code of the incomplete bulletin")
        return {"layers": layers, "check": check, "platform": (flowfile.getAttribute("registry.platform") or "").strip()}

    @staticmethod
    def _layers_only(jobs, resend, check, label):
        """The config and the asked-for layers of the planned jobs, no manifest, once the check code still matches."""
        if resend["check"] != check:
            raise RegistryError(f"refused: check code {resend['check']} does not match {check} for {label}, the tag has "
                                "moved; nothing was sent")
        blobs = [j for j in jobs if j["kind"] in ("blob", "config")]
        missing = sorted(resend["layers"] - {j["layer"] for j in blobs})
        if missing:
            raise RegistryError(f"{label} has no layer {missing[0]}: it has {max([j['layers'] for j in blobs] or [0])}")
        return [j for j in blobs if j["kind"] == "config" or j["layer"] in resend["layers"]]

    def transform(self, context, flowfile):
        try:
            client = RegistryClient(self._value(context, self.REGISTRY_URL), self._value(context, self.USERNAME),
                                    context.getProperty(self.PASSWORD).getValue(),
                                    trusted_der(context.getProperty(self.SSL_CONTEXT)),
                                    int(context.getProperty(self.TIMEOUT).getValue()))
            entries = [e for e in re.split(r"[,\s]+", self._value(context, self.REPOSITORIES) or "") if e]
            selection = [s for s in re.split(r"[,\s]+", self._value(context, self.PLATFORMS) or "all") if s]
            selection = [] if "all" in selection else selection
            transfer = flowfile.getAttribute("transfer.id") or str(uuid.uuid4())
            resend = self._resend(flowfile)
            if resend:
                selection = [resend["platform"]] if resend["platform"] else selection
            images = self._images(flowfile, entries, client.host)
            if resend and len(images) != 1:
                raise RegistryError("registry.layers resends one image: the request names " + str(len(images)))
            jobs = []
            for repository, tag, digest in images:
                found = client.manifest_digest(repository, tag) if tag else digest
                if found is None or (digest and found != digest):
                    raise RegistryError(f"{repository}:{tag} is not at {digest or 'any digest'} in {client.host}")
                plan = Plan(client, repository, tag, found, transfer, selection,
                            int(context.getProperty(self.MAX_DEPTH).getValue()),
                            int(context.getProperty(self.MAX_JOBS).getValue()))
                jobs += plan.jobs()
                if resend:
                    jobs = self._layers_only(jobs, resend, plan.check, f"{repository}:{tag or found}")
        except Exception as error:
            self.logger.error(f"Plan refused: {error}")
            return FlowFileTransformResult("failure", attributes={"registry.error": str(error)})
        self.logger.info(f"Planned {len(jobs)} job(s) for transfer {transfer} from {client.host}")
        return FlowFileTransformResult(
            "success", contents=json.dumps({"transfer.id": transfer, "jobs": jobs}).encode(),
            attributes={"transfer.id": transfer, "planner.job.count": str(len(jobs)),
                        "mime.type": "application/json"})
