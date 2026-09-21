# Inanovai Assistant

An AI assistant reachable from Microsoft Teams, Telegram, Google Chat, and —
once a phone number is available — WhatsApp. Ask it a question in a chat and
it answers. It remembers the recent messages in each conversation, so
follow-up questions work, and each person can pick which AI model answers.

**Status: deployed on Azure. Teams, Telegram and Google Chat are live.**

| Channel | State |
|---------|-------|
| Microsoft Teams | live |
| Telegram | live, [@Inanovai_bot](https://t.me/Inanovai_bot) |
| Google Chat | live, search *Inanovai Assistant* in Google Chat |
| WhatsApp | code ready, waiting on a phone number |

---

## How it works

```
Microsoft Teams ──▶ Azure Bot Service ──┐
Telegram ─────────▶ Telegram Bot API ───┤
Google Chat ──────▶ Google Chat API ────┼──▶ Azure Web App ──▶ agent.py ──▶ AI API
WhatsApp ─────────▶ Meta Cloud API ─────┘         (app.py)
```

Four chat apps, one bot, one brain.

---

## The design that matters

The project is two layers that know nothing about each other.

| Layer | Files | Responsibility |
|-------|-------|----------------|
| **Channel layer** | `app.py`, `bot.py`, `channels/` | Speaks each chat app. Holds no intelligence. |
| **Agent layer** | `agent.py`, `models.py` | Holds the intelligence. Knows nothing about any chat app. |

They meet at one method:

```python
async def ask(message: str, context: AgentContext) -> str
```

Text in, text out. Adding a chat app means writing one file in `channels/`.
Adding a model means one line in `models.py`. Neither side touches the other.

The one thing a channel does tell the agent is its own name, through
`AgentContext.extra["channel"]`. The agent uses it to tell the model which
app the person is typing in, so nobody on Telegram is told they are in Teams.

---

## Picking a model

Anyone can type `model` in any chat app:

```
You > model
Bot > Pick a model - reply with a number or its name.

      1. Llama 3.3 70B - balanced, the safe default   (using now)
      2. Llama 4 Maverick - newest Llama, fast
      ...
      11. Hermes 3 70B - conversational, less formal

You > 4
Bot > Switched to Qwen 2.5 Coder 32B. Best for code questions.
```

Also works as `model coder`, `use qwen` or `switch to gpt oss`. The choice
sticks to that one conversation. `reset` clears what was said but keeps the
chosen model.

All 11 models run through the Hugging Face router, so switching changes one
word in the request. To see the list, or which ones are answering right now:

```powershell
.venv\Scripts\python.exe models.py
.venv\Scripts\python.exe models.py --check
```

Availability changes through the month. A model whose provider has used its
allowance replies that it has run out of credits and suggests picking
another.

---

## Files

| File | What it does |
|------|--------------|
| `app.py` | Web server. Registers every channel that has credentials |
| `bot.py` | Teams layer. Strips @mentions, blocks duplicate deliveries, shows the typing indicator |
| `agent.py` | Conversation memory, the prompt, and the call to the model |
| `models.py` | **The model menu.** One line per model |
| `config.py` | Reads settings from `.env` |
| `channels/base.py` | Shared for non-Teams channels: duplicate guard, timeout, error handling |
| `channels/telegram.py` | Telegram webhook, typing action, group @mention handling |
| `channels/whatsapp.py` | Meta Cloud API webhook, verification, signature check, read receipts |
| `channels/googlechat.py` | Google Chat endpoint, token verification, both event formats |
| `orgs.py` | Client organizations, their Teams tenant or Google domain, and their own AI keys (encrypted) |
| `settings_web.py` | The `/admin` page for our team and the `/settings` page for client admins |
| `register_channels.py` | Points Telegram at the server. Prints what to paste into Meta for WhatsApp |
| `chat.py` | Talk to the bot from a terminal, through the real code path |
| `package_app.py` | Builds the `.zip` you upload to Teams |
| `run_tests.py` | Runs every test suite |
| `start.ps1` | Starts the bot and opens a chat, then cleans up |
| `check_azure.ps1` | Checks whether your Azure access is ready |
| `appPackage/` | `manifest.json` and icons — the Teams app definition |

---

## Routes

| Route | Handled by | Reaches us via |
|-------|-----------|----------------|
| `POST /api/messages` | `bot.py` | Azure Bot Service |
| `POST /telegram/webhook` | `channels/telegram.py` | Telegram Bot API |
| `POST /googlechat/webhook` | `channels/googlechat.py` | Google Chat |
| `GET`/`POST /whatsapp/webhook` | `channels/whatsapp.py` | Meta Cloud API |
| `GET /settings` | `settings_web.py` | a client's admin, in a browser |
| `GET /admin` | `settings_web.py` | the Inanovai team, in a browser |
| `GET /` | health check | anyone |

The health check reports which channels are live:

```json
{"status": "ok", "agent": "huggingface", "channels": ["teams", "telegram", "googlechat"]}
```

A channel with no credentials is skipped silently, so the bot always starts
with whatever is configured.

---

## Running it locally

You need Python 3.10 or newer.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Put your Hugging Face token in `.env`, then:

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

Open <http://localhost:3978/> for a health check.

---

## Tests

```powershell
.venv\Scripts\python.exe run_tests.py
```

| Suite | Tests | Needs the server running? |
|-------|-------|---------------------------|
| `test_agent.py` — agent logic | 15 | no |
| `test_bot.py` — Teams layer | 11 | no |
| `test_channels.py` — Telegram and WhatsApp | 24 | no |
| `test_models.py` — model picker | 24 | no |
| `test_googlechat.py` — Google Chat | 51 | no |
| `test_prompt.py` — right app named per channel | 14 | no |
| `test_orgs.py` — organizations and their own AI | 63 | no |
| `test_settings.py` — settings and admin pages | 38 | no |
| `test_local.py` — end to end | 9 | yes |

None of them call the real model or touch the network, and none need a key,
so they cost nothing to run.

---

## Configuration

Copy `.env.example` to `.env` and fill it in. Nothing in `.env` is committed.

### Core

| Setting | Purpose |
|---------|---------|
| `HF_TOKEN` | The Hugging Face token. Without it the bot falls back to an echo bot and still starts |
| `PORT` | Local port, default `3978` |
| `PUBLIC_URL` | The public https address, used when registering webhooks |

### Microsoft Teams

| Setting | Purpose |
|---------|---------|
| `MICROSOFT_APP_ID` | From the Azure Bot registration |
| `MICROSOFT_APP_PASSWORD` | The client secret |
| `MICROSOFT_APP_TYPE` | `SingleTenant` |
| `MICROSOFT_APP_TENANT_ID` | Your Entra ID tenant |

### Telegram

| Setting | Purpose |
|---------|---------|
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Any random string. Telegram sends it back so we know the call is genuine |

### Google Chat

| Setting | Purpose |
|---------|---------|
| `GOOGLE_CHAT_AUDIENCE` | The endpoint URL exactly as entered in Google Cloud. Turns the channel on |
| `GOOGLE_CHAT_PROJECT_NUMBER` | Optional. Limits add-on requests to your own Google Cloud project |
| `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` | Optional. Lets answers slower than 25 seconds arrive afterwards |

### WhatsApp

| Setting | Purpose |
|---------|---------|
| `WHATSAPP_TOKEN` | Meta access token |
| `WHATSAPP_PHONE_NUMBER_ID` | The number the bot answers on |
| `WHATSAPP_VERIFY_TOKEN` | Any string you invent. Paste the same one into the Meta dashboard |
| `WHATSAPP_APP_SECRET` | Optional but recommended. Meta signs every webhook with it |

All three required WhatsApp values must be set together, or the channel is
skipped.

### Organizations' own AI

| Setting | Purpose |
|---------|---------|
| `ORG_SECRETS_KEY` | Turns on `/admin` and `/settings`, and encrypts every saved client key. Make one with `python orgs.py new-secret-key`. Never change it once clients have saved keys, or they must enter them again |
| `ADMIN_PASSWORD` | Password for the `/admin` page |
| `DATA_DIR` | Where `orgs.json` lives. Blank means `./data` locally and `/home/data` on Azure, which survives redeploys |

### Dials

| Where | Setting | Effect |
|-------|---------|--------|
| `models.py` | `DEFAULT_KEY` | Which model a new conversation starts on |
| `agent.py` | `TEMPERATURE` | 0 = predictable, 1 = chatty |
| `agent.py` | `MAX_TOKENS` | Longest reply. Models marked `thinks` get three times this |
| `agent.py` | `MEMORY_TURNS` | How far back it remembers, 10 = 5 exchanges |
| `agent.py` | `SYSTEM_PROMPT` | Personality and reply style. `{app}` is filled in per channel |
| `bot.py`, `channels/base.py` | `TYPING_INTERVAL`, `SLOW_REPLY_AFTER`, `AGENT_TIMEOUT` | Timing |

---

## Conversation memory

Each conversation keeps its own history in memory. Telegram, WhatsApp and
Google Chat conversations are filed under a platform prefix; Teams uses the
conversation id Microsoft provides, which never collides with those:

```
19:abc...@thread.v2      telegram:555      googlechat:spaces/AAA      whatsapp:9199...
```

Two conversations can never see each other, even across chat apps. Nothing
is written to disk, so a restart clears every history — and nobody, including
us, can read anyone's conversation afterwards.

Typing `reset`, `clear` or `forget` wipes the current conversation.

---

## Built-in safeguards

| Behaviour | Why it exists |
|-----------|---------------|
| **Typing indicator** | Refreshes every 4 seconds in Teams and Telegram so the chat never looks frozen |
| **Duplicate guard** | Every platform re-sends a message when the bot is slow to acknowledge. Without this, one question gets answered twice |
| **Timeouts** | A stalled request fails politely. Google Chat waits 30 seconds, so the bot stops waiting at 25 |
| **Message splitting** | Long replies are split before a platform rejects them |
| **Out-of-credits message** | A model with no credits says so and suggests switching, instead of failing silently |
| **Echo fallback** | With no token the bot still starts, so a missing token never looks like a crash |

---

## Deployment

The bot runs on **Azure App Service** (Linux, Python 3.11, Free F1 tier) in
the `teams-bot-rg` resource group.

| Resource | Name |
|----------|------|
| Web app | `inanovai-teams-bot.azurewebsites.net` |
| Service plan | `inanovai-bot-plan` · Linux F1 |
| Azure Bot | `inanovai-teams-bot` · F0, single tenant |

Secrets live in **App Service application settings**, not in the deployed
code. `.env` is deliberately excluded from the deployment package.

### Redeploying after a code change

Run the tests first. Then:

```powershell
.venv\Scripts\python.exe -c "import zipfile; z=zipfile.ZipFile('deploy.zip','w',zipfile.ZIP_DEFLATED); [z.write(f) for f in ['app.py','bot.py','agent.py','models.py','config.py','requirements.txt','channels/__init__.py','channels/base.py','channels/telegram.py','channels/whatsapp.py','channels/googlechat.py','orgs.py','settings_web.py']]; z.close()"
az webapp deploy --resource-group teams-bot-rg --name inanovai-teams-bot --src-path deploy.zip --type zip
```

Leaving `models.py` or anything in `channels/` out of that list ships a build
that fails to start.

### Changing a setting without redeploying

```powershell
az webapp config appsettings set --resource-group teams-bot-rg --name inanovai-teams-bot --settings KEY=value
```

### Watching the logs

Container logging is switched on, so runtime output is kept:

```powershell
az webapp log tail --resource-group teams-bot-rg --name inanovai-teams-bot
```

---

## Organizations bringing their own AI

A client organization can use its own AI: one provider and API key, or
several (for example an OpenAI key and a Gemini key, up to 10). Their
people then get answers from their models, billed to them, and the bot
never falls back to ours for them. Everyone else keeps our default models.

Every model from every saved key appears in the chat `model` menu, and each
one is called with the key it was saved under. The first model of the
**default** key is what a new chat starts on.

| Their provider | What they enter |
|----------------|-----------------|
| OpenAI | API key and model names. Address is filled in |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/v1`, key, deployment names |
| Google Gemini | API key and model names. Address is filled in |
| Anything else OpenAI-compatible (vLLM, Ollama, OpenRouter) | Its public https address, key, model names |

**How the bot knows who is asking:** Teams sends the organization's
tenant ID with every message, and Google Chat sends the sender's email, so
the domain identifies the organization. Telegram and WhatsApp messages
always use our default models.

### Setting up a client (our team)

1. Open `<PUBLIC_URL>/admin` and sign in with `ADMIN_PASSWORD`.
2. **Add an organization**: its name, and one line per identifier:
   - `teams:<their Microsoft tenant ID>`
   - `google:<their email domain>`, for example `google:acme.com`

   Don't know the tenant ID? Ask anyone there to message the bot once. It
   then appears under **Recently seen, not registered** on the admin page.
3. Copy the **settings link**, **organization ID** and **access code** shown.
   The code is shown only once. Send the link and the code separately.

### Setting their AI (the client's admin)

1. Open the settings link and sign in with the access code.
2. Under **Add your AI**: give it a name, choose the provider, enter model
   names and paste the API key.
3. **Test connection**, then **Test and save**. A key that fails the test
   is never saved.
4. More keys: fill in **Add another AI** the same way. **Make default**
   picks which one new chats start on.

They can edit or remove any key, add more, or get a new access code at any
time, without asking us. A saved key is never shown again to
anyone, including our team.

### What protects it

- Keys are encrypted in `orgs.json` with `ORG_SECRETS_KEY`, and access codes
  are stored only as salted hashes.
- Each sign-in can reach only its own organization. Eight wrong codes from
  one address lock it out for 15 minutes.
- Forms posted from any other website are refused.
- An address pointing into a private network, such as localhost or the cloud
  metadata service, is refused. Only public https addresses are accepted. This
  is checked on save and again on every connection, including redirects, so an
  address re-pointed later is still refused.
- If their AI fails, the person is told why in plain words, for example that
  the key was rejected or the model name was not found. Their question is
  never sent to our AI instead.

## Adding a channel

### Telegram

1. Message **@BotFather**, send `/newbot`, copy the token.
2. Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` and `PUBLIC_URL`,
   locally and in App Service.
3. Point Telegram at the server:

   ```powershell
   .venv\Scripts\python.exe register_channels.py
   ```

### Google Chat

Needs a Google Workspace account — personal Gmail cannot host a Chat app.

1. In Google Cloud, create a project and enable the **Google Chat API**.
2. Chat API → **Configuration**: connection **HTTP endpoint URL**, set to
   `<PUBLIC_URL>/googlechat/webhook`, authentication audience
   **HTTP endpoint URL**. Tick 1:1 messages and spaces.
3. Set `GOOGLE_CHAT_AUDIENCE` in App Service to that exact URL — no trailing
   slash. A one-character difference fails verification.
4. If the app cannot be found in Google Chat, a Workspace admin allows Chat
   apps in admin.google.com.

Google sends one of two formats depending on how the app was created — a
plain Chat app or a Workspace add-on. The channel accepts both and replies in
whichever shape Google expects.

Without a service account, answers slower than 25 seconds show a short
"still thinking" note. Our Google organisation blocks service account key
creation, so that is the current behaviour.

### WhatsApp

1. In the Meta dashboard, add WhatsApp to a Business app and copy the
   **access token** and **Phone number ID**.
2. Set the `WHATSAPP_*` values, locally and in App Service. **Do this
   before step 3**, or verification fails.
3. Set the callback URL to `<PUBLIC_URL>/whatsapp/webhook` with your
   `WHATSAPP_VERIFY_TOKEN`, then subscribe to **messages**.

**A WhatsApp number can only point at one webhook.** Ours already serves the
ERP integration, so the bot needs a second number on the same Business
Account — same billing and verification, separate destination.

### Something else

Write a class in `channels/` with `register(app)` and a webhook handler,
inherit `ChannelBase` for the duplicate guard and timeout, add its display
name to `APP_NAMES` in `agent.py`, and list it in `build_channels()`.

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
No token was found. Check `.env`. `config.py` loads it with `override=True` on
purpose — without that, a stale system-wide environment variable of the same
name silently beats the one in `.env`. That cost us an afternoon of 401s that
looked exactly like a bad token.

**Every new chat says a model has run out of credits**
The default model's provider has used its allowance. Run
`models.py --check`, then change `DEFAULT_KEY` in `models.py` to one that
answers, or top up Hugging Face credits.

**`401` from `/api/messages` or `/googlechat/webhook`**
Expected for unsigned calls. Real traffic from Microsoft and Google is signed
and passes.

**Google Chat says "not responding"**
Check the logs for the line starting `rejected a`. A wrong audience means
`GOOGLE_CHAT_AUDIENCE` does not exactly match the URL in Google Cloud. If
nothing arrived at all, the endpoint URL in Google Cloud is wrong. The very
first message after idle can also time out while Azure wakes — send it again.

**`403` from `/telegram/webhook`**
The secret header did not match. Make sure `TELEGRAM_WEBHOOK_SECRET` in App
Service matches the one registered with `register_channels.py`.

**`AADSTS7000229` in the logs**
The app registration has no service principal in the tenant. Creating an app
registration does not create one:

```powershell
az ad sp create --id <your-app-id>
```

**Meta refuses to verify the webhook**
The server has to know `WHATSAPP_VERIFY_TOKEN` before Meta calls it. Set the
App Service settings and wait for the restart, then verify.

**Telegram is registered but nothing arrives**
Run `register_channels.py` — it prints the registered URL and the last error
Telegram saw.

**First message after idle is slow**
The Free F1 tier sleeps after about 20 minutes. The first request wakes it,
which takes roughly 20 seconds. Moving to B1 removes this, with no redeploy.

**Port 3978 already in use locally**
Something else is on it. Change `PORT` in `.env`.

---

## Cost

- **Azure Bot** — Free F0 tier
- **Azure App Service** — Free F1 tier
- **Google Chat** — no charge for the Chat API
- **AI models** — billed against the Hugging Face token, per message
- **WhatsApp** — Meta charges per conversation beyond a free monthly allowance

Every person using the bot spends against the same token. Watch usage once
the team is on it.
