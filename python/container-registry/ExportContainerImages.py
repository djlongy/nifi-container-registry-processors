import json
import os
import re
from datetime import datetime, timezone

from nifiapi.documentation import use_case
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import (RegistryClient, RegistryError, bundle_bytes,
                                       export_attributes, parse_size, plan_image, trusted_der, webhook_images, write_export)


def image_attributes(images):
    """registry.image.count and registry.image.N.repository, .tag and .digest for [(repository, tag, digest)]."""
    attributes = {"registry.image.count": str(len(images))}
    for n, (repository, tag, digest) in enumerate(images):
        attributes.update({f"registry.image.{n}.repository": repository, f"registry.image.{n}.tag": tag,
                           f"registry.image.{n}.digest": digest})
    return attributes


@use_case(description="Export the images a gitlab-registry-mirror webhook names from a registry for a data diode.",
          configuration="""ListenHTTP, then RouteOnAttribute on X-Artifact-Type equals container-images-request, then this
          processor with Source Registry URL, Allowed Repositories and Tar Output Directory (Tar Per Image false: one tar). Send success to PushContainerImages with Tar
          File set to the Tar Output Directory plus /${filename}.""",
          keywords=["container", "diode", "registry", "export", "webhook"])
class ExportContainerImages(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.5.3"
        description = ("Exports the images a gitlab-registry-mirror webhook names as one mirror.py export tar in Tar Output "
                       "Directory, on this node's local disk. The body only selects images: it must match the X-Sha256 "
                       "attribute, name the host of Source Registry URL and repositories in Allowed Repositories, and every "
                       "digest must be the one the registry resolves now. Any other body fails the FlowFile and exports "
                       "nothing. PushContainerImages must run on the same node, or the directory must be shared storage.")
        tags = ["container", "image", "oci", "docker", "registry", "export", "diode", "webhook"]

    REGISTRY_URL = PropertyDescriptor(
        name="Source Registry URL",
        description=(
            "Registry to read images from, such as https://registry.example.com, or "
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
            " reference a parameter, such as #{gitlab.container.low.token}."),
        sensitive=True)
    SSL_CONTEXT = PropertyDescriptor(
        name="SSL Context Service",
        description=(
            "A StandardSSLContextService whose PKCS12 or JKS truststore holds the registry's CA (it "
            "does not accept PEM). Its CAs add to the system store and SSL_CERT_FILE. Empty "
            "(default): only those are trusted. Its keystore is ignored."),
        controller_service_definition="org.apache.nifi.ssl.SSLContextProvider")
    TRUSTED = PropertyDescriptor(
        name="Trusted Storage Hosts",
        description=(
            "Hosts, IPs or CIDR ranges the registry may redirect blob transfers to although they are "
            "private, such as an on-premises MinIO: minio.internal, 10.20.0.0/16. Empty (default): "
            "private, loopback, link-local and http-from-https redirects are refused. The registry's "
            "own host is always allowed."))
    REPOSITORIES = PropertyDescriptor(
        name="Allowed Repositories",
        description=(
            "Allowlist, not the selection: the webhook body picks the images and each must be in "
            "here. Comma or new line separated: team/app, or team/* for a prefix, * for all. "
            "Required. Any other repository fails the FlowFile."),
        required=True,
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    OUTPUT_DIR = PropertyDescriptor(
        name="Tar Output Directory",
        description=(
            "Local directory on this node for the tars, such as /data/out. Set it from a parameter, "
            "#{gitlab.container.tar.dir}. Use a per-node volume, same path on every node, outside "
            "NiFi's repositories. Trap: Push reads it by path: keep the connection not load-balanced,"
            " or use shared storage."),
        required=True,
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PREFIX = PropertyDescriptor(
        name="Tar Filename Prefix",
        description=(
            "Start of each tar's file name, followed by the UTC time and .tar, such as container-"
            "images-20260101T000000000000Z.tar. Default container-images-. Pull treats any such file "
            "left in Tar Output Directory as its own, so keep it unique there."),
        required=True,
        default_value="container-images-")
    ARTIFACT_TYPE = PropertyDescriptor(
        name="X-Artifact-Type Value",
        description=(
            "Value of the X-Artifact-Type attribute on the output FlowFile, which a RouteOnAttribute "
            "downstream matches. Default container-images, the value the Push route expects. Change "
            "it only together with that route."),
        required=True,
        default_value="container-images")
    MAX_SIZE = PropertyDescriptor(
        name="Max Export Size",
        description=(
            "Most blob bytes in one tar, such as 20 GB; a layer shared by two images counts once. "
            "More fails the whole webhook, nothing is split. Empty (default): no limit."),
        validators=[StandardValidators.DATA_SIZE_VALIDATOR])
    TAR_PER_IMAGE = PropertyDescriptor(
        name="Tar Per Image",
        description=(
            "Default false: one tar for the webhook, shared layers once. true: one tar per image; layers repeat, "
            "so more bytes cross the diode (1.92x with 2 images sharing 4 of 5 layers). The FlowFile then "
            "holds one JSON line per tar, for SplitText."),
        required=True,
        allowable_values=["true", "false"],
        default_value="false")
    TIMEOUT = PropertyDescriptor(
        name="Request Timeout",
        description=(
            "Seconds a registry read may stall before it fails. Default 600 (10 minutes). Blobs "
            "stream, so this is a stall limit, not a limit on the total time."),
        required=True,
        default_value="600",
        validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])


    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, TRUSTED, REPOSITORIES, OUTPUT_DIR, PREFIX, ARTIFACT_TYPE,
                  MAX_SIZE, TAR_PER_IMAGE, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "Empty FlowFile whose filename and absolute.path attributes name the tar written to Tar Output Directory"),
                Relationship("failure", "The webhook was refused or the export failed; the registry.error attribute says why. Nothing was exported"),
                Relationship("original", "The incoming webhook FlowFile, unchanged. Auto-terminated by default", auto_terminated=True)]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor):
        value = evaluate(context.getProperty(descriptor), Attributes({}))
        return value.strip() if value else None

    def _request(self, context, flowfile, client):
        """The (repository, tag, digest) of each image the webhook names, once it has passed every check that needs
        no registry. Raises RegistryError at the first that fails."""
        entries = [e for e in re.split(r"[,\s]+", self._value(context, self.REPOSITORIES) or "") if e]
        return webhook_images(flowfile.getContentsAsBytes(), flowfile.getAttribute("X-Sha256"), client.host, entries)

    def _client(self, context):
        return RegistryClient(self._value(context, self.REGISTRY_URL), self._value(context, self.USERNAME),
                              context.getProperty(self.PASSWORD).getValue(),
                              trusted_der(context.getProperty(self.SSL_CONTEXT)),
                              int(context.getProperty(self.TIMEOUT).getValue()),
                              self._value(context, self.TRUSTED))

    @staticmethod
    def _batch(client, names):
        """[(repository, tag, digest, repository, root, children, blobs)] for each named image, refused when a tag moved."""
        batch = []
        for repository, tag, digest in names:
            if client.manifest_digest(repository, tag) != digest:
                raise RegistryError(f"{repository}:{tag} is not at {digest} in {client.host}")
            root, children, blobs = plan_image(client, repository, digest)
            batch.append((repository, tag, digest, repository, root, children, blobs))
        return batch

    @staticmethod
    def _check_size(groups, limit, per_image):
        for group in groups:
            total = bundle_bytes(image[6] for image in group)
            if limit and total > limit:
                raise RegistryError(f"{'an image holds' if per_image else 'the images hold'} {total} blob bytes, "
                                    "over Max Export Size")

    @staticmethod
    def _write_groups(client, groups, directory, prefix, stamp, per_image, artifact_type):
        """Write one tar per group and return each one's attributes; a failure removes every tar already written."""
        written, outputs = [], []
        try:
            for n, group in enumerate(groups):
                name = f"{prefix}{stamp}-{n:06d}.tar" if per_image else f"{prefix}{stamp}.tar"
                written.append(name)
                sha = write_export(client, os.path.join(directory, name), group)
                triples = [(r, t, d) for r, t, d, *_ in group]
                outputs.append({**export_attributes(directory, name, sha, client.host, artifact_type,
                                                    [f"{r}:{t}@{d}" for r, t, d in triples]),
                                **image_attributes(triples)})
        except Exception:
            for name in written:
                for stale in (os.path.join(directory, name), os.path.join(directory, "." + name + ".partial")):
                    if os.path.exists(stale):
                        os.remove(stale)
            raise
        return outputs

    def _export(self, context, flowfile, client):
        names = self._request(context, flowfile, client)
        batch = self._batch(client, names)
        per_image = context.getProperty(self.TAR_PER_IMAGE).getValue() == "true"
        groups = [[image] for image in batch] if per_image else [batch]
        self._check_size(groups, parse_size(context.getProperty(self.MAX_SIZE).getValue()), per_image)
        directory = self._value(context, self.OUTPUT_DIR)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        artifact_type = context.getProperty(self.ARTIFACT_TYPE).getValue()
        os.makedirs(directory, exist_ok=True)
        outputs = self._write_groups(client, groups, directory, context.getProperty(self.PREFIX).getValue(), stamp,
                                     per_image, artifact_type)
        return batch, per_image, artifact_type, directory, outputs

    def transform(self, context, flowfile):
        try:
            client = self._client(context)
            batch, per_image, artifact_type, directory, outputs = self._export(context, flowfile, client)
        except Exception as error:
            self.logger.error(f"Export refused: {error}")
            return FlowFileTransformResult("failure", attributes={"registry.error": str(error)})
        self.logger.info(f"Exported {len(batch)} image tag(s) from {client.host} as {len(outputs)} tar(s) in {directory}")
        if not per_image:
            return FlowFileTransformResult("success", attributes=outputs[0], contents=b"")
        # One FlowFile cannot become several in a Python FlowFileTransform: the content holds one JSON object of
        # attributes per tar, and SplitText plus EvaluateJsonPath turn each line into its own FlowFile.
        for output, (repository, tag, digest, *_) in zip(outputs, batch):
            output.update({"registry.repository": repository, "registry.tag": tag, "registry.digest": digest})
        lines = "".join(json.dumps(output, sort_keys=True) + "\n" for output in outputs).encode()
        triples = [(r, t, d) for r, t, d, *_ in batch]
        return FlowFileTransformResult("success", contents=lines, attributes={
            "mime.type": "application/x-ndjson", "registry.host": client.host, "X-Artifact-Type": artifact_type,
            "registry.tar.count": str(len(outputs)), "registry.images": "\n".join(f"{r}:{t}@{d}" for r, t, d in triples),
            **image_attributes(triples)})
