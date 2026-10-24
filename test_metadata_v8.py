
import unittest

import tender_boq_metadata_v8 as v8


class TestV8(unittest.TestCase):

    def setUp(self):
        self.base = {
            "tender_id": "",
            "tender_id_status": "missing_tender_id",
        }

    def extract(self, rows):
        return v8.extract_sheet_candidates(
            "test.zip",
            self.base,
            "BOQ_test.xls",
            "BoQ1",
            rows,
        )

    def test_inline_work_title(self):
        rows = [[
            "Name of Work: Repair and maintenance "
            "of municipal roads in Delhi"
        ]]
        result = self.extract(rows)
        self.assertEqual(len(result), 1)

    def test_adjacent_work_title(self):
        rows = [[
            "Name of Work",
            "Construction of boundary wall "
            "at municipal primary school",
        ]]
        result = self.extract(rows)
        self.assertEqual(len(result), 1)

    def test_next_row_title(self):
        rows = [
            ["Name of Work", ""],
            [
                "Supply and installation of "
                "street lighting equipment",
                "",
            ],
        ]
        result = self.extract(rows)
        self.assertEqual(len(result), 1)

    def test_generic_header_rejected(self):
        self.assertEqual(
            self.extract([
                ["Item No", "Description", "Quantity"],
                [1, "Supplying coarse sand", 50],
            ]),
            [],
        )

    def test_item_not_tender_title(self):
        result = self.extract([
            [
                "Item No",
                "Description",
                "Quantity",
            ],
            [
                1,
                "Supply of stone aggregate "
                "of nominal size 10 mm",
                25,
            ],
        ])
        self.assertEqual(result, [])

    def test_boilerplate_title_rejected(self):
        self.assertFalse(
            v8.plausible_title(
                "General Conditions of Contract"
            )
        )

    def test_short_title_rejected(self):
        self.assertFalse(
            v8.plausible_title("Road work")
        )

    def test_candidate_requires_review(self):
        result = self.extract([[
            "Name of Work: Construction and "
            "repair of municipal drainage system"
        ]])
        self.assertEqual(
            result[0]["review_status"],
            "NEEDS_REVIEW",
        )

    def test_no_fabricated_id(self):
        result = self.extract([[
            "Name of Work: Construction and "
            "repair of municipal drainage system"
        ]])
        self.assertEqual(result[0]["tender_id"], "")

    def test_duplicate_work_title(self):
        title = (
            "Name of Work: Construction and "
            "repair of municipal drainage system"
        )
        result = self.extract([
            [title],
            [title],
        ])
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
