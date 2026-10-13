import base64
import json
import os
import re
import socket
import tarfile
import time
import urllib.request
import zlib
from datetime import datetime, timezone

from nifiapi.componentstate import Scope
from nifiapi.documentation import use_case
from nifiapi.flowfilesource import FlowFileSource, FlowFileSourceResult
from nifiapi.properties import ExpressionLanguageScope, PropertyDependency, PropertyDescriptor, StandardValidators
from nifiapi.relationship import Relationship

from nifi_attributes import Attributes, evaluate
from container_registry_client import (DIGEST, REPOSITORY, TAG, RegistryClient, RegistryError, export_attributes,
                                       export_summary, parse_size, plan_image, read_limited, rebase, trusted_der,
                                       web_opener, write_export)

LISTING, LEDGER, IMAGE_LIST = "Registry Listing", "Ledger", "Image List"
STATE_KEY = "exported"   # zlib + base64 JSON {"repository:tag": first 16 hex of the digest}, in values exported.0, exported.1, ...
PENDING_KEY = "pending"  # the same encoding of [{"at", "where", "name", "images": [[repository, tag, short digest]]}]
STATE_LIMIT = 900_000    # ZooKeeper caps a component's cluster state near 1 MB
LEDGER_LIMIT = 64 * 1024 * 1024  # the biggest ledger.json read
STATE_VALUE = 60_000     # and each value it stores through writeUTF at 65535 bytes
LEFTOVER_AFTER = 3600    # seconds a finished tar may sit in Output Directory before a run emits it again



def _read(state, key):
    raw = state.get(key)
    if raw is None:
        pieces, number = [], 0
        while state.get(f"{key}.{number}") is not None:
            pieces.append(state.get(f"{key}.{number}"))
            number += 1
        raw = "".join(pieces)
    return json.loads(zlib.decompress(base64.b64decode(raw))) if raw else None


def load_state(state):
    """The exported map from a StateMap (or any object with get(key)), or None when there is none. NiFi's
    StateMap has no mapping protocol, only get, toMap and getStateVersion. The single value "exported" of
    version 1.4.0 loads too."""
    return _read(state, STATE_KEY)


def load_pending(state):
    """The exports whose FlowFile NiFi may not have committed: [{"at", "where", "name", "images"}, ...]."""
    return _read(state, PENDING_KEY) or []


def _pack(value):
    return base64.b64encode(zlib.compress(json.dumps(value, separators=(",", ":"), sort_keys=True).encode(), 9)).decode()


def _shards(key, raw):
    return {f"{key}.{n}": raw[start:start + STATE_VALUE] for n, start in enumerate(range(0, max(len(raw), 1), STATE_VALUE))}


def dump_state(exported, pending=()):
    """The state values for the exported map and the pending exports: each encoding cut into pieces that fit one
    writeUTF each."""
    raw, kept = _pack(exported), _pack(list(pending)) if pending else ""
    if len(raw) + len(kept) > STATE_LIMIT:
        raise RegistryError(f"state for {len(exported)} image tags is {len(raw) + len(kept)} bytes, over the {STATE_LIMIT} "
                            "byte limit of cluster state; set Max Export Size, narrow Repositories or Tag Filter, or split across processors")
    values = _shards(STATE_KEY, raw)
    if kept:
        values.update(_shards(PENDING_KEY, kept))
    return values


def short(digest):
    return digest[7:23]


@use_case(description="Export new and changed images from a low-side registry for a data diode.",
          configuration="""Set Registry URL, Repositories (team/* or *) and Output Directory. Run on the primary node
          every 15 minutes. Send success to FetchFile with Completion Strategy Delete File, then to the diode.""",
          keywords=["container", "diode", "registry", "export"])
