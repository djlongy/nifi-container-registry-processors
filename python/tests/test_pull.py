"""PullContainerImages: what it emits, and what its state says it emitted."""
import hashlib
import os
import tempfile
import time
import unittest
import unittest.mock

import nifi_stub
from fake_registry import FakeRegistry

import container_registry_client as client
import PullContainerImages as pull_module
from container_registry_client import RegistryError
from PullContainerImages import PullContainerImages


def make_pull(registry, directory, values=None, state=None, **dynamic):
    pull_module.RegistryClient = lambda *args, **kwargs: registry
    processor = PullContainerImages()
    processor.logger = nifi_stub.Logger()
    settings = {"Source Registry URL": "https://registry.test", "Image Source": "Image List", "Image List": "team/app:1.0",
                "Tar Output Directory": directory}
    settings.update(values or {})
    context = nifi_stub.FakeContext(PullContainerImages.PROPERTIES, settings, dynamic, state)
    return processor, context


class StateMapContract(unittest.TestCase):
    """NiFi 2.11 hands create() a nifiapi.componentstate.StateMap: get, toMap and getStateVersion only."""
    def test_the_stub_state_map_has_only_the_nifi_methods(self):
        state_map = nifi_stub.FakeStateManager().getState("CLUSTER")
        self.assertFalse(hasattr(state_map, "__contains__") or hasattr(state_map, "__iter__")
                         or hasattr(state_map, "items"))

    def test_load_state_reads_a_state_map(self):
        exported = {"team/app:1.0": "0123456789abcdef"}
        manager = nifi_stub.FakeStateManager()
        manager.setState(pull_module.dump_state(exported), "CLUSTER")
        self.assertEqual(pull_module.load_state(manager.getState("CLUSTER")), exported)
        self.assertIsNone(pull_module.load_state(nifi_stub.FakeStateManager().getState("CLUSTER")))

    def test_create_runs_and_the_next_run_sees_the_state(self):
        registry = FakeRegistry()
        registry.add_image("team/app", "1.0", "one")
        processor, context = make_pull(registry, tempfile.mkdtemp())
        self.assertIsNotNone(processor.create(context))
        self.assertIn("team/app:1.0", pull_module.load_state(context.state.getState("CLUSTER")))
        self.assertIsNone(processor.create(context))


class Leftover(unittest.TestCase):
    """NiFi 2.11 commits the FlowFile after create() returns, so a tar the state already counts can be left behind."""
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.registry = FakeRegistry()
        self.registry.add_image("team/app", "1.0", "one")

    def test_uncommitted_result_is_emitted_again(self):
        processor, context = make_pull(self.registry, self.directory)
        first = processor.create(context)  # NiFi never committed this FlowFile
        self.assertIsNotNone(first)
        tar = os.path.join(self.directory, first.attributes["filename"])
        self.assertTrue(os.path.exists(tar))
        self.assertIsNone(processor.create(context), "a fresh tar is left to FetchFile")
        old = time.time() - 2 * pull_module.LEFTOVER_AFTER
        os.utime(tar, (old, old))
        again = processor.create(context)
        self.assertIsNotNone(again)
        self.assertEqual(again.attributes["filename"], first.attributes["filename"])
        self.assertEqual(again.attributes["X-Sha256"], first.attributes["X-Sha256"])
        self.assertEqual(again.attributes["registry.images"], first.attributes["registry.images"])
        self.assertEqual(again.attributes["file.size"], first.attributes["file.size"])

    def test_fetched_tar_is_not_emitted_again(self):
        processor, context = make_pull(self.registry, self.directory)
        first = processor.create(context)
        os.remove(os.path.join(self.directory, first.attributes["filename"]))  # FetchFile deleted it
        self.assertIsNone(processor.create(context))

    def test_partial_and_foreign_files_are_ignored(self):
        processor, context = make_pull(self.registry, self.directory)
        processor.create(context)
        for name in (".container-images-x.tar.partial", "other.tar", "container-images-y.tar.pushing"):
            path = os.path.join(self.directory, name)
            open(path, "wb").close()
            os.utime(path, (1, 1))
        self.assertIsNone(processor.create(context))


