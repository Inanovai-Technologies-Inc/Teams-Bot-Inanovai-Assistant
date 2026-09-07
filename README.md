# Teams Agent — Stage 1 (Echo Bot)

A Microsoft Teams bot in Python. Right now it echoes your messages back.
The point of Stage 1 is not the echo — it is that all the plumbing works,
so that later you only swap one function to get a real agent, and eventually
NemoClaw.

## Status: running and verified locally ✅

## The five stages

| Stage | What it does | Status |
|-------|--------------|--------|
| 1 | Echo bot — proves the Teams plumbing works | **done, running** |
| 2 | Agent layer separated behind one function | **done** (`agent.py`) |
| 3 | Real model behind that function | **done** — GPT-4o, live |
| 4 | Slow replies — typing bubble, timeout, no double answers | **done** |
| 5 | Point the agent at NemoClaw | **next** |

Stages 1 and 2 are already built. `agent.py` is the only file you will
change for Stage 3 and Stage 5.

## The files

```
app.py            the server. One URL: POST /api/messages
bot.py            the Teams translator. Teams in -> plain text out
agent.py          <-- THE ONLY FILE YOU CHANGE LATER
config.py         reads settings from .env
test_local.py     fakes Teams so you can test with no Azure, no tunnel
test_agent.py     fakes the model so you can test the agent for free
test_bot.py       tests the typing bubble, timeout and duplicate guard
run_tests.py      runs all three suites
package_app.py    builds the .zip you upload to Teams
appPackage/       manifest.json + icons (the Teams app definition)
```

The important idea: `bot.py` knows about Teams but has no intelligence.
`agent.py` has the intelligence but knows nothing about Teams. Swapping
one never breaks the other.

---

## Run it (this already works)

Open a terminal in this folder.

**Terminal 1 — start the bot:**

```bash
.venv\Scripts\python.exe app.py
```

You should see `Teams bot is running`. Leave it open.

**Terminal 2 — talk to it:**

```bash
.venv\Scripts\python.exe test_local.py
```

Expected output:

```
  PASS  greeting
  PASS  ping command
  PASS  help command
  PASS  echoes arbitrary text
  PASS  empty message
  PASS  strips @mention
  PASS  welcome on install
  PASS  welcome without channelData
  8/8 passed
```

`test_local.py` stands up a fake Teams service on port 3979 and sends the
bot the same events real Teams sends: direct messages, an @mention inside a
channel, and the install event. Run it after any change to `agent.py`.

You can also open <http://localhost:3978/> in a browser for a health check.

---

## Getting it into real Teams

Everything above needs nothing from anyone else. The steps below need an
Azure account and your Teams admin. Do them in order.

### Step 1 — Expose your laptop to the internet

Teams cannot reach `localhost`. You need a public URL that forwards to it.

```bash
npx --yes devtunnel host -p 3978 --allow-anonymous
```

or with ngrok:

```bash
ngrok http 3978
```

Copy the `https://...` URL it prints. Keep this terminal open too.

### Step 2 — Create the Azure Bot

1. Go to <https://portal.azure.com> → **Create a resource** → search **Azure Bot** → Create.
2. Bot handle: anything unique. Type of App: **Multi Tenant**.
3. Creation type: **Create new Microsoft App ID**.
4. Once created, open it → **Configuration** → set
   **Messaging endpoint** to `https://YOUR-TUNNEL-URL/api/messages`.
5. Copy the **Microsoft App ID** shown there.
6. Click **Manage** next to the App ID → **Certificates & secrets** →
   **New client secret** → copy the **Value** immediately (it is shown once).
7. Back in the bot → **Channels** → add the **Microsoft Teams** channel.

### Step 3 — Put those two values in `.env`

```bash
copy .env.example .env
```

Then open `.env` and fill in:

```
MICROSOFT_APP_ID=<the App ID from step 2.5>
MICROSOFT_APP_PASSWORD=<the secret Value from step 2.6>
```

Restart `app.py` after saving.

### Step 4 — Build the Teams app package

