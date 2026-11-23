# 54 · Interview helper

> 🟡 Not tested on real devices yet. Built and tested with fake data only.

**In simple words:** when a company writes "can we talk?", this page does the boring part. It looks at your free time and writes a polite reply with three times you can do. Before a recruiter call it gives you a one-page card with your key points. When you know someone at the company it writes a short message asking for a referral. It only writes **drafts**. You copy them and send them from your own mail. It never sends anything and it never opens a booking link.

Open **Apps > Interview helper**. The same buttons also appear in the **Job tracker** (on each application) and on an interview-invite card in **Replies**.

## 1. Offer times for an interview invite
When reply sorting finds an interview invite in your mail, tap **Offer times**.
- You see up to three free slots in the next five working days (Monday to Friday, 10:00 to 19:00 IST by default, 30 minutes kept clear around every event) and a draft reply with them.
- Edit the draft if you like, tap **Copy reply**, paste it into your mail.
- If the mail has a booking link (Calendly and similar) the screen says so and **does not open it**. Open it yourself from your mail and pick a time.

> ⚠️ **Careful:** only calendars you switched on under Settings are read. **Google Calendar events are not read by the hub**, so check Google Calendar before you send. If no calendar is connected the times only follow your working hours, and the screen tells you that.

## 2. Call card
Tap **Call card** on an application. It shows the role, the company, where the job came from, its status, your **notice period**, your **salary ask**, your three strongest confirmed points for this job, questions to ask and red flags.
- The **salary ask is yours to type**. If you leave it empty it shows "not set". It is never guessed.
- The strong points are your own confirmed facts, shown as written. If none match the job, the card says so.
- Notice period comes from **Job preferences**. If it is not set, the card tells you where to set it.
- Friday can read it: say "call card for Acme" (tool `call_card`, read only).

## 3. Referral request
Tap **Referral draft** on an application. If someone in your **Networking** contacts works (or used to work) at the company, you get a short message: who you are applying for, one confirmed fact, and the question with the job link. Pick another contact from the list if you prefer.
- **Copy request** stays blocked until the job link and your name are filled.
- After you send it yourself, tap **I asked them**. That only writes today's date down; tap it again to undo. Nothing is sent from the app.
- If nobody in your contacts is linked to the company, the screen says so and you can add a contact in Networking.

## Settings
Working hours, buffer and slot length are keys in `hub.json` (`jobassist_work_start`, `jobassist_work_end`, `jobassist_buffer_minutes`, `jobassist_slot_minutes`). `jobassist_enabled: false` switches the whole page off.

Back to the [guide index](README.md).