class Failover(unittest.TestCase):
    """The tar of a lost FlowFile is on the dead node's disk; the cluster state has to say what it held."""
    def setUp(self):
        self.registry = FakeRegistry()
        self.registry.add_image("team/app", "1.0", "one")
        self.state = nifi_stub.FakeStateManager()
        self.node_a, self.node_b = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.a, self.ca = make_pull(self.registry, self.node_a, state=self.state)
        self.b, self.cb = make_pull(self.registry, self.node_b, state=self.state)

    def later(self, seconds):
        return unittest.mock.patch("PullContainerImages.time.time", return_value=time.time() + seconds)

    def test_a_new_primary_exports_what_the_lost_node_never_delivered(self):
        lost = self.a.create(self.ca)  # node A dies before NiFi commits this result
        self.assertIsNotNone(lost)
        with self.later(2 * pull_module.LEFTOVER_AFTER):
            recovered = self.b.create(self.cb)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.attributes["registry.images"], lost.attributes["registry.images"])
        self.assertEqual(recovered.attributes["absolute.path"], self.node_b + "/")
        self.assertTrue(os.path.isfile(os.path.join(self.node_b, recovered.attributes["filename"])))
        self.assertIn("never fetched", self.b.logger.text())

    def test_a_lost_export_survives_a_registry_error_on_the_new_primary(self):
        self.assertIsNotNone(self.a.create(self.ca))  # node A dies before NiFi commits this result
        outage = unittest.mock.patch("PullContainerImages.plan_image", side_effect=RegistryError("HTTP 503"))
        with self.later(2 * pull_module.LEFTOVER_AFTER), outage:
            self.assertIsNone(self.b.create(self.cb))  # the registry is briefly down while B takes over
        with self.later(2 * pull_module.LEFTOVER_AFTER + 900):
            recovered = self.b.create(self.cb)  # the next run, the registry is back
        self.assertIsNotNone(recovered, "team/app:1.0 is recorded as exported but no node ever delivered it")
        self.assertIn("team/app:1.0@", recovered.attributes["registry.images"])

    def test_an_image_that_cannot_be_planned_is_not_lost_from_a_partly_recovered_export(self):
        other = self.registry.add_image("team/other", "1.0", "two")
        values = {"Image List": "team/app:1.0\nteam/other:1.0"}
        a, ca = make_pull(self.registry, self.node_a, values, self.state)
        b, cb = make_pull(self.registry, self.node_b, values, self.state)
        self.assertEqual(len(a.create(ca).attributes["registry.images"].splitlines()), 2)
        real_plan = pull_module.plan_image

        def plan(client, repository, digest):
            if repository == "team/other":
                raise RegistryError("HTTP 503")
            return real_plan(client, repository, digest)
        with self.later(2 * pull_module.LEFTOVER_AFTER), unittest.mock.patch("PullContainerImages.plan_image", plan):
            partial = b.create(cb)
        self.assertEqual([line.split("@")[0] for line in partial.attributes["registry.images"].splitlines()],
                         ["team/app:1.0"])
        os.remove(os.path.join(self.node_b, partial.attributes["filename"]))  # FetchFile took it
        with self.later(2 * pull_module.LEFTOVER_AFTER + 900):
            rest = b.create(cb)
        self.assertEqual(rest.attributes["registry.images"], f"team/other:1.0@{other}")

    def test_the_recovery_is_itself_recoverable(self):
        self.a.create(self.ca)
        node_c = tempfile.mkdtemp()
        c, cc = make_pull(self.registry, node_c, state=self.state)
        with self.later(2 * pull_module.LEFTOVER_AFTER):
            self.assertIsNotNone(self.b.create(self.cb))  # node B dies too
        with self.later(4 * pull_module.LEFTOVER_AFTER):
            self.assertIsNotNone(c.create(cc))

    def test_a_young_export_of_another_node_is_left_alone(self):
        self.a.create(self.ca)
        with self.later(60):
            self.assertIsNone(self.b.create(self.cb))

    def test_a_fetched_export_is_forgotten_by_the_node_that_wrote_it(self):
        first = self.a.create(self.ca)
        self.assertEqual(len(pull_module.load_pending(self.state.getState("CLUSTER"))), 1)
        os.remove(os.path.join(self.node_a, first.attributes["filename"]))  # FetchFile took it
        self.assertIsNone(self.a.create(self.ca))
        self.assertEqual(pull_module.load_pending(self.state.getState("CLUSTER")), [])
        with self.later(2 * pull_module.LEFTOVER_AFTER):
            self.assertIsNone(self.b.create(self.cb), "a delivered export was exported again")

    def test_an_image_a_later_export_moved_on_is_not_exported_again(self):
        first = self.a.create(self.ca)
        moved = self.registry.add_image("team/app", "1.0", "two")
        second = self.a.create(self.ca)  # the first tar is still on node A's disk
        self.assertEqual(second.attributes["registry.images"].split("@")[1], moved)
        with self.later(2 * pull_module.LEFTOVER_AFTER):
            recovered = self.b.create(self.cb)
        self.assertEqual(recovered.attributes["registry.images"], second.attributes["registry.images"])
        self.assertNotEqual(recovered.attributes["registry.images"], first.attributes["registry.images"])


