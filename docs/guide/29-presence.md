# 29. Presence: alerts that know where you are

The hub watches a few signals (is the laptop in use, is it locked, is the phone app open, are you in a meeting) and decides how each alert should reach you. At the laptop you get a quiet toast. Away, the alert goes to your phone and is spoken if the app is open. In a meeting or in focus mode everything is held and you get one summary after ("You missed 3 things in the meeting"), except emergencies.

Say "Jarvis, I am going out", "I'm back" or "focus for an hour", or tap the presence row on Home to start focus. Change the rules in Settings, Delivery rules. Windows toasts are off until you set `presence_laptop_toasts` to true in `hub.json`. If the hub cannot tell where you are, everything is delivered the old way. Details: `docs/api/presence.md`.
