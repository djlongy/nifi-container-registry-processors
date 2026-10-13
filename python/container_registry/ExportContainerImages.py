import hashlib
import json
import os
import re
from datetime import datetime, timezone

from nifiapi.documentation import use_case
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import (DIGEST, REPOSITORY, TAG, RegistryClient, RegistryError, export_attributes,
                                       parse_size, plan_image, trusted_der, write_export)
from PullContainerImages import PullContainerImages as Pull


def host_of(registry):
    return re.sub(r":443$", "", (registry or "").lower())


def allowed(repository, entries):
    """True when repository equals an allowlist entry, or starts with one that ends in *."""
    return any(repository.startswith(e[:-1]) if e.endswith("*") else repository == e for e in entries)


@use_case(description="Export the images a gitlab-registry-mirror webhook names from a registry for a data diode.",
          configuration="""ListenHTTP, then RouteOnAttribute on X-Artifact-Type equals container-images-request, then this
          processor with Registry URL, Repositories and Output Directory. Send success to PushContainerImages with Tar
          File set to the Output Directory plus /${filename}.""",
          keywords=["container", "diode", "registry", "export", "webhook"])
class ExportContainerImages(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "2.0.0"
        description = ("Exports the images a gitlab-registry-mirror webhook names as one mirror.py export tar in Output "
                       "Directory. The body only selects images: it must match X-Sha256, name the host of Registry URL "
                       "and repositories in Repositories, and every digest must be the one the registry resolves now. "
                       "Any other body fails the FlowFile and exports nothing.")
        tags = ["container", "image", "oci", "docker", "registry", "export", "diode", "webhook"]

    REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, TRUSTED = (Pull.REGISTRY_URL, Pull.USERNAME, Pull.PASSWORD,
                                                              Pull.SSL_CONTEXT, Pull.TRUSTED)
    REPOSITORIES = PropertyDescriptor(name="Repositories", description="The repositories a webhook may export, separated by commas or "
                         "new lines. An entry ending in * is a prefix, such as team/*. Any other repository fails the FlowFile.",
                         required=True, expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    OUTPUT_DIR, PREFIX, ARTIFACT_TYPE, MAX_SIZE, TIMEOUT = (Pull.OUTPUT_DIR, Pull.PREFIX, Pull.ARTIFACT_TYPE,
                                                            Pull.MAX_SIZE, Pull.TIMEOUT)

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, TRUSTED, REPOSITORIES, OUTPUT_DIR, PREFIX, ARTIFACT_TYPE,
                  MAX_SIZE, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "A FlowFile naming the export tar written to Output Directory"),
                Relationship("failure", "The webhook was refused or the export failed; registry.error says why, nothing was exported"),
                Relationship("original", "The incoming FlowFile", auto_terminated=True)]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor):
        value = evaluate(context.getProperty(descriptor), Attributes({}))
        return value.strip() if value else None

    def _request(self, context, flowfile, client):
        """The (repository, tag, digest) of each image the webhook names, once it has passed every check that needs
        no registry. Raises RegistryError at the first that fails."""
        body = flowfile.getContentsAsBytes()
        if flowfile.getAttribute("X-Sha256") != hashlib.sha256(body).hexdigest():
            raise RegistryError("X-Sha256 is missing or is not the sha256 of the body")
        try:
            request = json.loads(body)
            registry, images = request["registry"], request["images"]
            names = [(i["repository"], i["tag"], i["digest"]) for i in images]
            valid = isinstance(registry, str) and names and all(isinstance(v, str) for n in names for v in n)
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            raise RegistryError('the body is not JSON {"registry": "host", "images": [{"repository", "tag", "digest"}]}')
        if host_of(registry) != host_of(client.host):
            raise RegistryError(f"the body names registry {registry}, not {client.host}")
        entries = [e for e in re.split(r"[,\s]+", self._value(context, self.REPOSITORIES) or "") if e]
        for repository, tag, digest in names:
            if not allowed(repository, entries):
                raise RegistryError(f"{repository} is not in Repositories")
            if not (REPOSITORY.fullmatch(repository) and TAG.fullmatch(tag) and DIGEST.fullmatch(digest)):
                raise RegistryError(f"not a valid repository, tag and digest: {repository}:{tag}@{digest}")
        return list(dict.fromkeys(names))

    def transform(self, context, flowfile):
        try:
            client = RegistryClient(self._value(context, self.REGISTRY_URL), self._value(context, self.USERNAME),
                                    context.getProperty(self.PASSWORD).getValue(),
                                    trusted_der(context.getProperty(self.SSL_CONTEXT)),
                                    int(context.getProperty(self.TIMEOUT).getValue()),
                                    self._value(context, self.TRUSTED))
            names = self._request(context, flowfile, client)
            batch, total = [], 0
            for repository, tag, digest in names:
                if client.manifest_digest(repository, tag) != digest:
                    raise RegistryError(f"{repository}:{tag} is not at {digest} in {client.host}")
                root, children, blobs = plan_image(client, repository, digest)
                batch.append((repository, tag, digest, repository, root, children, blobs))
                total += sum(blobs.values())
            limit = parse_size(context.getProperty(self.MAX_SIZE).getValue())
            if limit and total > limit:
                raise RegistryError(f"the images hold {total} blob bytes, over Max Export Size")
            directory = self._value(context, self.OUTPUT_DIR)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            name = context.getProperty(self.PREFIX).getValue() + stamp + ".tar"
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, name)
            try:
                sha = write_export(client, path, batch)
            except Exception:
                for stale in (path, os.path.join(directory, "." + name + ".partial")):
                    if os.path.exists(stale):
                        os.remove(stale)
                raise
            attributes = export_attributes(directory, name, sha, client.host,
                                           context.getProperty(self.ARTIFACT_TYPE).getValue(),
                                           [f"{r}:{t}@{d}" for r, t, d, *_ in batch])
        except Exception as error:
            self.logger.error(f"Export refused: {error}")
            return FlowFileTransformResult("failure", attributes={"registry.error": str(error)})
        self.logger.info(f"Exported {len(batch)} image tag(s) from {client.host} as {path}")
        return FlowFileTransformResult("success", attributes=attributes, contents=b"")
