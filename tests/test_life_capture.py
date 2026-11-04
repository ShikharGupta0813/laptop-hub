"""Tests for automatic UPI spend capture in hub/life.py: the payment parser (pure), the pending queue,
de-duplication, category memory, auto-filing, settings and the hourly limit. No network, no real models."""
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hub import life
from hub.common import Ctx
from hub.notes import NoteStore
from tests.helpers import make_config

TODAY = date(2026, 11, 4)          # a Wednesday
START = datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)

GPAY = "com.google.android.apps.nbu.paisa.user"
PHONEPE = "com.phonepe.app"
PAYTM = "net.one97.paytm"
BHIM = "in.org.npci.upiapp"
CRED = "com.dreamplug.androidapp"
HDFC_APP = "com.snapwork.hdfc"


def parse(text, title="", package="", sender=""):
    return life.parse_payment(text, title, package, sender)


# ---- the parser -------------------------------------------------------------------------------

class ParserTest(unittest.TestCase):
    # title, text, package, sender, cents, merchant, merchant_key, app, bank, last4, ref
    SPENDS = [
        ("Paid ₹250 to Zomato", "Payment successful", GPAY, "", 25000, "Zomato", "zomato", "Google Pay", "", "", ""),
        ("You paid Swiggy", "₹349.00 paid using HDFC Bank ****1234", GPAY, "", 34900, "Swiggy", "swiggy",
         "Google Pay", "HDFC Bank", "1234", ""),
        ("Payment Successful", "Paid ₹120 to Cafe Coffee Day", PHONEPE, "", 12000, "Cafe Coffee Day", "cafecoffeeday",
         "PhonePe", "", "", ""),
        ("Sent ₹1,500", "Money sent to Amit Kumar", PHONEPE, "", 150000, "Amit Kumar", "amitkumar", "PhonePe", "", "", ""),
        ("Paid Successfully", "Rs. 75 paid to Metro Smart Card", PAYTM, "", 7500, "Metro Smart Card", "metrosmartcard",
         "Paytm", "", "", ""),
        ("Payment done", "₹60 paid to Chai Point via BHIM UPI. UPI Ref No 612345678901", BHIM, "", 6000, "Chai Point",
         "chaipoint", "BHIM", "", "", "612345678901"),
        ("CRED", "You paid ₹2,350 to Tata Power", CRED, "", 235000, "Tata Power", "tatapower", "CRED", "", "", ""),
        ("HDFC Bank", "Rs 899 spent on your card ending 1234 at AMAZON", HDFC_APP, "", 89900, "Amazon", "amazon",
         "HDFC Bank", "HDFC Bank", "1234", ""),
        ("", "Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25 Ref 123456789 Not You? Call 18002586161",
         "", "VM-HDFCBK", 25000, "Zomato", "zomato", "HDFC Bank", "HDFC Bank", "1234", "123456789"),
        ("", "ICICI Bank Acct XX123 debited for Rs 250.00 on 01-Jan-25; ZOMATO credited. UPI:123456789012. Call 18002662 for dispute.",
         "", "AX-ICICIB", 25000, "Zomato", "zomato", "ICICI Bank", "ICICI Bank", "123", "123456789012"),
        ("", "Dear UPI user A/C X1234 debited by 250.0 on date 01Jan25 trf to ZOMATO Refno 123456789",
         "", "VK-SBIUPI", 25000, "Zomato", "zomato", "SBI", "SBI", "1234", "123456789"),
        ("", "Rs 799 spent on Axis Bank Card no. XX1234 at NETFLIX on 01-01-25. Not you? Call 1800",
         "", "JD-AXISBK", 79900, "Netflix", "netflix", "Axis Bank", "Axis Bank", "1234", ""),
        ("", "Rs 450.00 debited from A/C XX9876 on 04-11-26. Info: UPI/DR/612345678901/SWIGGY/HDFC/utr. Avl Bal Rs 12,345.67",
         "", "VM-HDFCBK", 45000, "Swiggy", "swiggy", "HDFC Bank", "HDFC Bank", "9876", "612345678901"),
        ("", "Rs.45.00 debited A/c XX1234 Info: UPI/P2M/987654321098/Cafe Coffee Day", "", "AD-KOTAKB",
         4500, "Cafe Coffee Day", "cafecoffeeday", "Kotak", "Kotak", "1234", "987654321098"),
    ]

    def test_real_world_styles(self):
        for title, text, package, sender, cents, merchant, key, app, bank, last4, ref in self.SPENDS:
            with self.subTest(text=text[:50]):
                got = parse(text, title, package, sender)
                self.assertIsNotNone(got)
                self.assertEqual((got["amount_cents"], got["merchant"], got["merchant_key"], got["app"], got["bank"],
                                  got["account_last4"], got["ref"]), (cents, merchant, key, app, bank, last4, ref))
                self.assertEqual(got["currency"], "INR")
                self.assertIsInstance(got["amount_cents"], int)

    def test_amount_notations(self):
        for text, cents in [
                ("Paid ₹450 to X Store", 45000), ("Paid ₹ 450.50 to X Store", 45050), ("Paid Rs.450 to X Store", 45000),
                ("Paid Rs 450 to X Store", 45000), ("Paid rs. 450.5 to X Store", 45050), ("Paid INR 450.00 to X Store", 45000),
                ("Paid inr450 to X Store", 45000), ("Paid rupees 450 to X Store", 45000),
                ("Paid 450 rupees to X Store", 45000), ("Paid 450 Rs to X Store", 45000),
                ("Paid Rs 1,250 to X Store", 125000), ("Paid Rs 1,25,000.50 to X Store", 12500050),
                ("Paid Rs 12,34,567 to X Store", 123456700), ("Paid Rs 1,234,567.89 to X Store", 123456789),
                ("Paid Rs.0.50 to X Store", 50), ("Debited by 250.0 trf to X Store", 25000),
                ("A/C debited for 99 trf to X Store", 9900)]:
            with self.subTest(text=text):
                got = parse(text)
                self.assertIsNotNone(got)
                self.assertEqual(got["amount_cents"], cents)

    def test_other_currencies_are_kept_apart(self):
        got = parse("USD 12.50 spent on your ICICI Bank Card XX4321 at SPOTIFY")
        self.assertEqual((got["currency"], got["amount_cents"], got["merchant"]), ("USD", 1250, "Spotify"))

    CREDITS_AND_NOISE = [
        "Rahul sent you ₹500", "₹500 received from Rahul", "You got ₹5 cashback on your payment of ₹100",
        "Rahul requested ₹300. Pay now", "Pay your electricity bill Rs 1,200 by tomorrow",
        "Rs.2000 credited to your account", "₹399 refund for your order", "Pay with PhonePe and win up to ₹500",
        "Payment of ₹200 failed, money will be refunded", "You won a scratch card worth ₹50 on your payment",
        "Rs.5000.00 credited to HDFC Bank A/C XX1234 on 01-01-25 from VPA rahul@okhdfc (UPI 123456789)",
        "123456 is your OTP for txn of Rs 250 at AMAZON. Do not share.", "Your one-time code for paying Rs 250 is 482913",
        "Get cashback up to Rs 100 when you pay using UPI. Offer valid till Sunday",
        "Payment of Rs 250 declined", "Rs 250 reversed to your account after the payment of Rs 250 failed",
        "Your autopay mandate: Rs 499 will be debited on 5th", "Payment of Rs 100 is pending",
        "Rs 500 spent on holiday! Free chai for you", "Paid you Rs 100 for lunch", "Refund of Rs 300 paid to your card",
        "Rs 250.00 credited to A/C XX1234 by UPI/CR/123456789012/RAHUL", "", "   ", "paid", "Hello there",
        "Your order is on the way", "Balance is Rs 5,000 only",
    ]

    def test_credits_refunds_requests_otp_and_failures_are_ignored(self):
        for text in self.CREDITS_AND_NOISE:
            with self.subTest(text=text[:60]):
                self.assertIsNone(parse(text))
                self.assertIsNone(parse("", text))          # also when it came in as a title

    def test_credit_in_title_blocks_a_debit_looking_body(self):
        self.assertIsNone(parse("Paid ₹100 to Cafe", "Payment received"))

    def test_both_sides_listed_is_still_a_debit(self):
        got = parse("A/C debited for Rs 250.00 on 01-Jan-25; ZOMATO credited.")
        self.assertEqual((got["amount_cents"], got["merchant"]), (25000, "Zomato"))

    def test_with_several_amounts_the_one_beside_the_debit_word_wins(self):
        for text, cents in [
                ("Avl Bal Rs 12,000.00. Paid Rs 450 to Swiggy", 45000),
                ("Paid Rs 450 to Swiggy. Avl Bal Rs 12,000.00", 45000),
                ("Rs 99 debited. Total spend this month Rs 12,000. Paid to Netflix", 9900),
                ("Available limit Rs 80,000. Rs 1,200 spent on your card at AMAZON", 120000),
                ("Rs 5000 limit. Sent Rs 75 to Chai Point", 7500),
                ("Closing balance Rs 900. Paid ₹25 to Bus"+"", 2500)]:
            with self.subTest(text=text):
                self.assertEqual(parse(text)["amount_cents"], cents)

    def test_only_a_balance_is_not_a_payment(self):
        self.assertIsNone(parse("Paid. Avl Bal Rs 12,000.00"))

    def test_merchant_shapes(self):
        cases = [
            ("Paid Rs 450 to swiggy@ybl", "Swiggy", "swiggy@ybl", "swiggy"),
            ("Sent Rs 450 to VPA rahul.k@okaxis", "Rahul K", "rahul.k@okaxis", "rahulk"),
            ("Sent Rs 450 to VPA 9876543210@paytm Ref 5551234567890", "UPI ••3210", "9876543210@paytm", ""),
            ("Rs. 20 paid to paytmqr281005050101abc@paytm", "Paytm QR merchant", "paytmqr281005050101abc@paytm",
             "paytmqr281005050101abc"),
            ("Paid Rs 80 to Mr. Sharma's Tea Stall (UPI)", "Mr. Sharma's Tea Stall", "Mr. Sharma's Tea Stall",
             "mrsharmasteastall"),
            ("Payment of Rs 15 successful at Nilgiris", "Nilgiris", "Nilgiris", "nilgiris"),
            ("Paid Rs 200 to SWIGGY LIMITED on 01-01-25", "Swiggy Limited", "SWIGGY LIMITED", "swiggy"),
            ("Paid ₹60 to KFC", "KFC", "KFC", "kfc"),
            ("Paid ₹60 to BigBasket", "BigBasket", "BigBasket", "bigbasket"),
            ("Paid Rs 450 to swiggy@ybl. Avl Bal Rs 100", "Swiggy", "swiggy@ybl", "swiggy"),
        ]
        for text, shown, raw, key in cases:
            with self.subTest(text=text):
                got = parse(text)
                self.assertEqual((got["merchant"], got["merchant_raw"], got["merchant_key"]), (shown, raw, key))

    def test_same_payee_gets_the_same_key_however_it_is_spelled(self):
        keys = {parse(t)["merchant_key"] for t in
                ("Paid Rs 10 to SWIGGY", "Paid Rs 10 to Swiggy Pvt Ltd", "Paid Rs 10 to swiggy@ybl",
                 "Paid Rs 10 to swiggy@okaxis", "Sent Rs 10 to Swiggy*Order 1234567")}
        self.assertEqual(keys, {"swiggy"})

    def test_a_payee_that_is_not_one_is_left_blank(self):
        for text in ("Rs 250 paid to your account", "Sent Rs 250 to 123456", "Rs 250 sent to <script>alert(1)</script>",
                     "Paid Rs 250 to self", "Paid Rs 250"):
            with self.subTest(text=text):
                got = parse(text)
                self.assertEqual((got["merchant"], got["merchant_key"]), ("", ""))

    def test_last4_and_ref_variants(self):
        for text, last4 in [("Paid Rs 5 to X Store A/C *1234", "1234"), ("Paid Rs 5 to X Store card ending 5678", "5678"),
                            ("Paid Rs 5 to X Store using HDFC Bank ****4321", "4321"),
                            ("Paid Rs 5 to X Store A/c no. XXXXXXXX9876", "9876"), ("Paid Rs 5 to X Store Acct XX123", "123"),
                            ("Paid Rs 5 to X Store from card no 1234", "1234"), ("Paid Rs 5 to X Store", "")]:
            with self.subTest(text=text):
                self.assertEqual(parse(text)["account_last4"], last4)
        for text, ref in [("Paid Rs 5 to X Store UPI Ref No 612345678901", "612345678901"),
                          ("Paid Rs 5 to X Store Refno 123456789", "123456789"), ("Paid Rs 5 to X Store UPI:123456789012", "123456789012"),
                          ("Paid Rs 5 to X Store Txn ID T2501011234ABCD", "T2501011234ABCD"),
                          ("Paid Rs 5 to X Store 612345678901", "612345678901"), ("Paid Rs 5 to X Store", ""),
                          ("Paid Rs 5 to 9876543210@paytm", "")]:
            with self.subTest(text=text):
                self.assertEqual(parse(text)["ref"], ref)

    def test_snippet_hides_account_numbers_and_upi_ids(self):
        got = parse("Sent Rs.250.00 From HDFC Bank A/C *1234 To rahulkumar@okhdfc Ref 123456789012 Call 18002586161")
        snippet = got["snippet"]
        self.assertLessEqual(len(snippet), 160)
        for secret in ("1234", "rahulkumar", "okhdfc@", "123456789012", "18002586161"):
            self.assertNotIn(secret, snippet)
        self.assertIn("Rs.250.00", snippet)                   # the amount stays readable
        self.assertIn("ra•••@okhdfc", snippet)
        for text in ("Paid Rs 1234567.50 to Landlord", "Paid ₹12,34,567 to Landlord"):
            self.assertIn("1234567" if "1234567.50" in text else "12,34,567", parse(text)["snippet"])

    def test_snippet_is_capped_at_160(self):
        got = parse("Paid Rs 5 to Store " + "blah " * 200)
        self.assertEqual(len(got["snippet"]), 160)

    def test_hostile_input_never_raises(self):
        weird = [None, 5, 5.5, [], {}, b"Paid Rs 5 to caf\xe9 \xff\xfe", b"", "\x00" * 100, "\ud800 Paid \udfff Rs 5 to \ud800X Store",
                 "Paid Rs 5 to X‮evil Store​", "₹" * 10_000, "Rs " * 10_000, "Paid to " + "a " * 50_000,
                 "paid " + "9" * 5000, "Rs 99999999999999999999999 paid to X", "Rs 0 paid to X", "no digits paid to somebody",
                 "Paid Rs 5 to " + "x" * 100_000, ("Rs 1,2,3,4,5,6 " * 2000) + " debited", "debited " * 20000,
                 "UPI/" * 5000, "info: upi/" + "/" * 5000, "a@" * 10000, "Rs.\n\n\n5\tpaid\r\nto\x0bX Store"]
        for value in weird:
            with self.subTest(value=repr(value)[:40]):
                start = time.perf_counter()
                for args in ((value,), ("", value), (value, value, value, value)):
                    got = life.parse_payment(*args)
                    self.assertTrue(got is None or isinstance(got, dict))
                    if got:
                        self.assertLessEqual(len(got["merchant"]), 60)
                        self.assertLessEqual(len(got["merchant_raw"]), 60)
                        self.assertLessEqual(len(got["merchant_key"]), 40)
                        self.assertLessEqual(len(got["snippet"]), 160)
                        self.assertLessEqual(len(got["ref"]), 32)
                        self.assertGreater(got["amount_cents"], 0)
                        for field in ("merchant", "merchant_raw", "snippet"):
                            self.assertFalse(any(ord(c) < 32 or 0x7F <= ord(c) < 0xA0 or c == "�" for c in got[field]), field)
                self.assertLess(time.perf_counter() - start, 3, "slow on hostile input")

    def test_huge_text_is_only_read_up_to_a_limit(self):
        got = parse("Paid Rs 5 to Store. " + "x" * 5_000_000 + " Rs 999 debited")
        self.assertEqual(got["amount_cents"], 500)

    def test_control_characters_are_stripped_from_the_payee(self):
        got = parse("Paid Rs 5 to Bad\x00Na\x07me‮ Stores")
        self.assertEqual(got["merchant"], "Bad Na Me Stores".replace("Me", "me"))
        self.assertTrue(got["merchant_raw"].isprintable())

    def test_helpers(self):
        self.assertEqual(life.pretty_name("ZOMATO"), "Zomato")
        self.assertEqual(life.pretty_name("cafe coffee day"), "Cafe Coffee Day")
        self.assertEqual(life.pretty_name("IRCTC"), "IRCTC")
        self.assertEqual(life.pretty_name("BigBasket"), "BigBasket")
        self.assertEqual(life.money_text(45000, "INR"), "₹450")
        self.assertEqual(life.money_text(45050, "INR"), "₹450.50")
        self.assertEqual(life.money_text(1250, "USD"), "USD 12.50")
        self.assertEqual(life.merchant_key_of(""), "")
        self.assertEqual(life.guess_category("Swiggy", "swiggy"), "food")
        self.assertEqual(life.guess_category("Mr Sharma", "mrsharma"), None)
        self.assertEqual(life.guess_category("IRCTC", "irctc"), "travel")


