
import unittest
from decimal import Decimal

import tender_financial_validation_v9_1 as v91


class TestFinancialV911(unittest.TestCase):

    def candidate(
        self,
        field,
        value,
        evidence,
        status="NEEDS_REVIEW",
        zip_name="work_100.zip",
        document="test.pdf",
    ):
        return {
            "zip_filename": zip_name,
            "tender_id": "2026_MCD_294506_1",
            "candidate_field": field,
            "candidate_value": str(value),
            "candidate_basis": "explicit_label",
            "review_status": status,
            "document": document,
            "location": "PDF page 1",
            "evidence": evidence,
        }

    def metadata(self):
        return {
            "zip_filename": "work_100.zip",
            "tender_id": "2026_MCD_294506_1",
            "tender_id_status": "mapped",
            "estimated_value_inr": "",
            "emd_inr": "",
            "tender_fee_inr": "",
        }

    def test_valid_estimate(self):
        c = self.candidate(
            "estimated_value_inr",
            "4471306.00",
            "Estimated Cost: Rs. 44,71,306/-",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "SOURCE_BACKED",
        )

    def test_valid_emd(self):
        c = self.candidate(
            "emd_inr",
            "200000.00",
            "Bid Security / EMD | Rs. 2,00,000/-",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "SOURCE_BACKED",
        )

    def test_valid_fee(self):
        c = self.candidate(
            "tender_fee_inr",
            "1180.00",
            "Tender Fee: Rs. 1,180/-",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "SOURCE_BACKED",
        )

    def test_stamp_paper_rejected(self):
        c = self.candidate(
            "emd_inr",
            "50.00",
            "EMD shall be accompanied with a "
            "Letter of Undertaking on non-judicial "
            "paper of Rs. 50/-",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "REJECTED",
        )

    def test_annexure_page_rejected(self):
        c = self.candidate(
            "emd_inr",
            "38.00",
            "ANNEXURE – B : Bank Guarantee Format EMD | 38",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "REJECTED",
        )

    def test_section_number_rejected(self):
        c = self.candidate(
            "emd_inr",
            "17.10",
            "17.0 | BID SECURITY / "
            "EARNEST MONEY DEPOSIT | 17.1",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "REJECTED",
        )

    def test_eligibility_threshold_rejected(self):
        c = self.candidate(
            "estimated_value_inr",
            "1341000.00",
            "Turnover should be at least 30% of "
            "the estimated cost, i.e. Rs. 13.41 Lakhs.",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "REJECTED",
        )

    def test_general_clause_rejected(self):
        c = self.candidate(
            "estimated_value_inr",
            "1500000.00",
            "For works having estimated cost "
            "more than Rs. 15 lacs - Clause 6A",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "REJECTED",
        )

    def test_group_emd_held(self):
        c = self.candidate(
            "emd_inr",
            "1800000.00",
            "EMD for Group 1: Rs. 18.00 lakhs "
            "(for Indian Bidders)",
            "CONDITIONAL_REVIEW",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "CONDITIONAL_REVIEW",
        )

    def test_category_emd_held(self):
        c = self.candidate(
            "emd_inr",
            "150000.00",
            "EMD: Rs. 1.5 lakh for Diamond category "
            "territories",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "CONDITIONAL_REVIEW",
        )

    def test_multiple_period_estimate_held(self):
        c = self.candidate(
            "estimated_value_inr",
            "2828575.20",
            "Estimated cost for two year is "
            "Rs. 28,28,575.20 without GST",
        )
        self.assertEqual(
            v91.classify_candidate(c)[0],
            "BASIS_REVIEW",
        )

    def test_existing_value_preserved(self):
        row = self.metadata()
        row["emd_inr"] = "5000.00"

        c = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )

        output, _ = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "5000.00",
        )

    def test_missing_identity_blocks(self):
        row = self.metadata()
        row["tender_id"] = ""
        row["tender_id_status"] = "missing_tender_id"

        c = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )

        output, _ = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "",
        )

    def test_conflicting_amounts_held(self):
        first = self.candidate(
            "emd_inr",
            "10000.00",
            "EMD: Rs. 10,000",
        )
        second = self.candidate(
            "emd_inr",
            "20000.00",
            "EMD: Rs. 20,000",
        )

        output, _ = v91.validate_and_promote(
            [self.metadata()],
            [first, second],
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "",
        )

    def test_duplicate_amounts_allowed(self):
        first = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )
        second = dict(first)
        second["document"] = "second.pdf"

        output, _ = v91.validate_and_promote(
            [self.metadata()],
            [first, second],
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "200000.00",
        )

    def test_conditional_blocks_scalar(self):
        ordinary = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )
        conditional = self.candidate(
            "emd_inr",
            "1800000.00",
            "EMD for Group 1: Rs. 18.00 lakhs",
            "CONDITIONAL_REVIEW",
        )

        output, _ = v91.validate_and_promote(
            [self.metadata()],
            [ordinary, conditional],
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "",
        )

    def test_existing_matching_value_confirmed(self):
        row = self.metadata()
        row["emd_inr"] = "200000.00"

        c = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )

        _, reviews = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            reviews[0]["decision"],
            "EXISTING_VALUE_CONFIRMED",
        )

    def test_existing_conflicting_value_flagged(self):
        row = self.metadata()
        row["emd_inr"] = "5000.00"

        c = self.candidate(
            "emd_inr",
            "200000.00",
            "EMD: Rs. 2,00,000",
        )

        output, reviews = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            output[0]["emd_inr"],
            "5000.00",
        )
        self.assertEqual(
            reviews[0]["decision"],
            "EXISTING_VALUE_CONFLICT",
        )

    def test_rejected_candidate_not_masked(self):
        row = self.metadata()
        row["emd_inr"] = "228000.00"

        c = self.candidate(
            "emd_inr",
            "17.10",
            "17.0 | BID SECURITY / "
            "EARNEST MONEY DEPOSIT | 17.1",
        )

        _, reviews = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            reviews[0]["decision"],
            "REJECTED",
        )

    def test_conditional_candidate_not_masked(self):
        row = self.metadata()
        row["emd_inr"] = "2000000.00"

        c = self.candidate(
            "emd_inr",
            "1800000.00",
            "EMD for Group 1: Rs. 18.00 lakhs",
        )

        _, reviews = v91.validate_and_promote(
            [row], [c]
        )

        self.assertEqual(
            reviews[0]["decision"],
            "CONDITIONAL_REVIEW",
        )

    def test_dvc_basis_annotation(self):
        row = self.metadata()
        row["zip_filename"] = "work_343524.zip"
        row["estimated_value_inr"] = "2828575.20"

        c = self.candidate(
            "estimated_value_inr",
            "2828575.20",
            "Estimated cost for two year is "
            "Rs. 28,28,575.20 without GST",
            zip_name="work_343524.zip",
            document="ANNEXURE-BOQ_PriceBreak-up.pdf",
        )

        output, _ = v91.validate_and_promote(
            [row], [c]
        )

        v91.annotate_dvc_basis(
            output, [c]
        )

        self.assertIn(
            "TWO_YEARS_EXCLUDING_GST",
            output[0][
                "v9_1_financial_provenance_json"
            ],
        )

    def test_dvc_basis_not_invented(self):
        row = self.metadata()
        row["zip_filename"] = "work_343524.zip"
        row["estimated_value_inr"] = "2828575.20"

        output, _ = v91.validate_and_promote(
            [row], []
        )

        v91.annotate_dvc_basis(
            output, []
        )

        self.assertNotIn(
            "TWO_YEARS_EXCLUDING_GST",
            output[0][
                "v9_1_financial_provenance_json"
            ],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
