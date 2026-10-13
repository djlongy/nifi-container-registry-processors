"""PushContainerImages.transform with the NiFi API stood in."""
import os
import tempfile
import types
import unittest

import nifi_stub

import PushContainerImages as push_module
from PushContainerImages import PushContainerImages


class FlowFile:
    def getAttribute(self, name):
        return None

    def getAttributes(self):
        return nifi_stub.JavaMap()


def ssl_service(ders):
    """An SSLContextProvider whose truststore holds certificates with these DER encodings."""
    certs = [types.SimpleNamespace(getEncoded=lambda der=der: der) for der in ders]
    manager = types.SimpleNamespace(getAcceptedIssuers=lambda: certs)
    return types.SimpleNamespace(createTrustManager=lambda: manager)


def make_push(**values):
    processor = PushContainerImages()
    processor.logger = nifi_stub.Logger()
    settings = {"Registry URL": "http://127.0.0.1:1", "Delete Tar File": "true"}
    settings.update(values)
    return processor, nifi_stub.FakeContext(PushContainerImages.PROPERTIES, settings)


class MissingTar(unittest.TestCase):
    def test_a_missing_tar_routes_to_failure_with_the_reason(self):
        path = os.path.join(tempfile.mkdtemp(), "gone.tar")
        processor, context = make_push(**{"Tar File": path})
        result = processor.transform(context, FlowFile())
        self.assertEqual(result.relationship, "failure")
        self.assertIn("gone.tar", result.attributes["registry.error"])
        self.assertNotIn("pushing", result.attributes["registry.error"])
        self.assertIn("gone.tar", processor.logger.text())


class EnvironmentScope(unittest.TestCase):
    def test_username_comes_from_the_environment_and_the_ssl_service_becomes_der(self):
        os.environ["TEST_PUSH_USER"] = "robot"
        self.addCleanup(os.environ.pop, "TEST_PUSH_USER")
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"Username": "${TEST_PUSH_USER}", "SSL Context Service": "ssl",
                                          "Tar File": "/nonexistent.tar"})
        context.services["SSL Context Service"] = ssl_service([b"der-one"])
        processor.transform(context, FlowFile())
        self.assertEqual((seen[0][1], seen[0][3]), ("robot", [b"der-one"]))

    def test_no_ssl_service_means_no_extra_ca(self):
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"Tar File": "/nonexistent.tar"})
        processor.transform(context, FlowFile())
        self.assertEqual(seen[0][3], [])


if __name__ == "__main__":
    unittest.main()
