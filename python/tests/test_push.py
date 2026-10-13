"""PushContainerImages.transform with the NiFi API stood in."""
import os
import tempfile
import unittest

import nifi_stub

import PushContainerImages as push_module
from PushContainerImages import PushContainerImages


class FlowFile:
    def getAttribute(self, name):
        return None

    def getAttributes(self):
        return nifi_stub.JavaMap()


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
    def test_username_and_ca_file_come_from_the_environment(self):
        os.environ["TEST_PUSH_USER"] = "robot"
        os.environ["TEST_PUSH_CA"] = "/etc/ca.pem"
        self.addCleanup(os.environ.pop, "TEST_PUSH_USER")
        self.addCleanup(os.environ.pop, "TEST_PUSH_CA")
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"Username": "${TEST_PUSH_USER}", "CA Certificate File": "${TEST_PUSH_CA}",
                                          "Tar File": "/nonexistent.tar"})
        processor.transform(context, FlowFile())
        self.assertEqual((seen[0][1], seen[0][3]), ("robot", "/etc/ca.pem"))

    def test_text_after_a_reference_is_kept(self):
        os.environ["TEST_PUSH_CA"] = "/etc"
        self.addCleanup(os.environ.pop, "TEST_PUSH_CA")
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"CA Certificate File": "${TEST_PUSH_CA}/ca.pem", "Tar File": "/nonexistent.tar"})
        processor.transform(context, FlowFile())
        self.assertEqual(seen[0][3], "/etc/ca.pem")


if __name__ == "__main__":
    unittest.main()
