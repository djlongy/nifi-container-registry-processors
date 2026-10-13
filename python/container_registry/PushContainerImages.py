import os
import tempfile

from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators

from container_registry_client import RegistryClient, push_export


def _prop(name, description, **kw):
    return PropertyDescriptor(name=name, description=description, **kw)


class PushContainerImages(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.3.0"
        description = ("Pushes every image of a mirror.py export tar to an OCI registry. The tar is read from Tar "
                       "File, or from the FlowFile content when Tar File is empty. Every target tag is checked "
                       "before the first upload, so a refusal pushes nothing.")
        tags = ["container", "image", "oci", "docker", "registry", "import", "diode"]

    REGISTRY_URL = _prop("Registry URL", "The registry to push to: https://registry.example.internal, or "
                         "https://<host>/artifactory/api/docker/<repo-key> for Artifactory.", required=True,
                         validators=[StandardValidators.URL_VALIDATOR],
                         expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    USERNAME = _prop("Username", "Registry user. Empty: anonymous.",
                     expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = _prop("Password", "Registry password or token.", sensitive=True)
    CA_FILE = _prop("CA Certificate File", "A PEM file of CA certificates trusted in addition to the system store "
                    "and SSL_CERT_FILE.", expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    TAR_FILE = _prop("Tar File", "Path of the export tar on local disk, such as /data/in/${filename} after PutFile. "
                     "Empty: the FlowFile content, which NiFi hands to Python in memory, so keep that for small tars.",
                     expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    DELETE_TAR = _prop("Delete Tar File", "Delete Tar File once every image is pushed.", required=True,
                       allowable_values=["true", "false"], default_value="false")
    REWRITE = _prop("Rewrite Base Path", "Replace the first path segment of each target repository with this.",
                    expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    STRIP = _prop("Strip Base Path", "Remove the first path segment of each target repository.", required=True,
                  allowable_values=["true", "false"], default_value="false")
    MOVE_TAGS = _prop("Move Existing Tags", "Let a push move a tag that points at another digest. false: such a "
                      "tar fails and nothing is pushed.", required=True, allowable_values=["true", "false"],
                      default_value="false")
    TIMEOUT = _prop("Request Timeout", "Seconds a registry read or write may stall.", required=True,
                    default_value="600", validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, CA_FILE, TAR_FILE, DELETE_TAR, REWRITE, STRIP, MOVE_TAGS, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor, flowfile):
        value = context.getProperty(descriptor).evaluateAttributeExpressions(flowfile).getValue()
        return value.strip() if value else None

    def transform(self, context, flowfile):
        path = self._value(context, self.TAR_FILE, flowfile)
        temporary = None
        try:
            client = RegistryClient(self._value(context, self.REGISTRY_URL, flowfile),
                                    self._value(context, self.USERNAME, flowfile),
                                    context.getProperty(self.PASSWORD).getValue(),
                                    self._value(context, self.CA_FILE, flowfile),
                                    int(context.getProperty(self.TIMEOUT).getValue()))
            if self._value(context, self.REWRITE, flowfile) and context.getProperty(self.STRIP).getValue() == "true":
                raise ValueError("set Rewrite Base Path or Strip Base Path, not both")
            if not path:
                handle, temporary = tempfile.mkstemp(suffix=".tar")
                with os.fdopen(handle, "wb") as out:
                    out.write(flowfile.getContentsAsBytes())
            pushed, present, lines = push_export(
                client, path or temporary, self._value(context, self.REWRITE, flowfile),
                context.getProperty(self.STRIP).getValue() == "true",
                context.getProperty(self.MOVE_TAGS).getValue() == "true", self.logger.info)
            if path and context.getProperty(self.DELETE_TAR).getValue() == "true":
                os.remove(path)
            self.logger.info(f"{client.host}: pushed {pushed} image tag(s), {present} already present")
            return FlowFileTransformResult("success", attributes={
                "registry.host": client.host, "registry.images": "\n".join(lines),
                "registry.pushed": str(pushed), "registry.present": str(present)})
        except Exception as error:
            self.logger.error(f"Push of {path or 'the FlowFile content'} failed: {error}")
            return FlowFileTransformResult("failure", attributes={"registry.error": str(error)})
        finally:
            if temporary:
                os.remove(temporary)
