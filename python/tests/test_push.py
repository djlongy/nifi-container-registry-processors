"""PushContainerImages.transform with the NiFi API stood in."""
import fcntl
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
    settings = {"Target Registry URL": "http://127.0.0.1:1", "Delete Input Tar": "true"}
    settings.update(values)
    return processor, nifi_stub.FakeContext(PushContainerImages.PROPERTIES, settings)


class MissingTar(unittest.TestCase):
    def test_a_missing_tar_routes_to_failure_with_the_reason(self):
        path = os.path.join(tempfile.mkdtemp(), "gone.tar")
        processor, context = make_push(**{"Input Tar Path": path})
        result = processor.transform(context, FlowFile())
        self.assertEqual(result.relationship, "failure")
        self.assertIn("gone.tar", result.attributes["registry.error"])
        self.assertNotIn("pushing", result.attributes["registry.error"])
        self.assertIn("gone.tar", processor.logger.text())


class Named(FlowFile):
    def __init__(self, filename):
        self.filename = filename

    def getAttribute(self, name):
        return self.filename if name == "filename" else None


class Contained(unittest.TestCase):
    """Input Tar Path built from an attribute that arrives with the data must stay inside its fixed directory."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.inbox = os.path.join(self.root, "in")
        os.mkdir(self.inbox)
        self.victim = os.path.join(self.root, "victim.tar")
        with open(self.victim, "w") as victim:
            victim.write("not yours")
        original = (push_module.RegistryClient, push_module.push_export)
        push_module.RegistryClient = lambda *args: types.SimpleNamespace(host="registry.test")
        push_module.push_export = lambda client, tar, *rest: (1, 0, [f"read {os.path.basename(tar)}"])
        self.addCleanup(lambda: (setattr(push_module, "RegistryClient", original[0]),
                                 setattr(push_module, "push_export", original[1])))

    def run_push(self, template, filename, delete="true"):
        processor, context = make_push(**{"Input Tar Path": template, "Delete Input Tar": delete})
        return processor.transform(context, Named(filename))

    def test_a_filename_that_climbs_out_is_refused_and_the_file_is_not_touched(self):
        for delete in ("true", "false"):
            with self.subTest(delete=delete):
                result = self.run_push(self.inbox + "/${filename}", "../victim.tar", delete)
                self.assertEqual(result.relationship, "failure")
                self.assertIn("outside", result.attributes["registry.error"])
                with open(self.victim) as victim:
                    self.assertEqual(victim.read(), "not yours")
                self.assertEqual(os.listdir(self.root).count("victim.tar"), 1)

    def test_a_symlink_that_leaves_the_directory_is_refused(self):
        os.symlink(self.victim, os.path.join(self.inbox, "link.tar"))
        result = self.run_push(self.inbox + "/${filename}", "link.tar")
        self.assertEqual(result.relationship, "failure")
        self.assertTrue(os.path.exists(self.victim))
        self.assertTrue(os.path.islink(os.path.join(self.inbox, "link.tar")))

    def test_a_leftover_beside_a_filename_outside_the_directory_is_not_adopted(self):
        open(self.victim + ".pushing-abc", "w").close()
        result = self.run_push(self.inbox + "/${filename}", "../victim.tar")
        self.assertEqual(result.relationship, "failure")
        self.assertTrue(os.path.exists(self.victim + ".pushing-abc"))

    def test_a_path_with_no_fixed_directory_in_front_of_the_expression_is_refused(self):
        result = self.run_push("${filename}", self.victim)
        self.assertEqual(result.relationship, "failure")
        self.assertIn("fixed directory", result.attributes["registry.error"])
        self.assertTrue(os.path.exists(self.victim))

    def test_a_filename_inside_the_directory_is_pushed_and_deleted(self):
        open(os.path.join(self.inbox, "a.tar"), "w").close()
        result = self.run_push(self.inbox + "/${filename}", "a.tar")
        self.assertEqual(result.relationship, "success", result.attributes)
        self.assertEqual(os.listdir(self.inbox), [])


class Adopt(unittest.TestCase):
    def push(self, path):
        original = (push_module.RegistryClient, push_module.push_export)
        push_module.RegistryClient = lambda *args: types.SimpleNamespace(host="registry.test")
        push_module.push_export = lambda client, tar, *rest: (1, 0, [f"read {os.path.basename(tar)}"])
        self.addCleanup(lambda: (setattr(push_module, "RegistryClient", original[0]),
                                 setattr(push_module, "push_export", original[1])))
        processor, context = make_push(**{"Input Tar Path": path})
        return processor.transform(context, FlowFile())

    def test_a_tar_left_by_a_killed_push_is_adopted_and_deleted(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.tar")
        open(path + ".pushing-abc", "w").close()
        result = self.push(path)
        self.assertEqual(result.relationship, "success")
        self.assertEqual(os.listdir(directory), [])

    def test_more_than_one_leftover_is_an_error_naming_them_all(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.tar")
        for suffix in ("one", "two"):
            open(f"{path}.pushing-{suffix}", "w").close()
        result = self.push(path)
        self.assertEqual(result.relationship, "failure")
        self.assertIn("a.tar.pushing-one", result.attributes["registry.error"])
        self.assertIn("a.tar.pushing-two", result.attributes["registry.error"])
        self.assertEqual(len(os.listdir(directory)), 2)


    def hold(self, name):
        handle = open(name, "r+")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(handle.close)

    def test_a_leftover_held_by_a_live_push_is_not_adopted(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.tar")
        open(path + ".pushing-live", "w").close()
        self.hold(path + ".pushing-live")
        result = self.push(path)
        self.assertEqual(result.relationship, "failure")
        self.assertIn("still running", result.attributes["registry.error"])
        self.assertEqual(os.listdir(directory), ["a.tar.pushing-live"])

    def test_only_the_unlocked_leftover_is_adopted(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.tar")
        for suffix in ("live", "dead"):
            open(f"{path}.pushing-{suffix}", "w").close()
        self.hold(path + ".pushing-live")
        self.assertEqual(self.push(path).relationship, "success")
        self.assertEqual(os.listdir(directory), ["a.tar.pushing-live"])

    def test_the_claimed_tar_is_locked_while_it_is_pushed(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "a.tar")
        open(path, "w").close()
        seen = []

        def probe(client, tar, *rest):
            handle = open(tar, "r+")
            self.addCleanup(handle.close)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except OSError:
                seen.append("locked")
            return 1, 0, []
        original = (push_module.RegistryClient, push_module.push_export)
        push_module.RegistryClient = lambda *args: types.SimpleNamespace(host="registry.test")
        push_module.push_export = probe
        self.addCleanup(lambda: (setattr(push_module, "RegistryClient", original[0]),
                                 setattr(push_module, "push_export", original[1])))
        processor, context = make_push(**{"Input Tar Path": path})
        self.assertEqual(processor.transform(context, FlowFile()).relationship, "success")
        self.assertEqual(seen, ["locked"])


class EnvironmentScope(unittest.TestCase):
    def test_username_comes_from_the_environment_and_the_ssl_service_becomes_der(self):
        os.environ["TEST_PUSH_USER"] = "robot"
        self.addCleanup(os.environ.pop, "TEST_PUSH_USER")
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"Target Registry Username": "${TEST_PUSH_USER}", "SSL Context Service": "ssl",
                                          "Input Tar Path": "/nonexistent.tar"})
        context.services["SSL Context Service"] = ssl_service([b"der-one"])
        processor.transform(context, FlowFile())
        self.assertEqual((seen[0][1], seen[0][3]), ("robot", [b"der-one"]))

    def test_no_ssl_service_means_no_extra_ca(self):
        seen = []
        original = push_module.RegistryClient
        push_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        self.addCleanup(setattr, push_module, "RegistryClient", original)
        processor, context = make_push(**{"Input Tar Path": "/nonexistent.tar"})
        processor.transform(context, FlowFile())
        self.assertEqual(seen[0][3], [])


if __name__ == "__main__":
    unittest.main()
