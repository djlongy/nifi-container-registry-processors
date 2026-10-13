"""SSL Context Service: the CAs of its truststore verify a registry that the system store does not."""
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nifi_stub  # noqa: F401
from test_push import ssl_service

import container_registry_client as c

DIGEST = "sha256:" + "b" * 64


def make_ca(directory, name):
    """A CA named name, and a server certificate for 127.0.0.1 it signed. Returns (CA DER, server cert file, key file)."""
    def run(*args):
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}-ca.key", "-out", f"{name}-ca.pem",
        "-subj", f"/CN={name} test CA", "-days", "2", "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", "/CN=127.0.0.1")
    with open(f"{directory}/{name}.ext", "w") as ext:
        ext.write("subjectAltName=IP:127.0.0.1\n")
    run("x509", "-req", "-in", f"{name}.csr", "-CA", f"{name}-ca.pem", "-CAkey", f"{name}-ca.key", "-CAcreateserial",
        "-out", f"{name}.pem", "-days", "2", "-extfile", f"{name}.ext")
    pem = open(f"{directory}/{name}-ca.pem").read()
    return ssl.PEM_cert_to_DER_cert(pem), f"{directory}/{name}.pem", f"{directory}/{name}.key"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Docker-Content-Digest", DIGEST)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TrustedByService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.directory)
        cls.good_der, cert, key = make_ca(cls.directory, "good")
        cls.other_der, _, _ = make_ca(cls.directory, "other")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.addClassCleanup(server.server_close)
        cls.addClassCleanup(server.shutdown)
        cls.url = f"https://127.0.0.1:{server.server_port}"

    def digest(self, ders):
        return c.RegistryClient(self.url, ca_der=ders).manifest_digest("team/app", "1")

    def test_a_server_signed_by_a_CA_of_the_service_verifies(self):
        value = nifi_stub.Value("ssl", False)
        value.asControllerService = lambda: ssl_service([self.other_der, self.good_der])
        ders = c.trusted_der(value)
        self.assertEqual(self.digest(ders), DIGEST)

    def test_an_unrelated_CA_fails(self):
        with self.assertRaises(c.RegistryError) as raised:
            self.digest([self.other_der])
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(raised.exception))

    def test_no_CA_fails(self):
        with self.assertRaises(c.RegistryError):
            self.digest([])

    def test_trusted_der_reads_the_service_and_is_empty_when_unset(self):
        value = nifi_stub.Value("ssl", False)
        value.asControllerService = lambda: ssl_service([self.good_der])
        self.assertEqual(c.trusted_der(value), [self.good_der])
        self.assertEqual(c.trusted_der(nifi_stub.Value(None, False)), [])


if __name__ == "__main__":
    unittest.main()
