"""The curated skills dictionary behind the ATS match score (hub/ats.py).

Each line is `canonical: synonym, synonym`. A posting or resume that uses ANY spelling counts as the canonical skill, so
"JS" and "JavaScript" are one skill, "k8s" and "Kubernetes" are one skill. Groups: tech, data, product, ops, India (terms
that matter in the Indian market), soft skills and certifications. This is plain data: nothing here is ever added to a
resume; it only lets the score understand words.
"""
from __future__ import annotations

import re

_HARD = """
python: py3, python3
java:
javascript: js, ecmascript, es6, vanilla js
typescript: ts
golang: go lang, go language
rust:
php: php7, php8
ruby:
ruby on rails: rails, ror
c++: cpp
c#: csharp, c sharp
.net: dotnet, asp.net, .net core, dot net
kotlin:
swift:
scala:
bash: shell scripting, shell script, shell
powershell:
sql: t-sql, plsql, pl/sql, structured query language
nosql: no-sql
html: html5
css: css3
sass: scss
tailwind: tailwind css, tailwindcss
bootstrap:
react: reactjs, react.js
redux:
next.js: nextjs, next js
angular: angularjs, angular.js
vue: vuejs, vue.js
svelte:
jquery:
node.js: nodejs, node js, node
express: expressjs, express.js
nestjs: nest.js
django:
flask:
fastapi: fast api
spring boot: springboot, spring
laravel:
codeigniter: code igniter
symfony:
graphql: graph ql
rest api: restful, restful api, rest apis, rest services, restful services, rest
grpc:
websocket: websockets, web sockets
microservices: micro services, microservice, service oriented architecture, soa
system design: distributed systems, scalability, high availability, low level design, high level design, lld, hld
data structures: algorithms, dsa, data structures and algorithms
oop: object oriented, object-oriented, object oriented programming
design patterns:
unit testing: unit tests, junit, pytest, jest, mocha, phpunit
tdd: test driven development, test-driven development
selenium:
cypress:
playwright:
postman:
api testing: api test
manual testing: functional testing, regression testing, test cases
automation testing: test automation, qa automation
android:
ios:
flutter:
react native: react-native
mysql:
postgresql: postgres, psql, pgsql
mariadb:
mongodb: mongo
redis:
elasticsearch: elastic search, opensearch, elk
dynamodb: dynamo db
cassandra:
oracle: oracle db, oracle database
sql server: mssql, ms sql, microsoft sql server
sqlite:
snowflake:
bigquery: big query
redshift:
aws: amazon web services
ec2:
s3:
lambda: aws lambda, serverless
azure: microsoft azure
gcp: google cloud, google cloud platform
docker: containers, containerization, containerisation
kubernetes: k8s, eks, gke, aks
terraform: infrastructure as code, iac
ansible:
jenkins:
github actions: gh actions
gitlab ci: gitlab
ci/cd: cicd, ci cd, continuous integration, continuous delivery, continuous deployment
git: github, version control, bitbucket
linux: unix, ubuntu
nginx:
apache:
grafana:
prometheus:
datadog:
new relic: newrelic
monitoring: observability
rabbitmq: rabbit mq
kafka: apache kafka
message queue: sqs, message queues, pub/sub, pubsub
nifi: apache nifi
maxwell: maxwell's daemon, cdc, change data capture
caching: memcached, cache
security: application security, appsec, owasp, penetration testing, vapt
networking: tcp/ip, dns, load balancing, load balancer
etl: elt, data pipeline, data pipelines, data integration
data warehouse: data warehousing, dwh, data lake, datalake
data modeling: data modelling, dimensional modeling
spark: pyspark, apache spark
hadoop: hive, hdfs, mapreduce
airflow: apache airflow
dbt:
pandas:
numpy:
scikit-learn: sklearn, scikit learn
machine learning: ml, ml models
deep learning: neural networks, neural network
nlp: natural language processing
computer vision: opencv, image processing
pytorch:
tensorflow: keras
llm: llms, large language models, generative ai, genai, gen ai
prompt engineering: prompt design
mlops: ml ops, model deployment
statistics: statistical analysis, statistical modeling, hypothesis testing, regression analysis
a/b testing: ab testing, a/b tests, experimentation, split testing
data analysis: data analytics, analytics, data analyst
data visualization: data visualisation, dashboards, dashboarding, reporting
tableau:
power bi: powerbi, power-bi
looker: looker studio, data studio
excel: ms excel, microsoft excel, advanced excel, vlookup, pivot tables, pivot table, spreadsheets, google sheets
r programming: rstudio, r language
sas:
product management: product manager, product owner, product ownership
roadmap: product roadmap, roadmapping, roadmaps
product strategy: go-to-market, gtm, go to market
user stories: acceptance criteria, requirements gathering, requirement gathering, brd, prd, frd
user research: customer research, customer discovery, user interviews, usability testing
wireframing: wireframes, prototyping, prototypes
figma:
ux design: ui/ux, ui ux, user experience, ux, ui design, user interface, interaction design
product analytics: mixpanel, amplitude, clevertap, moengage, google analytics, ga4
okr: okrs, kpi, kpis, metrics
agile: agile methodology, agile development
scrum: scrum master, sprint planning, sprints, sprint
kanban:
jira: confluence, atlassian
backlog grooming: backlog prioritization, backlog prioritisation, backlog management, prioritization, prioritisation
project management: program management, project planning, pmo, project manager
supply chain: supply chain management, scm, logistics, warehouse management, warehousing
procurement: purchasing, sourcing, vendor management, vendor development
inventory management: inventory control, inventory planning, demand planning, demand forecasting
process improvement: process optimization, process optimisation, continuous improvement, kaizen
six sigma: lean six sigma, lean, black belt, green belt
sop: standard operating procedures, sops
operations management: business operations, operations, ops
erp: sap, sap erp, oracle erp, netsuite, odoo
crm: salesforce, hubspot, zoho crm, zoho
customer support: customer service, customer success, helpdesk, zendesk, freshdesk, support tickets
sales: b2b sales, inside sales, lead generation, business development, account management
digital marketing: seo, sem, performance marketing, social media marketing, google ads, facebook ads, content marketing
email marketing: mailchimp, marketing automation
content writing: copywriting, technical writing, content creation, blogging
budgeting: forecasting, financial planning, fp&a, financial modeling, financial modelling, p&l, profit and loss
accounting: bookkeeping, accounts payable, accounts receivable, general ledger, reconciliation, tally, tally erp, quickbooks
audit: internal audit, statutory audit, auditing
compliance: regulatory compliance, risk management, risk assessment, governance, grc
taxation: gst, tds, income tax, direct tax, indirect tax
recruitment: talent acquisition, sourcing candidates, hiring, onboarding, hr operations, payroll
"""

