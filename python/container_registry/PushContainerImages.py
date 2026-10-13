import os
import tempfile
import uuid

from nifiapi.documentation import use_case
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import RegistryClient, RegistryError, push_export


@use_case(description="Push container images that arrived through a data diode into the high-side registry.",
          configuration="""Route on X-Artifact-Type equals container-images, PutFile the tar to a directory, then set
          Tar File to that directory plus /${filename} and Delete Tar File to true. Auto-terminate original.""",
          keywords=["container", "diode", "registry", "import"])
class PushContainerImages(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.4.0"
        description = ("Pushes every image of a mirror.py export tar to an OCI registry. The tar is read from Tar "
                       "File, or from the FlowFile content when Tar File is empty. Every target tag is checked "
                       "before the first upload, so a refusal pushes nothing.")
        tags = ["container", "image", "oci", "docker", "registry", "import", "diode"]

    REGISTRY_URL = PropertyDescriptor(name="Registry URL", description="The registry to push to: https://registry.example.internal, or "
                         "https://<host>/artifactory/api/docker/<repo-key> for Artifactory.", required=True,
                         validators=[StandardValidators.URL_VALIDATOR],
                         expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    USERNAME = PropertyDescriptor(name="Username", description="Registry user. Empty: anonymous.",
                     expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = PropertyDescriptor(name="Password", description="Registry password or token.", sensitive=True)
    CA_FILE = PropertyDescriptor(name="CA Certificate File", description="A PEM file of CA certificates trusted in addition to the system store "
                    "and SSL_CERT_FILE.", expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    TRUSTED = PropertyDescriptor(name="Trusted Storage Hosts", description="Comma separated host names, addresses or CIDR ranges "
                    "(10.0.0.0/8) that the registry may redirect blob downloads and uploads to although they are not public "
                    "addresses, such as an on-premises MinIO. Anything else the registry names that is loopback, link-local, "
                    "private or plain http from an https registry is refused. The registry's own host is always allowed at the addresses its name resolves to.")
    TAR_FILE = PropertyDescriptor(name="Tar File", description="Path of the export tar on local disk, such as /data/in/${filename} after PutFile. "
                     "Empty: the FlowFile content, which NiFi hands to Python in memory, so keep that for small tars.",
                     expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    DELETE_TAR = PropertyDescriptor(name="Delete Tar File", description="Delete Tar File once every image is pushed.", required=True,
                       allowable_values=["true", "false"], default_value="false")
    REWRITE = PropertyDescriptor(name="Rewrite Base Path", description="Replace the first path segment of each target repository with this.",
                    expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    STRIP = PropertyDescriptor(name="Strip Base Path", description="Remove the first path segment of each target repository.", required=True,
                  allowable_values=["true", "false"], default_value="false")
    MOVE_TAGS = PropertyDescriptor(name="Move Existing Tags", description="Let a push move a tag that points at another digest. false: such a "
                      "tar fails and nothing is pushed.", required=True, allowable_values=["true", "false"],
                      default_value="false")
    TIMEOUT = PropertyDescriptor(name="Request Timeout", description="Seconds a registry read or write may stall.", required=True,
                    default_value="600", validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, CA_FILE, TRUSTED, TAR_FILE, DELETE_TAR, REWRITE, STRIP, MOVE_TAGS, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "Every image of the tar is in the registry"),
                Relationship("failure", "Nothing was pushed or the push stopped part way; registry.error says why"),
                Relationship("original", "The incoming FlowFile", auto_terminated=True)]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor, flowfile):
        # Username and CA Certificate File are ENVIRONMENT scoped: environment variables, not the FlowFile
        scope = Attributes() if descriptor in (self.USERNAME, self.CA_FILE) else flowfile
        value = evaluate(context.getProperty(descriptor), scope)
        return value.strip() if value else None

    def transform(self, context, flowfile):
        path = self._value(context, self.TAR_FILE, flowfile)
        delete = bool(path) and context.getProperty(self.DELETE_TAR).getValue() == "true"
        temporary = claimed = None
        try:
            client = RegistryClient(self._value(context, self.REGISTRY_URL, flowfile),
                                    self._value(context, self.USERNAME, flowfile),
                                    context.getProperty(self.PASSWORD).getValue(),
                                    self._value(context, self.CA_FILE, flowfile),
                                    int(context.getProperty(self.TIMEOUT).getValue()),
                                    self._value(context, self.TRUSTED, flowfile))
            if delete:
                # Renamed for the push, so a second task given the same path fails instead of racing on it.
                if not os.path.isfile(path):
                    raise RegistryError(f"Tar File {path} does not exist")
                claim = f"{path}.pushing-{uuid.uuid4().hex}"
                os.rename(path, claim)
                claimed = claim  # only once the tar is really moved, so a failure does not try to move it back
            elif not path:
                handle, temporary = tempfile.mkstemp(suffix=".tar")
                with os.fdopen(handle, "wb") as out:
                    out.write(flowfile.getContentsAsBytes())
            pushed, present, lines = push_export(
                client, claimed or path or temporary, self._value(context, self.REWRITE, flowfile),
                context.getProperty(self.STRIP).getValue() == "true",
                context.getProperty(self.MOVE_TAGS).getValue() == "true", self.logger.info)
        except Exception as error:
            if claimed:
                os.rename(claimed, path)  # back where it was, for a retry
            self.logger.error(f"Push of {path or 'the FlowFile content'} failed: {error}")
            return FlowFileTransformResult("failure", attributes={"registry.error": str(error)})
        finally:
            if temporary:
                os.remove(temporary)
        attributes = {"registry.host": client.host, "registry.images": "\n".join(lines),
                      "registry.pushed": str(pushed), "registry.present": str(present)}
        if claimed:
            try:
                os.remove(claimed)
            except OSError as error:  # the images are pushed; a leftover file is not a failed push
                self.logger.warn(f"Pushed, but could not delete {claimed}: {error}")
                attributes["registry.tar.leftover"] = claimed
        self.logger.info(f"{client.host}: pushed {pushed} image tag(s), {present} already present")
        return FlowFileTransformResult("success", attributes=attributes)
