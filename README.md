# AFK Bot — Setup Guide (Follow every step in order)

## STEP 1: Get a fresh, working bot token

1. Open this link in your browser: https://discord.com/developers/applications
2. Click on your bot's application (the one named something like "AFK").
3. On the left side, click **Bot**.
4. Look at the top of that page. If you see any orange or red warning text,
   take a screenshot of it and stop here — send it back before continuing.
5. Find the button that says **Reset Token**. Click it. Confirm if asked.
6. A new token will appear ONE TIME ONLY. Click the **Copy** button right
   next to it immediately.
7. Do not click Reset again. Do not refresh the page. Just copy it once.

## STEP 2: Put the token in the right place

1. In your project folder (`AFK bot`), find the file named `.env`
   - If you don't have one yet, copy `.env.example` and rename the copy to `.env`
2. Open `.env` using Notepad (right-click the file → Open with → Notepad).
3. Delete everything on the `DISCORD_TOKEN=` line after the `=` sign.
4. Paste your freshly copied token right after the `=` sign, with:
   - no quotes around it
   - no spaces before or after it
   - nothing else on that line
   
   It should look exactly like this (but with your real token):
   ```
   DISCORD_TOKEN=MTUyMjU2ODIxOTMy...your-own-long-token...
   ```
5. Save the file (Ctrl+S) and fully close Notepad.

## STEP 3: Turn on required bot settings (only needs doing once)

1. Back on the Discord Developer Portal, still on the **Bot** page.
2. Scroll down to **Privileged Gateway Intents**.
3. Turn ON:
   - Presence Intent
   - Server Members Intent
4. Scroll down and click **Save Changes** if it appears.

## STEP 4: Run the bot

1. Open PowerShell in your project folder (the one with `bot.py` in it).
2. Make sure your virtual environment is active — you should see `(venv)`
   at the start of the line. If not, run:
   ```
   .\venv\Scripts\activate
   ```
3. Run:
   ```
   python bot.py
   ```
4. You should see lines ending in something like:
   ```
   INFO Logged in as AFK#0337 (id: ...)
   INFO AFK timeout set to 120 seconds
   ```
   If you see that, it worked — leave this window open and running.

## STEP 5: Test it

1. In Discord, join any voice channel.
2. Mute your mic OR deafen yourself (click the mic or headphone icon).
3. Wait 2 full minutes without unmuting.
4. Check the text channel (or your server's default channel) — you should
   see a message like:
   ```
   🔴 YourName has gone AFK (muted/deafened).
   ```
5. Unmute/undeafen — you should then see:
   ```
   🟢 YourName is back.
   ```

## If it still doesn't log in (still shows "Improper token" or "401")

This means the token itself isn't being accepted by Discord — it is not
something in the code. Please check:

- Is the bot's application still shown normally on
  https://discord.com/developers/applications (not greyed out, no
  "disabled" label)?
- Did you copy the token from the **Bot** tab (NOT the OAuth2 tab —
  Client ID and Client Secret will NOT work here)?
- Is `.env` saved as plain text, literally named `.env` (not `.env.txt`)?

If all of that looks right and it still fails, the bot's application may
have been disabled by Discord itself, and a new application may need to
be created from scratch (Step 1, but click "New Application" instead of
selecting the existing one).

## Dead by Daylight: one control panel for everything

Everyone stays in **one** voice channel the whole time. When it's someone's
turn to be killer, the bot server-deafens them — they can still talk and
everyone hears them, but they can no longer hear the survivors. Every DBD
feature (killer swap, rotation queue, ready checks, random builds, result
tracking, scoreboard) is available from a single button panel — no typing
needed once it's set up.

**One-time setup:**

1. In the Discord Developer Portal, on the **Bot** page, make sure
   **Server Members Intent** is turned on.
2. Give the bot's role the **Deafen Members** permission:
   Server Settings → Roles → click the bot's role → turn on **Deafen Members**.
