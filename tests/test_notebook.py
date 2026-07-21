import unittest
from pathlib import Path

import nbformat


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "sql_catalog_from_dataframe.ipynb"


class DataFrameNotebookTests(unittest.TestCase):
    def test_notebook_is_executed_without_errors_and_keeps_html_optional(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        source = "\n".join(cell.source for cell in notebook.cells)
        errors = [
            output
            for cell in notebook.cells
            if cell.cell_type == "code"
            for output in cell.get("outputs", [])
            if output.get("output_type") == "error"
        ]

        self.assertFalse(errors)
        self.assertTrue(all(cell.get("id") for cell in notebook.cells))
        self.assertIn("row_analysis_df = result.row_analysis_df", source)
        self.assertIn("aggregate_df = result.aggregate_df", source)
        self.assertIn("build_html=False", source)
        self.assertIn("analytics.query_log", source)
        self.assertNotIn("GP_PASSWORD =", source)


if __name__ == "__main__":
    unittest.main()