_INDIA = """
upi: unified payments interface, upi payments
npci: national payments corporation
bbps: bharat bill payment
imps:
neft: rtgs
aadhaar: aadhar, ekyc, e-kyc
kyc: know your customer, aml, anti money laundering, kyc/aml
fintech: financial technology, payments, payment systems
payment gateway: razorpay, paytm, phonepe, payu, cashfree, ccavenue, billdesk, juspay
lending: loan origination, loan management, nbfc, credit underwriting, underwriting, credit risk, collections
core banking: banking, cbs, finacle, flexcube, bancs
rbi: reserve bank of india, rbi guidelines, rbi compliance
cibil: credit bureau, credit score, experian
ondc: open network for digital commerce
digilocker:
gst filing: gstr, gst returns, e-invoicing, e-way bill
msme: sme
ifrs: ind as, gaap, us gaap
ecommerce: e-commerce, d2c, marketplace, quick commerce, flipkart, amazon seller, shopify
edtech:
healthtech:
insurance: insurtech, claims, policy administration
"""

_SOFT = """
communication: communication skills, verbal communication, written communication, excellent communication, interpersonal
leadership: led a team, led teams, team lead, team leadership, people management, managed a team, managed teams, team management
teamwork: team player, collaboration, collaborative, cross-functional, cross functional, collaborate
problem solving: problem-solving, troubleshooting, analytical thinking, analytical skills, analytical, critical thinking
stakeholder management: stakeholder communication, stakeholder engagement, client management, client facing, client-facing, customer facing
ownership: end to end ownership, end-to-end ownership, take ownership, accountability, self-starter, self starter, proactive, initiative
mentoring: mentorship, mentored, coaching, coached, training others
adaptability: adaptable, flexibility, fast learner, quick learner, learning agility, learn quickly
time management: prioritisation skills, deadline driven, deadline-driven, meet deadlines, multitasking
presentation: presentation skills, public speaking, presenting
negotiation: negotiating, negotiation skills, conflict resolution
decision making: decision-making, judgement, judgment
attention to detail: detail oriented, detail-oriented, accuracy
creativity: creative thinking, innovative, innovation
customer focus: customer centric, customer-centric, customer obsession, user focused, user-focused
"""

_CERTS = """
aws certified: aws certification, aws solutions architect, aws certified solutions architect, aws developer associate
azure certified: az-900, az-104, azure certification
google cloud certified: gcp certification, professional cloud architect
cka: certified kubernetes administrator, ckad
pmp: project management professional
csm: certified scrum master, psm, safe agilist
cissp:
ceh: certified ethical hacker
itil:
ccna: ccnp
cfa: chartered financial analyst
frm:
chartered accountant: icai, ca final, ca inter
cpa:
company secretary: icsi
cma: cost accountant, icmai
"""


