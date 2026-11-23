"""Registry client shared by PlanRegistryTransfer and RouteRegistryTransfer.

Standard library only. It speaks the OCI distribution (Docker Registry HTTP API v2) endpoints the two
processors need, with Bearer token auth from the realm a 401 names (Quay, Artifactory, Harbor, GitLab,
registry:2 with token auth) or Basic auth (registry:2 with htpasswd), or none for an open registry.

TLS trusts the system store plus SSL_CERT_FILE (OpenSSL reads it), plus an optional extra CA file.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

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
MANIFEST_LIMIT = 4 * 1024 * 1024    # the largest manifest the distribution spec asks registries to accept
CONTROL_LIMIT = 16 * 1024 * 1024    # token, listing and upload answers, which are small
UPLOADS = "/blobs/uploads/"
FOREIGN_LAYERS = {
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
}


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


def redact(url):
    """url as scheme://host/path: no user name, password, query (pre-signed signatures) or fragment, for messages."""
    try:
        parts = urllib.parse.urlsplit(str(url))
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))
    except ValueError:
        return "<unreadable URL>"


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


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
        self.scheme = parts.scheme
        self.context = context
        self.auth = {}

    # -- transport ---------------------------------------------------------------------------
    def _check_storage(self, host, ip):
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

    def _open(self, method, url, headers=None, data=None):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            return self.opener.open(request, timeout=self.timeout)
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

    # -- writes ------------------------------------------------------------------------------
    def _upload_location(self, response, repository):
        location = urllib.parse.urljoin(f"{self.base}/v2/{repository}{UPLOADS}", response.headers.get("Location", ""))
        if not location:
            raise RegistryError("upload started without a Location header")
        parts = urllib.parse.urlsplit(location)
        own = parts.netloc == self.host and parts.path.startswith("/v2/") and UPLOADS in parts.path
        # Artifactory's /artifactory/api/docker/<repo-key> base answers with a Location that drops that path.
        if own and self.base_path:
            upload_id = parts.path.split(UPLOADS, 1)[1]
            return f"{self.base}/v2/{repository}{UPLOADS}{upload_id}" + (f"?{parts.query}" if parts.query else "")
        if own:
            named = urllib.parse.unquote(parts.path[len("/v2/"):].split(UPLOADS, 1)[0])
            if named != repository:  # the registry would store the blob where the manifest check does not look
                raise RegistryError(f"upload Location names repository {named}, not {repository}")
        return location

    def start_upload(self, repository, digest=None, mount_from=None):
        """The upload URL, or None when the registry mounted the blob from mount_from."""
        query = f"?mount={urllib.parse.quote(digest)}&from={urllib.parse.quote(mount_from)}" if mount_from else ""
        response = self._send(repository, "pull,push", "POST", f"{repository}{UPLOADS}{query}",
                              {"Content-Length": "0"}, b"", mount_from)
        if mount_from and response.status == 201:
            read_limited(response, CONTROL_LIMIT, f"mount into {repository}")
            return None
        self._expect(response, f"start upload to {repository}", 202)
        read_limited(response, CONTROL_LIMIT, f"start upload to {repository}")
        return self._upload_location(response, repository)


def _manifest_type(raw, fallback=None):
    data = json.loads(raw)
    media_type = data.get("mediaType") or fallback
    if not media_type or not MEDIA_TYPE.fullmatch(media_type):
        media_type = ("application/vnd.oci.image.index.v1+json" if "manifests" in data
                      else "application/vnd.oci.image.manifest.v1+json")
    return media_type


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