class Collision(unittest.TestCase):
    def test_two_images_rewritten_to_one_tag_are_not_exported_together(self):
        registry = FakeRegistry()
        registry.add_image("a/app", "v1", "a")
        registry.add_image("b/app", "v1", "b")
        processor, context = make_pull(registry, tempfile.mkdtemp(), {
            "Image List": "a/app:v1\nb/app:v1", "Rewrite Target Base Path": "merged"})
        result = processor.create(context)
        self.assertEqual(len(result.attributes["registry.images"].splitlines()), 1)
        self.assertIn("already ends at merged/app:v1", processor.logger.text())


class CollisionAcrossRuns(unittest.TestCase):
    """Push refuses a whole tar over one tag that moves, so Pull must not produce such a tar in a later run either."""
    def setUp(self):
        self.registry = FakeRegistry()
        self.a = self.registry.add_image("a/app", "v1", "a")
        self.registry.add_image("b/app", "v1", "b")
        self.directory = tempfile.mkdtemp()
        self.processor, self.context = make_pull(self.registry, self.directory, {
            "Image List": "a/app:v1\nb/app:v1", "Rewrite Target Base Path": "merged"})

    def test_the_second_run_does_not_export_the_colliding_image(self):
        first = self.processor.create(self.context)
        self.assertEqual(first.attributes["registry.images"], f"merged/app:v1@{self.a}")
        os.remove(os.path.join(self.directory, first.attributes["filename"]))  # FetchFile took it
        self.assertIsNone(self.processor.create(self.context))
        self.assertIn("a/app:v1 was already exported to merged/app:v1", self.processor.logger.text())
        self.assertNotIn("b/app:v1", pull_module.load_state(self.context.state.getState("CLUSTER")))

    def test_unrelated_images_still_go_and_push_accepts_every_tar(self):
        self.registry.add_image("c/other", "v2", "c")
        tar1 = os.path.join(self.directory, self.processor.create(self.context).attributes["filename"])
        self.context.values["Image List"] = "a/app:v1\nb/app:v1\nc/other:v2"
        second = self.processor.create(self.context)
        self.assertEqual(second.attributes["registry.images"].split("@")[0], "merged/other:v2")
        target = FakeRegistry()
        client.push_export(target, tar1)
        client.push_export(target, os.path.join(self.directory, second.attributes["filename"]))

    def test_the_same_source_may_move_its_own_tag(self):
        first = self.processor.create(self.context)
        os.remove(os.path.join(self.directory, first.attributes["filename"]))
        moved = self.registry.add_image("a/app", "v1", "a2")
        again = self.processor.create(self.context)
        self.assertEqual(again.attributes["registry.images"], f"merged/app:v1@{moved}")


class EnvironmentScope(unittest.TestCase):
    """NiFi 2.11 resolves a plain ${NAME} from the attribute map alone, so the environment must be in that map."""
    def test_properties_can_name_environment_variables(self):
        registry = FakeRegistry()
        registry.add_image("team/app", "1.0", "one")
        os.environ["TEST_PULL_ENV_LIST"] = "team/app:1.0"
        os.environ["TEST_PULL_ENV_DIR"] = tempfile.mkdtemp()
        self.addCleanup(os.environ.pop, "TEST_PULL_ENV_LIST")
        self.addCleanup(os.environ.pop, "TEST_PULL_ENV_DIR")
        processor, context = make_pull(registry, "${TEST_PULL_ENV_DIR}", {"Image List": "${TEST_PULL_ENV_LIST}"})
        result = processor.create(context)
        self.assertEqual(result.attributes["registry.images"].split("@")[0], "team/app:1.0")
        self.assertEqual(result.attributes["absolute.path"], os.environ["TEST_PULL_ENV_DIR"] + "/")

    def test_text_after_an_environment_reference_is_kept(self):
        base = tempfile.mkdtemp()
        os.environ["TEST_PULL_ENV_BASE"] = base
        self.addCleanup(os.environ.pop, "TEST_PULL_ENV_BASE")
        registry = FakeRegistry()
        registry.add_image("team/app", "1.0", "one")
        processor, context = make_pull(registry, "${TEST_PULL_ENV_BASE}/exports/${TEST_PULL_ENV_BASE_MISSING}out")
        result = processor.create(context)
        self.assertEqual(result.attributes["absolute.path"], base + "/exports/out/")
        self.assertTrue(os.path.isfile(os.path.join(base, "exports", "out", result.attributes["filename"])))

    def test_an_api_base_path_after_an_environment_reference_is_kept(self):
        os.environ["TEST_PULL_ENV_HOST"] = "https://artifactory.low"
        self.addCleanup(os.environ.pop, "TEST_PULL_ENV_HOST")
        seen = []
        pull_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        processor = PullContainerImages()
        processor.logger = nifi_stub.Logger()
        context = nifi_stub.FakeContext(PullContainerImages.PROPERTIES, {
            "Source Registry URL": "${TEST_PULL_ENV_HOST}/artifactory/api/docker/repo", "Tar Output Directory": tempfile.mkdtemp()})
        with self.assertRaises(RuntimeError):
            processor.create(context)
        self.assertEqual(seen[0][0], "https://artifactory.low/artifactory/api/docker/repo")

    def test_the_registry_url_comes_from_the_environment(self):
        os.environ["TEST_PULL_ENV_URL"] = "http://registry.low:5000"
        self.addCleanup(os.environ.pop, "TEST_PULL_ENV_URL")
        seen = []
        pull_module.RegistryClient = lambda *args: seen.append(args) or (_ for _ in ()).throw(RuntimeError("stop"))
        processor = PullContainerImages()
        processor.logger = nifi_stub.Logger()
        context = nifi_stub.FakeContext(PullContainerImages.PROPERTIES, {
            "Source Registry URL": "${TEST_PULL_ENV_URL}", "Tar Output Directory": tempfile.mkdtemp()})
        with self.assertRaises(RuntimeError):
            processor.create(context)
        self.assertEqual(seen[0][0], "http://registry.low:5000")


