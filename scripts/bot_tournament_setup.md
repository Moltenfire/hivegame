# Bot Tournament Setup

This guide sets up local bot accounts for tournament start coordination.

## 1. Create Bot Accounts

Run the database helper from the repo root:

```bash
BOT_COUNT=4 BOT_PASSWORD='bot-password' cargo run -p db --bin setup_bots
```

With Docker Compose, run the same helper inside the `app` container after the
containers are up and migrations have run:

```bash
docker compose exec app env BOT_COUNT=4 BOT_PASSWORD='bot-password' cargo run -p db --bin setup_bots
```

Defaults:

- `BOT_COUNT=4`
- `BOT_PREFIX=Bot`
- `BOT_PASSWORD=bot-password`

This creates or updates users named `Bot1`, `Bot2`, etc. Their emails are generated as:

```text
bot1@bots.local
bot2@bots.local
```

You can set individual passwords if needed:

```bash
BOT_COUNT=4 BOT1_PASSWORD='bot1-password' BOT2_PASSWORD='bot2-password' cargo run -p db --bin setup_bots
```

Docker Compose:

```bash
docker compose exec app env BOT_COUNT=4 BOT1_PASSWORD='bot1-password' BOT2_PASSWORD='bot2-password' cargo run -p db --bin setup_bots
```

## 2. Join Bots To A Tournament

After the tournament exists, join the bots:

```bash
TOURNAMENT_ID=LG2xvFdhFvb BOT_COUNT=4 cargo run -p db --bin join_bots_to_tournament
```

Docker Compose:

```bash
docker compose exec app env TOURNAMENT_ID=LG2xvFdhFvb BOT_COUNT=4 cargo run -p db --bin join_bots_to_tournament
```

`TOURNAMENT_ID` can be the tournament nanoid or UUID.

## 3. Create One Config File Per Bot

Create local config files, one per bot. Do not commit real credentials.

`bot1.json`:

```json
{
  "url": "http://localhost:3000",
  "tournament_id": "LG2xvFdhFvb",
  "name": "Bot1",
  "email": "bot1@bots.local",
  "password": "bot-password"
}
```

Repeat for `Bot2`, `Bot3`, and `Bot4`, changing `name` and `email`.

## 4. Run The Coordinator

Start one process per bot:

```bash
./scripts/bot_tournament_summary.py run bot1.json
./scripts/bot_tournament_summary.py run bot2.json
./scripts/bot_tournament_summary.py run bot3.json
./scripts/bot_tournament_summary.py run bot4.json
```

When using Docker Compose, run the coordinator on the host. The app container
publishes the API at `http://localhost:3000`, so keep that URL in each bot
config file.

The script logs in automatically, refreshes tokens before expiry, posts coordination heartbeats to tournament chat, and starts eligible games.

After this process starts or accepts a game, it leaves chat coordination and
focuses only on that game. It polls the bot API until that game is returned as
pending for this bot. If the game has an assigned opening from the tournament
description, and the current history is still a prefix of that opening, the
script submits the next opening move. It does not choose non-opening moves yet,
and it does not post further coordination chat messages for that process. If
the current game history differs from the assigned opening before the opening is
complete, the bot posts one normal tournament chat message describing the
mismatch and then stops polling or playing.

To inspect one bot's assigned tournament games and openings:

```bash
./scripts/bot_tournament_summary.py summary bot1.json
```