def _parse(block: str, kind: str, group: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in block.strip().splitlines():
        head, _, tail = line.partition(":")
        # "c++:" and ".net:" contain no colon, but "next.js: nextjs" does; the first colon always ends the name.
        canon = head.strip().lower()
        if not canon:
            continue
        syns = [s.strip().lower() for s in tail.split(",") if s.strip()]
        out[canon] = {"kind": kind, "group": group, "synonyms": syns}
    return out


SKILLS: dict[str, dict] = {}
SKILLS.update(_parse(_HARD, "hard", "general"))
SKILLS.update(_parse(_INDIA, "hard", "india"))
SKILLS.update(_parse(_SOFT, "soft", "soft"))
SKILLS.update(_parse(_CERTS, "cert", "cert"))

# Words that are skills only in a narrow sense; matched case-sensitively as written to avoid "go to market" and "r" noise.
_CASE_SENSITIVE = {"Go": "golang", "R": "r programming", "C": "c language"}

# Spellings so common in ordinary prose that a plain word match would be wrong; these only count with a clue next to them.
_AMBIGUOUS = {"shell", "node", "spring", "lean", "lambda", "metrics", "analytics", "operations", "ops", "hiring", "claims", "sourcing", "sprint", "sprints",
              "rest", "cache", "payments", "marketplace", "governance", "reporting", "dashboards", "initiative", "innovative", "innovation", "ux", "ui",
              "training others", "proactive", "forecasting", "ml", "sas", "ca", "sme", "ps", "cs", "iac", "ror", "ts", "py3", "js"}


def _build() -> tuple[dict[str, re.Pattern], dict[str, str]]:
    """One small pattern per first letter (longest spelling first, so 'react native' wins over 'react'): a word is tried only against the
    spellings that start with its letter, which keeps scanning a long posting fast."""
    spell: dict[str, str] = {}
    for canon, meta in SKILLS.items():
        spell[canon] = canon
        for s in meta["synonyms"]:
            spell.setdefault(s, canon)
    groups: dict[str, list[str]] = {}
    for term in sorted(spell, key=lambda t: (-len(t), t)):
        groups.setdefault(term[0], []).append(term)
    return {ch: re.compile("(?:" + "|".join(re.escape(t) for t in terms) + ")(?![a-z0-9+#])") for ch, terms in groups.items()}, spell


_BY_CHAR, SPELLINGS = _build()
_START = re.compile(r"(?<![a-z0-9+#])[a-z0-9.+#]")
# A spelling a plain-word match should ignore unless it sits in a skills-like context (see `_ambiguous_ok`).
AMBIGUOUS = _AMBIGUOUS


def canonical(term: str) -> str:
    """The canonical skill for any spelling ('k8s' -> 'kubernetes'); the cleaned text itself when it is not in the dictionary."""
    t = re.sub(r"\s+", " ", (term or "").strip().lower())
    return SPELLINGS.get(t, t)


def kind_of(term: str) -> str:
    return SKILLS.get(canonical(term), {}).get("kind", "hard")


def find(text: str) -> dict[str, list[int]]:
    """{canonical skill: [start offsets]} for every dictionary skill in `text` (any spelling). Ambiguous spellings count only
    when written in a skills-like context (see `_ambiguous_ok`)."""
    low = (text or "").lower()
    found: dict[str, list[int]] = {}
    for st in _START.finditer(low):
        rx = _BY_CHAR.get(st.group(0))
        m = rx.match(low, st.start()) if rx else None
        if m is None:
            continue
        word = m.group(0)
        canon = SPELLINGS[word]
        if word in AMBIGUOUS and not _ambiguous_ok(low, m):
            continue
        found.setdefault(canon, []).append(m.start())
    for word, canon in _CASE_SENSITIVE.items():
        for m in re.finditer(r"(?<![A-Za-z0-9+#])" + re.escape(word) + r"(?![A-Za-z0-9+#])", text or ""):
            around = (text[max(0, m.start() - 25):m.end() + 25]).lower()
            if word == "Go" and re.search(r"\b(?:golang|in go|using go|go,|go and|and go|go/|, go\b|experience with go)", around) and "go to" not in around:
                found.setdefault(canon, []).append(m.start())
            elif word in ("R", "C") and re.search(r"(?:python|sql|java|stata|sas|matlab)[, /&]+" + word + r"\b|\b" + word + r"[, /&]+(?:python|sql|java|c\+\+)", text or "", re.I):
                found.setdefault(canon, []).append(m.start())
    return found


def _ambiguous_ok(low: str, m: re.Match) -> bool:
    """An ambiguous spelling counts when it sits in a list of skills ('Python, SQL, shell') or after 'experience with'."""
    left = low[max(0, m.start() - 40):m.start()]
    right = low[m.end():m.end() + 40]
    if re.search(r"(?:experience (?:with|in|of)|knowledge of|proficien\w+ (?:in|with)|skilled in|expertise in|hands[- ]on|familiar\w* with|using|tools?:|stack:|skills?:)\s*[^.\n]{0,40}$", left):
        return True
    return bool(re.match(r"\s*[,/|;]\s*[a-z0-9+#.]", right)) or bool(re.search(r"[,/|;]\s*$", left))
