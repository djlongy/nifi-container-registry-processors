"""python/scripts/push_from_stdin.py against a fake registry: blob push, wrong digest, manifest bytes, credentials."""
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_fixture import reply, serve

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "push_from_stdin.py")


def chunked_body(handler):
    data = b""
    while True:
        size = int(handler.rfile.readline().strip() or b"0", 16)
        if size == 0:
            handler.rfile.readline()
            return data
        data += handler.rfile.read(size)
        handler.rfile.readline()


class FakeRegistry(ThreadingHTTPServer):
    def __init__(self, password=None):
        super().__init__(("127.0.0.1", 0), Handler)
        self.password = password
        self.uploads, self.blobs, self.manifests = {}, {}, {}
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body(self):
        if self.headers.get("Transfer-Encoding") == "chunked":
            return chunked_body(self)
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def allowed(self):
        wanted = self.server.password
        if wanted is None:
            return True
        good = "Basic " + base64.b64encode(f"robot:{wanted}".encode()).decode()
        if self.headers.get("Authorization") == good:
            return True
        self.body()
        self.reply(401, {"WWW-Authenticate": 'Basic realm="fake"'})
        return False

    def do_GET(self):
        if not self.allowed():
            return
        digest = self.path.rsplit("/", 1)[-1]
        if "/blobs/" not in self.path:
            return self.reply(200)
        if digest not in self.server.blobs:
            return self.reply(404)
        data = self.server.blobs[digest]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_HEAD(self):
        if not self.allowed():
            return
        known = self.server.blobs if "/blobs/" in self.path else self.server.manifests
        self.reply(200 if self.path.rsplit("/", 1)[-1] in known else 404)

    def do_POST(self):
        if not self.allowed():
            return
        self.body()
        upload = str(len(self.server.uploads))
        self.server.uploads[upload] = b""
        self.reply(202, {"Location": f"/v2/team/app/blobs/uploads/{upload}"})

    def do_PATCH(self):
        upload = self.path.rsplit("/", 1)[-1]
        self.server.uploads[upload] += self.body()
        self.reply(202, {"Location": self.path})

    def do_PUT(self):
        data = self.body()
        if "/manifests/" in self.path:
            self.server.manifests[self.path.rsplit("/", 1)[-1]] = (data, self.headers.get("Content-Type"))
            return self.reply(201)
        path, _, query = self.path.partition("?digest=")
        digest = query.replace("%3A", ":")
        stored = self.server.uploads[path.rsplit("/", 1)[-1]]
        if "sha256:" + hashlib.sha256(stored).hexdigest() != digest:
            return self.reply(400)
        self.server.blobs[digest] = stored
        self.reply(201)


