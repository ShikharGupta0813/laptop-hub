# 26 · Work lanes: give routine work to the Council

**What this page is for:** save your Claude and Gemini limits. Drafts, summaries, reformatting and extraction do not need a paid model. Work lanes send them to the **free Council** (the team of local models) or your **local model** first, and use Gemini or Claude only when those could not do it.

## The idea
A **lane** is a place work can go: Council, Local model, Gemini, Claude (my login), or any AI you added in Providers. Each has a cost class (free, limited, paid) and an optional daily cap. A **class** is a kind of work (routine, research, coding, hard reasoning, private) with its lanes in order. Say "give this to the council" and routine work follows the routine order: Council, then Local, then Gemini.

## Using it
- **Talk:** the small row above the message box says Auto, Council, Local, Gemini... Auto is the normal assistant. Pick a lane and your message goes to that lane instead.
- **Memory (Brain) and Today:** a **Send to Council** button on tasks. The answer is added to the task as a note when the Council finishes.
- **Build:** "Run this step with" Council (default) or another lane.
- **Applications:** in the draft editor, "Write draft with" Template, Council or AI. You still check and tap Save and Send.
- **Ask Jarvis or Friday:** "give this to the council", "save my tokens". They ask you first and show which lane goes first.
- **Council screen:** the tile "Saved calls this week" counts the calls a free lane answered so Gemini and Claude were not used.

## When it moves to a stronger lane
Only when the lane failed, timed out or was not available; when the Council's own check failed, it stopped or gave no answer; or when you tap **Try a stronger model** on a result. Each step records why. A lane over its daily cap is skipped with a note.

## Private work
The class **Private** never leaves the laptop: only local models are used (the Council is limited to its local models), and it is never sent to Gemini or Claude, even if you edit the list. If nothing local could answer, the run says so and waits for you.

## Edit it
**Settings, Work lanes** has two lists: Lanes (name, kind, cost, daily cap, on or off) and Task classes (which lanes, in what order, private or not). Add your own, reorder, switch off, delete, or go back to the ready-made lists. Claude through the Council still needs `council_allow_claude` in hub.json.

Details for builders: `docs/api/lanes.md`.
