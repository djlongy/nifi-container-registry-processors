"""nifi_attributes.evaluate against the NiFi 2.11 shortcut for a value that starts with ${NAME}."""
import unittest
import unittest.mock

import nifi_stub
from nifi_attributes import Attributes, evaluate


def value(raw):
    return nifi_stub.Value(raw, True, environment={})


class Evaluate(unittest.TestCase):
    def test_a_function_embedded_in_text_is_left_to_java(self):
        self.assertEqual(evaluate(value("/exports/${hostname()}"), Attributes()), "/exports/nifi-node-a")

    def test_a_function_alone_is_left_to_java(self):
        self.assertEqual(evaluate(value("${hostname()}"), Attributes()), "nifi-node-a")

    def test_a_plain_reference_followed_by_a_function_keeps_the_rest(self):
        text = evaluate(value("${BASE}/${TAG:toLower()}"), Attributes({"BASE": "/exports", "TAG": "IMAGE"}))
        self.assertEqual(text, "/exports/image")

    def test_a_plain_reference_followed_by_a_subject_function_keeps_the_rest(self):
        self.assertEqual(evaluate(value("${BASE:toLower()}/x"), Attributes({"BASE": "/EXPORTS"})), "/exports/x")

    def test_plain_references_and_text_are_resolved_whole(self):
        self.assertEqual(evaluate(value("${BASE}/exports/${MISSING}out"), Attributes({"BASE": "/data"})),
                         "/data/exports/out")

    def test_a_plain_reference_falls_back_to_the_environment(self):
        with unittest.mock.patch.dict("os.environ", {"FROM_ENV": "env-value"}):
            self.assertEqual(evaluate(value("${FROM_ENV}/x"), Attributes()), "env-value/x")

    def test_text_without_expressions_is_returned_as_it_is(self):
        self.assertEqual(evaluate(value("/exports"), Attributes()), "/exports")


if __name__ == "__main__":
    unittest.main()
