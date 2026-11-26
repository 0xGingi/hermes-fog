# hermes-fog

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) platform plugin that puts your agent on
**Fog**, the end-to-end encrypted messenger.

Hermes never touches Fog's encryption. A small daemon, `fog-bot`, runs the bot's Fog account on your machine:
it holds the keys, encrypts and decrypts with the Fog app's own engine, and gives Hermes a plain HTTP API on
`127.0.0.1`. This plugin is the Hermes side of that API. The Fog server sees an ordinary account and never
learns what anyone says, or that it's a bot.

```
Fog app  ⇄  Fog server  ⇄  fog-bot (keys, E2EE)  ⇄  hermes-fog  ⇄  Hermes gateway  ⇄  agent
```

## Setup

### 1. Run fog-bot

`fog-bot` lives in the Fog repository (`bot/`). Create the bot's account once, then keep it running:

```sh
fog-bot init --username my_agent --name "My Agent" --owner @you   # prints the recovery phrase and the API token
fog-bot run                                                         # API on http://127.0.0.1:8790
```

`--owner` gives fog-bot an allowlist: anyone not on it can't reach the bot at all (no receipts, nothing in
the API), even before Hermes sees it. Add more people with `fog-bot allow --add @them`. Keep
`FOG_ALLOWED_USERS` below as well: two locks, one in fog-bot and one in Hermes.

`fog-bot token` prints the API token again. Keep the bot's folder (`~/.fog-bot` by default) private: it
holds the account's keys. Run exactly one Hermes gateway per fog-bot: the adapter takes a lock so a second
one can't steal its events.

### 2. Install the plugin

```sh
hermes plugins install 0xGingi/hermes-fog
hermes plugins enable hermes-fog
```

or with pip, into the same environment as Hermes:

```sh
pip install git+https://github.com/0xGingi/hermes-fog
hermes plugins enable hermes-fog
```

### 3. Configure

In `~/.hermes/.env`:

```sh
FOG_BOT_TOKEN=...                    # from `fog-bot init` / `fog-bot token`
FOG_ALLOWED_USERS=@ana,@bo           # Fog @usernames or account ids allowed to use the bot
FOG_HOME_CHANNEL=@ana                # where cron jobs deliver (optional)
```

Then start (or restart) the gateway: `hermes gateway run`. Open the invite link `fog-bot init` printed, or
search for the bot's `@username` in Fog, and say hi.

| Variable | Default | |
|---|---|---|
| `FOG_BOT_TOKEN` | (required) | fog-bot's API token. |
| `FOG_BOT_URL` | `http://127.0.0.1:8790` | Where fog-bot listens. |
| `FOG_ALLOWED_USERS` | | Comma-separated account ids or `@usernames`. Usernames are resolved to ids at connect. |
| `FOG_ALLOW_ALL_USERS` | `false` | Anyone who can message the bot may use it. |
| `FOG_GROUP_ALLOWED_CHATS` | | Group ids (`g:…`) where every member may use the bot. |
| `FOG_ALLOW_BOTS` | | Let other bots talk to this one (`mentions` / `all`). |
| `FOG_REQUIRE_MENTION` | `true` | In groups, answer only messages that @mention or reply to the bot (threads the bot is in don't need it). |
| `FOG_FREE_RESPONSE_CHATS` | | Group ids (or `*`) where the bot answers without a mention. |
| `FOG_AUTO_THREAD` | `true` | Each @mention in a group starts a thread on that message, with its own session. |
| `FOG_HISTORY_BACKFILL` | `true` | When mentioned in a group, give the agent the messages since its last reply. |
| `FOG_REACTIONS` | `true` | React 👀 while working, then ✅ or ❌. |
| `FOG_NOTIFICATIONS` | `important` | `important`: only replies, approvals and the start of a streamed reply notify; tool progress and status notes arrive silently. `all`: everything notifies. |
| `FOG_HOME_CHANNEL` / `_NAME` | | Cron / notification target: account id, `@username` or `g:group`; add `/<thread id>` for a thread. |

Every option except the token and URL can also live in `config.yaml`:

```yaml
gateway:
  platforms:
    fog:
      enabled: true
      extra:
        require_mention: true
        free_response_chats: ["g:abc…"]
        auto_thread: true
```

Unauthorized people get Hermes' usual treatment: pairing codes in DMs, unless an allowlist is set, in which
case they are ignored.

## What works

Everything Hermes does on Discord and Telegram that Fog supports:

- **DMs, groups and threads.** Group messages carry who was mentioned, so mention gating is exact. Replies
  bring the quoted text along. A thread is its own session (like Discord threads), and an @mention in a group
  starts one automatically on that message. After the first turn the thread is renamed to the session's title.
  The bot answers follow-ups in threads it is part of without a mention. `/thread Name | first message` starts
  one by hand. Hermes handoffs and `/branch` get a fresh thread too.
- **Streaming.** Replies stream into one live message (no "edited" label) and are finalized when done. Long
  answers split at 16,000 characters (Fog allows 64k).
- **Markdown as-is**, including tables, task lists, spoilers and fenced code. No conversion step.
- **Buttons, dropdowns, forms and cards:**
  - Command approvals: Allow once / for the session / always / Deny, with the card updated to the outcome and
    retired if the request times out.
  - `clarify`: numbered buttons plus "Other (type an answer)", or a multi-select dropdown.
  - Slash-command confirmations, the `/update` prompt.
  - `/model`: provider dropdown, then model dropdown(s), edited in place, with the expensive-model warning.
  - Choice pickers (`/reasoning`, `/fast`, ...) as a dropdown.
  - Browser vault one-time codes: a button opens a private form with a secret field. The code goes straight to
    the browser and is never posted in the chat or shown to the model.
  - Only the right person can answer: everyone else gets a private alert and the prompt stays open.
- **Command menu.** Hermes' gateway commands, plugin commands, `/thread` and `/skill` with autocomplete
  over your skills (filtered by name and description, nothing shown to unauthorized users). It refreshes after
  `/reload-skills`.
- **Files both ways**, up to 2 GB: photos, videos, audio, voice notes (transcribed by Hermes' speech-to-text),
  stickers and documents in; `MEDIA:` tags, image URLs, voice replies (`[[audio_as_voice]]`, TTS) and documents out.
- **Reactions** both ways (yours arrive as platform events), typing indicator, read receipts, private notices
  in groups (visible to one member only), edits and deletes surfaced as `gateway_platform_event` hooks
  (`reaction`, `message_edited`, `message_deleted`, `thread_created`, `thread_renamed`).
- **Cron and `hermes send`** deliver to `FOG_HOME_CHANNEL` (or any `g:…`, `@username`, account id, with
  `/<thread>` for a thread), even when the gateway isn't running.

## Limitations

- No voice channels or calls: Fog has calls, but fog-bot doesn't expose them yet.
- Fog has no per-user command menus, so everyone in a chat sees the same commands. Hermes still checks who may
  run each one.
- `/branch` stays in place: Hermes only opens a sibling thread for its built-in platforms. Handoffs get a
  thread.
- Pinning isn't used: Hermes has no generic pin action.

## Development

```sh
pip install -e /path/to/hermes-agent        # Hermes itself, for the gateway modules
pip install -e '.[test]'
pytest tests                                 # unit tests against a fake fog-bot
hermes plugins validate .
```

`tests/e2e/run_live.py` runs the real adapter against a live fog-bot with a scripted Fog user
(`tests/e2e/fog_user.ts`, run with bun from a Fog checkout). See the docstring for the environment it needs.

## License

MIT
