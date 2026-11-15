"""hub/oppsnorm.py: remote, pay, employment, posted-time parsing, duplicate keys and the CSS selector engine. Pure functions."""
import unittest

from hub import oppsnorm as N


class Pay(unittest.TestCase):
    def check(self, text, lo, hi, unit, cur):
        p = N.parse_pay(text)
        self.assertIsNotNone(p, text)
        self.assertEqual((p["min"], p["max"], p["unit"], p["currency"]), (lo, hi, unit, cur), text)

    def test_ranges_and_units(self):
        self.check("$90-120k per year", 90000, 120000, "year", "USD")
        self.check("$90,000 - $120,000 / year", 90000, 120000, "year", "USD")
        self.check("EUR 50/hr", 50, 50, "hour", "EUR")
        self.check("€50/hr", 50, 50, "hour", "EUR")
        self.check("£400 a day", 400, 400, "day", "GBP")
        self.check("60 to 80 USD per hour", 60, 80, "hour", "USD")
        self.check("$5k/month", 5000, 5000, "month", "USD")
        self.check("₹10-15 LPA", 1000000, 1500000, "year", "INR")
        self.check("Budget: $500", 500, 500, "project", "USD")
        self.check("Fixed price $1,500", 1500, 1500, "project", "USD")
        self.check("Salary $85,000", 85000, 85000, "year", "USD")

    def test_not_pay(self):
        for text in ("up to 5 years experience", "We pay 2 per day", "Team of 40 people", "", "call 555 1234"):
            self.assertIsNone(N.parse_pay(text), text)

    def test_struct_and_label(self):
        p = N.pay_struct(90000, 120000, "usd", "year")
        self.assertEqual(N.pay_label(p), "$90k-120k/year")
        self.assertIsNone(N.pay_struct(None, None))
        self.assertIsNone(N.pay_struct(0, 0))
        self.assertEqual(N.pay_struct(120, 100, "EUR", "hour")["min"], 100)


class Remote(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(N.parse_remote("Fully remote role")[0], "remote")
        self.assertEqual(N.parse_remote("", "Remote - EMEA")[0], "remote")
        self.assertEqual(N.parse_remote("Hybrid, 3 days a week", "London")[0], "hybrid")
        self.assertEqual(N.parse_remote("This role is not remote", "NYC")[0], "onsite")
        self.assertEqual(N.parse_remote("On-site only")[0], "onsite")
        self.assertEqual(N.parse_remote("", "Berlin")[0], "onsite")
        self.assertEqual(N.parse_remote("Work from home friendly team")[0], "remote")
        self.assertEqual(N.parse_remote("", "")[0], "unknown")

    def test_geo(self):
        mode, geo = N.parse_remote("Remote, US only. Must be located in the United States.")
        self.assertEqual(geo["regions"], ["US"])
        self.assertFalse(geo["worldwide"])
        mode, geo = N.parse_remote("", "Remote - Europe")
        self.assertEqual(geo["regions"], ["Europe"])
        mode, geo = N.parse_remote("Work from anywhere in the world", "Worldwide")
        self.assertTrue(geo["worldwide"])
        self.assertEqual(geo["regions"], [])
        mode, geo = N.parse_remote("Remote role, no restriction stated")
        self.assertEqual((mode, geo["regions"]), ("remote", []))


class Employment(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(N.parse_employment("Summer internship"), "internship")
        self.assertEqual(N.parse_employment("Part-time tutor"), "part-time")
        self.assertEqual(N.parse_employment("Contract Python developer"), "contract")
        self.assertEqual(N.parse_employment("Freelance designer"), "freelance")
        self.assertEqual(N.parse_employment("AI data annotator"), "task-based")
        self.assertEqual(N.parse_employment("", "FULL_TIME"), "full-time")
        self.assertEqual(N.parse_employment("", "PartTime"), "part-time")
        self.assertEqual(N.parse_employment("Engineer"), "unspecified")
        self.assertEqual(N.parse_employment("Engineer", kind="freelance"), "freelance")
        self.assertEqual(N.parse_employment("Engineer", kind="llm-task"), "task-based")


class Posted(unittest.TestCase):
    NOW = 1_800_000_000.0

    def test_formats(self):
        self.assertEqual(N.parse_posted("3 days ago", self.NOW), self.NOW - 3 * 86400)
        self.assertEqual(N.parse_posted("yesterday", self.NOW), self.NOW - 86400)
        self.assertEqual(N.parse_posted(1_799_000_000_000, self.NOW), 1_799_000_000.0)
        self.assertEqual(N.parse_posted(1_799_000_000, self.NOW), 1_799_000_000.0)
        self.assertGreater(N.parse_posted("2027-01-02T03:04:05Z", self.NOW), 0)
        self.assertGreater(N.parse_posted("Mon, 02 Jan 2027 03:04:05 GMT", self.NOW), 0)
        self.assertEqual(N.parse_posted("", self.NOW), 0.0)
        self.assertEqual(N.parse_posted("garbage", self.NOW), 0.0)
        self.assertEqual(N.parse_posted(self.NOW + 10 * 86400, self.NOW), 0.0)


class Dupes(unittest.TestCase):
    def test_same_job_same_key(self):
        a = N.fingerprint("Acme Inc.", "Senior Python Engineer (Remote)")
        self.assertEqual(a, N.fingerprint("ACME", "Senior Python Engineer - Remote"))
        self.assertEqual(a, N.fingerprint("Acme, LLC", "Senior Python Engineer [m/f/d]"))
        self.assertNotEqual(a, N.fingerprint("Acme", "Junior Python Engineer"))
        self.assertNotEqual(a, N.fingerprint("Other", "Senior Python Engineer"))
        self.assertEqual(N.fingerprint("", "Engineer"), "")


class Selectors(unittest.TestCase):
    HTML = ("<html><body><ul id='jobs'><li class='job hot'><a class='t' href='/j/1'>Engineer <b>A</b></a><span class='co'>X Corp</span></li>"
            "<li class='job'><a class='t' href='https://e.com/j/2'>Engineer B</a></li></ul><div class='job'>not a list item</div>"
            "<script>var a='<li class=job>no</li>'</script></body></html>")

    def test_select_and_extract(self):
        dom = N.parse_dom(self.HTML)
        nodes = N.Selector("ul#jobs > li.job").select(dom)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(N.extract_field(nodes[0], "a.t@href", "https://e.com"), "https://e.com/j/1")
        self.assertEqual(N.extract_field(nodes[0], "a.t"), "Engineer A")
        self.assertEqual(N.extract_field(nodes[0], ".co"), "X Corp")
        self.assertEqual(N.extract_field(nodes[1], ".co"), "")
        self.assertEqual(len(N.Selector("li.job.hot, div.job").select(dom)), 2)
        self.assertEqual(len(N.Selector("a[href^='https']").select(dom)), 1)
        self.assertEqual(len(N.Selector("body li").select(dom)), 2)

    def test_unsupported_selectors_refused(self):
        for bad in ("", "li:nth-child(2)", "a + b", "li{", "x" * 400):
            with self.assertRaises(N.SelectorError, msg=bad):
                N.Selector(bad)


if __name__ == "__main__":
    unittest.main()
