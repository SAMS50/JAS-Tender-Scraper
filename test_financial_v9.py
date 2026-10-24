
import unittest

import tender_financial_v9 as v9


class TestFinancialV9(unittest.TestCase):

    def setUp(self):
        self.base = {
            "tender_id": "2026_MCD_294506_1",
            "tender_id_status": "mapped",
        }

    def scan(self, text):
        return v9.scan_text(
            "test.zip",
            self.base,
            "test.pdf",
            "PDF page 1",
            text,
        )

    def test_estimated_value(self):
        result = self.scan(
            "Estimated Cost: Rs. 4,77,621"
        )
        self.assertEqual(
            result[0]["candidate_value"],
            "477621.00",
        )

    def test_emd(self):
        result = self.scan(
            "Earnest Money Deposit: INR 9,098"
        )
        self.assertEqual(
            result[0]["candidate_field"],
            "emd_inr",
        )

    def test_tender_fee(self):
        result = self.scan(
            "Tender Fee: Rs. 500"
        )
        self.assertEqual(
            result[0]["candidate_field"],
            "tender_fee_inr",
        )

    def test_lakh_conversion(self):
        self.assertEqual(
            v9.parse_money("2.5", "lakh"),
            "250000.00",
        )

    def test_crore_conversion(self):
        self.assertEqual(
            v9.parse_money("1.2", "crore"),
            "12000000.00",
        )

    def test_unlabelled_amount_rejected(self):
        self.assertEqual(
            self.scan("Total amount Rs. 50000"),
            [],
        )

    def test_no_invented_amount(self):
        self.assertEqual(
            self.scan("EMD is applicable"),
            [],
        )

    def test_conditional_emd_review(self):
        result = self.scan(
            "EMD for group 1: Rs. 1800000"
        )
        self.assertEqual(
            result[0]["review_status"],
            "CONDITIONAL_REVIEW",
        )

    def test_no_auto_verification(self):
        result = self.scan(
            "Estimated Cost: Rs. 500000"
        )
        self.assertEqual(
            result[0]["review_status"],
            "NEEDS_REVIEW",
        )

    def test_no_boq_total_as_estimate(self):
        self.assertEqual(
            self.scan(
                "BOQ Grand Total: Rs. 900000"
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
