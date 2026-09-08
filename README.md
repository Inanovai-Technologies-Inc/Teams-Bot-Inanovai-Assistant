# Inanovai Assistant — Microsoft Teams AI Bot

An AI assistant that lives inside Microsoft Teams. Ask it a question in a
chat and it answers. It remembers the recent messages in each conversation,
so follow-up questions work.

**Status: deployed and running on Azure.**

---

## How it works

```
You (Teams) → Microsoft Teams → Azure Bot Service → Azure Web App
                                                          ↓
                                            app.py → bot.py → agent.py → AI API
                                                          ↓
                                          the reply returns along the same path
```

A diagram of this is in [`docs/workflow.png`](docs/workflow.png), with an
editable PowerPoint version in [`docs/workflow.pptx`](docs/workflow.pptx).

---

## The design that matters

The project is split into two layers that know nothing about each other.

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **Teams layer** | `app.py`, `bot.py` | Speaks Microsoft Teams. Holds no intelligence. |
| **Agent layer** | `agent.py` | Holds the intelligence. Knows nothing about Teams. |

`agent.py` exposes a single method:

```python
async def ask(message: str, context: AgentContext) -> str
```

Text in, text out. To change the brain — a different model, a different
provider, a local runtime — you write one class with that method and return
it from `get_agent()`. Nothing else in the project changes.

---

## Files

| File | What it does |
|------|--------------|
| `app.py` | Web server. One route that matters: `POST /api/messages` |
| `bot.py` | Teams layer. Strips @mentions, blocks duplicate deliveries, shows the typing indicator |
| `agent.py` | **The only file you change to swap the AI.** Model, prompt, memory |
| `config.py` | Reads settings from `.env` |
| `chat.py` | Talk to the bot from a terminal, through the real Teams code path |
| `package_app.py` | Builds the `.zip` you upload to Teams |
| `run_tests.py` | Runs all three test suites |
| `start.ps1` | Starts the bot and opens a chat, then cleans up |
| `check_azure.ps1` | Checks whether your Azure access is ready |
| `appPackage/` | `manifest.json` and icons — the Teams app definition |
| `docs/` | Workflow diagram, and the script that generates it |

---

## Running it locally

You need Python 3.9 or newer.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Put your AI API key in `.env`, then:

```powershell
.venv\Scripts\python.exe app.py
```

In a second terminal:

```powershell
.venv\Scripts\python.exe chat.py
```

Or use the launcher, which does both and stops the server when you quit:

```powershell
.\start.ps1
```

Open <http://localhost:3978/> for a health check. The `agent` field tells you
which brain is live.

---

## Tests

```powershell
.venv\Scripts\python.exe run_tests.py
```

| Suite | Tests | Needs a key? | Needs the server running? |
|-------|-------|--------------|---------------------------|
| `test_agent.py` — agent logic | 15 | no | no |
| `test_bot.py` — Teams layer | 11 | no | no |
| `test_local.py` — end to end | 9 | no | yes |

None of them call the real model, so none of them cost anything.
`test_local.py` stands up a fake Microsoft Teams on port 3979 and sends the
bot the same events real Teams sends: direct messages, an @mention inside a
channel, and the install event.

---

## Configuration

Copy `.env.example` to `.env` and fill it in. Nothing in `.env` is committed.

| Setting | Purpose |
|---------|---------|
| `HF_TOKEN` | The Hugging Face token. Without it the bot falls back to an echo bot and still starts |
| `MICROSOFT_APP_ID` | From the Azure Bot registration |
| `MICROSOFT_APP_PASSWORD` | The client secret |
| `MICROSOFT_APP_TYPE` | `SingleTenant` |
| `MICROSOFT_APP_TENANT_ID` | Your Entra ID tenant |
| `PORT` | Local port, default `3978` |

Dials at the top of `agent.py`:

| Setting | Default | Effect |
|---------|---------|--------|
| `MODEL` | `meta-llama/Llama-3.3-70B-Instruct` | Which model answers |
| `TEMPERATURE` | `0.7` | 0 = predictable, 1 = chatty |
| `MAX_TOKENS` | `1000` | Longest reply |
| `MEMORY_TURNS` | `10` | How far back it remembers, 10 = 5 exchanges |
| `SYSTEM_PROMPT` | — | Personality and reply style |

Timing dials at the top of `bot.py`: `TYPING_INTERVAL`, `SLOW_REPLY_AFTER`,
`AGENT_TIMEOUT`.

---

## Built-in safeguards

| Behaviour | Why it exists |
|-----------|---------------|
| **Typing indicator** | Refreshes every 4 seconds so the chat never looks frozen while the model thinks |
| **Duplicate guard** | Teams re-sends a message when the bot is slow to acknowledge. Without this, one question gets answered twice |
| **60 second timeout** | A stalled request fails politely instead of hanging the conversation |
| **Echo fallback** | With no API key the bot still starts, so a missing key never looks like a crash |

---

## Deployment

The bot runs on **Azure App Service** (Linux, Python 3.11, Free F1 tier) in
the `teams-bot-rg` resource group. The Azure Bot registration is single
tenant and points at the web app's `/api/messages`.

Secrets live in **App Service application settings**, not in the deployed
code. `.env` is deliberately excluded from the deployment package.

### Redeploying after a code change

```powershell
.venv\Scripts\python.exe -c "import zipfile; z=zipfile.ZipFile('deploy.zip','w',zipfile.ZIP_DEFLATED); [z.write(f) for f in ['app.py','bot.py','agent.py','config.py','requirements.txt']]; z.close()"
az webapp deploy --resource-group teams-bot-rg --name inanovai-teams-bot --src-path deploy.zip --type zip
```

### Changing a setting without redeploying

```powershell
az webapp config appsettings set --resource-group teams-bot-rg --name inanovai-teams-bot --settings KEY=value
```

### Watching the logs

```powershell
az webapp log tail --resource-group teams-bot-rg --name inanovai-teams-bot
```

---

## Publishing to Teams

1. Build the package:

   ```powershell
   .venv\Scripts\python.exe package_app.py
   ```

2. A Teams administrator uploads it: Teams admin center → **Teams apps** →
   **Manage apps** → **Actions** → **Upload new app**.

3. It then appears for everyone under **Apps → Built for your org**.

Publishing requires the Teams Administrator or Global Administrator role.
Submitting the app from inside the Teams client only creates a pending
request; an administrator still has to approve it.

---

## Troubleshooting

**Startup says `agent: echo` instead of `agent: huggingface`**
No token was found. Check `.env`. Note that `config.py` loads it with
`override=True` on purpose — without that, a stale system-wide environment
variable of the same name silently beats the one in `.env`.

**`401` from the messaging endpoint**
Expected for unauthenticated calls once `MICROSOFT_APP_ID` is set. Real
traffic from Microsoft is signed and passes.

**`AADSTS7000229` in the logs**
The app registration has no service principal in the tenant. Creating an app
registration does not create one automatically:

```powershell
az ad sp create --id <your-app-id>
```

**First message after idle is slow**
The Free F1 tier sleeps after about 20 minutes. The first request wakes it,
which takes roughly 20 seconds. Moving to the B1 tier removes this.

**Port 3978 already in use locally**
Something else is on it. Change `PORT` in `.env`.

---

## Cost

- **Azure Bot** — Free F0 tier
- **Azure App Service** — Free F1 tier
- **AI API** — billed per message against the configured key

Every person using the bot spends against the same API key. Watch usage once
the team is on it.