class StateSize(unittest.TestCase):
    """ZooKeeperStateProvider stores each state value with writeUTF, which cannot take more than 65535 bytes."""
    def tags(self, count):
        return {f"team/app{n // 50}:v{n}": hashlib.sha256(str(n).encode()).hexdigest()[:16] for n in range(count)}

    def test_large_state_is_stored_in_values_the_provider_accepts(self):
        exported = self.tags(6000)
        stored = pull_module.dump_state(exported)
        self.assertGreater(sum(len(v) for v in stored.values()), nifi_stub.WRITE_UTF_LIMIT)
        manager = nifi_stub.FakeStateManager()
        manager.setState(stored, "CLUSTER")  # raises like writeUTF when a value is too long
        self.assertEqual(pull_module.load_state(manager.getState("CLUSTER")), exported)

    def test_state_in_the_old_single_value_still_loads(self):
        exported = self.tags(10)
        stored = pull_module.dump_state(exported)
        legacy = {pull_module.STATE_KEY: "".join(stored[k] for k in sorted(stored))}
        self.assertEqual(pull_module.load_state(legacy), exported)
        self.assertIsNone(pull_module.load_state({}))

    def test_state_over_the_limit_fails_before_a_tar_is_written(self):
        directory = tempfile.mkdtemp()
        registry = FakeRegistry()
        registry.add_image("team/app", "1.0", "one")
        processor, context = make_pull(registry, directory)
        written = []
        original, limit = pull_module.write_export, pull_module.STATE_LIMIT
        pull_module.write_export = lambda *args: written.append(args)
        pull_module.STATE_LIMIT = 10
        try:
            with self.assertRaises(RegistryError):
                processor.create(context)
        finally:
            pull_module.write_export, pull_module.STATE_LIMIT = original, limit
        self.assertEqual(written, [], "the export ran although its state cannot be stored")
        self.assertEqual(os.listdir(directory), [])

    def test_a_pending_record_costs_no_more_per_tag_than_the_exported_map(self):
        registry = FakeRegistry()
        names = [f"team{n % 7}/service-{n}" for n in range(300)]
        for name in names:
            registry.add_image(name, "1.0", name)
        processor, context = make_pull(registry, tempfile.mkdtemp(), {"Image List": "\n".join(f"{n}:1.0" for n in names)})
        self.assertIsNotNone(processor.create(context))
        snapshot = context.state.getState("CLUSTER")
        record = pull_module.load_pending(snapshot)[0]
        self.assertEqual(len(record["images"]), 300)
        self.assertTrue(all(len(digest) == 16 for _r, _t, digest in record["images"]))
        self.assertLess(len(pull_module._pack(record["images"])), 1.2 * len(pull_module._pack(pull_module.load_state(snapshot))))

    def test_the_state_error_names_max_export_size(self):
        exported = {f"team/app{n}:v{n}": hashlib.sha256(str(n).encode()).hexdigest()[:16] for n in range(60_000)}
        with self.assertRaises(RegistryError) as raised:
            pull_module.dump_state(exported)
        self.assertIn("Max Export Size", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
