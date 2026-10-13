"""Registry client and export tar format shared by PushContainerImages and PullContainerImages.

Standard library only. It speaks the OCI distribution (Docker Registry HTTP API v2) push and pull
endpoints, with Bearer token auth from the realm a 401 names (Quay, Artifactory, Harbor, GitLab,
registry:2 with token auth) or Basic auth (registry:2 with htpasswd), or none for an open registry.

TLS trusts the system store plus SSL_CERT_FILE (OpenSSL reads it), plus an optional extra CA file.

The tar layout is the mirror.py export format:
  export.json                  {"images": [{"dir", "source", "target", "tag", "digest"}]}
  NNNNNN/manifest.json         the image's manifest or index, byte for byte
  NNNNNN/<hex>.manifest.json   each child manifest of an index
  NNNNNN/<hex>                 each config and layer blob; a repeat is a hard link to the first
  NNNNNN/version               Directory Transport Version: 1.1
"""
import base64
import hashlib
import io
import json
import os
import re
import ssl
import tarfile
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
INDEX = "export.json"
KEPT = re.compile(r"export\.json|[0-9]{6}/(?:manifest|[0-9a-f]{64}\.manifest)\.json")


class RegistryError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand, so credentials never travel to another host (object storage)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def rebase(target, rewrite=None, strip=False):
    """target with its first path segment replaced (rewrite) or removed (strip)."""
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