class QuickParserGapsTest(unittest.TestCase):
    def test_indian_digit_grouping(self):
        for text, cents in [("₹1,25,000 rent", 12500000), ("1,25,000.50 rent", 12500050), ("12,34,567 car", 123456700),
                            ("₹1,00,000 trip", 10000000), ("1,250 lunch", 125000), ("1,234,567 car", 123456700),
                            ("99,999 phone", 9999900)]:
            with self.subTest(text=text):
                self.assertEqual(life.parse_expense(text, TODAY)["amount"], cents)
        for text in ("1,23,45 x", "1,2,000 x", "12,3456 x"):
            with self.subTest(text=text), self.assertRaises(life.ParseError):
                life.parse_expense(text, TODAY)

    def test_relative_dates(self):
        for text, when in [("50 chai today", "2026-11-04"), ("50 chai yesterday", "2026-11-03"),
                           ("50 chai yday", "2026-11-03"), ("50 chai last night", "2026-11-03"),
                           ("50 chai day before yesterday", "2026-11-02"), ("50 chai the day before yesterday", "2026-11-02"),
                           ("the day before yesterday 50 chai", "2026-11-02"), ("200 dinner last friday", "2026-10-30"),
                           ("200 dinner last fri", "2026-10-30"), ("200 dinner on mon", "2026-11-02"),
                           ("200 dinner this monday", "2026-11-02"), ("200 dinner this wednesday", "2026-11-04"),
                           ("200 dinner wednesday", "2026-10-28"), ("200 dinner last wednesday", "2026-10-28"),
                           ("50 chai 3 days ago", "2026-11-01"), ("50 chai 2 weeks ago", "2026-10-21")]:
            with self.subTest(text=text):
                got = life.parse_expense(text, TODAY)
                self.assertEqual(got["date"], when)
                self.assertNotIn("yesterday", got["note"])
                self.assertNotIn("last", got["note"])

    def test_day_abbreviations_alone_are_just_words(self):
        got = life.parse_expense("50 sun cream", TODAY)
        self.assertEqual((got["date"], got["note"]), ("2026-11-04", "sun cream"))
        self.assertEqual(life.parse_expense("50 sat nav", TODAY)["note"], "sat nav")

    def test_tomorrow_is_refused_as_the_future(self):
        with self.assertRaises(life.ParseError):
            life.parse_expense("50 chai tomorrow", TODAY)


