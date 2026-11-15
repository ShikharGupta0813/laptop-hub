# 38 · Google Chat with a personal Google account

**What this page is for:** you use Google Chat with a normal Google account (not Google Workspace). REDLINE can tell you what people wrote and prepare your answers, but it **cannot send a Chat message for you**. You always paste and press Send.

## Why it cannot send
Google's Chat webhooks and Chat apps are Workspace features, and it is not confirmed that a personal account may use the Chat API at all, so REDLINE does not pretend to. What it does instead is safe and works today.

## Where to find it
Settings > Alerts > **Google Chat channels**. Replies: the **Chat replies** button there, or **Reply in Google Chat** on a Chat alert.

## Reading Chat (two ways, both optional)
1. **Phone notifications.** With the Android app's notification access on and Google Chat on your capture list (Alerts settings), each message becomes an alert with the sender, the space (or "Direct message"), and whether you were mentioned. REDLINE only sees the preview line Chat shows, never the whole conversation.
2. **Chat emails.** Turn on email notifications in Chat and connect your mail. Chat emails you about messages you missed; REDLINE reads them as Chat messages. They arrive late, and if you saw the same message as a notification you still get **one** alert. The email format is a best guess: paste a real one into **Check a sample** to see what REDLINE reads, and edit the sender or subject words in the "Google Chat notification emails" entry if it does not match.

## Your channels
Add a **space or person** to handle it your way: switch it **off to mute** it, tick **only when I am mentioned**, and paste the space's **Google Chat link** (open it in Chat on the web and copy the address) so Reply jumps straight to that conversation. Without a link Chat opens on its home screen and you pick the conversation.

## Replying (copy, then open)
Each waiting message has its **own** text box. Edit it, or tap **New draft** (built from your Memory and tasks; it quotes only what you saved). Nothing you write for one conversation is copied to another.
- **On the phone:** **Copy and open Google Chat** copies your text and opens Chat. Paste it and press Send.
- **At the laptop:** **Open on the laptop** copies to the laptop's clipboard and opens chat.google.com in your own browser. Paste and press Send. (On Linux install `wl-clipboard`, `xclip` or `xsel`.)
Ask the assistant too, by voice or text: "anything in Google Chat I need to answer?" and "draft a reply to the design space saying I will review by 3". It only writes drafts.

## Experimental: draft in the Chat web page (off)
Switch on **Chat web page draft** (a warning asks you to confirm), save the space link, install Playwright, then tap **Draft in Chat web page** on a message. A visible browser window opens, types your draft into the compose box and **stops**. You check it is the right conversation and press Send.
> ⚠️ **Risk:** Google may refuse to sign in to an automated browser (then this cannot work), and automating a Google web app may be against Google's terms, so your account could be limited. It is not verified against the live page. Prefer Copy and open.

## Not offered
The Chat REST API with your own sign-in: not shipped because it is unconfirmed that a personal account can use it. Details and what was and was not verified: `docs/api/chatchan.md`. Back to the [guide index](README.md).
