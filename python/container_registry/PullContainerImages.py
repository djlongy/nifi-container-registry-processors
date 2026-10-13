import json
import os
import re
import urllib.request
from datetime import datetime, timezone

from nifiapi.__jvm__ import JvmHolder
from nifiapi.componentstate import Scope
from nifiapi.flowfilesource import FlowFileSource, FlowFileSourceResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDescriptor, StandardValidators

from container_registry_client import (DIGEST, REPOSITORY, TAG, RegistryClient, RegistryError, plan_image,
                                       rebase, web_opener, write_export)

LISTING, LEDGER, IMAGE_LIST = "Registry Listing", "Ledger", "Image List"
INITIALISED = "__initialised"
UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def _prop(name, description, **kw):
    return PropertyDescriptor(name=name, description=description, **kw)


class _Attributes:
    """What evaluateAttributeExpressions reads, for a FlowFile that does not exist yet."""
    def __init__(self, values):
        self.values = values

    def getAttribute(self, name):
        return self.values.get(name)

    def getAttributes(self):
        java = JvmHolder.jvm.java.util.HashMap()
        for key, value in self.values.items():
            java.put(key, value)
        return java


class PullContainerImages(FlowFileSource):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileSource"]

    class ProcessorDetails:
        version = "1.3.0"
        description = ("Exports container images from an OCI registry as a mirror.py export tar written to Output "
                       "Directory, and emits a FlowFile that names it, for FetchFile to read. Each run exports what "
                       "changed since the last export, recorded in cluster state. Schedule it on the primary node only.")
        tags = ["container", "image", "oci", "docker", "registry", "export", "diode"]

    REGISTRY_URL = _prop("Registry URL", "The registry to export from: https://registry.example.internal, or "
                         "https://<host>/artifactory/api/docker/<repo-key> for Artifactory.", required=True,
                         validators=[StandardValidators.URL_VALIDATOR],
                         expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    USERNAME = _prop("Username", "Registry user. Empty: anonymous.",
                     expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = _prop("Password", "Registry password or token.", sensitive=True)
    CA_FILE = _prop("CA Certificate File", "A PEM file of CA certificates trusted in addition to the system store "
                    "and SSL_CERT_FILE, for the registry and the Ledger URL.",
                    expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    IMAGE_SOURCE = _prop("Image Source", "What a run exports. Registry Listing: every tag of Repositories whose "
                         "digest differs from the last export. Ledger: what the mirror pipeline's ledger says it "
                         "synced. Image List: the images in Image List.", required=True,
                         allowable_values=[LISTING, LEDGER, IMAGE_LIST], default_value=LISTING)
    REPOSITORIES = _prop("Repositories", "For Registry Listing: repositories, separated by commas or new lines. "
                         "team/* matches every catalog repository with that prefix, * matches all.",
                         expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    TAG_FILTER = _prop("Tag Filter", "For Registry Listing: a regular expression a tag must match in full. The "
                       "default skips sha256-... signature and attestation tags.", required=True,
                       default_value="(?!sha256-).+", validators=[StandardValidators.REGULAR_EXPRESSION_VALIDATOR])
    LEDGER_URL = _prop("Ledger URL", "For Ledger: the ledger.json URL, read only.",
                       expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    LEDGER_TOKEN = _prop("Ledger Token", "Token for the Ledger URL.", sensitive=True)
    LEDGER_TOKEN_HEADER = _prop("Ledger Token Header", "Header that carries the Ledger Token: PRIVATE-TOKEN for "
                                "GitLab, Authorization for a Bearer value.", default_value="PRIVATE-TOKEN")
    IMAGE_LIST = _prop("Image List", "For Image List: one image per line, repository:tag or "
                       "repository@sha256:..., optionally prefixed with the registry host. # starts a comment.",
                       expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    INITIAL_RUN = _prop("Initial Run", "What a run does when the state is empty. Export All exports every image. "
                        "Record Only records the current digests and exports nothing.", required=True,
                        allowable_values=["Export All", "Record Only"], default_value="Export All")
    REWRITE = _prop("Rewrite Base Path", "Replace the first path segment of each target repository with this.",
                    expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    STRIP = _prop("Strip Base Path", "Remove the first path segment of each target repository.", required=True,
                  allowable_values=["true", "false"], default_value="false")
    OUTPUT_DIR = _prop("Output Directory", "Where the tar is written. It must hold the largest export.",
                       required=True, expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PREFIX = _prop("Filename Prefix", "Start of each tar's name.", required=True, default_value="container-images-")
    ARTIFACT_TYPE = _prop("Artifact Type", "The X-Artifact-Type attribute.", required=True,
                          default_value="container-images")
    MAX_SIZE = _prop("Max Export Size", "The most blob bytes one tar holds, such as 20 GB. The rest go in the next "
                     "run. An image larger than this goes alone. Empty: no limit.",
                     validators=[StandardValidators.DATA_SIZE_VALIDATOR])
    TIMEOUT = _prop("Request Timeout", "Seconds a registry read may stall.", required=True, default_value="600",
                    validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, CA_FILE, IMAGE_SOURCE, REPOSITORIES, TAG_FILTER, LEDGER_URL,
                  LEDGER_TOKEN, LEDGER_TOKEN_HEADER, IMAGE_LIST, INITIAL_RUN, REWRITE, STRIP, OUTPUT_DIR, PREFIX,
                  ARTIFACT_TYPE, MAX_SIZE, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def getDynamicPropertyDescriptor(self, name):
        return PropertyDescriptor(name=name, description="Added to each FlowFile as an attribute.", dynamic=True,
                                  expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)

    def _value(self, context, descriptor):
        value = context.getProperty(descriptor).evaluateAttributeExpressions(_Attributes({})).getValue()
        return value.strip() if value else None

    # -- what to export ---------------------------------------------------------------------------
    def _from_listing(self, context, client):
        tag_filter = re.compile(context.getProperty(self.TAG_FILTER).getValue())
        catalog, repositories = None, []
        for entry in re.split(r"[,\s]+", self._value(context, self.REPOSITORIES) or ""):
            if not entry:
                continue
            if entry.endswith("*"):
                catalog = client.catalog() if catalog is None else catalog
                repositories += [r for r in catalog if r.startswith(entry[:-1]) and r not in repositories]
            elif entry not in repositories:
                repositories.append(entry)
        for repository in repositories:
            for tag in client.tags(repository) or []:  # None: the repository is gone
                if not tag_filter.fullmatch(tag):
                    continue
                if not TAG.fullmatch(tag) or not REPOSITORY.fullmatch(repository):
                    self.logger.warn(f"Skipping {repository}:{tag}: not a valid repository and tag")
                    continue
                # None (deleted since the listing) keeps its state entry and exports nothing
                yield repository, tag, client.manifest_digest(repository, tag)

    def _from_ledger(self, context, client):
        url = self._value(context, self.LEDGER_URL)
        if not url:
            raise RegistryError("Image Source Ledger needs a Ledger URL")
        headers = {}
        token = context.getProperty(self.LEDGER_TOKEN).getValue()
        if token:
            headers[context.getProperty(self.LEDGER_TOKEN_HEADER).getValue()] = token
        request = urllib.request.Request(url, headers=headers)
        with web_opener(client.context).open(request, timeout=client.timeout) as response:  # the token never follows a redirect
            ledger = json.loads(response.read())
        owner = ledger.get("registry") or ""
        if owner and owner != client.host:
            raise RegistryError(f"the ledger belongs to {owner}, not {client.host}; set Registry URL to it")
        for key, record in (ledger.get("synced") or {}).items():
            digest = (record or {}).get("digest", "")
            if ":" not in key or not DIGEST.fullmatch(digest):
                self.logger.warn(f"Skipping ledger entry {key}: no valid digest")
                continue
            repository = key.rsplit(":", 1)[0]
            for tag in record.get("tags") or [key.rsplit(":", 1)[1]]:
                if not TAG.fullmatch(tag) or not REPOSITORY.fullmatch(repository):
                    self.logger.warn(f"Skipping ledger entry {repository}:{tag}: not a valid repository and tag")
                    continue
                yield repository, tag, digest

    def _from_list(self, context, client):
        bare = re.sub(r":443$", "", client.host)
        for line in (self._value(context, self.IMAGE_LIST) or "").splitlines():
            reference = line.split("#", 1)[0].strip()
            if not reference:
                continue
            for prefix in (client.host + "/", bare + "/"):
                if reference.startswith(prefix):
                    reference = reference[len(prefix):]
                    break
            reference, _, digest = reference.partition("@")
            repository, tag = reference, None
            if reference.rfind(":") > reference.rfind("/"):
                repository, tag = reference.rsplit(":", 1)
            if (not REPOSITORY.fullmatch(repository) or (tag and not TAG.fullmatch(tag))
                    or (digest and not DIGEST.fullmatch(digest))):
                raise RegistryError(f"not an image reference: {line.strip()}")
            if not digest:
                tag = tag or "latest"
                digest = client.manifest_digest(repository, tag)
                if not digest:
                    raise RegistryError(f"{client.host}/{repository}:{tag} is not in the registry")
            yield repository, tag or "sha256-" + digest[7:19], digest

    # -- one run ----------------------------------------------------------------------------------
    def create(self, context):
        client = RegistryClient(self._value(context, self.REGISTRY_URL), self._value(context, self.USERNAME),
                                context.getProperty(self.PASSWORD).getValue(), self._value(context, self.CA_FILE),
                                int(context.getProperty(self.TIMEOUT).getValue()))
        source = context.getProperty(self.IMAGE_SOURCE).getValue()
        found = {LISTING: self._from_listing, LEDGER: self._from_ledger, IMAGE_LIST: self._from_list}[source]
        state_manager = context.getStateManager()
        exported = state_manager.getState(Scope.CLUSTER).toMap()
        initial = INITIALISED not in exported

        current = {f"{r}:{t}": (r, t, d) for r, t, d in found(context, client)}
        # Remember only what the source still lists, so the state does not grow with every tag ever seen.
        state = {k: v for k, v in exported.items() if k in current}
        state[INITIALISED] = "true"
        wanted = [v for k, v in current.items() if v[2] and exported.get(k) != v[2]]

        if initial and context.getProperty(self.INITIAL_RUN).getValue() == "Record Only":
            state.update({f"{r}:{t}": d for r, t, d in wanted})
            state_manager.setState(state, Scope.CLUSTER)
            self.logger.info(f"Initial run: recorded {len(wanted)} image tag(s) without exporting them")
            return None
        if not wanted:
            if state != exported:
                state_manager.setState(state, Scope.CLUSTER)
            return None

        limit = self._max_size(context)
        rewrite = self._value(context, self.REWRITE)
        strip = context.getProperty(self.STRIP).getValue() == "true"
        if strip and rewrite:
            raise RegistryError("set Rewrite Base Path or Strip Base Path, not both")
        batch, planned, total = [], {}, 0
        for repository, tag, digest in wanted:
            key = f"{repository}@{digest}"
            if key not in planned:
                try:
                    planned[key] = plan_image(client, repository, digest)
                except RegistryError as error:
                    # Garbage-collected, or the sync is mid-run: this image waits for the next run.
                    self.logger.warn(f"Skipping {repository}:{tag}@{digest} this run: {error}")
                    continue
            try:
                target = rebase(repository, rewrite, strip)
            except RegistryError as error:
                self.logger.warn(f"Skipping {repository}:{tag}: {error}")
                continue
            root, children, blobs = planned[key]
            size = sum(blobs.values())
            if limit and batch and total + size > limit:
                continue  # the next run takes it, since the state does not record it
            batch.append((repository, tag, digest, target, root, children, blobs))
            total += size
        if not batch:
            state_manager.setState(state, Scope.CLUSTER)
            return None

        directory = self._value(context, self.OUTPUT_DIR)
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = context.getProperty(self.PREFIX).getValue() + stamp + ".tar"
        path = os.path.join(directory, name)
        try:
            sha = write_export(client, path, batch)
            attributes = {
                "filename": name, "Filename": name, "absolute.path": directory.rstrip("/") + "/",
                "Content-Type": "application/octet-stream", "mime.type": "application/x-tar",
                "X-Sha256": sha, "X-Artifact-Type": context.getProperty(self.ARTIFACT_TYPE).getValue(),
                "X-Artifact-Format": "tar", "registry.host": client.host,
                "registry.images": "\n".join(f"{t}:{tag}@{d}" for _r, tag, d, t, *_ in batch),
                "file.size": str(os.path.getsize(path)),
            }
            for descriptor in context.getProperties():
                if descriptor.dynamic:
                    value = context.getProperty(descriptor).evaluateAttributeExpressions(_Attributes(attributes))
                    attributes[descriptor.name] = value.getValue() or ""
            # Last, so a failure above leaves the state as it was and the next run exports these again.
            state.update({f"{r}:{t}": d for r, t, d, *_ in batch})
            state_manager.setState(state, Scope.CLUSTER)
        except Exception:
            for leftover in (path, os.path.join(directory, "." + name + ".partial")):
                if os.path.exists(leftover):
                    os.remove(leftover)
            raise
        self.logger.info(f"Exported {len(batch)} image tag(s) from {client.host} as {path}")
        return FlowFileSourceResult(relationship="success", attributes=attributes)

    def _max_size(self, context):
        value = context.getProperty(self.MAX_SIZE).getValue()
        if not value:
            return 0
        number, unit = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B)\s*", value.upper()).groups()
        return int(float(number) * UNITS[unit])