3. If typing `/` doesn't show the bot's commands, re-invite it with this
   URL (replace YOUR_CLIENT_ID with the Application ID from the
   **General Information** tab) — this won't remove the bot or lose
   settings, it just adds the missing permission scope:
   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=8&scope=bot%20applications.commands
   ```
4. Restart the bot after any of the above changes.

**Post the panel (do this once, ever, in whichever channel you want it in):**

Type `/dbd_panel` and send it. From then on, the panel **automatically
reposts itself to stay the last message in that channel** — every time
anyone posts anything else there, the bot deletes the old panel and drops
a fresh copy at the bottom. You never run `/dbd_panel` again unless you
want to move it to a different channel.

**What each button does:**

| Button | What it does |
|---|---|
| 🔪 I'm Killer | Deafens you — you can still talk, but can't hear survivors |
| 🏃 I'm Back | Restores your hearing when your round ends |
| ⏭️ Next Killer | Advances the rotation set up with `/queue_setup` |
| ✅ Ready Check | Pings everyone in your voice channel, tracks who's confirmed ready |
| 🎲 Random Build | Gives you a random 4-perk build (survivor, or killer if you're currently marked as one) |
| 🏃💨 Escaped | Reports your own outcome for the current round |
| 💀 Sacrificed | Reports your own outcome for the current round |
| 📊 Scoreboard | Shows kills/escapes standings across everyone |
| 📋 Show Queue | Shows the killer rotation order and whose turn it is |

Clicking **I'm the Killer**, **Next Killer**, or picking someone via
`/killer` automatically starts tracking a "round" behind the scenes — that's
what makes the Escaped/Sacrificed buttons and the scoreboard work without
any extra setup. The round closes automatically the moment **I'm Back** is
clicked (or `/back` is run), and the results get folded into the permanent
scoreboard right then.

### 🔊 Keeping break/loadout time fair to everyone

The deafening should only ever cover the actual match itself — not the
break in between. Here's the workflow that keeps it fair:

1. **The moment a match ends, click "I'm Back"** for whoever was killer.
   Their hearing is restored immediately, so they're part of the normal
   conversation for the break/loadout chat just like everyone else —
   nobody's left out.
2. When you're actually ready to **start** the next match, click
   **🔪 I'm Killer** (or **⏭️ Next Killer**) for whoever's turn it is. That's
   the only moment anyone gets deafened, and only for the duration of
   that match.

So deafening only ever happens *during* a live match — the break is
always fully normal, two-way conversation for everyone, including
whoever just played killer.

**Killer rotation queue setup:**

1. Run `/queue_setup` and pick 4–6 players in the order they take turns.
2. From then on, just use the **⏭️ Next Killer** button (or `/next_killer`)
   each round — it automatically picks the next person, deafens them, and
   restores the last killer.
3. The rotation loops forever and is saved to disk, so it survives bot
   restarts. Re-run `/queue_setup` any time to change the lineup.

**Fallback slash commands** (do the same things as the panel buttons, in
case you'd rather type a command and name someone directly):
- `/killer @player`, `/back @player`
- `/next_killer`, `/queue_show`
- `/result escaped` / `/result sacrificed`
- `/scoreboard`
- `/random_build`
- `/ready_check`

## Custom status rotator

The bot's Discord status (shown under its name in the member list) cycles
through a rotating set of fun Dead by Daylight-themed messages every 10
minutes automatically — no setup needed. Examples:
- "Watching 4 survivors run"
- "Listening to generator noises"
- "Watching for muted mics"

To change how often it rotates, add this to `.env` (value in minutes):
```
STATUS_ROTATE_MINUTES=15
```
To change the messages themselves, open `bot.py` and edit the
`STATUS_MESSAGES` list near the top of the file — each entry is an
activity type (`watching`, `listening`, or `playing`) plus the text.


## Optional: choose which channel gets the AFK announcements

By default, messages post in your server's system/default channel. To pick
a specific channel instead:

1. In Discord, go to Settings → Advanced → turn on **Developer Mode**.
2. Right-click the channel you want → **Copy Channel ID**.
3. In `.env`, add this line:
   ```
   AFK_ANNOUNCE_CHANNEL_ID=paste_the_id_here
   ```
4. Save `.env` and restart the bot (Ctrl+C in PowerShell, then `python bot.py` again).

## Voice session summary (one message per group hangout)

A "session" here means a **voice channel**, not an individual person. It
starts the moment a channel goes from empty to having someone in it, and
ends the moment it goes back to fully empty. When that happens, the bot
posts one summary in the same channel as the AFK announcements — total
duration and everyone who was in it at any point during that time. All
times are shown in **Sri Lanka Time (SLT, UTC+5:30)**:

```
📊 Voice session #3 ended in Music 24/7 — 18:02–21:15 SLT (3h 13m).
🚀 Started by: Alex
👥 5 participant(s): Alex, NoisyBoy, Sam, Jordan, Kai
```

Notes:
- Everyone who was in the channel at any point during the session gets
  listed, even if they left a bit before the very end.
- If someone hops between two different voice channels, that's treated as
  leaving one channel's session and joining another's — each channel
  tracks its own session independently.
- The `#N` counter counts up for as long as the bot keeps running: it
  resets back to 1 only when the bot itself is restarted (there's no
  daily/weekly reset).
- If the bot is restarted while a channel is occupied, that in-progress
  session isn't recovered — tracking starts fresh for whoever is in voice
  at the moment the bot (re)starts, using that moment as the new start time.

