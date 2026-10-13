"""The export tar: write_export, push_export and plan_image against an in-memory registry."""
import os
import tempfile
import unittest

import nifi_stub  # noqa: F401
import fake_registry
from fake_registry import FakeRegistry, digest

import container_registry_client as c


def export(source, images, directory=None):
    """images: [(repository, tag, digest)] -> path of the tar write_export made from source."""
    path = os.path.join(directory or tempfile.mkdtemp(), "export.tar")
    entries = [(repository, tag, root, repository, *c.plan_image(source, repository, root))
               for repository, tag, root in images]
    c.write_export(source, path, entries)
    return path


class Collisions(unittest.TestCase):
    def test_two_images_rewritten_to_one_tag_are_refused_before_any_upload(self):
        source = FakeRegistry()
        first = source.add_image("a/app", "v1", "a")
        second = source.add_image("b/app", "v1", "b")
        path = export(source, [("a/app", "v1", first), ("b/app", "v1", second)])
        target = FakeRegistry()
        with self.assertRaises(c.RegistryError) as raised:
            c.push_export(target, path, rewrite="merged", move_tags=False)
        self.assertIn("merged/app:v1", str(raised.exception))
        self.assertEqual((target.blobs, target.puts), ({}, []), "something was uploaded before the refusal")

    def test_collision_is_refused_with_move_tags_too(self):
        source = FakeRegistry()
        first = source.add_image("a/app", "v1", "a")
        second = source.add_image("b/app", "v1", "b")
        path = export(source, [("a/app", "v1", first), ("b/app", "v1", second)])
        with self.assertRaises(c.RegistryError):
            c.push_export(FakeRegistry(), path, rewrite="merged", move_tags=True)

    def test_the_same_digest_listed_twice_is_not_a_collision(self):
        source = FakeRegistry()
        first = source.add_image("a/app", "v1", "a")
        source.manifests[("b/app", "v1")] = source.manifests[("a/app", "v1")]
        for key, data in list(source.blobs.items()):
            source.blobs[("b/app", key[1])] = data
        source.manifests[("b/app", first)] = source.manifests[("a/app", first)]
        path = export(source, [("a/app", "v1", first), ("b/app", "v1", first)])
        pushed, present, _ = c.push_export(FakeRegistry(), path, rewrite="merged")
        self.assertEqual(pushed + present, 1)


class OptionalUrls(unittest.TestCase):
    URLS = ["https://example.invalid/optional-mirror"]

    def test_an_ordinary_layer_with_urls_is_copied(self):
        source = FakeRegistry()
        root = source.add_image("url/app", "v1", "url-layer", urls=self.URLS)
        layer = digest(b"url-layer")
        plan = c.plan_image(source, "url/app", root)
        self.assertIn(layer, plan[2])
        target = FakeRegistry()
        c.push_export(target, export(source, [("url/app", "v1", root)]), rewrite="url-target")
        self.assertTrue(target.blob_exists("url-target/app", layer))

    def test_a_foreign_layer_with_urls_is_left_to_its_urls(self):
        source = FakeRegistry()
        foreign = "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"
        root = source.add_image("url/app", "v1", "windows-base", urls=self.URLS, layer_type=foreign)
        self.assertNotIn(digest(b"windows-base"), c.plan_image(source, "url/app", root)[2])


class NestedIndexes(unittest.TestCase):
    def nested(self, source, depth):
        leaf = source.add_image("nested/app", "v1", "nested")
        top, media = leaf, fake_registry.MANIFEST
        chain = [leaf]
        for n in range(depth):
            top = source.add_index("nested/app", "v1" if n == depth - 1 else None, [(top, media)])
            media = fake_registry.INDEX
            chain.append(top)
        return leaf, chain

    def test_every_level_is_exported_and_pushed_children_first(self):
        source = FakeRegistry()
        leaf, chain = self.nested(source, 2)
        root = chain[-1]
        plan = c.plan_image(source, "nested/app", root)
        self.assertEqual(list(plan[1]), [leaf, chain[1]])
        self.assertEqual(len(plan[2]), 2, "the leaf's config and layer")
        path = export(source, [("nested/app", "v1", root)])
        target = FakeRegistry()
        pushed, present, _ = c.push_export(target, path, rewrite="nested-target")
        self.assertEqual((pushed, present), (1, 0))
        self.assertEqual([ref for _repo, ref in target.puts], [leaf, chain[1], "v1"])
        for blob in plan[2]:
            self.assertTrue(target.blob_exists("nested-target/app", blob))
        self.assertEqual(target.manifest_digest("nested-target/app", "v1"), root)

    def test_an_index_nested_deeper_than_supported_fails_the_plan(self):
        source = FakeRegistry()
        _leaf, chain = self.nested(source, c.MAX_INDEX_DEPTH + 2)
        with self.assertRaises(c.RegistryError):
            c.plan_image(source, "nested/app", chain[-1])


if __name__ == "__main__":
    unittest.main()
