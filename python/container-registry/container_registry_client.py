"""Registry client and export tar format shared by PushContainerImages and PullContainerImages.

Standard library only. It speaks the OCI distribution (Docker Registry HTTP API v2) push and pull
endpoints, with Bearer token auth from the realm a 401 names (Quay, Artifactory, Harbor, GitLab,
registry:2 with token auth) or Basic auth (registry:2 with htpasswd), or none for an open registry.

TLS trusts the system store plus SSL_CERT_FILE (OpenSSL reads it), plus an optional extra CA file.

The tar layout is the mirror.py export format:
  export.json                  {"images": [{"dir", "source", "target", "tag", "digest"}]}
  NNNNNN/manifest.json         the image's manifest or index, byte for byte
  NNNNNN/<hex>.manifest.json   each manifest below an index, nested indexes included
  NNNNNN/<hex>                 each config and layer blob; a repeat is a hard link to the first
  NNNNNN/version               Directory Transport Version: 1.1
"""
import base64
import hashlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")
MEDIA_TYPE = re.compile(r"[\w.+-]+/[\w.+-]+")
CHUNK = 8 * 1024 * 1024
MANIFEST_LIMIT = 4 * 1024 * 1024    # the largest manifest the distribution spec asks registries to accept
CONTROL_LIMIT = 16 * 1024 * 1024    # token, listing and upload answers, which are small
UPLOAD_ATTEMPTS = 3  # a blob upload that fails mid-stream starts again from the tar this many times
FOREIGN_LAYERS = {
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
}
MAX_INDEX_DEPTH = 8  # nested indexes a graph may have before it is refused as malformed
INDEX = "export.json"
KEPT = re.compile(r"export\.json|[0-9]{6}/(?:manifest|[0-9a-f]{64}\.manifest)\.json")


class RegistryError(Exception):
    pass


