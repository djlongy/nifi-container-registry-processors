import fcntl
import glob
import os
import tempfile
import uuid

from nifiapi.documentation import use_case
from nifiapi.flowfiletransform import FlowFileTransform, FlowFileTransformResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import RegistryClient, RegistryError, push_export, trusted_der


def _lock(path):
    """An fd holding an exclusive flock on path, or None when it is locked (a live push) or gone. Opened read-write
    because flock on NFS is emulated with fcntl locks, and an exclusive one needs a writable descriptor."""
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _contain(template, path):
    """Raise unless path, a template with attribute expressions filled in, stays strictly inside the directory the
    template fixes in front of its first expression. A filename that arrives with the data (../x, or a symlink the
    directory holds) must not choose which file is renamed, read or deleted. A template with no expression is
    the operator's own path, and no path means the tar is the FlowFile content."""
    start = template.find("${") if path else -1
    if start < 0:
        return
    fixed = template[:start]
    directory = fixed[:fixed.rfind("/") + 1]
    if not directory.strip("/"):
        raise RegistryError("Input Tar Path built from an expression needs a fixed directory in front of the "
                            f"expression, such as /data/in/${{filename}}: {template}")
    root = os.path.realpath(directory)
    real = os.path.realpath(path)
    if real == root or os.path.commonpath([root, real]) != root:
        raise RegistryError(f"Input Tar Path {path} is outside {root}")


def _take(path):
    """(file to claim, fd holding its lock). Input Tar Path itself, else the one <path>.pushing-* a killed push left.
    A leftover that is locked belongs to a push still running and is not touched."""
    if os.path.isfile(path):
        fd = _lock(path)
        if fd is None:
            raise RegistryError(f"Input Tar Path {path} is locked: a push of it is running")
        return path, fd
    found = [(name, _lock(name)) for name in sorted(glob.glob(glob.escape(path) + ".pushing-*"))]
    free = [(name, fd) for name, fd in found if fd is not None]
    if len(free) == 1:
        return free[0]
    for _, fd in free:
        os.close(fd)
    if free:
        raise RegistryError(f"Input Tar Path {path} does not exist and more than one push was left part way: "
                            f"{', '.join(name for name, _ in free)}. Keep one, delete the others")
    note = ", and a push of that tar is still running" if found else ""
    raise RegistryError(f"Input Tar Path {path} does not exist{note}")


@use_case(description="Push container images that arrived through a data diode into the high-side registry.",
          configuration="""Route on X-Artifact-Type equals container-images, PutFile the tar to a directory, then set
          Input Tar Path to that directory plus /${filename} and Delete Input Tar to true. Auto-terminate original.""",
          keywords=["container", "diode", "registry", "import"])