class PushFromStdin(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.addCleanup(self.registry.server_close)
        self.addCleanup(self.registry.shutdown)

    def run_script(self, kind, reference, data, password="", registry=None):
        env = {**os.environ, "REGISTRY_USER": "robot", "REGISTRY_PASSWORD": password}
        return subprocess.run([sys.executable, SCRIPT, kind, (registry or self.registry).url, "team/app", reference],
                              input=data, capture_output=True, env=env, timeout=60)

    def test_a_blob_is_streamed_into_the_registry(self):
        data = os.urandom(3 * (1 << 20) + 17)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        done = self.run_script("blob", digest, data)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registry.blobs[digest], data)
        self.assertEqual(json.loads(done.stdout)["bytes"], len(data))
        self.assertIn(b"upload=", done.stderr)

    def test_a_wrong_digest_exits_13_and_stores_nothing(self):
        done = self.run_script("blob", "sha256:" + "0" * 64, b"some bytes")
        self.assertEqual(done.returncode, 13, done.stderr)
        self.assertEqual(self.registry.blobs, {})

    def test_a_manifest_is_put_with_its_bytes_unchanged(self):
        raw = b'{\n  "schemaVersion": 2,\n    "mediaType": "application/vnd.docker.distribution.manifest.v2+json"\n}\n'
        done = self.run_script("manifest", "1.0", raw)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registry.manifests["1.0"], (raw, "application/vnd.docker.distribution.manifest.v2+json"))

    def test_input_that_is_not_json_is_a_manifest_error(self):
        self.assertEqual(self.run_script("manifest", "1.0", b"not json").returncode, 14)

    def test_wrong_credentials_exit_10_and_right_ones_push(self):
        guarded = FakeRegistry(password="secret")
        self.addCleanup(guarded.server_close)
        self.addCleanup(guarded.shutdown)
        data = b"layer"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.assertEqual(self.run_script("blob", digest, data, password="wrong", registry=guarded).returncode, 10)
        self.assertEqual(self.run_script("blob", digest, data, password="", registry=guarded).returncode, 10)
        self.assertEqual(self.run_script("blob", digest, data, password="secret", registry=guarded).returncode, 0)

    def run_url(self, url, password="secret", kind="manifest", reference="1.0", data=b'{"schemaVersion": 2}'):
        return self.run_script(kind, reference, data, password=password, registry=types.SimpleNamespace(url=url))

    def test_a_redirect_from_the_registry_never_carries_credentials(self):
        with serve(lambda handler, seen: reply(handler, 200)) as (elsewhere, collected):
            def registry(handler, seen):
                if handler.headers.get("Authorization"):
                    return reply(handler, 302, headers={"Location": elsewhere + "/capture"})
                reply(handler, 401, headers={"WWW-Authenticate": 'Basic realm="x"'})
            with serve(registry) as (url, _):
                done = self.run_url(url)
        self.assertEqual(done.returncode, 15, done.stderr)
        self.assertEqual(collected.requests, [])

    def test_a_token_realm_that_redirects_never_gets_the_credentials_forwarded(self):
        with serve(lambda handler, seen: reply(handler, 200)) as (elsewhere, collected):
            def registry(handler, seen):
                if handler.path.startswith("/token"):
                    return reply(handler, 302, headers={"Location": elsewhere + "/capture"})
                reply(handler, 401, headers={"WWW-Authenticate": f'Bearer realm="http://127.0.0.1:{handler.server.server_port}/token"'})
            with serve(registry) as (url, _):
                done = self.run_url(url)
        self.assertEqual(done.returncode, 15, done.stderr)
        self.assertEqual(collected.requests, [])

    def test_a_token_realm_on_another_host_over_plain_http_gets_no_credentials(self):
        with serve(lambda handler, seen: reply(handler, 200, b'{"token": "t"}')) as (elsewhere, collected):
            realm = elsewhere.replace("127.0.0.1", "localhost") + "/token"
            with serve(lambda handler, seen: reply(handler, 401, headers={"WWW-Authenticate": f'Bearer realm="{realm}"'})) as (url, _):
                done = self.run_url(url)
        self.assertEqual(done.returncode, 10, done.stderr)
        self.assertIn(b"not https", done.stderr)
        self.assertEqual(collected.requests, [])

    def test_a_token_realm_on_the_registrys_own_plain_http_origin_is_used(self):
        def registry(handler, seen):
            if handler.path.startswith("/token"):
                return reply(handler, 200, b'{"token": "issued"}')
            if handler.headers.get("Authorization") == "Bearer issued":
                return reply(handler, 201)
            reply(handler, 401, headers={"WWW-Authenticate": f'Bearer realm="http://127.0.0.1:{handler.server.server_port}/token"'})
        with serve(registry) as (url, seen):
            done = self.run_url(url)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_repository_or_reference_that_is_not_a_plain_name_is_refused_before_any_request(self):
        digest = "sha256:" + "a" * 64
        cases = [("blob", "team/app", "latest"), ("blob", "team/app", digest + "/../x"),
                 ("manifest", "team/app", "../../x"), ("manifest", "team/app", "a?b=c"), ("manifest", "team/app", "a/b")]
        cases += [(kind, repository, digest) for kind in ("blob", "manifest")
                  for repository in ["../admin", "team/app?x=1", "team//app", "Team/App", "team/app/../x", "team/app#f", "a b"]]
        with serve(lambda handler, seen: reply(handler, 200)) as (url, seen):
            for kind, repository, reference in cases:
                with self.subTest(kind=kind, repository=repository, reference=reference):
                    env = {**os.environ, "REGISTRY_USER": "robot", "REGISTRY_PASSWORD": ""}
                    done = subprocess.run([sys.executable, SCRIPT, kind, url, repository, reference], input=b"{}",
                                          capture_output=True, env=env, timeout=60)
                    self.assertEqual(done.returncode, 15, done.stderr)
        self.assertEqual(seen.requests, [])

    def test_bad_arguments_and_an_unreachable_registry_exit_15(self):
        self.assertEqual(subprocess.run([sys.executable, SCRIPT, "blob"], capture_output=True).returncode, 15)
        self.registry.shutdown()
        self.registry.server_close()
        self.assertEqual(self.run_script("blob", "sha256:" + "0" * 64, b"x").returncode, 15)


if __name__ == "__main__":
    unittest.main()
