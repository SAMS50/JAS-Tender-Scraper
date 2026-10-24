
import unittest

import tender_boq_promotion_v8_1 as v81


class TestPromotion(unittest.TestCase):

    def setUp(self):
        self.row = {
            "zip_filename": "work_100.zip",
            "tender_id": "2026_MCD_294506_1",
            "tender_id_status": "mapped",
            "tender_title": "",
            "title_source": "",
        }

        self.candidate = {
            "zip_filename": "work_100.zip",
            "candidate_field": "tender_title",
            "candidate_value":
                "Construction of municipal drainage system",
            "candidate_type": "explicit_label_inline",
            "review_status": "NEEDS_REVIEW",
            "boq_filename": "BOQ_100.xls",
            "sheet": "BoQ1",
            "row_number": "4",
            "evidence":
                "Name of Work: Construction of municipal drainage system",
        }

    def run_promotion(self, row=None, candidates=None):
        return v81.promote(
            [row if row is not None else self.row],
            candidates if candidates is not None
            else [self.candidate],
        )

    def test_valid_promotion(self):
        rows, _ = self.run_promotion()
        self.assertEqual(
            rows[0]["v8_1_title_promotion_status"],
            "PROMOTED_SOURCE_BACKED",
        )

    def test_title_filled(self):
        rows, _ = self.run_promotion()
        self.assertEqual(
            rows[0]["tender_title"],
            "Construction of municipal drainage system",
        )

    def test_existing_title_protected(self):
        row = dict(self.row)
        row["tender_title"] = "Existing title"

        rows, _ = self.run_promotion(row)
        self.assertEqual(
            rows[0]["tender_title"],
            "Existing title",
        )

    def test_missing_id_blocks_promotion(self):
        row = dict(self.row)
        row["tender_id"] = ""
        row["tender_id_status"] = "missing_tender_id"

        rows, _ = self.run_promotion(row)
        self.assertEqual(rows[0]["tender_title"], "")
        self.assertEqual(
            rows[0]["v8_1_title_promotion_status"],
            "HELD_IDENTITY_REVIEW",
        )

    def test_unmapped_status_blocks(self):
        row = dict(self.row)
        row["tender_id_status"] = "inferred"

        rows, _ = self.run_promotion(row)
        self.assertEqual(rows[0]["tender_title"], "")

    def test_conflicting_titles_blocked(self):
        second = dict(self.candidate)
        second["candidate_value"] = (
            "Construction of municipal boundary wall"
        )
        second["evidence"] = (
            "Name of Work: Construction of municipal boundary wall"
        )

        rows, _ = self.run_promotion(
            candidates=[self.candidate, second]
        )
        self.assertEqual(rows[0]["tender_title"], "")

    def test_missing_evidence_blocks(self):
        candidate = dict(self.candidate)
        candidate["evidence"] = ""

        rows, _ = self.run_promotion(
            candidates=[candidate]
        )
        self.assertEqual(rows[0]["tender_title"], "")

    def test_item_description_blocks(self):
        candidate = dict(self.candidate)
        candidate["candidate_type"] = (
            "boq_item_description"
        )

        rows, _ = self.run_promotion(
            candidates=[candidate]
        )
        self.assertEqual(rows[0]["tender_title"], "")

    def test_original_id_preserved(self):
        rows, _ = self.run_promotion()
        self.assertEqual(
            rows[0]["tender_id"],
            self.row["tender_id"],
        )

    def test_source_recorded(self):
        rows, _ = self.run_promotion()
        self.assertEqual(
            rows[0]["v8_1_title_source"],
            "BOQ_NAME_OF_WORK",
        )

    def test_verification_not_claimed(self):
        rows, _ = self.run_promotion()
        self.assertEqual(
            rows[0]["v8_1_title_verification"],
            "SOURCE_BACKED_PENDING_PORTAL_VERIFICATION",
        )

    def test_no_candidate(self):
        rows, _ = self.run_promotion(
            candidates=[]
        )
        self.assertEqual(
            rows[0]["v8_1_title_promotion_status"],
            "NO_ACTIONABLE_CANDIDATE",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