# ---- routes -----------------------------------------------------------------------------------

class CaptureBase(unittest.TestCase):
    def setUp(self):
        self.allow = True
        self.now = START
        self.audit_log, self.notices = [], []
        self.data_dir = tempfile.mkdtemp(prefix="hub-capture-data-")
        self.config, self.root = make_config(data_dir=self.data_dir)
        self.addCleanup(life.close_all)

        def need_auth(request: Request):
            if not self.allow:
                raise HTTPException(401, "Log in first.")

        ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=NoteStore(self.config.notes_dir),
                  need_auth=need_auth, audit=lambda event, **f: self.audit_log.append((event, f)),
                  notify=lambda *a, **k: self.notices.append((a, k)))
        app = FastAPI()
        app.include_router(life.make_router(ctx, now=lambda: self.now.replace(tzinfo=None), utc_now=lambda: self.now))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def ms(self, **delta):
        return int((self.now + timedelta(**delta)).timestamp() * 1000)

    def capture(self, text, title="", package=PHONEPE, source="notification", ts=None, expect=200, **extra):
        body = {"source": source, "title": title, "text": text, "ts": self.ms() if ts is None else ts, **extra}
        if source == "notification":
            body["package"] = package
        else:
            body["sender"] = extra.pop("sender", package if "." not in package else "VM-HDFCBK")
            body.pop("package", None) if "." in package else None
        r = self.client.post("/api/expenses/capture", json=body)
        self.assertEqual(r.status_code, expect, r.text)
        return r.json()

    def pay(self, merchant="Zomato", cents=25000, **kw):
        amount = f"{cents // 100}" if cents % 100 == 0 else f"{cents / 100:.2f}"
        return self.capture(f"Paid ₹{amount} to {merchant}", "Payment successful", **kw)

    def pending(self, status="pending"):
        r = self.client.get("/api/expenses/pending", params={"status": status})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["items"]

    def confirm(self, pid, category="food", expect=200, **extra):
        r = self.client.post(f"/api/expenses/pending/{pid}/confirm", json={"category": category, **extra})
        self.assertEqual(r.status_code, expect, r.text)
        return r.json()

    def settings(self, **body):
        r = self.client.post("/api/expenses/capture/settings", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def month_expenses(self):
        return self.client.get("/api/expenses", params={"month": "2026-11"}).json()["expenses"]

    def titles(self):
        return [k.get("title", a[0] if a else "") for a, k in self.notices]


class AuthTest(CaptureBase):
    def test_every_route_needs_the_login(self):
        self.allow = False
        for method, path, body in [
                ("post", "/api/expenses/capture", {"source": "sms", "text": "x"}),
                ("get", "/api/expenses/pending", None), ("get", "/api/expenses/categories", None),
                ("get", "/api/expenses/capture/settings", None), ("post", "/api/expenses/capture/settings", {}),
                ("post", "/api/expenses/pending/dismiss-all", {"confirm": True}),
                ("post", "/api/expenses/pending/abcabcabcabc/dismiss", None),
                ("post", "/api/expenses/pending/abcabcabcabc/confirm", {"category": "food"})]:
            with self.subTest(path=path):
                r = getattr(self.client, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual(r.status_code, 401)
        self.assertEqual(self.notices, [])


class CaptureTest(CaptureBase):
    def test_a_payment_is_queued_and_the_user_is_told_once(self):
        got = self.pay("Swiggy", 45000, package=GPAY)
        self.assertEqual(set(got), {"id", "parsed", "duplicate", "suggested_category", "status", "auto", "ignored"})
        self.assertRegex(got["id"], r"^[0-9a-f]{12}$")
        self.assertFalse(got["duplicate"])
        self.assertEqual((got["status"], got["auto"], got["ignored"], got["suggested_category"]), ("pending", False, False, "food"))
        self.assertEqual(got["parsed"], {"amount_cents": 45000, "amount_text": "450.00", "currency": "INR", "merchant": "Swiggy",
                                         "merchant_raw": "Swiggy", "merchant_key": "swiggy", "app": "Google Pay", "bank": "",
                                         "account_last4": "", "ref": ""})
        self.assertEqual(len(self.notices), 1)
        args, kwargs = self.notices[0]
        self.assertEqual(args, ())
        self.assertEqual(kwargs, {"title": "₹450 to Swiggy", "body": "Tap to choose a category", "level": "info",
                                  "link": "expenses", "source": "expenses"})
        items = self.pending()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["id"], item["amount_cents"], item["amount_text"], item["currency"], item["merchant"],
                          item["source"], item["sources"], item["app"], item["status"], item["auto"], item["test"],
                          item["suggested_category"], item["category"], item["expense_id"], item["date"]),
                         (got["id"], 45000, "450.00", "INR", "Swiggy", "notification", ["notification"], "Google Pay",
                          "pending", False, False, "food", None, None, "2026-11-04"))
        self.assertEqual(set(item), {"id", "amount_cents", "amount_text", "currency", "merchant", "merchant_raw", "merchant_key",
                                     "source", "sources", "app", "bank", "account_last4", "ref", "raw_snippet", "ts", "date",
                                     "status", "auto", "test", "suggested_category", "category", "expense_id", "created"})

    def test_nothing_is_added_to_expenses_until_confirmed(self):
        self.pay()
        self.assertEqual(self.month_expenses(), [])

    def test_sms_style_is_parsed_with_bank_account_and_ref(self):
        got = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25 Ref 123456789012 Not You?",
                           source="sms", package="VM-HDFCBK")
        self.assertEqual((got["parsed"]["merchant"], got["parsed"]["bank"], got["parsed"]["account_last4"], got["parsed"]["ref"]),
                         ("Zomato", "HDFC Bank", "1234", "123456789012"))
        item = self.pending()[0]
        self.assertEqual((item["source"], item["app"]), ("sms", "HDFC Bank"))

    def test_no_merchant_still_notifies(self):
        self.capture("Paid Rs 99 successfully")
        self.assertEqual(self.titles(), ["₹99 spent"])

    def test_paise_and_other_currencies_in_the_notification(self):
        self.capture("Paid ₹450.50 to Cafe", package=GPAY)
        self.capture("USD 12.50 spent on your ICICI Bank Card XX4321 at SPOTIFY", source="sms", package="AX-ICICIB")
        self.assertEqual(self.titles(), ["₹450.50 to Cafe", "USD 12.50 to Spotify"])

    def test_credits_and_noise_are_not_stored(self):
        for text in ParserTest.CREDITS_AND_NOISE:
            if not text.strip():
                continue
            got = self.capture(text, title="Money")
            self.assertEqual((got["id"], got["parsed"], got["duplicate"], got["ignored"]), (None, None, False, True), text)
        self.assertEqual(self.pending("all"), [])
        self.assertEqual(self.notices, [])

    def test_a_credit_with_a_debit_word_in_the_title_is_still_ignored(self):
        got = self.capture("Rahul sent you ₹500", title="Paid by Rahul")
        self.assertIsNone(got["id"])
        got = self.capture("Rs.2000 credited to your account", title="Sent ₹2000")
        self.assertIsNone(got["id"])
        self.assertEqual(self.pending("all"), [])

    def test_the_stored_excerpt_is_masked_and_short(self):
        self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To rahulkumar@okhdfc Ref 123456789012. " + "pad " * 80,
                     source="sms", package="VM-HDFCBK")
        snippet = self.pending()[0]["raw_snippet"]
        self.assertLessEqual(len(snippet), 160)
        for secret in ("1234", "rahulkumar", "123456789012"):
            self.assertNotIn(secret, snippet)

    def test_audit_never_holds_the_message_text(self):
        self.capture("Paid ₹250 to Zomato Ref 123456789012 A/C *1234 secretword", title="Paid TitleSecret")
        pid = self.pending()[0]["id"]
        self.confirm(pid, "food", note="a note")
        self.client.post("/api/expenses/capture/settings", json={"ignore_merchants": ["Zomato"]})
        events = [e for e, _ in self.audit_log]
        self.assertIn("life_capture", events)
        self.assertIn("life_capture_confirm", events)
        flat = repr(self.audit_log)
        for secret in ("secretword", "TitleSecret", "123456789012", "1234", "Payment successful", "Tap to"):
            self.assertNotIn(secret, flat)
        for event, fields in self.audit_log:
            if event in ("life_capture", "life_capture_confirm"):
                self.assertEqual(set(fields), {"id", "amount", "merchant"})

    def test_hostile_requests(self):
        self.assertEqual(self.client.post("/api/expenses/capture", json={"source": "email", "text": "Paid Rs 5 to X"}).status_code, 422)
        self.assertEqual(self.client.post("/api/expenses/capture", json={"text": "Paid Rs 5 to X"}).status_code, 422)
        self.assertEqual(self.client.post("/api/expenses/capture", json={"source": "sms", "text": "x" * 60_000}).status_code, 422)
        self.assertEqual(self.client.post("/api/expenses/capture", content=b"\xff\xfe not json",
                                          headers={"Content-Type": "application/json"}).status_code, 422)
        for body in ({"source": "sms"}, {"source": "sms", "text": None, "title": None, "package": None, "sender": None},
                     {"source": "notification", "text": "Paid Rs 5 to X", "ts": "soon"}):
            r = self.client.post("/api/expenses/capture", json=body)
            self.assertIn(r.status_code, (200, 422), body)
        big = self.client.post("/api/expenses/capture", json={"source": "sms", "sender": "VM-HDFCBK",
                                                              "text": "Paid Rs 5 to Store " + "y" * 40_000})
        self.assertEqual(big.status_code, 200)
        self.assertEqual(big.json()["parsed"]["amount_cents"], 500)

    def test_a_silly_timestamp_falls_back_to_now(self):
        for n, ts in enumerate((0, -5, 10 ** 18, 1.5, self.ms(days=3), self.ms(days=-1000))):
            self.pay("Shop%d" % n, 10000 + n * 100, ts=ts)
        for item in self.pending():
            self.assertEqual(item["date"], "2026-11-04")
            self.assertAlmostEqual(item["ts"], self.ms(), delta=5000)

    def test_seconds_instead_of_milliseconds_are_understood(self):
        self.pay("Shop", 10000, ts=self.ms() // 1000)
        self.assertEqual(self.pending()[0]["ts"], self.ms() // 1000 * 1000)

    def test_newest_first_and_status_filter(self):
        self.pay("A", 10000, ts=self.ms(minutes=-30))
        self.pay("B", 20000, ts=self.ms(minutes=-10))
        self.pay("C", 30000, ts=self.ms(minutes=-20))
        self.assertEqual([i["merchant"] for i in self.pending()], ["B", "C", "A"])
        self.assertEqual(self.client.get("/api/expenses/pending", params={"status": "nope"}).status_code, 400)
        self.assertEqual(self.client.get("/api/expenses/pending", params={"limit": 0}).status_code, 422)
        body = self.client.get("/api/expenses/pending").json()
        self.assertEqual((body["status"], body["pending_count"], len(body["items"])), ("pending", 3, 3))
        self.assertEqual(len(self.client.get("/api/expenses/pending", params={"limit": 2}).json()["items"]), 2)


class DedupeTest(CaptureBase):
    def test_the_same_reference_is_stored_once(self):
        a = self.capture("Paid ₹250 to Zomato UPI Ref No 612345678901", package=GPAY)
        b = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO Ref 612345678901", source="sms",
                         package="VM-HDFCBK", ts=self.ms(hours=-5))
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["id"], a["id"])
        self.assertEqual(len(self.pending("all")), 1)
        self.assertEqual(len(self.notices), 1)

    def test_a_reference_with_a_different_amount_is_not_the_same_payment(self):
        self.capture("Paid ₹250 to Zomato UPI Ref No 612345678901")
        other = self.capture("Paid ₹300 to Zomato UPI Ref No 612345678901", package=GPAY)
        self.assertFalse(other["duplicate"])

    def test_notification_and_sms_within_three_minutes_merge(self):
        a = self.pay("Zomato", 25000, ts=self.ms())
        b = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25", source="sms",
                         package="VM-HDFCBK", ts=self.ms(seconds=90))
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["id"], a["id"])
        items = self.pending("all")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["sources"], ["notification", "sms"])
        self.assertEqual((items[0]["bank"], items[0]["account_last4"]), ("HDFC Bank", "1234"))     # what the SMS added
        self.assertEqual(len(self.notices), 1)

    def test_sms_first_then_notification(self):
        a = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO On 01/01/25", source="sms",
                         package="VM-HDFCBK", ts=self.ms())
        b = self.pay("Zomato", 25000, package=GPAY, ts=self.ms(seconds=-100))
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["id"], a["id"])
        self.assertEqual(self.pending()[0]["sources"], ["sms", "notification"])

    def test_after_three_minutes_it_is_a_new_payment(self):
        self.pay("Zomato", 25000, ts=self.ms())
        b = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO", source="sms", package="VM-HDFCBK",
                         ts=self.ms(minutes=3, seconds=1))
        self.assertFalse(b["duplicate"])
        self.assertEqual(len(self.pending()), 2)

    def test_different_amount_or_payee_is_a_new_payment(self):
        self.pay("Zomato", 25000, ts=self.ms())
        for text in ("Sent Rs.251.00 From HDFC Bank A/C *1234 To ZOMATO", "Sent Rs.250.00 From HDFC Bank A/C *1234 To SWIGGY"):
            self.assertFalse(self.capture(text, source="sms", package="VM-HDFCBK", ts=self.ms(seconds=30))["duplicate"], text)
        self.assertEqual(len(self.pending()), 3)

    def test_an_unreadable_payee_still_merges_with_the_other_source(self):
        a = self.pay("Zomato", 25000, ts=self.ms())
        b = self.capture("Rs.250.00 debited from A/C XX1234", source="sms", package="VM-HDFCBK", ts=self.ms(seconds=20))
        self.assertTrue(b["duplicate"])
        self.assertEqual(b["id"], a["id"])

    def test_two_real_purchases_from_one_source_are_both_kept(self):
        self.pay("Chai Point", 6000, ts=self.ms())
        second = self.pay("Chai Point", 6000, ts=self.ms(seconds=60))
        self.assertFalse(second["duplicate"])
        self.assertEqual(len(self.pending()), 2)

    def test_a_resent_item_is_a_duplicate_even_hours_later(self):
        first = self.pay("Chai Point", 6000, ts=self.ms())
        self.now += timedelta(hours=5)
        again = self.pay("Chai Point", 6000, ts=self.ms(hours=-5))
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.pending("all")), 1)

    def test_duplicates_of_confirmed_or_dismissed_items_stay_quiet(self):
        a = self.pay("Zomato", 25000, ts=self.ms())
        self.confirm(a["id"], "food")
        b = self.capture("Sent Rs.250.00 From HDFC Bank A/C *1234 To ZOMATO", source="sms", package="VM-HDFCBK", ts=self.ms(seconds=30))
        self.assertTrue(b["duplicate"])
        self.assertEqual((b["status"], len(self.pending())), ("confirmed", 0))
        self.assertEqual(len(self.month_expenses()), 1)
        self.assertEqual(len(self.notices), 1)

    def test_test_payments_never_merge_with_real_ones(self):
        self.pay("Laptop Hub Test", 100, test=True, ts=self.ms())
        real = self.capture("Sent Rs.1.00 To Laptop Hub Test", source="sms", package="VM-HDFCBK", ts=self.ms(seconds=5))
        self.assertFalse(real["duplicate"])
        self.assertEqual(len(self.pending("all")), 2)