Open `appPackage/manifest.json` and replace
`REPLACE_WITH_YOUR_MICROSOFT_APP_ID` with the same App ID. Then:

```bash
.venv\Scripts\python.exe package_app.py
```

That produces `appPackage/EchoBot.zip`.

### Step 5 — Upload it to Teams

In Teams: **Apps** → **Manage your apps** → **Upload an app** →
**Upload a custom app** → pick `EchoBot.zip` → **Add**.

If you do not see "Upload a custom app", your tenant has it disabled.
Ask your Teams admin to turn on custom app uploading
(Teams admin center → Teams apps → Setup policies → *Upload custom apps*).
This is the one thing you cannot do yourself.

### Step 6 — Say hi

Message the bot in Teams. It should echo you back.

---

## Stage 3 — the real agent (live)

`OpenAIAgent` in `agent.py` answers using **GPT-4o**. The key lives in
`.env`:

```
OPENAI_API_KEY=sk-proj-...
```

Get one at <https://platform.openai.com/api-keys>. Restart `app.py` and
the startup line reads `agent: openai`. Without a key it falls back to
the echo bot, so it never fails to start.

**What it does:**

- Answers using GPT-4o
- Remembers the last 5 back-and-forths **per conversation**
- `reset` clears that memory
- Handles content filters and empty replies without crashing

**Dials at the top of `agent.py`:**

| Setting | Default | What it does |
|---|---|---|
| `MODEL` | `gpt-4o` | Also available: `gpt-4o-mini` (cheaper), `gpt-4.1` |
| `TEMPERATURE` | `0.7` | 0 = predictable, 1 = chatty |
| `MAX_TOKENS` | `1000` | Longest reply (~750 words) |
| `MEMORY_TURNS` | `10` | How far back it remembers |
| `SYSTEM_PROMPT` | — | Its personality and reply style |

**Test the agent logic without spending anything:**

```bash
.venv\Scripts\python.exe test_agent.py
```

That swaps in a fake model, so it needs no key and costs nothing.

### Gotcha worth knowing

`config.py` calls `load_dotenv(override=True)`. Without `override=True`,
a stale system-wide `OPENAI_API_KEY` silently beats the one in `.env` --
which cost us a confusing round of 401 errors. The project's `.env`
should always win.

## Stage 4 — surviving slow answers (done)

A real model takes a few seconds. Four things now handle that:

| Behaviour | What you see |
|---|---|
| **Typing bubble** | The `...` appears straight away and refreshes every 4s so it never fades mid-think |
| **Slow notice** | After 8 seconds: *"Still working on this one..."* |
| **Timeout** | After 60 seconds it gives up politely instead of hanging forever |
| **Duplicate guard** | Teams re-sends a message if the bot is slow to acknowledge — the bot now answers once, not twice |

The duplicate guard matters most. Without it, a slow answer means the
user gets asked-and-answered twice for one message.

Dials at the top of `bot.py`: `TYPING_INTERVAL`, `SLOW_REPLY_AFTER`,
`AGENT_TIMEOUT`.

## Running the tests

```bash
.venv\Scripts\python.exe run_tests.py
```

| Suite | Needs a key? | Needs the server running? |
|---|---|---|
| `test_agent.py` — agent logic | no | no |
| `test_bot.py` — Teams layer | no | no |
| `test_local.py` — end to end | no | yes |

None of them cost money or call the real model.

## Stage 5 — NemoClaw

Identical shape. Write `NemoClawAgent` with the same `ask()` method,
return it from `get_agent()`. That is the entire integration surface,
which is why the layers were split this way from day one.

## Troubleshooting

**Port 3978 already in use** — something else is running. Change `PORT`
in `.env`, and update your tunnel to match.

**Bot replies locally but not in Teams** — the messaging endpoint in Azure
must end in `/api/messages`, and the tunnel must still be running. Tunnel
URLs change every restart unless you pay for a fixed one.

**401 Unauthorized in Teams** — App ID or password in `.env` is wrong, or
you copied the secret's *ID* instead of its *Value*.