def read_limited(response, limit, what):
    """The whole body, or a RegistryError once it is longer than limit; the response is closed either way."""
    try:
        body = response.read(limit + 1)
    finally:
        response.close()
    if len(body) > limit:
        raise RegistryError(f"{what}: the answer is over {limit} bytes")
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand, so credentials never travel to another host (object storage)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _checking(connection_class, check):
    """connection_class that resolves the host once, connects, and hands the peer address and every address of the
    answer to check(host, peer, answer) before anything is sent. When check returns False the request goes out
    without an Authorization header."""
    class Checked(connection_class):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = self._connect_checked
            self._credentials = True

        def _connect_checked(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
            host, port = address
            answer = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            failure = None
            for family, kind, proto, _name, sockaddr in answer:
                sock = socket.socket(family, kind, proto)
                try:
                    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                        sock.settimeout(timeout)
                    if source_address:
                        sock.bind(source_address)
                    sock.connect(sockaddr)
                except OSError as error:
                    sock.close()
                    failure = error
                    continue
                try:
                    self._credentials = check(host, sock.getpeername()[0], [info[4][0] for info in answer]) is not False
                except Exception:
                    sock.close()
                    raise
                return sock
            raise failure or OSError("getaddrinfo returned no address")

        def putrequest(self, *args, **kwargs):
            if self.sock is None:
                self.connect()  # so putheader knows whether this peer may see credentials
            return super().putrequest(*args, **kwargs)

        def putheader(self, header, *values):
            if self._credentials or header.lower() != "authorization":
                super().putheader(header, *values)
    return Checked


def web_opener(context, check=None):
    """http and https only (no file: or ftp:), and no automatic redirects. With check, every connection is
    vetted by check(host, peer ip) once it is made and before a byte is sent."""
    class Http(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(_checking(http.client.HTTPConnection, check) if check else http.client.HTTPConnection, req)

    class Https(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(_checking(http.client.HTTPSConnection, check) if check else http.client.HTTPSConnection,
                                req, context=self._context)

    opener = urllib.request.OpenerDirector()
    for handler in (Http(), Https(context=context),
                    urllib.request.HTTPErrorProcessor(), urllib.request.HTTPDefaultErrorHandler(), _NoRedirect()):
        opener.add_handler(handler)
    return opener


def next_link(header, base):
    """The URL of the rel=next link in a Link header, whatever order its parameters come in, or None."""
    for target, params in re.findall(r"<([^>]*)>([^<]*)", header or ""):
        for name, value in re.findall(r';\s*([^\s=;,]+)\s*=\s*("[^"]*"|[^\s;,]*)', params):
            if name.lower() == "rel" and "next" in value.strip('"').lower().split():
                return urllib.parse.urljoin(base, target.strip())
    return None


def redact(url):
    """url as scheme://host/path: no user name, password, query (pre-signed signatures) or fragment, for messages."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    except ValueError:
        return "<unreadable URL>"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def rebase(target, rewrite=None, strip=False):
    """target with its first path segment replaced (rewrite) or removed (strip)."""
    if strip and rewrite:
        raise RegistryError("set Rewrite Target Base Path or Strip Target Base Path, not both")
    if strip:
        if "/" not in target:
            raise RegistryError(f"{target} has no base path to strip")
        moved = target.split("/", 1)[1]
    elif rewrite:
        moved = rewrite.strip("/") + "/" + target.split("/", 1)[-1]
    else:
        moved = target
    if not REPOSITORY.fullmatch(moved):
        raise RegistryError(f"{target} becomes {moved}, not a valid repository name")
    return moved


UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}


def parse_size(value):
    """Bytes in a size such as 20 GB; 0 for none."""
    if not value:
        return 0
    match = re.fullmatch(r"([\d.]+)\s*([KMGT]?B)?", value.strip().upper())
    if not match:
        raise RegistryError(f"Max Export Size {value!r} is not a size such as 20 GB")
    return int(float(match.group(1)) * UNITS[match.group(2) or "B"])


def trusted_der(property_value):
    """The DER bytes of every CA in the SSL Context Service property_value names, [] when it is unset.
    py4j hands getEncoded() over as a Java byte[], which bytes() converts."""
    if not property_value.getValue():
        return []
    service = property_value.asControllerService()
    manager = service.createTrustManager()
    if manager is None:
        raise RegistryError("the SSL Context Service has no truststore")
    return [bytes(issuer.getEncoded()) for issuer in manager.getAcceptedIssuers()]


def export_attributes(directory, name, sha, host, artifact_type, images):
    """The FlowFile attributes that name an export tar in directory: what Pull and Export both emit."""
    return {
        "filename": name, "Filename": name, "absolute.path": directory.rstrip("/") + "/",
        "Content-Type": "application/octet-stream", "mime.type": "application/x-tar",
        "X-Sha256": sha, "X-Artifact-Type": artifact_type, "X-Artifact-Format": "tar",
        "registry.host": host, "registry.images": "\n".join(images),
        "file.size": str(os.path.getsize(os.path.join(directory, name))),
    }


class RegistryClient:
    def __init__(self, url, username=None, password=None, ca_der=(), timeout=3600, trusted_hosts=None):
        url = (url or "").rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise RegistryError("The registry URL must start with https:// or http://")
        parts = urllib.parse.urlsplit(url)
        if "@" in parts.netloc:
            raise RegistryError("The registry URL must not carry a user name or password; set the registry username and password properties")
        self.base = url
        self.host = parts.netloc
        self.base_path = parts.path
        self.username = username or None
        self.password = password or ""
        self.timeout = timeout
        context = ssl.create_default_context()  # system store, and SSL_CERT_FILE when set
        for der in ca_der or ():
            context.load_verify_locations(cadata=bytes(der))  # one DER certificate each, added to the system store
        if ca_der:
            # Python 3.13 sets VERIFY_X509_STRICT, which rejects a private CA without the key usage extension
            # ("CA cert does not include key usage extension"), as a quick `openssl req -x509` CA is.
            # Trust still comes only from the system store and these CAs.
            context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.pins = {}  # host name -> the addresses its first connection saw it resolve to
        self.opener = web_opener(context, self._check_pinned)
        self.trusted_names, self.trusted_nets = set(), []
        for entry in re.split(r"[,\s]+", trusted_hosts or ""):
            if not entry:
                continue
            try:
                self.trusted_nets.append(ipaddress.ip_network(entry.strip("[]"), strict=False))
            except ValueError:
                self.trusted_names.add(entry.lower())
        self.storage_opener = web_opener(context, self._check_storage)  # for hosts the registry points at
        self.scheme = parts.scheme
        self.context = context
        self.auth = {}

    # -- transport ---------------------------------------------------------------------------
    def _check_storage(self, host, ip, answer=()):
        """Raise unless host, at ip, may receive what the registry told us to send or fetch: a public address, or
        a Trusted Storage Hosts entry. Loopback, link-local (cloud metadata), private and similar are refused."""
        address = ipaddress.ip_address(ip.split("%")[0])
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if ((host or "").lower().strip("[]") in self.trusted_names
                or any(address in net for net in self.trusted_nets if net.version == address.version)
                or (address.is_global and not address.is_multicast)):
            return
        raise RegistryError(f"{self.host} named {host}, which is {address}, not a public address; add it to "
                            "Trusted Storage Hosts if it is the registry's object storage")

    @staticmethod
    def _address(ip):
        address = ipaddress.ip_address(ip.split("%")[0])
        return str(address.ipv4_mapped if address.version == 6 and address.ipv4_mapped else address)

    def _check_pinned(self, host, ip, answer=()):
        """The first connection to a name pins every address that name resolved to (answer, and the peer ip), the
        set of addresses of the same host. A later connection to that name at a pinned address may carry the
        credentials. At any other address the name was not seen to resolve to, such as one it was re-pointed at,
        the connection goes out without them, and only where _check_storage allows it. Returns True when
        credentials may be sent."""
        peer = self._address(ip)
        seen = self.pins.setdefault((host or "").lower(), {self._address(a) for a in answer} | {peer})
        if peer in seen:
            return True
        self._check_storage(host, ip)
        return False

    def _guard(self, url, what):
        """The scheme rules for a URL the registry supplied; True when it is the registry's own host."""
        target = urllib.parse.urlsplit(url)
        if target.scheme not in ("http", "https"):
            raise RegistryError(f"{what} points at {target.scheme}:, refused")
        if self.scheme == "https" and target.scheme != "https":
            raise RegistryError(f"{what} points at plain http on {target.hostname} from an https registry, refused")
        return target.netloc == self.host and target.scheme == self.scheme

    def _open(self, method, url, headers=None, data=None, foreign=False):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            return (self.storage_opener if foreign else self.opener).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error  # an HTTPError is a response: status, headers and body
        except urllib.error.URLError as error:
            raise RegistryError(f"{method} {redact(url)}: {error.reason}") from error
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise RegistryError(f"{method} {redact(url)}: {type(error).__name__}") from None

    def _send(self, repository, actions, method, path, headers=None, data=None, mount_from=None):
        """One request to <base>/v2/<path>, authorised on a 401 and sent once more."""
        key = (repository, actions, mount_from)
        url = f"{self.base}/v2/{path}"
        response = self._open(method, url, self._with_auth(key, headers), data)
        if response.status != 401:
            return response
        challenge = response.headers.get("WWW-Authenticate", "")
        response.close()
        header = self._authorize(challenge, repository, actions, mount_from)
        self.auth[key] = header
        if mount_from:
            self.auth[(repository, actions, None)] = header  # the upload after a declined mount uses it
        if hasattr(data, "seek"):
            data.seek(0)
        response = self._open(method, url, self._with_auth(key, headers), data)
        if response.status in (401, 403):
            what = "the catalog" if repository is None else f"{actions} on {repository}"
            who = ("an anonymous client; set the registry username and password properties" if self.username is None
                   else f"{self.username}; check the credentials and that the account may {actions} there")
            raise RegistryError(f"{self.host} refused {what} to {who} (HTTP {response.status})")
        return response

    def _with_auth(self, key, headers):
        headers = dict(headers or {})
        header = self.auth.get(key) or (self.auth.get((key[0], "pull,push", None)) if key[1] == "pull" else None)
        if header:
            headers["Authorization"] = header
        return headers

    def _basic(self):
        return "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()

    def _authorize(self, challenge, repository, actions, mount_from):
        if challenge[:5].lower() == "basic":
            if self.username is None:
                raise RegistryError(f"{self.host} asks for a username and password; set the registry username and password properties")
            return self._basic()
        if challenge[:6].lower() != "bearer":
            raise RegistryError(f"{self.host} refused the request with no usable challenge ({challenge.split(' ')[0]})")
        params = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', challenge)}
        realm = params.get("realm")
        if not realm:
            raise RegistryError(f"{self.host} sent a Bearer challenge without a realm: {challenge}")
        realm_parts = urllib.parse.urlsplit(realm)
        if realm_parts.scheme != "https" and not (realm_parts.scheme == "http" and self.scheme == "http"
                                                  and realm_parts.hostname == urllib.parse.urlsplit(self.base).hostname):
            raise RegistryError(f"{self.host} names a token realm {redact(realm)} that is not https; credentials not sent")
        query = []
        if "service" in params:
            query.append(("service", params["service"]))
        query.append(("scope", "registry:catalog:*" if repository is None else f"repository:{repository}:{actions}"))
        if mount_from:
            query.append(("scope", f"repository:{mount_from}:pull"))
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)
        headers = {"Authorization": self._basic()} if self.username else {}
        response = self._open("GET", url, headers)
        service = redact(realm)  # a realm can carry a signed or secret query
        body = read_limited(response, CONTROL_LIMIT, f"token from {service}")
        if response.status != 200:
            raise RegistryError(f"token from {service}: HTTP {response.status}")  # the body may echo the realm query
        answer = json.loads(body)
        token = answer.get("token") or answer.get("access_token")
        if not token:
            raise RegistryError(f"no token in the answer from {service}")
        return "Bearer " + token

    @staticmethod
    def _expect(response, what, *codes):
        if response.status not in codes:
            try:
                body = response.read(300).decode(errors="replace").strip()
            finally:
                response.close()
            raise RegistryError(f"{what}: HTTP {response.status}" + (f": {body}" if body else ""))

    # -- reads -------------------------------------------------------------------------------
    def manifest_digest(self, repository, reference):
        """The digest a tag or digest points at, or None when there is no such manifest."""
        response = self._send(repository, "pull", "HEAD", f"{repository}/manifests/{reference}", {"Accept": MANIFEST_TYPES})
        if response.status == 404:
            return None
        self._expect(response, f"HEAD manifest {repository}:{reference}", 200)
        digest = response.headers.get("Docker-Content-Digest")
        if digest:
            return digest
        return "sha256:" + sha256_hex(self.get_manifest(repository, reference)[0])

    def get_manifest(self, repository, reference):
        response = self._send(repository, "pull", "GET", f"{repository}/manifests/{reference}", {"Accept": MANIFEST_TYPES})
        self._expect(response, f"GET manifest {repository}:{reference}", 200)
        body = read_limited(response, MANIFEST_LIMIT, f"manifest {repository}:{reference}")
        if reference.startswith("sha256:") and "sha256:" + sha256_hex(body) != reference:
            raise RegistryError(f"manifest {repository}@{reference} does not match its digest")
        return body, response.headers.get("Content-Type", "").split(";")[0].strip()

    def blob_exists(self, repository, digest):
        response = self._send(repository, "pull", "HEAD", f"{repository}/blobs/{digest}")
        response.close()
        if response.status == 404:
            return False
        if response.status in (200, 307, 302):  # a redirect to storage means it exists
            return True
        raise RegistryError(f"HEAD blob {digest} in {repository}: HTTP {response.status}")

    def open_blob(self, repository, digest):
        """A blob's bytes. A redirect to storage is followed without the registry's credentials."""
        response = self._send(repository, "pull", "GET", f"{repository}/blobs/{digest}")
        for _ in range(5):
            if response.status == 200:
                return response
            if response.status not in (301, 302, 303, 307, 308):
                self._expect(response, f"GET blob {digest} from {repository}", 200)
            location = urllib.parse.urljoin(response.url if hasattr(response, "url") else self.base,
                                            response.headers.get("Location", ""))
            response.close()
            same = self._guard(location, f"blob {digest}")
            headers = self._with_auth((repository, "pull", None), {}) if same else {}
            response = self._open("GET", location, headers, foreign=not same)
        raise RegistryError(f"too many redirects for blob {digest}")

    def _paged(self, repository, first, field, what):
        out, url, seen = [], f"{self.base}/v2/{first}", set()
        while url:
            if url in seen or len(seen) >= 10000:
                raise RegistryError(f"listing {what} did not end after {len(seen)} pages")
            seen.add(url)
            path = urllib.parse.urlsplit(url)
            if "/v2/" not in path.path:
                raise RegistryError(f"listing {what} was sent a next page that is not under /v2/")
            path = path.path.split("/v2/", 1)[1] + (f"?{path.query}" if path.query else "")  # onto the registry URL base
            response = self._send(repository, "pull", "GET", path)
            if response.status == 404 and repository is not None and not out:
                return None  # no such repository
            self._expect(response, f"list {what}", 200)
            out += json.loads(read_limited(response, CONTROL_LIMIT, f"list {what}")).get(field) or []
            url = next_link(response.headers.get("Link", ""), url)
        return out

    def catalog(self):
        return self._paged(None, "_catalog?n=1000", "repositories", "the catalog")

    def tags(self, repository):
        return self._paged(repository, f"{repository}/tags/list?n=1000", "tags", repository)

    # -- writes ------------------------------------------------------------------------------
    def _upload_location(self, response, repository):
        location = urllib.parse.urljoin(f"{self.base}/v2/{repository}/blobs/uploads/", response.headers.get("Location", ""))
        if not location:
            raise RegistryError("upload started without a Location header")
        parts = urllib.parse.urlsplit(location)
        # Artifactory's /artifactory/api/docker/<repo-key> base answers with a Location that drops that path.
        if self.base_path and parts.netloc == self.host and parts.path.startswith("/v2/") and "/blobs/uploads/" in parts.path:
            upload_id = parts.path.split("/blobs/uploads/", 1)[1]
            location = f"{self.base}/v2/{repository}/blobs/uploads/{upload_id}" + (f"?{parts.query}" if parts.query else "")
        return location

    def start_upload(self, repository, digest=None, mount_from=None):
        """The upload URL, or None when the registry mounted the blob from mount_from."""
        query = f"?mount={urllib.parse.quote(digest)}&from={urllib.parse.quote(mount_from)}" if mount_from else ""
        response = self._send(repository, "pull,push", "POST", f"{repository}/blobs/uploads/{query}",
                              {"Content-Length": "0"}, b"", mount_from)
        if mount_from and response.status == 201:
            read_limited(response, CONTROL_LIMIT, f"mount into {repository}")
            return None
        self._expect(response, f"start upload to {repository}", 202)
        read_limited(response, CONTROL_LIMIT, f"start upload to {repository}")
        return self._upload_location(response, repository)

    def upload_blob(self, repository, digest, stream, size, upload=None):
        """One PUT of a blob read from stream (size bytes)."""
        upload = upload or self.start_upload(repository)
        url = upload + ("&" if "?" in upload else "?") + "digest=" + urllib.parse.quote(digest)
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
        same = self._guard(upload, f"the upload location for {digest}")
        if same:
            headers = self._with_auth((repository, "pull,push", None), headers)
        response = self._open("PUT", url, headers, stream if size else b"", foreign=not same)
        if response.status == 401:
            self.auth.pop((repository, "pull,push", None), None)  # expired mid-upload: the next attempt authorizes again
        self._expect(response, f"upload blob {digest} to {repository}", 201, 204)
        read_limited(response, CONTROL_LIMIT, f"upload blob {digest} to {repository}")

    def put_manifest(self, repository, reference, manifest, media_type):
        response = self._send(repository, "pull,push", "PUT", f"{repository}/manifests/{reference}",
                              {"Content-Type": media_type}, manifest)
        self._expect(response, f"push manifest {repository}:{reference}", 201, 200)
        read_limited(response, CONTROL_LIMIT, f"push manifest {repository}:{reference}")


class _Hashing(io.RawIOBase):
    """Reads from a stream, hashing as it goes, so a blob is checked while it is sent."""
    def __init__(self, stream):
        self.stream, self.digest, self.count = stream, hashlib.sha256(), 0

    def readable(self):
        return True

    def read(self, n=-1):
        if n == 0:
            return b""
        data = self.stream.read(n if n and n > 0 else CHUNK)
        self.digest.update(data)
        self.count += len(data)
        return data


# -- the export tar ------------------------------------------------------------------------------
def _manifest_type(raw, fallback=None):
    data = json.loads(raw)
    media_type = data.get("mediaType") or fallback
    if not media_type or not MEDIA_TYPE.fullmatch(media_type):
        media_type = ("application/vnd.oci.image.index.v1+json" if "manifests" in data
                      else "application/vnd.oci.image.manifest.v1+json")
    return media_type


def _blobs_of(manifest):
    """config and layer digests with sizes. Only a foreign layer, which Docker and OCI mark by media type and
    serve from its urls, is not copied; urls on any other layer are optional alternative locations."""
    out = {}
    for descriptor in [manifest.get("config")] + list(manifest.get("layers") or []):
        if not descriptor:
            continue
        digest = descriptor.get("digest", "")
        if not DIGEST.fullmatch(digest) or "size" not in descriptor:
            raise RegistryError(f"unsupported descriptor {descriptor}")
        if not (descriptor.get("urls") and descriptor.get("mediaType") in FOREIGN_LAYERS):
            out[digest] = int(descriptor["size"])
    return out


def export_summary(path):
    """(sha256 hex of the tar, ["target:tag@digest", ...]) for a tar write_export finished."""
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    with tarfile.open(path, "r:") as tar:
        member = next((m for m in tar if (m.name[2:] if m.name.startswith("./") else m.name) == INDEX), None)
        if member is None or not member.isfile() or member.size > 64 * 1024 * 1024:
            raise RegistryError(f"{path} has no usable {INDEX}")
        images = json.loads(tar.extractfile(member).read())["images"]
    return digest.hexdigest(), [f"{i['target']}:{i['tag']}@{i['digest']}" for i in images]


def read_export(path):
    """The images in an export tar: [(entry, root bytes, [(child digest, bytes)], {blob: member name})]."""
    with tarfile.open(path, "r:") as tar:
        members = {m.name[2:] if m.name.startswith("./") else m.name: m for m in tar.getmembers()}
        if sum(m.size for n, m in members.items() if KEPT.fullmatch(n)) > 256 * 1024 * 1024:
            raise RegistryError("the manifests are too large to be an export")

        def read(name):
            member = members.get(name)
            if member is None or not member.isfile():
                raise RegistryError(f"missing {name}")
            return tar.extractfile(member).read()

        index = json.loads(read(INDEX))
        if not isinstance(index, dict) or not isinstance(index.get("images"), list):
            raise RegistryError(f"invalid {INDEX}")
        images = []
        for image in index["images"]:
            entry = {k: str(image.get(k, "")) for k in ("dir", "target", "tag", "digest")}
            if (not re.fullmatch(r"[0-9]{6}", entry["dir"]) or not REPOSITORY.fullmatch(entry["target"])
                    or not TAG.fullmatch(entry["tag"]) or not DIGEST.fullmatch(entry["digest"])):
                raise RegistryError(f"invalid entry in {INDEX}: {image!r}")
            root = read(f"{entry['dir']}/manifest.json")
            if "sha256:" + sha256_hex(root) != entry["digest"]:
                raise RegistryError(f"{entry['target']}:{entry['tag']} does not match its recorded digest")
            root_json = json.loads(root)
            children, blobs = [], {}
            manifests = []

            def descend(data, depth):
                """Image manifests are collected; every manifest below an index is a child, deepest first."""
                if "manifests" not in data:
                    manifests.append(data)
                    return
                if depth >= MAX_INDEX_DEPTH:
                    raise RegistryError(f"{entry['target']}:{entry['tag']}: indexes nested deeper than {MAX_INDEX_DEPTH}")
                for child in data["manifests"]:
                    digest = child.get("digest", "")
                    if not DIGEST.fullmatch(digest):
                        raise RegistryError(f"{entry['target']}:{entry['tag']}: unsupported child digest {digest}")
                    name = f"{entry['dir']}/{digest[7:]}.manifest.json"
                    if name not in members or any(d == digest for d, *_ in children):
                        continue  # a platform the export left out, or one already read
                    raw = read(name)
                    if "sha256:" + sha256_hex(raw) != digest:
                        raise RegistryError(f"{name} does not match its digest in the index")
                    parsed = json.loads(raw)
                    descend(parsed, depth + 1)
                    children.append((digest, raw, _manifest_type(raw, child.get("mediaType"))))

            descend(root_json, 0)
            for manifest in manifests:
                for digest in _blobs_of(manifest):
                    name = f"{entry['dir']}/{digest[7:]}"
                    if name in members:
                        member = members[name]
                        if member.islnk():  # a repeat blob: upload from the entry that holds the bytes
                            member = members.get(member.linkname[2:] if member.linkname.startswith("./") else member.linkname)
                            if member is None or not member.isfile():
                                raise RegistryError(f"{name} links to a missing entry")
                        blobs[digest] = member.name  # as stored, ./ prefix and all
            images.append((entry, root, _manifest_type(root), children, blobs))
        return images


def tag_is_current(client, repository, entry, move_tags, log):
    """True when the tag already points at the entry's digest. A tag at another digest moves, as docker push
    moves it, unless move_tags is false: then it raises before anything is pushed."""
    current = client.manifest_digest(repository, entry["tag"])
    if current == entry["digest"]:
        return True
    reference = f"{client.host}/{repository}:{entry['tag']}"
    if current and not move_tags:
        raise RegistryError(f"{reference} is {current}, the tar has {entry['digest']}; "
                            "a tag does not move unless Move Existing Tags is true")
    if current and log:  # the old image stays reachable by its digest
        log(f"moving tag {reference} from {current} to {entry['digest']}")
    return False


def push_export(client, path, rewrite=None, strip=False, move_tags=True, log=None):
    """Push every image of an export tar. Every check runs before the first upload, so a refusal pushes
    nothing. Returns (pushed, present, lines)."""
    images = read_export(path)
    plan, present, lines, final = [], 0, [], {}
    for entry, root, media_type, children, blobs in images:
        repository = rebase(entry["target"], rewrite, strip)
        lines.append(f"{repository}:{entry['tag']}@{entry['digest']}")
        earlier = final.setdefault((repository, entry["tag"]), entry["digest"])
        if earlier != entry["digest"]:
            raise RegistryError(f"{repository}:{entry['tag']} is {earlier} and {entry['digest']} in the tar; "
                                "two images cannot end at one tag, so nothing is pushed")
        if any(p[0] == repository and p[1]["tag"] == entry["tag"] for p in plan):
            continue  # the same image listed again
        if tag_is_current(client, repository, entry, move_tags, log):
            present += 1
        else:
            plan.append((repository, entry, root, media_type, children, blobs))
    uploaded = {}  # digest -> first repository that has it, for mounts
    with tarfile.open(path, "r:") as tar:
        for repository, entry, root, media_type, children, blobs in plan:
            for digest, name in blobs.items():
                if client.blob_exists(repository, digest):
                    uploaded.setdefault(digest, repository)
                    continue
                upload = None
                if digest in uploaded and uploaded[digest] != repository:
                    upload = client.start_upload(repository, digest, uploaded[digest])
                    if upload is None:
                        continue  # mounted
                member = tar.getmember(name)
                for attempt in range(1, UPLOAD_ATTEMPTS + 1):
                    stream = _Hashing(tar.extractfile(member))
                    try:
                        client.upload_blob(repository, digest, stream, member.size, upload)
                        break
                    except (RegistryError, OSError) as error:
                        if "sha256:" + stream.digest.hexdigest() != digest and stream.count == member.size:
                            raise RegistryError(f"blob {digest} in the tar does not match its digest") from error
                        if attempt == UPLOAD_ATTEMPTS:
                            raise
                        if log:
                            log(f"Upload of {digest} to {repository} failed ({error}); attempt {attempt + 1} of {UPLOAD_ATTEMPTS}")
                        time.sleep(5 * attempt)
                        upload = None  # a fresh upload session
                if "sha256:" + stream.digest.hexdigest() != digest or stream.count != member.size:
                    raise RegistryError(f"blob {digest} in the tar does not match its digest")
                uploaded.setdefault(digest, repository)
            for digest, raw, child_type in children:
                client.put_manifest(repository, digest, raw, child_type)
            client.put_manifest(repository, entry["tag"], root, media_type)
            after = client.manifest_digest(repository, entry["tag"])
            if after != entry["digest"]:
                raise RegistryError(f"digest mismatch after the push: {repository}:{entry['tag']} is {after}")
            if log:
                log(f"Pushed {client.host}/{repository}:{entry['tag']}@{entry['digest']}")
    return len(plan), present, lines


def plan_image(client, repository, digest):
    """(root bytes, {descendant manifest digest: bytes}, {blob digest: size}) with every blob confirmed present.
    Manifests below an index come deepest first, so each can be pushed before the index that names it."""
    root, _ = client.get_manifest(repository, digest)
    children, blobs = {}, {}

    def descend(data, depth):
        if "manifests" not in data:
            blobs.update(_blobs_of(data))
            return
        if depth >= MAX_INDEX_DEPTH:
            raise RegistryError(f"{repository}@{digest}: indexes nested deeper than {MAX_INDEX_DEPTH}")
        for child in data["manifests"]:
            child_digest = child.get("digest", "")
            if not DIGEST.fullmatch(child_digest):
                raise RegistryError(f"unsupported child digest {child_digest}")
            if child_digest in children:
                continue
            raw, _ = client.get_manifest(repository, child_digest)
            descend(json.loads(raw), depth + 1)
            children[child_digest] = raw

    descend(json.loads(root), 0)
    for blob in blobs:
        if not client.blob_exists(repository, blob):
            raise RegistryError(f"blob {blob} is missing")
    return root, children, blobs


def host_of(registry):
    return re.sub(r":443$", "", (registry or "").lower())


def allowed(repository, entries):
    """True when repository equals an allowlist entry, or starts with one that ends in *."""
    return any(repository.startswith(e[:-1]) if e.endswith("*") else repository == e for e in entries)


def webhook_images(body, sha256, host, entries):
    """[(repository, tag, digest)] a gitlab-registry-mirror webhook body names, once it has passed every check that
    needs no registry: the X-Sha256 attribute, the registry host, Allowed Repositories entries and name syntax."""
    if sha256 != hashlib.sha256(body).hexdigest():
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
    if host_of(registry) != host_of(host):
        raise RegistryError(f"the body names registry {registry}, not {host}")
    for repository, tag, digest in names:
        if not allowed(repository, entries):
            raise RegistryError(f"{repository} is not in Allowed Repositories")
        if not (REPOSITORY.fullmatch(repository) and TAG.fullmatch(tag) and DIGEST.fullmatch(digest)):
            raise RegistryError(f"not a valid repository, tag and digest: {repository}:{tag}@{digest}")
    return list(dict.fromkeys(names))


def bundle_bytes(blob_maps):
    """Blob bytes of one tar: each distinct blob digest counted once, however many images share it."""
    return sum({digest: size for blobs in blob_maps for digest, size in blobs.items()}.values())


def write_export(client, path, images):
    """Write images [(source repository, tag, digest, target, root, children, blobs)] as an export tar at path
    (through path.partial, renamed once complete). Returns its sha256."""
    partial = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".partial")  # hidden from ListFile
    written, entries = {}, []
    with open(partial, "wb") as raw:
        hashing = _HashingWriter(raw)
        with tarfile.open(fileobj=hashing, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for n, (repository, tag, digest, target, root, children, blobs) in enumerate(images):
                folder = f"./{n:06d}"
                directory = tarfile.TarInfo(folder + "/")
                directory.type, directory.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(directory)
                _add_bytes(tar, f"{folder}/version", b"Directory Transport Version: 1.1\n")
                _add_bytes(tar, f"{folder}/manifest.json", root)
                for child_digest, child in children.items():
                    _add_bytes(tar, f"{folder}/{child_digest[7:]}.manifest.json", child)
                for blob, size in blobs.items():
                    name = f"{folder}/{blob[7:]}"
                    if blob in written:
                        link = tarfile.TarInfo(name)
                        link.type, link.linkname = tarfile.LNKTYPE, written[blob]
                        tar.addfile(link)
                        continue
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = size, 0o644
                    response = client.open_blob(repository, blob)
                    stream = _Hashing(response)
                    try:
                        tar.addfile(info, stream)
                    finally:
                        response.close()
                    if stream.count != size or "sha256:" + stream.digest.hexdigest() != blob:
                        raise RegistryError(f"blob {blob} from {repository} did not match its size or digest")
                    written[blob] = name
                entries.append({"dir": f"{n:06d}", "source": f"{client.host}/{repository}:{tag}",
                                "target": target, "tag": tag, "digest": digest})
            index = json.dumps({"created": datetime.now(timezone.utc).isoformat(), "images": entries}, indent=2).encode()
            _add_bytes(tar, f"./{INDEX}", index)
    os.replace(partial, path)
    return hashing.digest.hexdigest()


def _add_bytes(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), 0o644
    tar.addfile(info, io.BytesIO(data))


class _HashingWriter:
    def __init__(self, raw):
        self.raw, self.digest = raw, hashlib.sha256()

    def write(self, data):
        self.digest.update(data)
        return self.raw.write(data)

    def flush(self):
        self.raw.flush()

    def close(self):
        pass
