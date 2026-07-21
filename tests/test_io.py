import json
import tempfile
import unittest
from pathlib import Path

from gp_sql_analyzer.io import JsonlWriter, iter_jsonl_records


class JsonlIoTests(unittest.TestCase):
    def test_reads_weighted_query_records_in_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "query_id": "q64",
                        "query_text": "SELECT * FROM item WHERE i_color = 'purple'",
                        "query_text_template": "SELECT * FROM item WHERE i_color = '&CHARACTER'",
                        "source_row_count": 4,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            batches = list(iter_jsonl_records(path, batch_size=10))

        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][0].query_id, "q64")
        self.assertEqual(batches[0][0].source_row_count, 4)

    def test_jsonl_writer_emits_sorted_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "details.jsonl"
            with JsonlWriter(path) as writer:
                writer.write({"z": "Книги", "a": 1})

            line = path.read_text(encoding="utf-8")

        self.assertEqual(line, '{"a": 1, "z": "Книги"}\n')


if __name__ == "__main__":
    unittest.main()
