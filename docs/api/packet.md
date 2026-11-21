# Application packet API

`hub/packet.py` (packet, routes, assistant tool `packet_prepare`). Screen `js/ats.js` (Apps > ATS and packets, tab Packet). Needs the login cookie. Switched off with `tailor_enabled: false`. Data: `packets.json` (references only: which variant, which application, the posting text and questions; at most 40).

One screen for one posting: the tailored resume, the cover note, the ATS score, answers from the answer bank for the form's questions, a short company summary from the posting text, the pay range if the posting shows one, and links. You review it once, then put it in the application queue with one tap, or copy / download it.

## Rules
- **It never submits, sends or posts anything.** The module imports no mail, browser or form code. "Put in the queue" writes a draft into the application queue (`hub/applyq.py`); sending from there still needs your own tap, and a draft with a `[FILL: ...]` blank is refused by the queue itself.
- Built from your facts and the posting text only. No outside call; the company summary is quoted from the posting, nothing is looked up. An AI model is used only if `tailor_ai` is on (same rule as Tailor).
- Answers: only your approved bank answers. A question only you answer (gender, ethnicity, disability, ratings...) is shown as "only you answer this" and is never put in a draft. An answer with a blank to fill, or no answer, is not put in a draft either. A suggestion below your confidence limit is shown, and goes into the draft only when you tap "put in the queue" (that tap is your review).
- The tailored version is reused when the application already has one (`fresh: true` makes a new one). A blocked cover note (unbacked sentence, blank, scam signal not acknowledged) stays visibly blocked: no cover download, and the queue draft carries the blank.

## Routes
- `POST /api/packet/prepare {application_id | job:{title, description, company?, url?, contact_email?}, questions?[], master_id?, fresh?, ai?}` -> the packet (below). 400 for a posting without text or no facts; 404 for an unknown application.
- `GET /api/packet` -> `{packets[{id, title, company, state, created, application_id}]}`. `GET /api/packet/{id}` -> the packet rebuilt from the current facts and answer bank. `DELETE /api/packet/{id}`.
- `POST /api/packet/{id}/queue` -> `{ok, application_id, created, blocked, blockers, answers_added, message, nothing_sent: true}`. A packet from a pasted posting adds a draft item (source `packet`); one from an application updates that item's draft (cover note, usable answers) and records `resume_version`. A second tap changes nothing new.
- `GET /api/packet/{id}/text` -> plain text of the whole packet (to copy). Downloads of the resume and cover note are Tailor's own exports (`/api/tailor/variants/{vid}/export`), listed in `downloads[]`.

Packet: `{id, state draft|queued, application_id, job, channel, fit{score, label, summary, flags[], risk_blocked}, ats{score, label, breakdown[], matched[], missing{listed_in_facts[], gap[]}, tips[]}, resume{variant_id, text, version, master}, cover{text, words}, blocked, blockers[], answers[{question, status auto|suggest|owner_only|none, answer, confidence, reason, fills[], owner_only, usable}], company{company, text, source, site, email_domain, note}, pay{found, lines[], amounts[], note}, links{posting, apply_email, yours[]}, downloads[], can_queue, nothing_sent: true, note}`.
The form's questions are yours (`questions`), questions written in the posting, and the standard ones (notice period, salary, work authorization when mentioned, portfolio when mentioned, relocation when mentioned, why this company).

## Friday
`packet_prepare` (draft only; untrusted: posting text): `application_id` or `title` + `description`, optional `questions[]`. Builds the packet and says where to review it; it does not queue, send or apply. Group `ats`.

Tests: `tests/test_packet.py` (PacketTests, PacketPieceTests).
