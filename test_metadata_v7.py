
import unittest

import tender_pdf_extractor_v7 as v7


class TestV7(unittest.TestCase):

    def test_shared_circular(self):
        result = v7.classify_document(
            "circular.pdf",
            "253f3751960a772a" + "0" * 48,
        )
        self.assertEqual(result, "shared_circular")

    def test_shared_gcc(self):
        result = v7.classify_document(
            "GCC2024.pdf",
            "03b049531944a625" + "0" * 48,
        )
        self.assertFalse(v7.should_scan(result))

    def test_shared_conditions(self):
        result = v7.classify_document(
            "Conditions.pdf",
            "467343d85e9b47a5" + "0" * 48,
        )
        self.assertFalse(v7.should_scan(result))

    def test_specific_conditions(self):
        result = v7.classify_document(
            "ConditionChhatghat.pdf",
            "a" * 64,
        )
        self.assertEqual(result, "tender_specific")
        self.assertTrue(v7.should_scan(result))

    def test_17_01_pdf(self):
        result = v7.classify_document(
            "17_01.pdf",
            "b" * 64,
        )
        self.assertTrue(v7.should_scan(result))

    def test_false_title_rejected(self):
        title = (
            "Contractual cost: Funding of Project/Scheme: "
            "Name of contractor with mobile number:"
        )
        self.assertFalse(v7.valid_title(title))

    def test_realistic_title_accepted(self):
        title = (
            "Supply of various materials at municipal "
            "store for maintenance of roads"
        )
        self.assertTrue(v7.valid_title(title))

    def test_duplicate_candidates(self):
        candidate = {
            "candidate_field": "organization",
            "candidate_value": "Municipal Corporation of Delhi",
            "review_status": "NEEDS_REVIEW",
        }

        result = v7.filter_candidates(
            [candidate.copy(), candidate.copy()],
            {"organization": ""},
        )

        self.assertEqual(len(result), 1)

    def test_existing_organization_not_repeated(self):
        candidate = {
            "candidate_field": "organization",
            "candidate_value": "Municipal Corporation of Delhi",
            "review_status": "NEEDS_REVIEW",
        }

        result = v7.filter_candidates(
            [candidate],
            {"organization": "Municipal Corporation of Delhi"},
        )

        self.assertEqual(result, [])

    def test_financial_candidate_remains_unverified(self):
        candidate = {
            "candidate_field": "emd_inr",
            "candidate_value": "9098.00",
            "review_status": "NEEDS_REVIEW",
        }

        result = v7.filter_candidates(
            [candidate],
            {"emd_inr": ""},
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0]["review_status"],
            "NEEDS_REVIEW",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