class RegistryClient:
    def __init__(self, url, username=None, password=None, ca_file=None, timeout=3600):
        url = (url or "").rstrip("/")
        if not url.startswith(("https://", "http://")):
            raise RegistryError(f"Registry URL must start with https:// or http://: {url}")
        self.base = url
        parts = urllib.parse.urlsplit(url)
        self.host = parts.netloc
        self.base_path = parts.path
        self.username = username or None
        self.password = password or ""
        self.timeout = timeout
        context = ssl.create_default_context()  # system store, and SSL_CERT_FILE when set
        if ca_file:
            context.load_verify_locations(cafile=ca_file)
        self.opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
        self.context = context
        self.auth = {}

    # -- transport ---------------------------------------------------------------------------
    def _open(self, method, url, headers=None, data=None):
        request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            return error  # an HTTPError is a response: status, headers and body
        except urllib.error.URLError as error:
            raise RegistryError(f"{method} {url}: {error.reason}") from error

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
            who = ("an anonymous client; set Username and Password" if self.username is None
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
                raise RegistryError(f"{self.host} asks for a username and password; set Username and Password")
            return self._basic()
        if challenge[:6].lower() != "bearer":
            raise RegistryError(f"{self.host} refused the request with no usable challenge: {challenge}")
        params = {k.lower(): v for k, v in re.findall(r'(\w+)="([^"]*)"', challenge)}
        realm = params.get("realm")
        if not realm:
            raise RegistryError(f"{self.host} sent a Bearer challenge without a realm: {challenge}")
        query = []
        if "service" in params:
            query.append(("service", params["service"]))
        query.append(("scope", "registry:catalog:*" if repository is None else f"repository:{repository}:{actions}"))
        if mount_from:
            query.append(("scope", f"repository:{mount_from}:pull"))
        url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(query)
        headers = {"Authorization": self._basic()} if self.username else {}
        response = self._open("GET", url, headers)
        body = response.read()
        if response.status != 200:
            raise RegistryError(f"token from {realm}: HTTP {response.status}: {body[:300].decode(errors='replace')}")
        token = json.loads(body).get("token") or json.loads(body).get("access_token")
        if not token:
            raise RegistryError(f"no token in the answer from {realm}")
        return "Bearer " + token

    @staticmethod
    def _expect(response, what, *codes):
        if response.status not in codes:
            body = response.read()[:300].decode(errors="replace").strip()
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
        body = response.read()
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
            same = urllib.parse.urlsplit(location).netloc == self.host
            headers = self._with_auth((repository, "pull", None), {}) if same else {}
            response = self._open("GET", location, headers)
        raise RegistryError(f"too many redirects for blob {digest}")

    def _paged(self, repository, first, field, what):
        out, url, seen = [], f"{self.base}/v2/{first}", set()
        while url and url not in seen and len(seen) < 1000:
            seen.add(url)
            path = url[len(self.base) + len("/v2/"):] if url.startswith(self.base) else url
            response = self._send(repository, "pull", "GET", path)
            if response.status == 404 and repository is not None:
                return out
            self._expect(response, f"list {what}", 200)
            out += json.loads(response.read()).get(field) or []
            match = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', response.headers.get("Link", ""))
            url = urllib.parse.urljoin(url, match.group(1)) if match else None
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
        response.read()
        if mount_from and response.status == 201:
            return None
        self._expect(response, f"start upload to {repository}", 202)
        return self._upload_location(response, repository)

    def upload_blob(self, repository, digest, stream, size, upload=None):
        """One PUT of a blob read from stream (size bytes)."""
        upload = upload or self.start_upload(repository)
        url = upload + ("&" if "?" in upload else "?") + "digest=" + urllib.parse.quote(digest)
        headers = self._with_auth((repository, "pull,push", None),
                                  {"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        response = self._open("PUT", url, headers, stream if size else b"")
        response.read()
        self._expect(response, f"upload blob {digest} to {repository}", 201, 204)

    def put_manifest(self, repository, reference, manifest, media_type):
        response = self._send(repository, "pull,push", "PUT", f"{repository}/manifests/{reference}",
                              {"Content-Type": media_type}, manifest)
        response.read()
        self._expect(response, f"push manifest {repository}:{reference}", 201, 200)


class _Hashing(io.RawIOBase):
    """Reads from a stream, hashing as it goes, so a blob is checked while it is sent."""
    def __init__(self, stream):
        self.stream, self.digest, self.count = stream, hashlib.sha256(), 0

    def readable(self):
        return True

    def read(self, n=-1):
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
    """config and layer digests with sizes; a foreign layer (with urls) is not copied."""
    out = {}
    for descriptor in [manifest.get("config")] + list(manifest.get("layers") or []):
        if not descriptor:
            continue
        digest = descriptor.get("digest", "")
        if not DIGEST.fullmatch(digest) or "size" not in descriptor:
            raise RegistryError(f"unsupported descriptor {descriptor}")
        if not descriptor.get("urls"):
            out[digest] = int(descriptor["size"])
    return out


def read_export(path):
    """The images in an export tar: [(entry, root bytes, [(child digest, bytes)], {blob: member name})]."""
    with tarfile.open(path, "r:") as tar:
        members = {m.name[2:] if m.name.startswith("./") else m.name: m for m in tar.getmembers()}
        if sum(m.size for n, m in members.items() if KEPT.fullmatch(n)) > 256 * 1024 * 1024:
            raise RegistryError("the manifests are too large to be an export")

        def read(name):
            member = members.get(name)
            if member is None:
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
            manifests = [(root, root_json)]
            if "manifests" in root_json:
                manifests = []
                for child in root_json["manifests"]:
                    digest = child.get("digest", "")
                    if not DIGEST.fullmatch(digest):
                        raise RegistryError(f"{entry['target']}:{entry['tag']}: unsupported child digest {digest}")
                    name = f"{entry['dir']}/{digest[7:]}.manifest.json"
                    if name not in members:
                        continue  # a platform the export left out
                    raw = read(name)
                    if "sha256:" + sha256_hex(raw) != digest:
                        raise RegistryError(f"{name} does not match its digest in the index")
                    children.append((digest, raw, _manifest_type(raw, child.get("mediaType"))))
                    manifests.append((raw, json.loads(raw)))
            for _raw, manifest in manifests:
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


def push_export(client, path, rewrite=None, strip=False, move_tags=False, log=None):
    """Push every image of an export tar. Every check runs before the first upload, so a refusal pushes
    nothing. Returns (pushed, present, lines)."""
    images = read_export(path)
    plan, present, lines = [], 0, []
    for entry, root, media_type, children, blobs in images:
        repository = rebase(entry["target"], rewrite, strip)
        current = client.manifest_digest(repository, entry["tag"])
        lines.append(f"{repository}:{entry['tag']}@{entry['digest']}")
        if current == entry["digest"]:
            present += 1
        elif current and not move_tags:
            raise RegistryError(f"{client.host}/{repository}:{entry['tag']} is {current}, the tar has {entry['digest']}; "
                                "a tag does not move unless Move Existing Tags is true")
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
                stream = _Hashing(tar.extractfile(member))
                client.upload_blob(repository, digest, stream, member.size, upload)
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
    """(root bytes, {child digest: bytes}, {blob digest: size}) with every blob confirmed present."""
    root, _ = client.get_manifest(repository, digest)
    data = json.loads(root)
    children, blobs = {}, {}
    if "manifests" in data:
        for child in data["manifests"]:
            child_digest = child.get("digest", "")
            if not DIGEST.fullmatch(child_digest):
                raise RegistryError(f"unsupported child digest {child_digest}")
            raw, _ = client.get_manifest(repository, child_digest)
            children[child_digest] = raw
            blobs.update(_blobs_of(json.loads(raw)))
    else:
        blobs.update(_blobs_of(data))
    for blob in blobs:
        if not client.blob_exists(repository, blob):
            raise RegistryError(f"blob {blob} is missing")
    return root, children, blobs


def write_export(client, path, images):
    """Write images [(source repository, tag, digest, target, root, children, blobs)] as an export tar at path
    (through path.partial, renamed once complete). Returns its sha256."""
    partial = f"{path}.partial"
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
                    tar.addfile(info, stream)
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
