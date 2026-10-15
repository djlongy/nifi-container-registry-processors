"""python/scripts/post_blob_loopback.py: a registry blob streamed into a local HTTP server as a chunked POST."""
import hashlib
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_fixture import reply, serve
from test_push_from_stdin import FakeRegistry, chunked_body

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "post_blob_loopback.py")


class Listener(ThreadingHTTPServer):
    def __init__(self, refuse=0):
        super().__init__(("127.0.0.1", 0), ListenerHandler)
        self.refuse, self.received = refuse, []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class ListenerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        refuse = self.server.refuse > 0
        self.server.refuse -= 1
        if refuse:  # like ListenHTTP under back pressure: answer before the body is read, then close
            self.close_connection = True
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.received.append((self.path, dict(self.headers), chunked_body(self)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class PostBlobLoopback(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        self.data = os.urandom(2 * (1 << 20) + 5)
        self.digest = "sha256:" + hashlib.sha256(self.data).hexdigest()
        self.registry.blobs[self.digest] = self.data
        self.addCleanup(self.registry.server_close)
        self.addCleanup(self.registry.shutdown)

    def post(self, listener, *extra):
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        env = {**os.environ, "REGISTRY_USER": "", "REGISTRY_PASSWORD": ""}
        return subprocess.run([sys.executable, SCRIPT, self.registry.url, "team/app", self.digest,
                               str(listener.server_port), "--scheme", "http", "--name", "nifi-loopback", *extra],
                              capture_output=True, env=env, timeout=120)

    def test_the_blob_arrives_chunked_with_its_headers_and_the_certificate_name_as_host(self):
        listener = Listener()
        done = self.post(listener, "--header", "X-Image-Id=img1", "--header", "X-Layer-N=2")
        self.assertEqual(done.returncode, 0, done.stderr)
        path, headers, body = listener.received[0]
        self.assertEqual(path, "/blobs")
        self.assertEqual(body, self.data)
        self.assertEqual(headers["Transfer-Encoding"], "chunked")
        self.assertEqual(headers["Host"], f"nifi-loopback:{listener.server_port}")
        self.assertEqual((headers["X-Image-Id"], headers["X-Layer-N"], headers["X-Blob-Digest"], headers["X-Attempt"]),
                         ("img1", "2", self.digest, "1"))
        self.assertEqual(headers["filename"], self.digest.split(":")[1])

    def test_a_refused_attempt_is_retried_and_the_attempt_number_is_sent(self):
        listener = Listener(refuse=2)
        done = self.post(listener, "--attempts", "4")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(listener.received[0][1]["X-Attempt"], "3")
        self.assertEqual(listener.received[0][2], self.data)

    def test_it_gives_up_with_exit_6(self):
        listener = Listener(refuse=99)
        done = self.post(listener, "--attempts", "2")
        self.assertEqual(done.returncode, 6)
        self.assertEqual(listener.received, [])

    def test_a_repository_or_digest_that_is_not_a_plain_name_exits_15_without_a_request(self):
        for repository, digest in [("../admin", self.digest), ("team/app?x", self.digest), ("team/app", "latest"),
                                   ("team/app", self.digest + "/../x"), ("team/app", "sha256:" + "A" * 64)]:
            with self.subTest(repository=repository, digest=digest):
                with serve(lambda handler, seen: reply(handler, 200)) as (url, seen):
                    done = subprocess.run([sys.executable, SCRIPT, url, repository, digest, "1", "--scheme", "http"],
                                          capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 15, done.stderr)
                self.assertEqual(seen.requests, [])

    def redirecting_registry(self, location, password="secret"):
        """A registry that wants Basic credentials and then answers the blob GET with a redirect."""
        def respond(handler, seen):
            if handler.headers.get("Authorization") is None:
                return reply(handler, 401, headers={"WWW-Authenticate": 'Basic realm="x"'})
            if "/blobs/" in handler.path:
                return reply(handler, 302, headers={"Location": location(handler) if callable(location) else location})
            reply(handler, 200)
        return serve(respond)

    def post_through(self, url, *extra):
        listener = Listener()
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        env = {**os.environ, "REGISTRY_USER": "robot", "REGISTRY_PASSWORD": "secret"}
        done = subprocess.run([sys.executable, SCRIPT, url, "team/app", self.digest, str(listener.server_port),
                               "--scheme", "http", "--attempts", "1", *extra],
                              capture_output=True, env=env, timeout=120)
        return done, listener

    def test_a_redirect_to_a_file_url_is_refused_and_nothing_is_forwarded(self):
        with self.redirecting_registry("file:///etc/hostname") as (url, _):
            done, listener = self.post_through(url)
        self.assertEqual(done.returncode, 15, done.stderr)
        self.assertEqual(listener.received, [])

    def test_a_redirect_to_a_private_address_is_refused_unless_trusted(self):
        with serve(lambda handler, seen: reply(handler, 200, b"internal secret")) as (internal, seen):
            with self.redirecting_registry(internal + "/metadata") as (url, _):
                done, listener = self.post_through(url)
            self.assertEqual(done.returncode, 15, done.stderr)
            self.assertIn(b"not a public address", done.stderr)
            self.assertEqual((seen.requests, listener.received), ([], []))
            with self.redirecting_registry(internal + "/metadata") as (url, _):
                done, listener = self.post_through(url, "--trusted-storage", "127.0.0.0/8")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(listener.received[0][2], b"internal secret")
            self.assertNotIn("Authorization", seen.requests[0][2])  # the registry credentials stay behind

    def test_a_redirect_that_stays_on_the_registry_keeps_its_credentials(self):
        def blob(handler, seen):
            if handler.path.startswith("/v2/") and "/blobs/" in handler.path and handler.headers.get("Authorization"):
                return reply(handler, 302, headers={"Location": "/stored"})
            if handler.path == "/stored":
                return reply(handler, 200 if handler.headers.get("Authorization") else 401, b"payload",
                             headers={} if handler.headers.get("Authorization") else {"WWW-Authenticate": 'Basic realm="x"'})
            reply(handler, 401 if not handler.headers.get("Authorization") else 200,
                  headers={} if handler.headers.get("Authorization") else {"WWW-Authenticate": 'Basic realm="x"'})
        with serve(blob) as (url, _):
            done, listener = self.post_through(url)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(listener.received[0][2], b"payload")

    def test_a_redirect_loop_is_refused(self):
        with self.redirecting_registry(lambda handler: f"http://127.0.0.1:{handler.server.server_port}/v2/team/app/blobs/x") as (url, _):
            done, listener = self.post_through(url)
        self.assertEqual(done.returncode, 15, done.stderr)
        self.assertIn(b"redirects", done.stderr)

    def test_https_without_certificate_files_exits_15(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPBACK_")}
        done = subprocess.run([sys.executable, SCRIPT, self.registry.url, "team/app", self.digest, "1"],
                              capture_output=True, env=env)
        self.assertEqual(done.returncode, 15)
        self.assertIn(b"--ca", done.stderr)


if __name__ == "__main__":
    unittest.main()
