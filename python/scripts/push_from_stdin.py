#!/usr/bin/env python3
"""Push one blob or one manifest into a registry, reading it from stdin. For NiFi ExecuteStreamCommand, where the
FlowFile content arrives on stdin, so the Python API's getContentsAsBytes limit never applies.

usage: push_from_stdin.py <blob|manifest> <registry URL> <repository> <digest for a blob | tag or digest for a manifest>
env:   REGISTRY_USER, REGISTRY_PASSWORD (empty password: no credentials sent, for an open registry)
blob:     POST /v2/<repo>/blobs/uploads/, one chunked PATCH from stdin, PUT ?digest=. Memory is one 1 MiB buffer.
manifest: stdin is read whole (limit 64 MiB) and PUT with its bytes unchanged, so its digest does not move. The
          Content-Type is the document's own mediaType.
Exit codes: 0 pushed, 10 credentials refused, 11 upload start refused, 12 upload transfer failed, 13 commit refused
(digest does not match the bytes), 14 manifest refused, 15 bad arguments or network error.
stdout: one JSON line on success. stderr: the reason, and for a blob the upload id (a killed run leaves that upload
open in the registry; the next push of the blob replaces it)."""
import base64
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK, LIMIT = 1 << 20, 64 << 20
DEFAULT_MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"


def fail(code, message):
    print(message, file=sys.stderr)
    sys.exit(code)


def target(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        fail(15, f"registry URL {url!r} is not http(s)://host[:port]")
    return parsed


def challenge_params(header):
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def authorization(base, parsed, repository, user, password, actions="pull,push"):
    """The Authorization header the registry asks for: none for an open registry, Basic, or a Bearer token from the
    realm its WWW-Authenticate names. Raises SystemExit(10) when the credentials are refused."""
    try:
        urllib.request.urlopen(urllib.request.Request(f"{base}/v2/"), timeout=60).read()
        return {}
    except urllib.error.HTTPError as error:
        if error.code != 401:
            fail(15, f"GET /v2/ answered HTTP {error.code}")
        header = error.headers.get("WWW-Authenticate", "")
    except OSError as error:
        fail(15, f"cannot reach {parsed.netloc}: {error}")
    if not password:
        fail(10, "the registry wants credentials and REGISTRY_PASSWORD is empty")
    basic = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
    if not header.lower().startswith("bearer"):
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/v2/", headers=basic), timeout=60).read()
        except urllib.error.HTTPError as error:
            fail(10, f"the registry refused the credentials: HTTP {error.code}")
        except OSError as error:
            fail(15, f"cannot reach {parsed.netloc}: {error}")
        return basic
    params = challenge_params(header)
    query = {"service": params.get("service", parsed.netloc), "scope": f"repository:{repository}:{actions}"}
    realm = params.get("realm", f"{base}/v2/auth")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{realm}?{urllib.parse.urlencode(query)}", headers=basic),
                                    timeout=60) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        fail(10, f"the registry refused the credentials: HTTP {error.code}")
    except (OSError, ValueError) as error:
        fail(15, f"token request failed: {error}")
    token = body.get("token") or body.get("access_token")
    if not token:
        fail(10, "the token answer holds no token")
    return {"Authorization": "Bearer " + token}


class Registry:
    def __init__(self, parsed, headers):
        self.parsed, self.headers = parsed, headers
        self.prefix = parsed.path.rstrip("/")  # a registry served under a path, such as an Artifactory repo key

    def step(self, method, path, body=None, headers=None, chunked=False):
        connection = (http.client.HTTPSConnection if self.parsed.scheme == "https" else http.client.HTTPConnection)(
            self.parsed.netloc, timeout=600)
        try:
            connection.request(method, path, body=body, headers={**self.headers, **(headers or {})},
                               encode_chunked=chunked)
            response = connection.getresponse()
            return response, response.read()
        except OSError as error:
            fail(15, f"{method} {path.split('?')[0]}: {error}")
        finally:
            connection.close()


def location(response):
    parsed = urllib.parse.urlparse(response.getheader("Location") or "")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def reason(data):
    return data[:200].decode(errors="replace")


def put_manifest(registry, repository, reference, raw):
    """PUT raw unchanged, with the mediaType written inside it as Content-Type. Exits 14 when it is refused."""
    try:
        content_type = json.loads(raw).get("mediaType") or DEFAULT_MANIFEST_TYPE
    except (ValueError, AttributeError):
        fail(14, "stdin is not a JSON manifest")
    response, data = registry.step("PUT", f"{registry.prefix}/v2/{repository}/manifests/{reference}", body=raw,
                                   headers={"Content-Type": content_type})
    if response.status not in (200, 201):
        fail(14, f"manifest {reference} refused: HTTP {response.status} {reason(data)}")


def push_manifest(registry, repository, reference, started):
    raw = sys.stdin.buffer.read(LIMIT + 1)
    if len(raw) > LIMIT:
        fail(14, "manifest larger than 64 MiB")
    put_manifest(registry, repository, reference, raw)
    return {"pushed": f"{repository}:{reference}", "bytes": len(raw), "secs": round(time.time() - started, 1)}


def push_blob(registry, repository, digest, started):
    response, data = registry.step("POST", f"{registry.prefix}/v2/{repository}/blobs/uploads/")
    if response.status != 202:
        fail(11, f"upload start refused: HTTP {response.status} {reason(data)}")
    path = location(response)
    print(f"upload={path.split('?')[0].rsplit('/', 1)[-1]}", file=sys.stderr, flush=True)
    sent = [0]

    def body():
        while True:
            chunk = sys.stdin.buffer.read(CHUNK)
            if not chunk:
                return
            sent[0] += len(chunk)
            yield chunk

    response, data = registry.step("PATCH", path, body=body(), headers={"Content-Type": "application/octet-stream"},
                                   chunked=True)
    if response.status != 202:
        fail(12, f"upload transfer refused after {sent[0]} bytes: HTTP {response.status} {reason(data)}")
    path = location(response)
    path += ("&" if "?" in path else "?") + "digest=" + urllib.parse.quote(digest)
    response, data = registry.step("PUT", path)
    if response.status != 201:
        fail(13, f"commit refused (does {digest} match the {sent[0]} bytes sent?): HTTP {response.status} {reason(data)}")
    return {"pushed": digest, "repo": repository, "bytes": sent[0], "secs": round(time.time() - started, 1)}


def main(argv):
    if len(argv) != 5 or argv[1] not in ("blob", "manifest"):
        fail(15, "usage: push_from_stdin.py <blob|manifest> <registry URL> <repository> <digest|reference>")
    kind, url, repository, reference = argv[1], argv[2].rstrip("/"), argv[3], argv[4]
    parsed = target(url)
    started = time.time()
    headers = authorization(url, parsed, repository, os.environ.get("REGISTRY_USER", ""),
                            os.environ.get("REGISTRY_PASSWORD", ""))
    registry = Registry(parsed, headers)
    push = push_manifest if kind == "manifest" else push_blob
    print(json.dumps(push(registry, repository, reference, started)))


if __name__ == "__main__":
    main(sys.argv)