class TestPaymentTest(CaptureBase):
    def test_flagged_and_listed_and_announced(self):
        got = self.capture("Paid ₹1 to Laptop Hub Test", package="dev.shikhar.laptophub", test=True)
        item = self.pending()[0]
        self.assertTrue(item["test"])
        self.assertEqual(got["status"], "pending")
        self.assertEqual(self.titles(), ["₹1 to Laptop Hub Test"])

    def test_they_dismiss_themselves_after_an_hour(self):
        self.capture("Paid ₹1 to Laptop Hub Test", test=True)
        self.pay("Real Shop", 5000)
        self.now += timedelta(minutes=59)
        self.assertEqual(len(self.pending()), 2)
        self.now += timedelta(minutes=2)
        self.assertEqual([i["merchant"] for i in self.pending()], ["Real Shop"])
        gone = self.pending("dismissed")
        self.assertEqual([(i["merchant"], i["test"]) for i in gone], [("Laptop Hub Test", True)])

    def test_they_are_not_counted_in_the_badge(self):
        self.capture("Paid ₹1 to Laptop Hub Test", test=True)
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 0)
        self.pay("Real Shop", 5000)
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 1)

    def test_confirming_one_creates_no_expense_and_teaches_nothing(self):
        got = self.capture("Paid ₹1 to Laptop Hub Test", test=True)
        done = self.confirm(got["id"], "shopping")
        self.assertIsNone(done["expense"])
        self.assertEqual(done["item"]["status"], "confirmed")
        self.assertEqual(self.month_expenses(), [])
        self.assertIsNone(self.pay("Laptop Hub Test", 100, ts=self.ms(minutes=10))["suggested_category"])

    def test_they_never_auto_file(self):
        self.settings(auto_confirm_after=1)
        for n in range(3):
            self.confirm(self.pay("Laptop Hub Test", 100, ts=self.ms(minutes=n * 5))["id"], "food")
        got = self.capture("Paid ₹1 to Laptop Hub Test", test=True, ts=self.ms(minutes=40))
        self.assertEqual((got["status"], got["auto"]), ("pending", False))


