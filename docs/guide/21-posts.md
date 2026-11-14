# 21 · Posts: write once, send to many places

**What this page is for:** turning what you already did (bugs fixed, decisions made) into posts for dev.to, Hashnode, your own blog, LinkedIn, X and more, without anything being posted behind your back.

## The idea

Open **Apps, Posts**. Four tabs: **Ideas, Drafts, Published, Destinations**.

1. **Ideas**: tap *Find ideas from my work*. Jarvis reads your memories, build specs and recent commit titles (nothing marked private or confidential) and suggests titles with a two-line angle.
2. **Drafts**: tap *Draft this*. A markdown draft appears, saved on your laptop only. Edit it, use *Preview*, tap *Save*.
3. **Send to destinations** (inside a draft): tick the places you want and tap *Prepare selected*. Each place gets its own version (a long article for blogs, a short post for LinkedIn, a numbered thread for X), and
   places that keep drafts (dev.to, Hashnode, WordPress, Ghost, a markdown folder) get an **unpublished** draft you can look at there.
4. **Publish** is a different button for each place, with its own question ("Publish on dev.to?"). Nothing publishes by itself, ever: not the weekly reminder, not the schedule, not the assistant without your approval
   card, and not even in Full Control.
5. **Manual places** (LinkedIn, Medium, X, Reddit and any you add): tap *Show text*, *Copy*, *Open* the site, paste, then tap **I posted** (optionally paste the link to your post).

## Destinations: your list, your rules

The **Destinations** tab is the list of places. Tap one to edit it, switch it off, test it or delete it. **Add a destination**: pick a kind and give it a name.

- **dev.to, Hashnode, WordPress, Ghost**: paste the key the service gives you (dev.to: Settings, Extensions; Hashnode: Settings, Developer, plus your publication id; WordPress: Users, Profile, Application Passwords, plus the site address and your username;
  Ghost: Settings, Integrations, the Admin API key, plus the site address). Tap **Test connection**. Keys live in this laptop's keyring and are never shown again.
- **Markdown file / GitHub Pages**: type the folder of your site (for example the `_posts` folder). The hub writes a `.md` file with front matter (`draft: true` until you publish). It never runs git: you commit and push.
- **Webhook**: any address that accepts a POST; when you publish it receives `title`, `body_markdown`, `tags`, `canonical_url`. This is how you reach a site with no ready-made kind (Zapier, n8n, your own server).
- **Manual (copy and paste)**: type a name and the site's "new post" address. This is how LinkedIn, Medium, X and Reddit work, because they have no safe way for the hub to post for you.

Each destination has its own **writing rules**: style (article, short post, thread), maximum length, hashtags on or off, how many tags, title length, and a template for short posts. *Reset to starter list* brings back dev.to, Hashnode, WordPress,
Ghost, Medium, LinkedIn, X, Reddit and a blog webhook (places you added are removed).

Your old dev.to key and drafts were moved into a **dev.to** destination automatically.

## Safety

- Words that must never appear in a post (your company, a customer) go in **Blocklist** (bottom of the Destinations tab). **Do not publish company confidential material.** A draft with one of those words cannot be saved to any place, published or turned into text.
- Reminders and stats (off by default): a **weekly posting reminder** ("Chief, no post for 10 days, ideas ready.") and a **stats check** that reads views, reactions and new comments from every place that offers them. Friday tells you in her batch message.
  Comments are written by strangers, so they are shown as plain text only.

## If something goes wrong

- "Finish setting up X first": open the destination, fill in the fields and save the key.
- "X refused the key": make a new one on that service and save it again.
- "This text mentions ... (blocklist)": remove that word from the draft or from the blocklist.
- "Save it as a ... draft first": tap *Save draft* for that place, look at it there, then publish.
- A draft marked "template, no AI": the AI was not available; write over it.
