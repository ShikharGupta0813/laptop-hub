# 17 · Build flow: tell Jarvis what to build

**What this page is for:** how to get a small project built from a sentence, without writing the plan yourself.

## What happens

1. You say "Jarvis, build me a page that tracks my habits" (or open Apps, Build, type it, pick a folder).
2. Jarvis asks a few short questions, one at a time. Tap a suggested answer or type your own. At most 6.
3. He writes a **spec** (a plain-language description: goal, scope, what is not included, how, how to check, risks) into `docs/specs/` in your folder. You see the file name and the first lines and tap to approve.
4. He turns it into a **plan**: a list of small steps with boxes, in `docs/plans/`. Open the file and change it if you like; the boxes in that file are the progress.
5. Tap **Do next step** (or say "next step"). The free Council models work on that one step in the background. When it passes, the box is ticked. If it fails, the box stays and you see why.
6. When every box is ticked you get a notification, and a note is saved in Memory.

## Good to know

- The folder must be one of your shared folders. Jarvis will not build anywhere else.
- Nothing is ever overwritten: a clash gets a `-2` on the file name.
- You approve each file and each step (unless Full Control is on).
- Questions use the free AI (Gemini or your local model), never the paid Claude API.
- Delete a build on the screen and your spec and plan files stay.
- Switch it off with `buildflow_enabled` in Settings.