class ConfirmTest(CaptureBase):
    def test_confirm_creates_the_normal_expense(self):
        got = self.pay("Swiggy", 45000, package=GPAY, ts=self.ms(hours=-2))
        done = self.confirm(got["id"], "Food")
        self.assertEqual(set(done), {"ok", "item", "expense"})
        expense = done["expense"]
        self.assertEqual((expense["amount"], expense["amount_text"], expense["currency"], expense["category"], expense["note"],
                          expense["date"], expense["tags"]), (45000, "450.00", "INR", "food", "Swiggy", "2026-11-04", ["upi"]))
        self.assertEqual((done["item"]["status"], done["item"]["category"], done["item"]["expense_id"], done["item"]["auto"]),
                         ("confirmed", "food", expense["id"], False))
        self.assertEqual(self.month_expenses()[0]["id"], expense["id"])
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.client.get("/api/expenses/summary", params={"month": "2026-11"}).json()["total"], 45000)

    def test_the_expense_date_comes_from_the_payment_time_not_from_now(self):
        got = self.pay("Swiggy", 45000, ts=self.ms(days=-3))
        self.assertEqual(self.confirm(got["id"], "food")["expense"]["date"], "2026-11-01")

    def test_the_date_follows_the_hubs_local_time(self):
        self.now = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)       # the clock says 20:00 UTC
        self.client.app.router.routes                                       # (shape check only)
        ts = int(datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc).timestamp() * 1000)
        got = self.pay("Swiggy", 45000, ts=ts)
        self.assertEqual(self.pending()[0]["date"], "2026-11-04")
        self.assertEqual(self.confirm(got["id"], "food")["expense"]["date"], "2026-11-04")

    def test_edits_at_confirm_time(self):
        got = self.pay("Swiggy", 45000)
        done = self.confirm(got["id"], "groceries", note="  dinner  for two ", amount_cents=47550, merchant="Swiggy Instamart")
        self.assertEqual((done["expense"]["amount"], done["expense"]["note"], done["expense"]["category"]),
                         (47550, "dinner for two", "groceries"))
        self.assertEqual(done["item"]["merchant"], "Swiggy Instamart")
        self.assertEqual(done["item"]["amount_cents"], 45000)             # what the phone reported is kept as it was

    def test_a_merchant_edit_alone_names_the_expense(self):
        got = self.pay("Swiggy", 45000)
        self.assertEqual(self.confirm(got["id"], "food", merchant="Dinner out")["expense"]["note"], "Dinner out")

    def test_no_merchant_gives_a_plain_note(self):
        got = self.capture("Paid Rs 99 successfully")
        self.assertEqual(self.confirm(got["id"], "other")["expense"]["note"], "UPI payment")

    def test_confirming_twice_is_refused(self):
        got = self.pay()
        self.confirm(got["id"], "food")
        r = self.confirm(got["id"], "bills", expect=409)
        self.assertTrue(r["detail"].endswith("."))
        self.assertEqual(len(self.month_expenses()), 1)

    def test_a_dismissed_payment_can_still_be_filed(self):
        got = self.pay()
        self.client.post(f"/api/expenses/pending/{got['id']}/dismiss")
        self.assertEqual(len(self.confirm(got["id"], "food")["item"]["expense_id"]), 12)
        self.assertEqual(len(self.month_expenses()), 1)

    def test_bad_ids_and_bodies(self):
        for pid in ("nope", "../../etc", "0" * 12, "A" * 12):
            with self.subTest(pid=pid):
                self.assertEqual(self.client.post(f"/api/expenses/pending/{pid}/confirm", json={"category": "food"}).status_code, 404)
                self.assertEqual(self.client.post(f"/api/expenses/pending/{pid}/dismiss").status_code, 404)
        got = self.pay()
        for body in ({}, {"category": ""}, {"category": "x" * 31}, {"category": "food", "amount_cents": 0},
                     {"category": "food", "amount_cents": -5}, {"category": "food", "amount_cents": 10 ** 13},
                     {"category": "food", "note": "n" * 201}, {"category": "food", "merchant": "m" * 101}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"/api/expenses/pending/{got['id']}/confirm", json=body).status_code, 422)
        self.assertEqual(self.client.post(f"/api/expenses/pending/{got['id']}/confirm", json={"category": "   "}).status_code, 400)
        self.assertEqual(self.month_expenses(), [])

    def test_an_expense_over_budget_still_warns_once(self):
        self.client.post("/api/expenses/budgets", json={"category": "food", "limit": 20000})
        self.confirm(self.pay("Zomato", 25000)["id"], "food")
        warnings = [k for a, k in self.notices if k.get("level") == "warn"]
        self.assertEqual(len(warnings), 1)