class PushContainerImages(FlowFileTransform):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileTransform"]

    class ProcessorDetails:
        version = "1.5.2"
        description = ("Pushes every image of a mirror.py export tar into the target registry, digests unchanged. The tar "
                       "is read from the local disk path in Input Tar Path (set it for any real tar), or from the "
                       "FlowFile content when that is empty. Every target tag is checked before the first upload, so "
                       "a refusal pushes nothing. In a cluster the file must be on the node that runs this processor.")
        tags = ["container", "image", "oci", "docker", "registry", "import", "diode"]

    REGISTRY_URL = PropertyDescriptor(
        name="Target Registry URL",
        description=(
            "Registry to push images into, such as https://registry.example.com, or "
            "https://host/artifactory/api/docker/repo-key for Artifactory. No user:password in the "
            "URL. Required, no default. FlowFile attributes allowed."),
        required=True,
        validators=[StandardValidators.URL_VALIDATOR],
        expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    USERNAME = PropertyDescriptor(
        name="Target Registry Username",
        description=(
            "User for the target registry. Empty (default): anonymous access, which works only on a "
            "registry that allows it. Environment variables allowed: ${REGISTRY_USER}."),
        expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = PropertyDescriptor(
        name="Target Registry Password",
        description=(
            "Password or access token for the target registry. Empty (default): none sent. Sensitive:"
            " reference a parameter, such as #{registry_password}."),
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
    TAR_FILE = PropertyDescriptor(
        name="Input Tar Path",
        description=(
            "Path of the export tar on this node's local disk, such as #{container-images.tar.dir}/"
            "${filename}. Export must write it here, so no load balancing. Never in NiFi's "
            "repositories. If missing, one <path>.pushing-* left by a kill is adopted. Empty "
            "(default): FlowFile content, in memory, small tars only."),
        expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    DELETE_TAR = PropertyDescriptor(
        name="Delete Input Tar",
        description=(
            "Delete the tar once every image is pushed. Default false: it stays. A failed push keeps it. "
            "The tar is renamed to <path>.pushing-<id> while pushing, so a killed push leaves that name; "
            "a replay adopts it. Ignored when Input Tar Path is empty."),
        required=True,
        allowable_values=["true", "false"],
        default_value="false")
    REWRITE = PropertyDescriptor(
        name="Rewrite Target Base Path",
        description=(
            "Replace the first path segment of every repository pushed: team/app becomes new/app with"
            " new. Empty (default): the name recorded in the tar. Not with Strip Target Base Path. "
            "FlowFile attributes allowed."),
        expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)
    STRIP = PropertyDescriptor(
        name="Strip Target Base Path",
        description=(
            "Remove the first path segment of every target repository: team/app becomes app. Default "
            "false: names unchanged. Not with Rewrite Target Base Path."),
        required=True,
        allowable_values=["true", "false"],
        default_value="false")
    MOVE_TAGS = PropertyDescriptor(
        name="Move Existing Tags",
        description=(
            "Move a tag that already points at another digest in the target registry, as docker push does. "
            "Default true. Set false to protect tags: such a tar then fails with registry.error and nothing "
            "is pushed. A tag already at the right digest is skipped either way."),
        required=True,
        allowable_values=["true", "false"],
        default_value="true")
    TIMEOUT = PropertyDescriptor(
        name="Request Timeout",
        description=(
            "Seconds a registry read or write may stall before it fails. Default 600 (10 minutes). "
            "Blobs stream, so this is a stall limit, not a limit on the total time."),
        required=True,
        default_value="600",
        validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, TRUSTED, TAR_FILE, DELETE_TAR, REWRITE, STRIP, MOVE_TAGS, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "Every image of the tar is now in the target registry, pushed or already present; registry.images lists them"),
                Relationship("failure", "Nothing was pushed, or the push stopped part way; registry.error says why. Input Tar Path is kept for a retry"),
                Relationship("original", "The incoming FlowFile, unchanged. Auto-terminated by default", auto_terminated=True)]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def _value(self, context, descriptor, flowfile):
        # Username is ENVIRONMENT scoped: environment variables, not the FlowFile
        scope = Attributes() if descriptor in (self.USERNAME,) else flowfile
        value = evaluate(context.getProperty(descriptor), scope)
        return value.strip() if value else None

    def transform(self, context, flowfile):
        path = self._value(context, self.TAR_FILE, flowfile)
        delete = bool(path) and context.getProperty(self.DELETE_TAR).getValue() == "true"
        temporary = claimed = lock = None
        try:
            _contain(context.getProperty(self.TAR_FILE).getValue(), path)
            client = RegistryClient(self._value(context, self.REGISTRY_URL, flowfile),
                                    self._value(context, self.USERNAME, flowfile),
                                    context.getProperty(self.PASSWORD).getValue(),
                                    trusted_der(context.getProperty(self.SSL_CONTEXT)),
                                    int(context.getProperty(self.TIMEOUT).getValue()),
                                    self._value(context, self.TRUSTED, flowfile))
            if delete:
                # Renamed for the push, so a second task given the same path fails instead of racing on it.
                source, lock = _take(path)
                claim = f"{path}.pushing-{uuid.uuid4().hex}"
                os.rename(source, claim)  # the lock follows the open file, not the name
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
            if lock is not None:
                os.close(lock)
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
        if lock is not None:
            os.close(lock)  # only now, so no replay can adopt the tar while it is still being deleted
        self.logger.info(f"{client.host}: pushed {pushed} image tag(s), {present} already present")
        return FlowFileTransformResult("success", attributes=attributes)
