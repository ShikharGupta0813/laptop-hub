"""hub/oppsources.py: shapes read from the LIVE public endpoints on 2026-11-15 (hand-trimmed to a few made-up jobs, no real
people or postings). Each test pins one shape that was checked or fixed against the real answer."""
import json
import unittest

from hub import oppsources as S
from tests.test_oppsources import Base


def preset_rows(pid, data):
    return S.parse_json_rows(data, S.PRESET_BY_ID[pid]["settings"])


class AtsShapes(unittest.TestCase):
    def test_greenhouse(self):
        body = json.dumps({"jobs": [{"absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1", "id": 1, "title": "Backend Engineer",
                                     "company_name": "Acme", "first_published": "2026-07-22T13:38:40-04:00", "updated_at": "2026-11-06T15:25:07-05:00",
                                     "location": {"name": "Remote, United States"}, "content": "&lt;p&gt;Build things.&lt;/p&gt;", "metadata": []}], "meta": {"total": 1}})
        rows, complete = S.parse_ats("greenhouse", body, {"board": "acme"})
        self.assertTrue(complete)
        r = rows[0]
        self.assertEqual((r["title"], r["location"], r["ext_id"]), ("Backend Engineer", "Remote, United States", "1"))
        self.assertIn("Build things", r["description"])
        self.assertTrue(r["url"].startswith("https://job-boards.greenhouse.io/"))

    def test_ashby(self):
        body = json.dumps({"apiVersion": "1", "jobs": [
            {"id": "a1", "title": "Designer", "department": "Design", "team": "Product", "employmentType": "FullTime", "location": "Remote - EU",
             "isListed": True, "isRemote": True, "workplaceType": "Remote", "publishedAt": "2026-11-01T10:00:00.000+00:00",
             "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "applyUrl": "https://jobs.ashbyhq.com/acme/a1/application",
             "descriptionPlain": "Design well.", "compensation": {"compensationTierSummary": "EUR 60K - 80K"}},
            {"id": "a2", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/a2"}]})
        rows, _ = S.parse_ats("ashby", body, {"board": "acme"})
        self.assertEqual([r["title"] for r in rows], ["Designer"])
        self.assertEqual((rows[0]["remote"], rows[0]["pay_text"]), ("Remote", "EUR 60K - 80K"))

    def test_smartrecruiters_list_has_no_description_and_url_is_built(self):
        body = json.dumps({"offset": 0, "limit": 100, "totalFound": 1, "content": [
            {"id": "743999", "name": "Support Specialist", "uuid": "u-1", "refNumber": "R1", "company": {"identifier": "acme", "name": "Acme"},
             "releasedDate": "2026-11-10T08:00:00.000Z", "location": {"city": "Pune", "country": "in", "remote": False, "hybrid": True},
             "typeOfEmployment": {"id": "permanent", "label": "Full-time"}, "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/743999"}]})
        rows, _ = S.parse_ats("smartrecruiters", body, {"board": "acme"})
        r = rows[0]
        self.assertEqual((r["company"], r["location"], r["employment"], r["remote"]), ("Acme", "Pune, in", "Full-time", "hybrid"))
        self.assertEqual(r["url"], "https://jobs.smartrecruiters.com/acme/743999")
        self.assertEqual(r["description"], "")

    def test_recruitee(self):
        body = json.dumps({"offers": [{"id": 9, "title": "Data Analyst", "company_name": "Acme", "careers_url": "https://acme.recruitee.com/o/data-analyst",
                                       "description": "<p>Numbers.</p>", "published_at": "2026-11-02 09:00:00 UTC", "location": "Amsterdam, Netherlands",
                                       "remote": True, "on_site": False, "salary": {"min": "3000", "max": "4000", "currency": "EUR", "period": "month"}}]})
        rows, _ = S.parse_ats("recruitee", body, {"board": "acme"})
        r = rows[0]
        self.assertEqual((r["company"], r["remote"], r["location"]), ("Acme", "remote", "Amsterdam, Netherlands"))
        self.assertEqual((r["pay"]["min"], r["pay"]["unit"], r["pay"]["currency"]), (3000.0, "month", "EUR"))

    def test_workable_empty_widget_answer_gives_no_rows(self):
        # live 2026-11-15: the widget API answered {"name", "description", "jobs": []} for every account tried
        rows, complete = S.parse_ats("workable", json.dumps({"name": "Acme", "description": None, "jobs": []}), {"board": "acme"})
        self.assertEqual((rows, complete), ([], True))

    def test_teamtailor_nested_locations_and_remote_status(self):
        feed = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel><title>Acme</title>
<item><title>Support Lead</title><description>&lt;p&gt;Help people.&lt;/p&gt;</description><pubDate>Fri, 13 Nov 2026 15:00:10 +0100</pubDate>
<link>https://acme.teamtailor.com/jobs/1-support-lead</link><remoteStatus>hybrid</remoteStatus>
<tt:locations><tt:location><tt:name>HQ</tt:name><tt:city>Stockholm</tt:city><tt:country>Sweden</tt:country></tt:location>
<tt:location><tt:city>Stockholm</tt:city><tt:country>Sweden</tt:country></tt:location></tt:locations></item></channel></rss>"""
        rows, _ = S.parse_ats("teamtailor", feed, {"board": "acme", "company": "Acme"})
        self.assertEqual((rows[0]["location"], rows[0]["remote"], rows[0]["company"]), ("Stockholm, Sweden", "hybrid", "Acme"))


class RemoteBoardShapes(unittest.TestCase):
    def test_remoteok_skips_legal_row_and_reads_epoch(self):
        data = [{"last_updated": 1794556821, "legal": "API Terms of Service: link back."},
                {"slug": "s", "id": "1", "epoch": 1794556821, "date": "2026-11-13T08:00:21+00:00", "company": "Fuku", "position": "BD Lead",
                 "tags": ["finance"], "description": "<p>About the role</p>", "location": "Paris", "apply_url": "https://remoteok.com/apply/1",
                 "salary_min": 0, "salary_max": 0, "url": "https://remoteok.com/remote-jobs/1"}]
        rows = preset_rows("remoteok", data)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["title"], rows[0]["company"], rows[0]["published"]), ("BD Lead", "Fuku", 1794556821))
        self.assertIsNone(rows[0]["pay"])

    def test_remotive(self):
        data = {"00-warning": "note", "0-legal-notice": "link back", "job-count": 1, "jobs": [
            {"id": 1, "url": "https://remotive.com/remote-jobs/1", "title": "QA Engineer", "company_name": "Acme", "tags": ["qa"], "job_type": "full_time",
             "publication_date": "2026-11-12T10:00:00", "candidate_required_location": "Worldwide", "salary": "$40k - $60k", "description": "<p>Test.</p>"}]}
        r = preset_rows("remotive", data)[0]
        self.assertEqual((r["company"], r["location"], r["pay_text"], r["remote"]), ("Acme", "Worldwide", "$40k - $60k", "remote"))

    def test_himalayas_epoch_and_pay_period_from_feed(self):
        data = {"updatedAt": 1794762765, "offset": 0, "limit": 1, "totalCount": 5, "nextCursor": "abc", "jobs": [
            {"title": "Ops Manager", "companyName": "Acme", "employmentType": "Full Time", "minSalary": 9000, "maxSalary": 12000, "salaryPeriod": "month",
             "currency": "USD", "locationRestrictions": ["India", "Philippines"], "pubDate": 1794762000, "applicationLink": "https://himalayas.app/j/1",
             "description": "<p>Run ops.</p>", "guid": "g1"}]}
        r = preset_rows("himalayas", data)[0]
        self.assertEqual(r["pay"]["unit"], "month")
        self.assertEqual((r["location"], r["published"], r["pay"]["currency"]), ("India, Philippines", 1794762000, "USD"))

    def test_jobicy(self):
        data = {"apiVersion": "2.2.19", "jobCount": 1, "success": True, "jobs": [
            {"id": 7, "url": "https://jobicy.com/jobs/7", "jobTitle": "Content Writer", "companyName": "Acme", "jobIndustry": ["Marketing"], "jobType": ["Full-Time"],
             "jobGeo": "Anywhere", "jobLevel": "Junior", "jobExcerpt": "x", "jobDescription": "<p>Write.</p>", "pubDate": "2026-11-15 10:00:00",
             "salaryMin": 20000, "salaryMax": 30000, "salaryCurrency": "USD", "salaryPeriod": "yearly"}]}
        r = preset_rows("jobicy", data)[0]
        self.assertEqual((r["employment"], r["location"], r["pay"]["unit"]), ("Full-Time", "Anywhere", "year"))

    def test_arbeitnow_epoch_created_at_and_bool_remote(self):
        data = {"data": [{"slug": "s", "company_name": "Acme GmbH", "title": "Sachbearbeiter", "description": "<p>Aufgaben</p>", "remote": True,
                          "url": "https://www.arbeitnow.com/jobs/s", "tags": ["office"], "job_types": ["full time"], "location": "Berlin", "created_at": 1794700000}],
                "links": {"next": None}, "meta": {"terms": "free public API"}}
        r = preset_rows("arbeitnow", data)[0]
        self.assertEqual((r["company"], r["remote"], r["published"], r["location"]), ("Acme GmbH", "remote", 1794700000, "Berlin"))

    def test_workingnomads_top_level_list_with_comma_tags(self):
        data = [{"url": "https://www.workingnomads.com/job/go/1", "title": "Tester", "description": "<p>Test apps.</p>", "company_name": "Tester Work",
                 "category_name": "Development", "tags": "qa,tech", "location": "Worldwide", "pub_date": "2026-11-13T09:12:26-05:00"}]
        r = preset_rows("workingnomads", data)[0]
        self.assertEqual((r["company"], r["location"], r["remote"]), ("Tester Work", "Worldwide", "remote"))
        self.assertIn("Tags: qa,tech", r["description"])


class FeedQuirks(unittest.TestCase):
    NODESK = """<?xml version="1.0" encoding="utf-8" standalone="yes"?><rss version="2.0"><channel><title>Remote Jobs on NoDesk</title>
<copyright>&copy; 2026 NoDesk</copyright><item><title>Staff Engineer at Rebuy</title><link>https://nodesk.co/remote-jobs/rebuy-staff-engineer/</link>
<description>Join us &amp; build.</description><pubDate>Fri, 13 Nov 2026 08:00:00 +0200</pubDate></item></channel></rss>"""

    def test_html_entity_in_plain_xml_is_tolerated(self):
        rows = S.parse_rss_rows(self.NODESK)
        self.assertEqual(rows[0]["title"], "Staff Engineer at Rebuy")
        self.assertIn("build", rows[0]["description"])

    def test_entity_declarations_are_still_refused(self):
        bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;&copy;</title></item></channel></rss>'
        with self.assertRaises(ValueError):
            S.parse_rss_rows(bomb)

    def test_role_at_company_split(self):
        r = S.split_role_at_company({"title": "Staff Engineer at Rebuy", "company": ""})
        self.assertEqual((r["title"], r["company"]), ("Staff Engineer", "Rebuy"))
        r = S.split_role_at_company({"title": "Looking at data", "company": "Keep"})
        self.assertEqual((r["title"], r["company"]), ("Looking at data", "Keep"))

    def test_hn_replies_are_not_postings(self):
        data = {"hits": [
            {"objectID": "101", "parent_id": 100, "story_id": 100, "created_at": "2026-11-02T17:00:00Z",
             "comment_text": "Acme | Backend Engineer | Remote | Full-time<p>We build things."},
            {"objectID": "102", "parent_id": 101, "story_id": 100, "created_at": "2026-11-02T18:00:00Z", "comment_text": "Is this role open to India?"}]}
        rows = S.parse_hn_comments(data)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["company"], rows[0]["title"]), ("Acme", "Backend Engineer"))


class PresetCatalogue(Base):
    def test_new_presets_add_and_use_only_public_hosts(self):
        banned = ("linkedin", "indeed", "naukri", "instahyre", "wellfound", "glassdoor")
        for p in S.PRESETS:
            self.assertFalse(any(b in json.dumps(p["settings"]).lower() for b in banned), p["id"])
        for pid in ("workingnomads", "wwr-programming", "jobspresso", "nodesk", "python-jobs", "himalayas", "jobicy", "arbeitnow"):
            r = self.client.post(f"/api/oppsources/presets/{pid}/add", json={})
            self.assertEqual(r.status_code, 200, (pid, r.text))
            self.assertEqual(r.json()["adapter"], S.PRESET_BY_ID[pid]["adapter"])

    def test_every_added_preset_names_the_terms_to_respect(self):
        for pid in ("workingnomads", "wwr-programming", "jobspresso", "nodesk", "python-jobs", "jobicy", "arbeitnow", "remotive", "remoteok"):
            self.assertIn("link back", S.PRESET_BY_ID[pid]["note"].lower(), pid)


if __name__ == "__main__":
    unittest.main()