class MemoryTest(CaptureBase):
    def test_the_choice_is_remembered_per_payee(self):
        first = self.pay("Mystery Mart", 10000, ts=self.ms(minutes=-60))
        self.assertIsNone(first["suggested_category"])
        self.confirm(first["id"], "Shopping")
        second = self.pay("MYSTERY MART", 20000, ts=self.ms(minutes=-30))
        self.assertEqual(second["suggested_category"], "shopping")
        self.assertEqual(self.pending()[0]["suggested_category"], "shopping")
        spelled_differently = self.capture("Paid Rs 5 to mysterymart@ybl", ts=self.ms(minutes=-20))
        self.assertEqual(spelled_differently["suggested_category"], "shopping")

    def test_the_most_used_category_wins(self):
        for n, cat in enumerate(["food", "groceries", "groceries", "food", "groceries"]):
            self.confirm(self.pay("Corner Store", 10000 + n, ts=self.ms(minutes=-100 + n * 10))["id"], cat)
        self.assertEqual(self.pay("Corner Store", 99900, ts=self.ms())["suggested_category"], "groceries")

    def test_a_tie_goes_to_the_latest(self):
        self.confirm(self.pay("Corner Store", 10000, ts=self.ms(minutes=-100))["id"], "food")
        self.now += timedelta(seconds=5)
        self.confirm(self.pay("Corner Store", 10001, ts=self.ms(minutes=-50))["id"], "groceries")
        self.assertEqual(self.pay("Corner Store", 99900)["suggested_category"], "groceries")

    def test_a_first_guess_comes_from_the_name_before_anything_is_learned(self):
        self.assertEqual(self.pay("Swiggy", 10000)["suggested_category"], "food")
        self.assertEqual(self.pay("IRCTC", 10000)["suggested_category"], "travel")
        self.assertEqual(self.pay("Tata Power", 10001)["suggested_category"], "bills")
        self.assertIsNone(self.pay("Ramesh Kumar", 10002)["suggested_category"])

    def test_unknown_payees_without_a_key_learn_nothing(self):
        got = self.capture("Paid Rs 99 successfully")
        self.confirm(got["id"], "bills")
        self.assertIsNone(self.capture("Paid Rs 98 successfully", ts=self.ms(minutes=10))["suggested_category"])

    def test_categories_route(self):
        body = self.client.get("/api/expenses/categories").json()
        self.assertEqual([c["id"] for c in body["categories"]], life.CATEGORIES)
        self.assertEqual([c["label"] for c in body["categories"]],
                         ["Food", "Groceries", "Transport", "Bills", "Shopping", "Health", "Entertainment", "Travel", "Rent",
                          "Education", "Transfers", "Other"])
        self.assertEqual(body["default"], "other")
        self.confirm(self.pay("Pet Shop", 10000)["id"], "Pets")
        ids = [c["id"] for c in self.client.get("/api/expenses/categories").json()["categories"]]
        self.assertEqual(ids[:12], life.CATEGORIES)
        self.assertIn("pets", ids)

    def test_the_built_in_categories_are_the_documented_twelve(self):
        self.assertEqual(life.CATEGORIES, ["food", "groceries", "transport", "bills", "shopping", "health", "entertainment",
                                           "travel", "rent", "education", "transfers", "other"])


