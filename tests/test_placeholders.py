import unittest

from gp_sql_analyzer.placeholders import extract_placeholder_values


class PlaceholderExtractionTests(unittest.TestCase):
    def test_extracts_exact_character_value(self) -> None:
        matches = extract_placeholder_values("&CHARACTER", "Advanced Degree")

        self.assertEqual([match.value for match in matches], ["Advanced Degree"])
        self.assertTrue(matches[0].matched)

    def test_extracts_value_inside_like_mask(self) -> None:
        matches = extract_placeholder_values("%&CHARACTER%", "%ought%")

        self.assertEqual([match.value for match in matches], ["ought"])

    def test_extracts_multiple_values_without_greedy_overlap(self) -> None:
        matches = extract_placeholder_values(
            "^(&CHARACTER)-(&CHARACTER)$", "^(catalog)-(returns)$"
        )

        self.assertEqual([match.value for match in matches], ["catalog", "returns"])

    def test_treats_regex_metacharacters_in_template_as_fixed_text(self) -> None:
        matches = extract_placeholder_values(
            "^(?:&CHARACTER)[0-9]+$", "^(?:item)[0-9]+$"
        )

        self.assertEqual([match.value for match in matches], ["item"])

    def test_falls_back_to_full_literal_when_fixed_parts_do_not_align(self) -> None:
        matches = extract_placeholder_values("%&CHARACTER%", "purple")

        self.assertEqual([match.value for match in matches], ["purple"])
        self.assertFalse(matches[0].matched)


if __name__ == "__main__":
    unittest.main()
