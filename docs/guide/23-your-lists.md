# 23 · Your lists: assistants, notifications, safety, privacy, memory

**What this page is for:** the things you can change yourself in Settings without touching code. Each one is a short list. The way it worked before is always the first entry or the default, and a "reset to defaults" button brings it back.

Open Settings > **Assistants and rules**. Every list looks the same: rows you can switch on or off, a pencil to edit, a bin to delete, **Add** at the bottom and **Reset to defaults**.

## Assistants
Jarvis and Friday are two ready-made assistants. You can:
- **rename** one ("Tony" instead of "Jarvis"; the new name wakes it too),
- **add** another, for example **Atlas** for study: give it a name, the words that wake it, how the microphone often mis-hears it, a short role prompt, a colour and the areas it owns (its "tool focus"),
- **switch one off** or **delete** it. At least one always stays on.

"Hey Atlas, quiz me on verbs" then works in the open app and on the laptop microphone. Tip: one-word names that are not everyday words wake best ("Atlas", not "Study"). The always-on Android listener still knows only its built-in phrases.

## Notification preferences
For every kind of notice (reminders, Autopilot drafts, research finished, backups, applications, posts, urgent alerts...) choose how it reaches you:
**Now** (a push), **Batch** (one short summary every half hour), **Quiet only** (visible in the app, never pushed) or **Off**. "Default" keeps the built-in behaviour. New kinds added by new features appear in the list on their own. Quiet hours are set on the same screen.

## My safety rules
The assistant already treats dangerous things as critical (typed word, then a countdown). Here you add your own: "anything touching `D:\payroll` is critical", "any command with `deploy` asks first". A rule can only make things **stricter**, never looser. Use the **Test** box to type a command and see its level before you rely on it. Plain words are enough; switch on "regular expression" only if you know what that is. A rule that could hang the hub is refused.

## My redaction rules
Cloud AI never sees emails, numbers and keys already. Add your own words (a client name, a project code name) or a pattern (`TCK-\d{4}`); they are swapped for `[CLIENT_1]` before text leaves the laptop and put back in the answer. **What will be hidden?** shows a preview. The activity log records only how many were hidden, never what.

## Memory kinds and tags
Memory items have a **kind** (note, fact, decision, RCA, task, research by default) and any number of **tags** you type yourself (`payroll, q4`). In Memory you can filter by kind, by tag and by assistant, and edit tags on each card. Under Settings > **Memory kinds** you add your own kinds and say which assistant sees them by default. A kind that still has items cannot be deleted. Asking Jarvis "recall the payroll RCA" can also filter by tag.

## If something looks wrong
- A name does not wake: check the wake name is one word and that two assistants do not share it. The save screen says so.
- A rule is refused: the message says why (too long, not valid, or a shape that could freeze the hub). Simplify it.
- Everything is back to normal with **Reset to defaults** (or "Remove all my rules").