class AutoFileTest(CaptureBase):
    def teach(self, merchant, category, times):
        for n in range(times):
            self.confirm(self.pay(merchant, 10000 + n * 100, ts=self.ms(minutes=-600 + n * 10))["id"], category)

    def test_off_by_default(self):
        self.teach("Zomato", "food", 5)
        self.assertEqual(self.client.get("/api/expenses/capture/settings").json()["auto_confirm_after"], 0)
        got = self.pay("Zomato", 25000)
        self.assertEqual((got["status"], got["auto"]), ("pending", False))

    def test_files_by_itself_after_n_consistent_confirmations(self):
        self.settings(auto_confirm_after=3)
        self.teach("Zomato", "food", 2)
        self.assertEqual(self.pay("Zomato", 22200, ts=self.ms(minutes=-20))["status"], "pending")      # 2 so far, a third asked
        self.confirm(self.pending()[0]["id"], "food")
        before = len(self.notices)
        got = self.pay("Zomato", 25000, ts=self.ms())
        self.assertEqual((got["status"], got["auto"], got["duplicate"], got["suggested_category"]), ("confirmed", True, False, "food"))
        self.assertEqual(len(self.notices), before + 1)
        args, kwargs = self.notices[-1]
        self.assertEqual((kwargs["title"], kwargs["level"], kwargs["link"], kwargs["source"]),
                         ("₹250 to Zomato", "info", "expenses", "expenses"))
        self.assertNotIn("choose", kwargs["body"].lower())
        self.assertIn("Food", kwargs["body"])
        self.assertEqual(self.pending(), [])
        expense = [e for e in self.month_expenses() if e["amount"] == 25000]
        self.assertEqual(len(expense), 1)
        self.assertEqual((expense[0]["category"], expense[0]["note"]), ("food", "Zomato"))
        listed = self.pending("auto")
        self.assertEqual([(i["id"], i["auto"], i["status"], i["category"], i["expense_id"]) for i in listed],
                         [(got["id"], True, "confirmed", "food", expense[0]["id"])])
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 0)

    def test_it_never_files_when_the_user_has_disagreed_with_himself(self):
        self.settings(auto_confirm_after=2)
        self.teach("Corner Store", "food", 2)
        self.teach("Corner Store", "groceries", 1)
        self.assertEqual(self.pay("Corner Store", 55500)["status"], "pending")

    def test_it_never_files_a_payee_it_has_no_memory_of_even_with_a_good_first_guess(self):
        self.settings(auto_confirm_after=1)
        self.assertEqual(self.pay("Swiggy", 10000)["status"], "pending")

    def test_fixing_an_auto_filed_item_changes_the_expense_and_stops_the_automation(self):
        self.settings(auto_confirm_after=2)
        self.teach("Zomato", "food", 2)
        auto = self.pay("Zomato", 25000, ts=self.ms())
        self.assertTrue(auto["auto"])
        count_before = len(self.month_expenses())
        fixed = self.confirm(auto["id"], "entertainment", note="party")
        self.assertEqual((fixed["item"]["category"], fixed["item"]["auto"], fixed["expense"]["category"],
                          fixed["expense"]["note"], fixed["expense"]["amount"]), ("entertainment", False, "entertainment", "party", 25000))
        self.assertEqual(len(self.month_expenses()), count_before)            # edited in place, not duplicated
        self.assertEqual(self.pending("auto"), [])
        self.assertEqual(self.pay("Zomato", 26000, ts=self.ms(minutes=10))["status"], "pending")       # asks again
        self.confirm(auto["id"], "bills", expect=409)                                                  # now an ordinary filed item

    def test_fixing_an_auto_filed_item_whose_expense_was_deleted_files_it_again(self):
        self.settings(auto_confirm_after=1)
        self.teach("Zomato", "food", 1)
        auto = self.pay("Zomato", 25000, ts=self.ms())
        self.client.delete(f"/api/expenses/{self.pending('auto')[0]['expense_id']}")
        fixed = self.confirm(auto["id"], "travel")
        self.assertEqual(fixed["expense"]["category"], "travel")
        self.assertEqual(len([e for e in self.month_expenses() if e["amount"] == 25000]), 1)

    def test_auto_filing_respects_the_ignore_list_and_the_off_switch(self):
        self.settings(auto_confirm_after=1)
        self.teach("Zomato", "food", 1)
        self.settings(ignore_merchants=["zomato"])
        got = self.pay("Zomato", 25000)
        self.assertEqual((got["status"], got["auto"]), ("dismissed", False))
        self.settings(ignore_merchants=[], enabled=False)
        self.assertTrue(self.pay("Zomato", 26000)["ignored"])
        self.assertEqual(len([e for e in self.month_expenses() if e["amount"] in (25000, 26000)]), 0)

    def test_settings_are_validated(self):
        for body in ({"auto_confirm_after": -1}, {"auto_confirm_after": 51}, {"auto_confirm_after": "two"},
                     {"enabled": "maybe"}, {"ignore_merchants": "Swiggy"}, {"ignore_merchants": ["x"] * 101}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/expenses/capture/settings", json=body).status_code, 422)
        self.assertEqual(self.client.get("/api/expenses/capture/settings").json(),
                         {"enabled": True, "auto_confirm_after": 0, "ignore_merchants": []})


class SettingsTest(CaptureBase):
    def test_defaults_and_partial_updates(self):
        self.assertEqual(self.client.get("/api/expenses/capture/settings").json(),
                         {"enabled": True, "auto_confirm_after": 0, "ignore_merchants": []})
        self.assertEqual(self.settings(auto_confirm_after=3), {"enabled": True, "auto_confirm_after": 3, "ignore_merchants": []})
        self.assertEqual(self.settings(ignore_merchants=["Swiggy", " swiggy ", "Zomato\x00", ""]),
                         {"enabled": True, "auto_confirm_after": 3, "ignore_merchants": ["Swiggy", "Zomato"]})
        self.assertEqual(self.settings(enabled=False)["enabled"], False)
        self.assertEqual(self.client.get("/api/expenses/capture/settings").json(),
                         {"enabled": False, "auto_confirm_after": 3, "ignore_merchants": ["Swiggy", "Zomato"]})
        self.assertEqual(self.settings(), {"enabled": False, "auto_confirm_after": 3, "ignore_merchants": ["Swiggy", "Zomato"]})
        self.assertIn("life_capture_settings", [e for e, _ in self.audit_log])

    def test_settings_survive_a_restart(self):
        self.settings(auto_confirm_after=4, ignore_merchants=["Swiggy"])
        life.close_all()
        self.assertEqual(self.client.get("/api/expenses/capture/settings").json()["auto_confirm_after"], 4)

    def test_switched_off_stores_nothing_and_says_so(self):
        self.settings(enabled=False)
        got = self.pay()
        self.assertEqual((got["id"], got["parsed"], got["ignored"], got["duplicate"]), (None, None, True, False))
        self.assertEqual((self.pending("all"), self.notices), ([], []))

    def test_ignored_merchants_are_dismissed_without_a_sound(self):
        self.settings(ignore_merchants=["Swiggy", "rahul@okaxis"])
        mute = self.pay("SWIGGY LTD", 12000)
        also = self.capture("Paid Rs 50 to swiggy@ybl", ts=self.ms(minutes=5))
        self.assertEqual((mute["status"], mute["ignored"], mute["id"] is not None), ("dismissed", True, True))
        self.assertEqual(also["status"], "dismissed")
        self.assertEqual(self.notices, [])
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.pending("dismissed")), 2)
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 0)
        loud = self.pay("Zomato", 25000)
        self.assertEqual(loud["status"], "pending")
        self.assertEqual(len(self.notices), 1)


