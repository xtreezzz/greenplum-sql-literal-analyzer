import unittest

from gp_sql_analyzer.patterns import classify_pattern


class PatternClassificationTests(unittest.TestCase):
    def test_classifies_like_shapes(self) -> None:
        cases = {
            "purple": "like_exact",
            "purple%": "like_prefix",
            "%purple": "like_suffix",
            "%purple%": "like_contains",
            "pur_le%": "like_complex",
        }

        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(classify_pattern("ILIKE", pattern).family, expected)

    def test_escaped_like_wildcards_are_literal(self) -> None:
        info = classify_pattern("LIKE", r"purple\%")

        self.assertEqual(info.family, "like_exact")

    def test_regex_features_capture_structure_and_case_mode(self) -> None:
        info = classify_pattern("~*", r"^(purple|burlywood)[0-9]+$")

        self.assertEqual(info.family, "regex")
        self.assertTrue(info.regex_features["anchored_start"])
        self.assertTrue(info.regex_features["anchored_end"])
        self.assertTrue(info.regex_features["groups"])
        self.assertTrue(info.regex_features["character_classes"])
        self.assertTrue(info.regex_features["quantifiers"])
        self.assertTrue(info.regex_features["alternation"])
        self.assertTrue(info.regex_features["case_insensitive"])

    def test_similar_to_is_not_merged_with_posix_regex(self) -> None:
        info = classify_pattern("SIMILAR TO", "(purple|blue)%")

        self.assertEqual(info.family, "similar_to")
        self.assertTrue(info.regex_features["groups"])
        self.assertTrue(info.regex_features["alternation"])

    def test_equality_is_an_exact_value(self) -> None:
        self.assertEqual(classify_pattern("=", "College").family, "exact_value")


if __name__ == "__main__":
    unittest.main()
