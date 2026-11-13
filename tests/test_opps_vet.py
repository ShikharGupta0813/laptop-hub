"""Scam/quality check (hub/opps_vet.py): pure functions, true and false positives."""
import datetime as dt
import unittest

from hub import opps_vet as v

NOW = dt.date(2026, 11, 13)


def codes(**kw):
    return {f["code"] for f in v.vet(now=NOW, **kw)["flags"]}


class Scams(unittest.TestCase):
    def test_true_positives_are_high(self):
        table = {
            "upfront_fee": ["Pay a small registration fee to start", "You must buy your own equipment from our vendor",
                            "A refundable deposit is required", "Upfront payment of $50 for your starter kit",
                            "training fee of $99 applies"],
            "wire_or_gift": ["Send payment by wire transfer", "We pay via Western Union", "Buy gift cards and send codes",
                             "Deposit the cheque we mail you and send the rest"],
            "off_platform": ["Contact us only on WhatsApp", "Message HR on Telegram to apply", "Telegram only", "Apply at t.me/hr_jobs"],
            "guaranteed_income": ["Guaranteed income of a lifetime", "100% guaranteed placement", "Make money fast from home",
                                  "No experience needed, earn $300 per day"],
            "early_id": ["Send your passport copy to begin", "Please provide your bank account details to apply",
                         "Share a copy of your driver's license now", "SSN is required"],
        }
        for code, texts in table.items():
            for text in texts:
                r = v.vet(description=text, now=NOW)
                self.assertIn(code, {f["code"] for f in r["flags"]}, text)
                self.assertEqual(r["severity"], "high", text)
                self.assertTrue(v.blocked(r))

    def test_negated_phrases_are_not_flagged(self):
        for text in ("There is no registration fee.", "We never ask you to pay a fee or buy equipment.",
                     "You will not be asked for a training fee", "No upfront payment, ever",
                     "We will collect your passport after you accept the offer",
                     "Without any deposit you can start"):
            self.assertEqual(v.vet(description=text, now=NOW)["severity"], "none", text)

    def test_clean_posts_stay_clean(self):
        for text in ("Senior Python engineer, remote. Build APIs with FastAPI and Postgres. Pay $90-120/hour contract.",
                     "We are a blockchain startup hiring a Solidity developer. Salary in USD.",
                     "Annotator for LLM evaluation, $25 per hour, flexible hours, apply on our website."):
            r = v.vet(title="Engineer", company="Acme Labs", url="https://acme.example.com/jobs/1", description=text, now=NOW)
            self.assertNotEqual(r["severity"], "high", text)
        self.assertEqual(v.vet(description="Backend developer, Python, remote", now=NOW)["severity"], "none")

    def test_crypto_alone_is_not_a_scam_but_crypto_only_pay_is_medium(self):
        self.assertEqual(v.vet(description="Crypto exchange hiring a Go developer", now=NOW)["severity"], "none")
        r = v.vet(description="You will be paid only in USDT", now=NOW)
        self.assertEqual(r["severity"], "medium")
        self.assertFalse(v.blocked(r))

    def test_free_mail_recruiter(self):
        self.assertEqual(v.vet(company="Acme", description="Recruiter: jane@gmail.com", now=NOW)["severity"], "medium")
        r = v.vet(title="Google recruiter", company="Google", contact="hr.google.jobs@gmail.com", now=NOW)
        self.assertEqual(r["severity"], "high")
        self.assertIn("free_mail_brand", {f["code"] for f in r["flags"]})
        # a free address with no company or recruiter claim is just someone's email
        self.assertEqual(v.vet(description="Email me at sam@gmail.com for details", now=NOW)["severity"], "none")

    def test_domain_mismatch(self):
        self.assertIn("domain_mismatch", codes(company="Acme Robotics", contact="hr@totally-different-jobs.com"))
        self.assertNotIn("domain_mismatch", codes(company="Acme Robotics", contact="hr@acme.com"))
        self.assertNotIn("domain_mismatch", codes(company="Acme Robotics", contact="alerts@upwork.com"))
        self.assertIn("domain_mismatch", codes(company="Acme Robotics", url="https://careers-portal.xyz/apply"))
        self.assertIn("cheap_tld", codes(url="https://jobs-now.top/apply"))
        self.assertIn("short_link", codes(url="https://bit.ly/abc"))

    def test_unrealistic_pay(self):
        self.assertIn("unrealistic_pay", codes(description="Earn $900 per hour typing"))
        r = v.vet(description="No experience needed. $5,000 per day from home", now=NOW)
        self.assertEqual(r["severity"], "high")
        self.assertEqual(v.vet(description="$900 per hour", now=NOW)["severity"], "medium")
        self.assertNotIn("unrealistic_pay", codes(description="$120 per hour, $200 a day"))
        self.assertNotIn("unrealistic_pay", codes(description="₹5000 per day"))     # about $62

    def test_urgency_and_new_company(self):
        self.assertEqual(v.vet(description="Apply immediately, limited spots, act now", now=NOW)["severity"], "medium")
        self.assertEqual(v.vet(description="Urgently hiring a designer", now=NOW)["severity"], "low")
        self.assertIn("new_company", codes(description="A recently founded startup"))
        self.assertIn("new_company", codes(description="Founded in 2026, we build tools"))
        self.assertNotIn("new_company", codes(description="Founded in 2012, we build tools"))

    def test_severity_is_the_worst_flag(self):
        r = v.vet(description="Urgently hiring. Pay a registration fee.", now=NOW)
        self.assertEqual(r["severity"], "high")
        self.assertIn("pay or buy", v.reason_text(r))
        self.assertEqual(v.vet(now=NOW), {"flags": [], "severity": "none"})

    def test_helpers(self):
        self.assertEqual(v.host_of("https://www.Example.com/a"), "example.com")
        self.assertEqual(v.host_of("nonsense"), "")
        self.assertEqual(v.pay_amounts("$50 per hour and $2k/week"), [(50.0, "hour"), (2000.0, "week")])


if __name__ == "__main__":
    unittest.main()