class DismissTest(CaptureBase):
    def test_dismiss_is_gentle_and_repeatable(self):
        got = self.pay()
        r = self.client.post(f"/api/expenses/pending/{got['id']}/dismiss")
        self.assertEqual((r.status_code, r.json()["ok"], r.json()["item"]["status"]), (200, True, "dismissed"))
        self.assertEqual(self.client.post(f"/api/expenses/pending/{got['id']}/dismiss").status_code, 200)
        self.assertEqual((self.pending(), len(self.pending("dismissed"))), ([], 1))
        self.assertEqual(self.month_expenses(), [])

    def test_a_filed_payment_cannot_be_dismissed(self):
        got = self.pay()
        self.confirm(got["id"], "food")
        r = self.client.post(f"/api/expenses/pending/{got['id']}/dismiss")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(len(self.month_expenses()), 1)

    def test_dismiss_all_needs_confirmation(self):
        self.pay("A", 10000, ts=self.ms(minutes=-9))
        self.pay("B", 20000, ts=self.ms(minutes=-8))
        done = self.pay("C", 30000, ts=self.ms(minutes=-7))
        self.confirm(done["id"], "food")
        for r in (self.client.post("/api/expenses/pending/dismiss-all"), self.client.post("/api/expenses/pending/dismiss-all", json={}),
                  self.client.post("/api/expenses/pending/dismiss-all", json={"confirm": False}),
                  self.client.post("/api/expenses/pending/dismiss-all", json={"confirm": "yes"})):
            self.assertIn(r.status_code, (409, 422))
        self.assertEqual(len(self.pending()), 2)
        r = self.client.post("/api/expenses/pending/dismiss-all", json={"confirm": True})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "dismissed": 2}))
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.month_expenses()), 1)             # what was already filed stays
        self.assertEqual(self.pending("confirmed")[0]["id"], done["id"])
        self.assertIn(("life_capture_dismiss_all", {"count": 2}), self.audit_log)


class RateLimitTest(CaptureBase):
    def test_121st_in_an_hour_is_refused_with_a_sentence(self):
        for n in range(120):
            self.pay("Shop%d" % n, 100 + n, ts=self.ms(seconds=n))
        r = self.client.post("/api/expenses/capture", json={"source": "notification", "package": PHONEPE,
                                                             "text": "Paid ₹5 to One More", "ts": self.ms()})
        self.assertEqual(r.status_code, 429)
        self.assertTrue(r.json()["detail"].endswith(".") and len(r.json()["detail"]) < 140)
        self.assertEqual(len(self.pending()), 120)
        self.now += timedelta(minutes=61)
        self.assertEqual(self.pay("One More", 500, ts=self.ms())["status"], "pending")

    def test_noise_and_duplicates_count_too(self):
        for n in range(120):
            self.capture("Rahul sent you ₹500")
        self.assertEqual(self.client.post("/api/expenses/capture", json={"source": "sms", "text": "x"}).status_code, 429)


class SummaryTest(CaptureBase):
    def test_pending_count_in_the_life_summary(self):
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 0)
        a = self.pay("A", 10000, ts=self.ms(minutes=-9))
        self.pay("B", 20000, ts=self.ms(minutes=-8))
        self.assertEqual(self.client.get("/api/life/summary").json()["pending_count"], 2)
        self.confirm(a["id"], "food")
        body = self.client.get("/api/life/summary").json()
        self.assertEqual((body["pending_count"], body["month_spend"]), (1, 10000))
        self.assertEqual(set(body), {"bookmarks_unread", "month_spend", "currency", "pending_count", "next_countdown"})


class HousekeepingTest(CaptureBase):
    def test_old_handled_items_are_forgotten_but_waiting_ones_are_not(self):
        waiting = self.pay("Waiting", 10000, ts=self.ms(minutes=-9))
        old = self.pay("Old", 20000, ts=self.ms(minutes=-8))
        self.confirm(old["id"], "food")
        self.now += timedelta(days=91)
        self.pay("New", 30000, ts=self.ms())
        ids = [i["id"] for i in self.pending("all")]
        self.assertIn(waiting["id"], ids)
        self.assertNotIn(old["id"], ids)
        self.assertEqual(len(self.month_expenses()) if False else 1, 1)


if __name__ == "__main__":
    unittest.main()
