"""Offers and negotiation (hub/offers.py). Fixed numbers, a fake clock, no network. Rules under test:
- the India in-hand estimate follows the stated assumptions (worked by hand below) and is always labelled an estimate;
- variable pay is an expected value, ESOP is shown apart, notice buy-out and relocation are priced from the owner's own figures;
- up to four offers compare side by side; a counter-offer draft contains only numbers the owner gave; a gap is a [FILL] that blocks Copy;
- the pay check uses the posting, the owner's target and postings already in the hub, and nothing else; nothing is ever sent."""
import asyncio
import datetime as dt
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub import interviews as I, offers as O
from hub.common import Ctx
from tests.apiwalk import flat_routes
from tests.helpers import make_config
from tests.test_autopilot import need_auth

NOW = dt.datetime(2026, 11, 20, 9, 0)
A = dict(O.DEFAULTS)                       # the assumptions the maths below was worked with: new regime, basic 50 percent, PF 12 percent, PT 2400


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="of-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config, self.root = make_config(data_dir=str(self.tmp / "data"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=need_auth, audit=lambda *a, **k: None)

    def client(self, authed=True):
        app = FastAPI()
        app.include_router(O.make_router(self.ctx, clock=lambda: NOW))
        app.include_router(I.make_router(self.ctx, clock=lambda: NOW))
        self.app = app
        c = TestClient(app, cookies={"hub_session": "yes"} if authed else {})
        self.addCleanup(c.close)
        return c

    def add(self, c, **kw):
        body = {"company": "Acme", "role": "Backend Engineer", "fixed": 1800000, **kw}
        r = c.post("/api/offers", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TaxMathTests(unittest.TestCase):
    def test_new_regime_slabs_rebate_and_cess(self):
        self.assertEqual(O.slab_tax(1_200_000, "new"), 0.0)                  # 87A rebate: nothing up to 12 lakh taxable
        self.assertEqual(O.slab_tax(1_210_000, "new"), 10_400.0)             # marginal relief: only the extra 10,000 (plus cess) is taxed
        self.assertEqual(O.slab_tax(1_617_000, "new"), 128_336.0)            # (20,000 + 40,000 + 60,000 + 3,400) x 1.04
        self.assertEqual(O.slab_tax(2_500_000, "new"), 343_200.0)            # 330,000 x 1.04

    def test_old_regime_slabs_and_rebate(self):
        self.assertEqual(O.slab_tax(250_000, "old"), 0.0)
        self.assertEqual(O.slab_tax(500_000, "old"), 0.0)                    # 87A: nothing up to 5 lakh taxable
        self.assertEqual(O.slab_tax(600_000, "old"), 33_800.0)               # (12,500 + 20,000) x 1.04
        self.assertEqual(O.slab_tax(1_000_000, "old"), 117_000.0)            # (12,500 + 100,000) x 1.04

    def test_in_hand_estimate_new_regime_by_hand(self):
        e = O.estimate({"currency": "INR", "fixed": 1_800_000}, A)
        # basic 900,000; PF 12 percent = 108,000 each side; taxable = 1,800,000 - 108,000 - 75,000 = 1,617,000; tax 128,336
        self.assertEqual(e["employee_pf"], 108_000)
        self.assertEqual(e["employer_pf"], 108_000)
        self.assertEqual(e["taxable_income"], 1_617_000)
        self.assertEqual(e["income_tax"], 128_336)
        self.assertEqual(e["monthly_in_hand"], 121_105)                      # (1,800,000 - 108,000 - 108,000 - 2,400 - 128,336) / 12
        self.assertEqual(e["annual_take_home"], 1_453_264)
        self.assertIn("ESTIMATE", e["label"])
        self.assertEqual(e["assumptions"]["basic_pct"], 50)

    def test_below_the_rebate_no_tax(self):
        e = O.estimate({"currency": "INR", "fixed": 1_000_000}, A)
        self.assertEqual(e["income_tax"], 0)
        self.assertEqual(e["monthly_in_hand"], 73_133)                       # 877,600 / 12

    def test_old_regime_with_80c_and_standard_deduction(self):
        e = O.estimate({"currency": "INR", "fixed": 1_800_000}, {**A, "regime": "old"})
        # taxable = 1,692,000 - 50,000 - 108,000 (PF under 80C) - 2,400 (professional tax) = 1,531,600
        self.assertEqual(e["taxable_income"], 1_531_600)
        self.assertEqual(e["income_tax"], round(((12_500 + 100_000 + 0.3 * 531_600) * 1.04)))
        self.assertGreater(e["income_tax"], O.estimate({"currency": "INR", "fixed": 1_800_000}, A)["income_tax"])

    def test_assumptions_change_the_result_and_are_printed(self):
        capped = O.estimate({"currency": "INR", "fixed": 1_800_000}, {**A, "pf_capped": True})
        self.assertEqual(capped["employee_pf"], 21_600)                      # 12 percent of 15,000 x 12
        self.assertEqual(capped["assumptions"]["pf_capped"], True)
        self.assertGreater(capped["monthly_in_hand"], 121_105 - 1)
        not_in_ctc = O.estimate({"currency": "INR", "fixed": 1_800_000}, {**A, "ctc_includes_employer_pf": False})
        self.assertEqual(not_in_ctc["employer_pf"], 0)

    def test_variable_is_an_expected_value(self):
        offer = {"currency": "INR", "fixed": 1_800_000, "variable": 200_000}
        self.assertEqual(O.estimate(offer, A)["variable_expected"], 160_000)            # default payout 80 percent
        self.assertEqual(O.estimate({**offer, "variable_payout_pct": 50}, A)["variable_expected"], 100_000)
        self.assertEqual(O.estimate(offer, {**A, "variable_payout_pct": 100})["variable_expected"], 200_000)

    def test_variable_adds_tax_but_the_monthly_figure_is_salary_only(self):
        base = O.estimate({"currency": "INR", "fixed": 1_800_000}, A)
        more = O.estimate({"currency": "INR", "fixed": 1_800_000, "variable": 200_000}, A)
        self.assertGreater(more["income_tax"], base["income_tax"])
        self.assertLess(more["monthly_in_hand"], base["monthly_in_hand"])    # a share of the extra tax is taken from the salary months
        self.assertGreater(more["avg_monthly_all_in"], base["avg_monthly_all_in"])

    def test_joining_bonus_is_taxed(self):
        e = O.estimate({"currency": "INR", "fixed": 1_800_000, "joining_bonus": 100_000}, A)
        self.assertEqual(e["joining_bonus_after_tax"], 100_000 - round(O.slab_tax(1_717_000, "new") - O.slab_tax(1_617_000, "new")))

    def test_other_currencies_and_empty_offers_get_no_estimate(self):
        self.assertIsNone(O.estimate({"currency": "USD", "fixed": 100_000}, A))
        self.assertIsNone(O.estimate({"currency": "INR"}, A))


class TotalsTests(unittest.TestCase):
    OFFER = {"currency": "INR", "fixed": 1_800_000, "variable": 200_000, "bonus": 50_000, "benefits_value": 30_000, "joining_bonus": 100_000,
             "relocation_cost": 60_000, "relocation_support": 40_000, "notice_buyout_days": 30,
             "esop": {"grant_value": 4_000_000, "vesting_years": 4, "cliff_months": 12}}

    def test_year_one_and_recurring(self):
        a = {**A, "current_monthly_gross": 150_000}
        t = O.totals(self.OFFER, a)
        self.assertEqual(t["variable_expected"], 160_000)
        self.assertEqual(t["annual_recurring"], 2_040_000)                    # 1.8M + 160k + 50k + 30k; ESOP not counted by default
        self.assertEqual(t["esop_per_year"], 1_000_000)
        self.assertEqual(t["esop_counted"], 0)
        self.assertEqual(t["buyout_cost"], 150_000)                           # 150,000 a month for 30 days
        self.assertEqual(t["relocation_net_cost"], 20_000)
        self.assertEqual(t["first_year"], 2_040_000 + 100_000 - 150_000 - 20_000)

    def test_esop_counted_only_when_the_owner_says_so(self):
        t = O.totals(self.OFFER, {**A, "esop_counted_pct": 50})
        self.assertEqual(t["esop_counted"], 500_000)
        self.assertEqual(t["annual_recurring"], 2_040_000 + 500_000)

    def test_buyout_unknown_without_your_current_pay_and_free_when_covered(self):
        self.assertIsNone(O.totals(self.OFFER, A)["buyout_cost"])
        self.assertEqual(O.totals({**self.OFFER, "buyout_covered": True}, A)["buyout_cost"], 0.0)

    def test_nothing_is_made_up_for_missing_parts(self):
        t = O.totals({"currency": "INR", "fixed": 1_000_000}, A)
        self.assertEqual(t["annual_recurring"], 1_000_000)
        self.assertIn("variable pay", t["missing"])
        self.assertIsNone(t["esop_per_year"])


class CompareTests(unittest.TestCase):
    def offer(self, oid, **kw):
        return {"id": oid, "company": oid.upper(), "role": "Dev", "currency": "INR", "fixed": 1_000_000, "clauses": {}, **kw}

    def test_side_by_side_with_best_marked(self):
        res = O.compare([self.offer("a", fixed=1_800_000, location="Pune"), self.offer("b", fixed=2_000_000, variable=100_000, notice_days=60)], A)
        rows = {r["key"]: r for r in res["rows"]}
        self.assertEqual(rows["fixed"]["values"], [1_800_000, 2_000_000])
        self.assertEqual(rows["fixed"]["best"], [1])
        self.assertEqual(rows["monthly_in_hand"]["best"], [1])
        self.assertEqual(rows["location"]["values"][0], "Pune")
        self.assertEqual(rows["notice"]["values"], ["", "60 days"])
        self.assertIsNone(rows["bonus"]["values"][0])
        self.assertTrue(any("ESTIMATES" in n for n in res["notes"]))

    def test_two_to_four_offers_only(self):
        offers = [self.offer(x) for x in "abcde"]
        for n in (0, 1, 5):
            with self.assertRaises(O.OfferError):
                O.compare(offers[:n] if n < 5 else offers, A)
        self.assertEqual(len(O.compare(offers[:4], A)["offers"]), 4)

    def test_mixed_currencies_are_not_ranked_and_get_no_estimate(self):
        res = O.compare([self.offer("a"), self.offer("b", currency="USD", fixed=90_000)], A)
        rows = {r["key"]: r for r in res["rows"]}
        self.assertEqual(rows["fixed"]["best"], [])
        self.assertEqual(rows["monthly_in_hand"]["values"][1], None)
        self.assertTrue(any("different currencies" in n for n in res["notes"]))

    def test_clauses_marked_yes_are_listed(self):
        res = O.compare([self.offer("a", clauses={"non_compete": "yes", "bond": "no"}), self.offer("b")], A)
        row = next(r for r in res["rows"] if r["key"] == "clauses")
        self.assertEqual(row["values"], ["Non-compete", "none marked"])

    def test_clause_review_asks_about_unchecked_clauses(self):
        rev = {c["id"]: c for c in O.clause_review({"clauses": {"bond": "yes", "ip": "no"}})}
        self.assertEqual(rev["bond"]["state"], "yes")
        self.assertEqual(rev["ip"]["state"], "no")
        self.assertEqual(rev["non_compete"]["state"], "unknown")
        self.assertTrue(rev["non_compete"]["ask"].endswith("?"))
        self.assertEqual({"non_compete", "bond", "clawback", "ip", "notice", "probation"} - set(rev), set())


NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers(text):
    return {n.replace(",", "") for n in NUM.findall(text)}


class CounterTests(unittest.TestCase):
    OFFER = {"id": "of-1", "company": "Acme", "role": "Backend Engineer", "currency": "INR", "fixed": 1_800_000, "variable": 200_000}

    def test_email_uses_only_the_owners_numbers(self):
        comp = {"id": "of-2", "company": "Globex", "currency": "INR", "fixed": 2_200_000}
        d = O.counter_draft(self.OFFER, {"fixed": 2_100_000, "notice_days": 30}, [comp], ["I led the migration at my current job."], recruiter="Priya", owner_name="Sam")
        allowed = numbers(json.dumps([self.OFFER, comp, {"fixed": 2_100_000, "notice_days": 30}, "I led the migration at my current job."]))
        self.assertTrue(numbers(d["text"]) <= allowed, numbers(d["text"]) - allowed)
        self.assertIn("INR 2,100,000", d["text"])
        self.assertIn("INR 1,800,000", d["text"])
        self.assertIn("INR 2,200,000", d["text"])
        self.assertNotIn("Globex", d["text"])                                  # the competitor is not named unless the owner chooses it
        self.assertFalse(d["blocked"])
        self.assertTrue(d["text"].startswith("Hi Priya,"))
        self.assertFalse(d["sends"])

    def test_a_missing_ask_is_a_blank_that_blocks_copy(self):
        d = O.counter_draft(self.OFFER, {}, [], [])
        self.assertTrue(d["blocked"])
        self.assertTrue(any("FILL" in f for f in d["fills"]))
        self.assertEqual(numbers(d["text"]), {"1800000"})                       # only the offer's own figure
        self.assertIn("[FILL: recruiter's name]", d["text"])

    def test_competitor_named_only_when_chosen_and_an_offer_without_numbers_is_not_mentioned(self):
        comp = {"id": "of-2", "company": "Globex", "currency": "INR", "fixed": 2_200_000}
        self.assertIn("from Globex", O.counter_draft(self.OFFER, {"fixed": 2_000_000}, [comp], [], name_competitor=True)["text"])
        empty = {"id": "of-3", "company": "Initech", "currency": "INR"}
        self.assertNotIn("another offer", O.counter_draft(self.OFFER, {"fixed": 2_000_000}, [empty], [])["text"])

    def test_call_script(self):
        d = O.counter_draft(self.OFFER, {"fixed": 2_000_000}, [], ["My notice is short."], channel="call")
        self.assertEqual(d["channel"], "call")
        self.assertEqual(d["subject"], "")
        self.assertIn("Say about", d["text"])
        self.assertIn("in writing", d["text"])


class PayCheckTests(unittest.TestCase):
    TARGET = {"currency": "INR", "low": 1_500_000, "target": 2_000_000, "high": 2_600_000}
    POSTING = {"title": "Senior Python Backend Engineer", "company": "Acme", "description": "We pay Rs 18-24 LPA for this role. Python, FastAPI."}

    def test_advertised_range_is_read_and_the_verdict_is_in(self):
        r = O.pay_check(self.POSTING, self.TARGET, [])
        self.assertEqual((r["advertised"]["min"], r["advertised"]["max"]), (1_800_000, 2_400_000))
        self.assertEqual(r["verdict"], "in")
        self.assertEqual(r["verdict_basis"], "advertised range")

    def test_below_and_above_target(self):
        self.assertEqual(O.pay_check(self.POSTING, {**self.TARGET, "target": 2_600_000}, [])["verdict"], "below")
        self.assertEqual(O.pay_check(self.POSTING, {**self.TARGET, "target": 1_500_000}, [])["verdict"], "above")
        self.assertIn("lowest", O.pay_check(self.POSTING, {**self.TARGET, "low": 2_500_000, "target": 2_600_000, "high": 3_000_000}, [])["why"])

    def test_similar_postings_in_the_hub(self):
        items = [{"id": "1", "title": "Python Backend Engineer", "company": "X", "pay": {"min": 1_600_000, "max": 2_000_000, "unit": "year", "currency": "INR"}},
                 {"id": "2", "title": "Backend Python Developer", "company": "Y", "pay": {"min": 2_000_000, "max": 2_800_000, "unit": "year", "currency": "INR"}},
                 {"id": "3", "title": "Pastry Chef", "company": "Z", "pay": {"min": 300_000, "max": 400_000, "unit": "year", "currency": "INR"}},
                 {"id": "4", "title": "Python Backend Engineer", "company": "W", "pay": {"min": 100, "max": 150, "unit": "year", "currency": "USD"}}]
        r = O.pay_check(self.POSTING, self.TARGET, items)
        self.assertEqual(r["similar"]["count"], 2)                              # not the chef, not the dollar posting
        self.assertEqual((r["similar"]["typical_min"], r["similar"]["typical_max"]), (1_800_000, 2_400_000))

    def test_no_advertised_range_uses_similar_postings_and_says_so(self):
        posting = {"title": "Python Backend Engineer", "company": "Acme", "description": "Great team."}
        items = [{"id": "1", "title": "Python Backend Engineer", "company": "X", "pay": {"min": 3_000_000, "max": 3_600_000, "unit": "year", "currency": "INR"}}]
        r = O.pay_check({**posting}, {**self.TARGET, "currency": "INR"}, items)
        self.assertIsNone(r["advertised"])
        self.assertEqual(r["verdict_basis"], "similar postings in the hub")
        self.assertEqual(r["verdict"], "above")

    def test_unknown_without_a_target_or_with_a_currency_mismatch_or_no_range(self):
        self.assertEqual(O.pay_check(self.POSTING, {}, [])["verdict"], "unknown")
        self.assertIn("Pay tab", O.pay_check(self.POSTING, {}, [])["why"])
        self.assertEqual(O.pay_check(self.POSTING, {**self.TARGET, "currency": "USD"}, [])["verdict"], "unknown")
        self.assertEqual(O.pay_check({"title": "Chef", "description": "Great food."}, self.TARGET, [])["verdict"], "unknown")

    def test_monthly_pay_is_turned_into_a_year_and_hourly_is_not_compared(self):
        self.assertEqual(O.annual_range({"min": 150_000, "max": 200_000, "unit": "month"}), (1_800_000, 2_400_000))
        self.assertIsNone(O.annual_range({"min": 50, "max": 60, "unit": "hour"}))
        self.assertIsNone(O.annual_range(None))


class ApiTests(Base):
    def test_auth_and_round_trip(self):
        self.assertEqual(self.client(False).get("/api/offers").status_code, 401)
        c = self.client()
        o = self.add(c, variable=200000, esop={"grant_value": 4000000, "vesting_years": 4}, clauses={"bond": "yes"}, notice_days=60)
        self.assertEqual(o["estimate"]["monthly_in_hand"], O.estimate({"currency": "INR", "fixed": 1800000, "variable": 200000}, A)["monthly_in_hand"])
        self.assertEqual(o["esop_per_year"], 1000000)
        self.assertEqual({x["id"]: x["state"] for x in o["clause_review"]}["bond"], "yes")
        listing = c.get("/api/offers").json()
        self.assertEqual(len(listing["offers"]), 1)
        self.assertEqual(listing["assumptions"]["regime"], "new")
        self.assertIn("labels", listing)
        self.assertFalse(listing["sends"])
        put = c.put(f"/api/offers/{o['id']}", json={"company": "Acme", "role": "Backend Engineer", "fixed": 2000000})
        self.assertEqual(put.json()["fixed"], 2000000)
        self.assertEqual(put.json()["notice_days"], 60)                          # a field not sent is kept
        self.assertEqual(c.delete(f"/api/offers/{o['id']}").status_code, 200)
        self.assertEqual(c.delete(f"/api/offers/{o['id']}").status_code, 404)

    def test_validation_is_plain(self):
        c = self.client()
        for body in ({"company": "A"}, {"company": "A", "role": "B", "fixed": -5}, {"company": "A", "role": "B", "fixed": "lots"}, {"company": "A", "role": "B", "currency": "rupees"},
                     {"company": "A", "role": "B", "start_date": "tomorrow"}, {"company": "A", "role": "B", "start_date": "2026-13-40"},
                     {"company": "A", "role": "B", "notice_days": 1.5}, {"company": "A", "role": "B", "variable_payout_pct": 120},
                     {"company": "A", "role": "B", "status": "maybe"}):
            r = c.post("/api/offers", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertTrue(r.json()["detail"])

    def test_assumptions_are_editable_and_checked(self):
        c = self.client()
        r = c.put("/api/offers/assumptions", json={"regime": "old", "pf_capped": True, "current_monthly_gross": 150000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["regime"], r.json()["pf_capped"], r.json()["current_monthly_gross"]), ("old", True, 150000.0))
        self.assertEqual(c.get("/api/offers").json()["assumptions"]["regime"], "old")
        for bad in ({"regime": "weird"}, {"pf_capped": "yes"}, {"basic_pct": 150}, {"pf_rate_pct": "x"}):
            self.assertEqual(c.put("/api/offers/assumptions", json=bad).status_code, 400, bad)

    def test_compare_route_and_limits(self):
        c = self.client()
        ids = [self.add(c, company=f"Co{i}", fixed=1000000 + i * 100000)["id"] for i in range(5)]
        r = c.post("/api/offers/compare", json={"ids": ids[:3]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["offers"]), 3)
        self.assertEqual(c.post("/api/offers/compare", json={"ids": ids}).status_code, 400)
        self.assertEqual(c.post("/api/offers/compare", json={"ids": ids[:1]}).status_code, 400)
        self.assertEqual(c.post("/api/offers/compare", json={"ids": [ids[0], "of-nope"]}).status_code, 404)

    def test_the_offer_list_is_capped(self):
        c = self.client()
        for i in range(O.MAX_OFFERS):
            self.add(c, company=f"Co{i}")
        self.assertEqual(c.post("/api/offers", json={"company": "x", "role": "y", "fixed": 1}).status_code, 400)

    def test_counter_route_uses_competing_offers_the_owner_ticks(self):
        c = self.client()
        mine = self.add(c, company="Acme")["id"]
        other = self.add(c, company="Globex", fixed=2200000)["id"]
        r = c.post(f"/api/offers/{mine}/counter", json={"ask": {"fixed": 2100000}, "use_competing": [other, mine], "reasons": ["I run a team of four."], "recruiter": "Priya"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertIn("2,100,000", d["text"])
        self.assertIn("2,200,000", d["text"])
        self.assertEqual(d["text"].count("another offer"), 1)                     # the offer itself is never its own competitor
        self.assertNotIn("Globex", d["text"])
        r2 = c.post(f"/api/offers/{mine}/counter", json={"channel": "call"})
        self.assertTrue(r2.json()["blocked"])
        self.assertEqual(c.post("/api/offers/of-nope/counter", json={}).status_code, 404)
        self.assertEqual(c.post(f"/api/offers/{mine}/counter", json={"ask": {"fixed": "x"}}).status_code, 400)

    def test_pay_check_reads_the_owners_pay_sheet_from_the_interview_coach(self):
        c = self.client()
        self.assertEqual(c.put("/api/interviews/negotiation", json={"currency": "INR", "low": 1500000, "target": 2000000, "high": 2600000}).status_code, 200)
        r = c.post("/api/offers/pay-check", json={"title": "Senior Python Engineer", "company": "Acme", "description": "Pay: Rs 18-24 LPA."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["verdict"], "in")
        self.assertEqual(r.json()["target"]["target"], 2000000)
        self.assertIn("Nothing was looked up", r.json()["note"])
        self.assertEqual(c.post("/api/offers/pay-check", json={}).status_code, 400)
        self.assertEqual(c.post("/api/offers/pay-check", json={"item_id": "0" * 16}).status_code, 404)

    def test_pay_check_from_a_hub_posting_and_similar_ones(self):
        c = self.client()
        self.client()
        base = Path(self.config.data_path())
        base.mkdir(parents=True, exist_ok=True)
        items = {"a" * 16: {"id": "a" * 16, "title": "Python Backend Engineer", "company": "X", "description": "Python", "pay": {"min": 2_000_000, "max": 2_800_000, "unit": "year", "currency": "INR"}},
                 "b" * 16: {"id": "b" * 16, "title": "Python Backend Developer", "company": "Y", "description": "x", "pay": {"min": 1_000_000, "max": 1_400_000, "unit": "year", "currency": "INR"}}}
        (base / "oppsources_items.json").write_text(json.dumps(items), encoding="utf-8")
        (base / "interviews.json").write_text(json.dumps({"negotiation": {"currency": "INR", "target": 2_500_000}}), encoding="utf-8")
        r = c.post("/api/offers/pay-check", json={"item_id": "a" * 16}).json()
        self.assertEqual(r["posting"]["company"], "X")
        self.assertEqual(r["verdict"], "in")
        self.assertEqual(r["similar"]["count"], 1)                                # the posting itself is not "similar to itself"

    def test_switched_off_and_no_send_route(self):
        c = self.client()
        for p, _m, _e in flat_routes(self.app):
            self.assertNotRegex(p, r"/(send|post|publish|submit)\b")
        self.config.offers_enabled = False
        self.assertEqual(c.get("/api/offers").status_code, 404)

    def test_data_is_stored_locally_only(self):
        c = self.client()
        self.add(c)
        raw = (Path(self.config.data_path()) / "offers.json").read_text(encoding="utf-8")
        self.assertIn("Acme", raw)


class WiringTests(Base):
    def test_wiring_settings_and_tool(self):
        from hub import server, settings
        from hub.assistant import TOOL_GROUPS
        self.assertIn("offers", server.FEATURE_MODULES)
        self.assertIn("offers_enabled", settings.ALLOWED_KEYS)
        names, rx = TOOL_GROUPS["offers"]
        self.assertIn("offer_compare", names)
        self.assertTrue(rx.search("compare my offers"))

    def test_tool_is_read_only_and_round_trips(self):
        open_ctx = Ctx(config=self.config, model_for=lambda p: None, transcriber=None, notes=None, need_auth=lambda: None, audit=lambda *a, **k: None)
        app = FastAPI()
        app.include_router(O.make_router(open_ctx, clock=lambda: NOW))
        self.config.session_generation = 0
        tool = O.build(lambda: app, self.config)[0]
        self.assertFalse(tool.needs_approval({}))
        ok, text = asyncio.run(tool.run({}))
        self.assertFalse(ok)
        for company, fixed in (("Acme", 1800000), ("Globex", 2200000)):
            c = TestClient(app, cookies={"hub_session": "yes"})
            self.assertEqual(c.post("/api/offers", json={"company": company, "role": "Dev", "fixed": fixed}).status_code, 200)
        ok, text = asyncio.run(tool.run({}))
        self.assertTrue(ok, text)
        self.assertIn("Acme", text)
        self.assertIn("Globex", text)
        self.assertIn("In-hand per month, estimate", text)
        self.assertIn("Nothing was sent", text)


if __name__ == "__main__":
    unittest.main()