class PullContainerImages(FlowFileSource):
    class Java:
        implements = ["org.apache.nifi.python.processor.FlowFileSource"]

    class ProcessorDetails:
        version = "2.0.0"
        description = ("Exports container images from an OCI registry as a mirror.py export tar written to Output "
                       "Directory, and emits a FlowFile that names it, for FetchFile to read. Each run exports what "
                       "changed since the last export, recorded in cluster state. Schedule it on the primary node only.")
        tags = ["container", "image", "oci", "docker", "registry", "export", "diode"]

    REGISTRY_URL = PropertyDescriptor(name="Registry URL", description="The registry to export from: https://registry.example.internal, or "
                         "https://<host>/artifactory/api/docker/<repo-key> for Artifactory.", required=True,
                         validators=[StandardValidators.URL_VALIDATOR],
                         expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    USERNAME = PropertyDescriptor(name="Username", description="Registry user. Empty: anonymous.",
                     expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PASSWORD = PropertyDescriptor(name="Password", description="Registry password or token.", sensitive=True)
    SSL_CONTEXT = PropertyDescriptor(name="SSL Context Service", description="The truststore whose CAs are trusted in addition to the system "
                    "store and SSL_CERT_FILE, for the registry and the Ledger URL.",
                    controller_service_definition="org.apache.nifi.ssl.SSLContextProvider")
    TRUSTED = PropertyDescriptor(name="Trusted Storage Hosts", description="Comma separated host names, addresses or CIDR ranges "
                    "(10.0.0.0/8) that the registry may redirect blob downloads and uploads to although they are not public "
                    "addresses, such as an on-premises MinIO. Anything else the registry names that is loopback, link-local, "
                    "private or plain http from an https registry is refused. The registry's own host is always allowed at the addresses its name resolves to.")
    IMAGE_SOURCE = PropertyDescriptor(name="Image Source", description="What a run exports. Registry Listing: every tag of Repositories whose "
                         "digest differs from the last export. Ledger: what the mirror pipeline's ledger says it "
                         "synced. Image List: the images in Image List.", required=True,
                         allowable_values=["Registry Listing", "Ledger", "Image List"], default_value="Registry Listing")
    REPOSITORIES = PropertyDescriptor(name="Repositories", description="For Registry Listing: repositories, separated by commas or new lines. "
                         "team/* matches every catalog repository with that prefix, * matches all.",
                         expression_language_scope=ExpressionLanguageScope.ENVIRONMENT,
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Registry Listing")])
    TAG_FILTER = PropertyDescriptor(name="Tag Filter", description="For Registry Listing: a regular expression a tag must match in full. The "
                       "default skips sha256-... signature and attestation tags.", required=True,
                       default_value="(?!sha256-).+", validators=[StandardValidators.REGULAR_EXPRESSION_VALIDATOR],
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Registry Listing")])
    LEDGER_URL = PropertyDescriptor(name="Ledger URL", description="For Ledger: the ledger.json URL, read only.",
                       expression_language_scope=ExpressionLanguageScope.ENVIRONMENT,
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Ledger")])
    LEDGER_TOKEN = PropertyDescriptor(name="Ledger Token", description="Token for the Ledger URL.", sensitive=True,
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Ledger")])
    LEDGER_TOKEN_HEADER = PropertyDescriptor(name="Ledger Token Header", description="Header that carries the Ledger Token: PRIVATE-TOKEN for "
                                "GitLab, Authorization for a Bearer value.", default_value="PRIVATE-TOKEN",
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Ledger")])
    IMAGE_LIST = PropertyDescriptor(name="Image List", description="For Image List: one image per line, repository:tag or "
                       "repository@sha256:..., optionally prefixed with the registry host. # starts a comment.",
                       expression_language_scope=ExpressionLanguageScope.ENVIRONMENT,
                   dependencies=[PropertyDependency(IMAGE_SOURCE, "Image List")])
    INITIAL_RUN = PropertyDescriptor(name="Initial Run", description="What a run does when the state is empty. Export All exports every image. "
                        "Record Only records the current digests and exports nothing.", required=True,
                        allowable_values=["Export All", "Record Only"], default_value="Export All")
    REWRITE = PropertyDescriptor(name="Rewrite Base Path", description="Replace the first path segment of each target repository with this.",
                    expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    STRIP = PropertyDescriptor(name="Strip Base Path", description="Remove the first path segment of each target repository.", required=True,
                  allowable_values=["true", "false"], default_value="false")
    OUTPUT_DIR = PropertyDescriptor(name="Output Directory", description="Where the tar is written. It must hold the largest export.",
                       required=True, expression_language_scope=ExpressionLanguageScope.ENVIRONMENT)
    PREFIX = PropertyDescriptor(name="Filename Prefix", description="Start of each tar's name.", required=True, default_value="container-images-")
    ARTIFACT_TYPE = PropertyDescriptor(name="Artifact Type", description="The X-Artifact-Type attribute.", required=True,
                          default_value="container-images")
    MAX_SIZE = PropertyDescriptor(name="Max Export Size", description="The most blob bytes one tar holds, such as 20 GB. The rest go in the next "
                     "run. An image larger than this goes alone. Empty: no limit.",
                     validators=[StandardValidators.DATA_SIZE_VALIDATOR])
    TIMEOUT = PropertyDescriptor(name="Request Timeout", description="Seconds a registry read may stall.", required=True, default_value="600",
                    validators=[StandardValidators.POSITIVE_INTEGER_VALIDATOR])

    PROPERTIES = [REGISTRY_URL, USERNAME, PASSWORD, SSL_CONTEXT, TRUSTED, IMAGE_SOURCE, REPOSITORIES, TAG_FILTER, LEDGER_URL,
                  LEDGER_TOKEN, LEDGER_TOKEN_HEADER, IMAGE_LIST, INITIAL_RUN, REWRITE, STRIP, OUTPUT_DIR, PREFIX,
                  ARTIFACT_TYPE, MAX_SIZE, TIMEOUT]

    def __init__(self, **kwargs):
        super().__init__()

    def getRelationships(self):
        return [Relationship("success", "A FlowFile naming each export tar written to Output Directory")]

    def getPropertyDescriptors(self):
        return self.PROPERTIES

    def getDynamicPropertyDescriptor(self, name):
        return PropertyDescriptor(name=name, description="Added to each FlowFile as an attribute.", dynamic=True,
                                  expression_language_scope=ExpressionLanguageScope.FLOWFILE_ATTRIBUTES)

    def _value(self, context, descriptor):
        value = evaluate(context.getProperty(descriptor), Attributes({}))
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
            ledger = json.loads(read_limited(response, LEDGER_LIMIT, "the ledger"))
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
                                context.getProperty(self.PASSWORD).getValue(), trusted_der(context.getProperty(self.SSL_CONTEXT)),
                                int(context.getProperty(self.TIMEOUT).getValue()),
                                self._value(context, self.TRUSTED))
        directory = self._value(context, self.OUTPUT_DIR)
        leftover = self._leftover(context, client, directory)
        if leftover:
            return leftover
        source = context.getProperty(self.IMAGE_SOURCE).getValue()
        found = {LISTING: self._from_listing, LEDGER: self._from_ledger, IMAGE_LIST: self._from_list}[source]
        state_manager = context.getStateManager()
        snapshot = state_manager.getState(Scope.CLUSTER)
        exported = load_state(snapshot)
        initial = exported is None
        exported = exported or {}
        pending = load_pending(snapshot)
        where = f"{socket.gethostname()}:{os.path.abspath(directory)}"
        pending, lost = self._settle(pending, where, directory)
        if pending != load_pending(snapshot):
            state_manager.setState(dump_state(exported, pending), Scope.CLUSTER)  # a fetched export no longer waits
        for record in sorted(lost, key=lambda r: r["at"]):
            regenerated, pending = self._regenerate(context, client, directory, record, exported, pending, where)
            if regenerated:
                return regenerated

        current = {f"{r}:{t}": (r, t, d) for r, t, d in found(context, client)}
        # Remember only what the source still lists, so the state does not grow with every tag ever seen.
        state = {k: v for k, v in exported.items() if k in current}
        wanted = [v for k, v in current.items() if v[2] and exported.get(k) != short(v[2])]

        def save():
            state_manager.setState(dump_state(state, pending), Scope.CLUSTER)

        if initial and context.getProperty(self.INITIAL_RUN).getValue() == "Record Only":
            state.update({f"{r}:{t}": short(d) for r, t, d in wanted})
            save()
            self.logger.info(f"Initial run: recorded {len(wanted)} image tag(s) without exporting them")
            return None
        if not wanted:
            if initial or state != exported:
                save()
            return None

        limit = self._max_size(context)
        rewrite = self._value(context, self.REWRITE)
        strip = context.getProperty(self.STRIP).getValue() == "true"
        rebase("check/x", rewrite, strip)  # fails the run when both are set
        recorded = {}  # (target repository, tag) of everything already exported -> (its state key, its short digest)
        for key, recorded_digest in state.items():
            try:
                recorded[(rebase(key.rpartition(":")[0], rewrite, strip), key.rpartition(":")[2])] = (key, recorded_digest)
            except RegistryError:
                continue
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
            held = recorded.get((target, tag))
            if held and held[0] != f"{repository}:{tag}" and held[1] != short(digest):
                # Push would refuse the whole tar, and Pull would have recorded every image in it as exported.
                self.logger.warn(f"Skipping {repository}:{tag}: {held[0]} was already exported to {target}:{tag} "
                                 "at another digest")
                continue
            if any(b[3] == target and b[1] == tag and b[2] != digest for b in batch):
                self.logger.warn(f"Skipping {repository}:{tag}: another image in this export already ends at {target}:{tag}")
                continue
            root, children, blobs = planned[key]
            size = sum(blobs.values())
            if limit and batch and total + size > limit:
                continue  # the next run takes it, since the state does not record it
            batch.append((repository, tag, digest, target, root, children, blobs))
            total += size
        if not batch:
            save()
            return None

        state.update({f"{r}:{t}": short(d) for r, t, d, *_ in batch})
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = context.getProperty(self.PREFIX).getValue() + stamp + ".tar"
        pending = pending + [{"at": time.time(), "where": where, "name": name,
                              "images": [[r, t, short(d)] for r, t, d, *_ in batch]}]
        dump_state(state, pending)  # a state that cannot be stored fails here, before a tar is written
        return self._emit(context, client, directory, name, batch, save)

    def _emit(self, context, client, directory, name, batch, save):
        """Write the tar for batch and return its FlowFile, saving the state last."""
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        try:
            sha = write_export(client, path, batch)
            attributes = self._attributes(context, client, directory, name, sha,
                                          [f"{t}:{tag}@{d}" for _r, tag, d, t, *_ in batch])
            # Last, so a failure above leaves the state as it was and the next run exports these again. Once saved,
            # a lost FlowFile is recovered from the tar by _leftover, or from the pending record by _regenerate on
            # another node, since NiFi 2.11 offers no hook after its commit.
            save()
        except Exception:
            for stale in (path, os.path.join(directory, "." + name + ".partial")):
                if os.path.exists(stale):
                    os.remove(stale)
            raise
        self.logger.info(f"Exported {len(batch)} image tag(s) from {client.host} as {path}")
        return FlowFileSourceResult(relationship="success", attributes=attributes)

    def _settle(self, pending, where, directory):
        """(pending without the exports that were fetched, those of them that another node wrote and lost).

        FetchFile with Completion Strategy Delete File removes a tar once it is fetched, so on the node that wrote
        it a missing tar was fetched. An export of another node whose tar is not in Output Directory either, and is
        older than LEFTOVER_AFTER, was lost with that node: nothing here tells it apart from one fetched since
        (the cost is a repeated export, which Push takes as already present).
        """
        live, lost = [], []
        for record in pending:
            if os.path.exists(os.path.join(directory, record["name"])):
                live.append(record)  # _leftover emits it again when it has sat too long
            elif record["where"] != where:
                live.append(record)
                if record["at"] < time.time() - LEFTOVER_AFTER:
                    lost.append(record)
        return live, lost

    def _regenerate(self, context, client, directory, record, exported, pending, where):
        """(FlowFile or None, pending): the images of a lost record exported again, for the node that is now primary.
        An image that cannot be planned now loses its entry in exported (changed in place), so the next run exports it."""
        remaining = [r for r in pending if r is not record]
        rewrite = self._value(context, self.REWRITE)
        strip = context.getProperty(self.STRIP).getValue() == "true"
        batch, planned = [], {}
        for repository, tag, recorded in record["images"]:
            if exported.get(f"{repository}:{tag}") != recorded:
                continue  # a later export moved this tag on
            try:
                digest = client.manifest_digest(repository, tag)
                if not digest or short(digest) != recorded:
                    continue  # gone from the registry, or moved on since: the regular run takes the tag as it is
                key = f"{repository}@{digest}"
                if key not in planned:
                    planned[key] = plan_image(client, repository, digest)
                target = rebase(repository, rewrite, strip)
            except RegistryError as error:
                # Garbage-collected, a transient failure, or a rewrite that no longer applies: forget the export so
                # the next regular run takes the tag at whatever digest it has by then, as for any image that waits.
                self.logger.warn(f"Not exporting {repository}:{tag}@{digest} again, it waits for the next run: {error}")
                del exported[f"{repository}:{tag}"]
                continue
            root, children, blobs = planned[key]
            batch.append((repository, tag, digest, target, root, children, blobs))
        state_manager = context.getStateManager()
        if not batch:
            state_manager.setState(dump_state(exported, remaining), Scope.CLUSTER)
            return None, remaining
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        name = context.getProperty(self.PREFIX).getValue() + stamp + ".tar"
        remaining = remaining + [{"at": time.time(), "where": where, "name": name,
                                  "images": [[r, t, short(d)] for r, t, d, *_ in batch]}]
        dump_state(exported, remaining)
        self.logger.warn(f"{record['name']} written on {record['where']} was never fetched; exporting its "
                         f"{len(batch)} image tag(s) again")
        return self._emit(context, client, directory, name, batch,
                          lambda: state_manager.setState(dump_state(exported, remaining), Scope.CLUSTER)), remaining

    def _attributes(self, context, client, directory, name, sha, images):
        attributes = export_attributes(directory, name, sha, client.host,
                                       context.getProperty(self.ARTIFACT_TYPE).getValue(), images)
        for descriptor in context.getProperties():
            if descriptor.dynamic:
                value = context.getProperty(descriptor).evaluateAttributeExpressions(Attributes(attributes))
                attributes[descriptor.name] = value.getValue() or ""
        return attributes

    def _leftover(self, context, client, directory):
        """A FlowFile for the oldest finished tar in Output Directory that has sat there past LEFTOVER_AFTER.

        NiFi creates and commits the FlowFile only after create() has returned, and the state is already written
        by then, so a crash or a failed commit in between leaves a tar that no FlowFile names. FetchFile with
        Completion Strategy Delete File removes a tar once it is fetched, so one still here was never fetched.
        """
        prefix = context.getProperty(self.PREFIX).getValue()
        if not os.path.isdir(directory):
            return None
        old = time.time() - LEFTOVER_AFTER
        names = []
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if name.startswith(prefix) and name.endswith(".tar") and os.path.isfile(path) and os.path.getmtime(path) < old:
                names.append((os.path.getmtime(path), name))
        for _mtime, name in sorted(names):
            try:
                sha, images = export_summary(os.path.join(directory, name))
            except (RegistryError, OSError, ValueError, KeyError, tarfile.TarError) as error:
                self.logger.warn(f"Not emitting {name} again: {error}")
                continue
            self.logger.warn(f"{name} is still in {directory} from an earlier run; emitting it again")
            return FlowFileSourceResult(relationship="success",
                                        attributes=self._attributes(context, client, directory, name, sha, images))
        return None

    def _max_size(self, context):
        return parse_size(context.getProperty(self.MAX_SIZE).getValue())
