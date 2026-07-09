# Bot Tournament Setup

This guide sets up local bot accounts and runs the tournament coordinator.

## 1. Create Bot Accounts

Run the database helper from the repo root:

```bash
BOT_COUNT=4 BOT_PASSWORD='bot-password' cargo run -p db --bin setup_bots
```

With Docker Compose, run the helper inside the `app` container after the
containers are up and migrations have run:

```bash
docker compose exec app env BOT_COUNT=4 BOT_PASSWORD='bot-password' cargo run -p db --bin setup_bots
```

Defaults:

- `BOT_COUNT=4`
- `BOT_PREFIX=Bot`
- `BOT_PASSWORD=bot-password`

This creates or updates users named `Bot1`, `Bot2`, etc. Their emails are
generated as:

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

## 3. Create Config Files

Create one shared tournament config and one config per bot. Do not commit real
credentials.

`tournament.json`:

```json
{
  "url": "http://localhost:3000",
  "tournament_id": "LG2xvFdhFvb",
  "poll_seconds": 5,
  "heartbeat_seconds": 20,
  "online_seconds": 45,
  "offer_seconds": 30
}
```

`bot1.json`:

```json
{
  "name": "Bot1",
  "email": "bot1@bots.local",
  "password": "bot-password",
  "logging": {
    "level": "INFO",
    "directory": "logs",
    "app_file": true,
    "uhp_file": true
  },
  "uhp": {
    "command": "/home/jondav01/scratch/nokamute/target/release/nokamute uhp",
    "options": {
      "NumThreads": 1
    },
    "bestmove": {
      "mode": "depth",
      "depth": 1
    }
  }
}
```

Repeat for `Bot2`, `Bot3`, and `Bot4`, changing `name` and `email`.
If the UHP binary path contains spaces, quote the path inside the command
string, for example `"'/path with spaces/engine' uhp"`.

The `uhp` section is optional. Without it, the coordinator starts games and
plays assigned opening moves only. With it, the coordinator starts one UHP
process for this bot runner, applies configured UHP options with
`options set <Name> <Value>`, and reuses the process across games. For
`nokamute`, set `"NumThreads": 1` to avoid one bot using all available cores.

UHP does not receive the full game clock as a separate value. It receives a
maximum thinking budget through `bestmove`. For fixed-depth play, use:

```json
"bestmove": {
  "mode": "depth",
  "depth": 1
}
```

For clock-based play, use:

```json
"bestmove": {
  "mode": "time",
  "use_clock": true,
  "moves_to_go": 20,
  "min_seconds": 1,
  "max_seconds": 10,
  "max_clock_fraction": 0.5,
  "protocol": "time"
}
```

With `use_clock`, the runner divides the current player's remaining time by
`moves_to_go`, clamps it between `min_seconds` and `max_seconds`, and also caps
it at `max_clock_fraction` of the current remaining time. `protocol: "time"`
sends UHP `bestmove time hh:mm:ss`. Use `protocol: "seconds"` only for engines
that support `bestmove seconds N`.

Runtime app logs go to stdout and to a per-run file such as
`logs/Bot1-20260708-143012.log`. UHP stdin/stdout/stderr wire logs are file-only
and use the same run timestamp, for example
`logs/Bot1-20260708-143012.uhp.log`.

## 4. Run The Coordinator

Start one process per bot:

```bash
./scripts/bot_tournament_summary.py run tournament.json bot1.json
./scripts/bot_tournament_summary.py run tournament.json bot2.json
./scripts/bot_tournament_summary.py run tournament.json bot3.json
./scripts/bot_tournament_summary.py run tournament.json bot4.json
```

When using Docker Compose, run the coordinator on the host. The app container
publishes the API at `http://localhost:3000`, so keep that URL in the
tournament config file.

The script logs in automatically, refreshes tokens before expiry, posts
coordination heartbeats to tournament chat, and starts eligible games.

Press `Ctrl+C` once to request a graceful shutdown. If the bot is idle, it exits
immediately. If it is playing a game, it stops coordinating new games, finishes
the focused game, and then exits. Press `Ctrl+C` a second time to exit
immediately.

After this process starts or accepts a game, it leaves chat coordination and
focuses only on that game. On restart, if the tournament already has an
unfinished in-progress game assigned to this bot, the process resumes that game
before reading coordination chat or offering new games. It polls the bot API
until the focused game is returned as pending for this bot. If the game has an
assigned opening from the tournament description, and the current history is
still a prefix of that opening, the script submits the next opening move. Once
the assigned opening is complete, or if no opening is assigned, the script asks
the configured UHP engine for moves. It does not post further coordination chat
messages for that process. If the current game history differs from the
assigned opening before the opening is complete, or if the UHP engine errors or
submits a rejected move, the bot posts one normal tournament chat message
describing the issue and then stops playing that game. When the focused game
finishes, including by timeout, the process clears its local UHP game state,
posts a fresh idle heartbeat, and then returns to tournament coordination on
the next poll.

To inspect one bot's assigned tournament games and openings:

```bash
./scripts/bot_tournament_summary.py summary tournament.json bot1.json
```

Summary mode validates assigned openings with the configured UHP engine by
loading each opening as a `newgame <GameString>`. Valid openings are printed
without extra output. Invalid openings are marked in the `Openings` section and
summary exits non-zero.

Opening lines can be plain moves or a markdown link whose text is the move list.
The coordinator uses only the moves for gameplay and validation.

```text
- wS1;bS1 -wS1
- [wS1;bS1 -wS1](https://example.test/analysis)
```
