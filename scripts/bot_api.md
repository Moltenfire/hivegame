# Bot API

HTTP API for bot accounts to authenticate, discover games, play moves, and
manage challenges.

Route handlers live in `apis/src/api/v1/bot/` (`users.rs`, `games.rs`,
`challenges.rs`, `play.rs`) and the token endpoint in
`apis/src/api/v1/auth/get_token_handler.rs`.

Every example below is a real, field-complete response shape taken from the
Rust response structs (`apis/src/responses/*.rs`, `db_lib::models::Game`,
`shared_types`, `hive_lib`/`engine`). Values are realistic placeholders;
collections that are board-size-dependent (`moves`, `spawns`,
`reserve_black`/`reserve_white`) are shown with a representative subset
rather than every possible entry.

## Response envelope

Every endpoint below returns HTTP 200 with a JSON body of the form:

```json
{ "success": true, "data": { ... } }
```

or, on a handled error:

```json
{ "success": false, "data": { "error": "message" } }
```

The token endpoint is the exception: it returns HTTP 400 (not 200) on
failure, with `data.message` instead of `data.error`. Auth failures on all
other endpoints (missing/invalid/expired token, or a non-bot account) return
HTTP 401 with `data.message`.

## Authentication

```
POST /api/v1/auth/token
```

Request:

```json
{
  "email": "bot1@bots.local",
  "password": "bot-password"
}
```

Success response (HTTP 200):

```json
{
  "success": true,
  "data": {
    "token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib3QxQGJvdHMubG9jYWwiLCJpc3MiOiJoaXZlZ2FtZS5jb20iLCJleHAiOjE3OTIwMDAwMDB9.9c1e0b6f2a7d4e5f8b3c1a0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f"
  }
}
```

Failure response (HTTP 400):

```json
{
  "success": false,
  "data": {
    "message": "Record not found"
  }
}
```

The JWT is signed with `HS256`; decoding its payload (without verifying)
gives:

```json
{
  "sub": "bot1@bots.local",
  "iss": "hivegame.com",
  "exp": 1792000000
}
```

`sub` is the bot's email, and `exp` is 100 minutes after issuance. The
account must have `bot = true` in the database or the request is rejected.

Send the token on every other request:

```
Authorization: Bearer <token>
```

A request with an expired token gets `data.message` (or `data.error`) equal
to `"ExpiredSignature"`:

```json
{
  "success": false,
  "data": {
    "message": "ExpiredSignature"
  }
}
```

The recommended handling is to call `/api/v1/auth/token` again and retry
once.

## Games

```
GET /api/v1/bot/games/pending
GET /api/v1/bot/games/ongoing
GET /api/v1/bot/game/{nanoid}
```

These return raw game database rows (`db_lib::models::Game`), not the richer
`GameResponse` shape used elsewhere (see [Challenges](#challenges) below) —
note `history` here is a single
semicolon-delimited string, `game_status`/`game_type`/`time_mode`/`speed`/
`conclusion`/`game_start`/`tournament_game_result` are plain strings rather
than typed objects, and there are no nested player/move/reserve objects.

`pending` returns games with notifications for the bot (its turn, or
otherwise needing attention); `ongoing` returns all of the bot's unfinished
games; `/bot/game/{nanoid}` returns a single-element list for that game. All
three filter out any game that has just timed out but isn't finalized yet,
so a returned game is always genuinely still playable.

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "games": [
      {
        "id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
        "nanoid": "qWeRtYuIoP1",
        "current_player_id": "a3f1c4d2-8b7e-4c1a-9d3e-5f6a7b8c9d11",
        "black_id": "a3f1c4d2-8b7e-4c1a-9d3e-5f6a7b8c9d11",
        "finished": false,
        "game_status": "InProgress",
        "game_type": "Base",
        "history": "wS1;bG1 -wS1;wA1 wS1/",
        "game_control_history": "",
        "rated": true,
        "tournament_queen_rule": true,
        "turn": 3,
        "white_id": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10",
        "white_rating": 1452.3,
        "black_rating": 1498.7,
        "white_rating_change": null,
        "black_rating_change": null,
        "created_at": "2026-08-13T14:02:11.000Z",
        "updated_at": "2026-08-13T14:05:47.000Z",
        "time_mode": "RealTime",
        "time_base": 600,
        "time_increment": 5,
        "last_interaction": "2026-08-13T14:05:47.000Z",
        "black_time_left": 590123456789,
        "white_time_left": 585987654321,
        "speed": "Rapid",
        "hashes": [1234567890123456789, 9876543210987654321, 4552367891234567890],
        "conclusion": "Unknown",
        "tournament_id": null,
        "tournament_game_result": "Unknown",
        "game_start": "Ready",
        "move_times": [null, null, 12045000000, 8321000000],
        "timeout_at": null
      }
    ]
  }
}
```

`black_time_left`/`white_time_left` and the values in `move_times` are
nanoseconds. `hashes` is one Zobrist-style board hash per ply played so far.
`game_control_history` is empty when no resign/draw/takeback control has
been sent; otherwise it looks like `"2. Resign[Black];"`.

## Playing a move

```
POST /api/v1/bot/games/play
```

Request:

```json
{
  "game_id": "qWeRtYuIoP1",
  "piece_pos": "wS1"
}
```

`piece_pos` is `"<piece>"` for the very first move of the game (turn 0, no
board reference needed), and `"<piece> <relative-position>"` for every move
after, e.g. `"bG1 -wS1"` (placed to the west of `wS1`) or `"wA1 wS1/"`
(placed northeast of `wS1`). This is standard Hive notation — the same
notation used in `history`.

Requirements: the game must not be finished, and it must be the bot's turn
(`current_player_id == bot.id`), or the request fails with `data.error`.

Success response:

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "history": "wS1;bG1 -wS1;wA1 wS1/"
  }
}
```

Failure response, e.g. playing out of turn:

```json
{
  "success": false,
  "data": {
    "error": "Not your turn"
  }
}
```

## Starting and controlling a game

```
POST /api/v1/bot/games/control
```

`control` is one of:

- `"start"` — signal readiness to begin. Both players must send `"start"`
  within 35 seconds of the first one, or the game does not proceed to
  move-play. Poll `ready`/`started`/`ready_seconds_left` rather than a fixed
  local timer, since the countdown begins on the first `"start"` sent by
  either side.
- `"resign"` — resign the game. Only allowed once turn 2 or later has been
  played.
- `"abort"` — abort the game. Only allowed before turn 2, and never for
  tournament games (`tournament_id` set).

Sending the same control twice in a row is rejected with `data.error`
(`"Control already sent"`).

Request, waiting on the opponent to also start:

```json
{
  "game_id": "qWeRtYuIoP1",
  "control": "start"
}
```

Response while waiting on the opponent:

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "game_id": "qWeRtYuIoP1",
    "game_status": "NotStarted",
    "finished": false,
    "ready": true,
    "started": false,
    "ready_seconds_left": 35
  }
}
```

Response once both sides have sent `"start"`:

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "game_id": "qWeRtYuIoP1",
    "game_status": "InProgress",
    "finished": false,
    "ready": false,
    "started": true,
    "ready_seconds_left": null
  }
}
```

Resign request/response:

```json
{ "game_id": "qWeRtYuIoP1", "control": "resign" }
```

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "game_id": "qWeRtYuIoP1",
    "game_status": "Finished",
    "finished": true,
    "ready": false,
    "started": false,
    "ready_seconds_left": null
  }
}
```

## Challenges

```
GET    /api/v1/bot/challenges/
POST   /api/v1/bot/challenges/
GET    /api/v1/bot/challenge/accept/{nanoid}
```

### List challenges

`GET /challenges/` returns the bot's own open challenges plus any direct
challenges addressed to it, each a `ChallengeResponse`:

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "challenges": [
      {
        "id": "9f8e7d6c-5b4a-3c2d-1e0f-a1b2c3d4e5f6",
        "challenge_id": "qaTq1dsIi3-i",
        "challenger": {
          "username": "Bot1",
          "uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10",
          "patreon": false,
          "bot": true,
          "admin": false,
          "deleted": false,
          "ratings": {
            "Rapid": {
              "speed": "Rapid",
              "rating": 1452,
              "played": 40,
              "win": 22,
              "loss": 15,
              "draw": 3,
              "certainty": "Rankable",
              "user_uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10"
            }
          },
          "takeback": "Always",
          "lang": null
        },
        "opponent": null,
        "game_type": "Base",
        "rated": true,
        "visibility": "Public",
        "color_choice": "Random",
        "created_at": "2026-08-13T14:00:00.000Z",
        "challenger_rating": 1452,
        "time_mode": "RealTime",
        "time_base": 600,
        "time_increment": 5,
        "speed": "Rapid",
        "band_upper": null,
        "band_lower": null
      }
    ]
  }
}
```

### Create a challenge

```json
{
  "game_type": "Base",
  "visibility": "Public",
  "opponent": null,
  "color_choice": "Random",
  "time_control": { "RealTime": { "base": 10, "increment": 5 } },
  "rated": true,
  "band_upper": null,
  "band_lower": null
}
```

- `game_type`: `"Base"` or `"MLP"` only (other `GameType` variants such as
  `"M"`, `"L"`, `"P"` are rejected here). `rated` cannot be `true` with
  `game_type: "Base"`.
- `visibility`: `"Direct"` | `"Public"` | `"Private"`. `"Direct"` requires
  `opponent` to be set to a username; any other visibility requires
  `opponent` to be `null`.
- `color_choice`: `"White"` | `"Black"` | `"Random"`.
- `time_control`, one of:
  - `"Untimed"` — cannot be combined with `rated: true`.
  - `{ "RealTime": { "base": <1-180 minutes>, "increment": <0-180 seconds> } }`
  - `{ "Correspondence": { "mode": "DaysPerMove" | "TotalTimeEach", "days": <1-20> } }`
- `band_upper` / `band_lower`: optional rating-band restriction; if both are
  set, `band_lower` must be `<= band_upper`.

Response is `{ "success": true, "data": { "bot": ..., "bot_username": ..., "challenge": <ChallengeResponse as above> } }`.

Validation failure (still HTTP 200):

```json
{
  "success": false,
  "data": {
    "error": "Direct challenges require an opponent username"
  }
}
```

### Accept a challenge

```
GET /api/v1/bot/challenge/accept/{nanoid}
```

Note this is a `GET`, not a `POST`, despite creating a game as a side
effect. Color is assigned per the challenge's `color_choice` (resolved
randomly server-side if `"Random"`). `data.game` is a full `GameResponse`
(distinct from the raw game rows returned by the `/games/*` endpoints
above — richer typed fields, nested player objects, per-piece move/reserve
maps):

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "bot_username": "Bot1",
    "game": {
      "uuid": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
      "game_id": "qWeRtYuIoP1",
      "tournament": null,
      "current_player_id": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10",
      "turn": 0,
      "finished": false,
      "game_status": "NotStarted",
      "game_type": "Base",
      "tournament_queen_rule": true,
      "white_player": {
        "username": "Bot1",
        "uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10",
        "patreon": false,
        "bot": true,
        "admin": false,
        "deleted": false,
        "ratings": {
          "Rapid": {
            "speed": "Rapid",
            "rating": 1452,
            "played": 40,
            "win": 22,
            "loss": 15,
            "draw": 3,
            "certainty": "Rankable",
            "user_uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10"
          }
        },
        "takeback": "Always",
        "lang": null
      },
      "black_player": {
        "username": "carol",
        "uid": "a3f1c4d2-8b7e-4c1a-9d3e-5f6a7b8c9d11",
        "patreon": true,
        "bot": false,
        "admin": false,
        "deleted": false,
        "ratings": {
          "Rapid": {
            "speed": "Rapid",
            "rating": 1498,
            "played": 120,
            "win": 60,
            "loss": 50,
            "draw": 10,
            "certainty": "Rankable",
            "user_uid": "a3f1c4d2-8b7e-4c1a-9d3e-5f6a7b8c9d11"
          }
        },
        "takeback": "CasualOnly",
        "lang": "en"
      },
      "moves": {
        "wQ": [{ "q": 0, "r": 0 }, { "q": 1, "r": 0 }]
      },
      "spawns": [{ "q": 0, "r": 0 }],
      "rated": true,
      "reserve_black": {
        "Queen": ["bQ"],
        "Spider": ["bS1", "bS2"],
        "Beetle": ["bB1", "bB2"],
        "Grasshopper": ["bG1", "bG2", "bG3"],
        "Ant": ["bA1", "bA2", "bA3"]
      },
      "reserve_white": {
        "Queen": ["wQ"],
        "Spider": ["wS1", "wS2"],
        "Beetle": ["wB1", "wB2"],
        "Grasshopper": ["wG1", "wG2", "wG3"],
        "Ant": ["wA1", "wA2", "wA3"]
      },
      "history": [],
      "game_control_history": [],
      "white_rating": 1452.0,
      "black_rating": 1498.0,
      "white_rating_change": null,
      "black_rating_change": null,
      "time_mode": "RealTime",
      "time_base": 600,
      "time_increment": 5,
      "speed": "Rapid",
      "black_time_left": 600000000000,
      "white_time_left": 600000000000,
      "last_interaction": null,
      "created_at": "2026-08-13T14:00:05.000Z",
      "updated_at": "2026-08-13T14:00:05.000Z",
      "hashes": [],
      "conclusion": "Unknown",
      "repetitions": [],
      "game_start": "Moves",
      "game_speed": "Rapid",
      "move_times": [],
      "tournament_game_result": "Unknown"
    }
  }
}
```

`history` here is `Vec<(String, String)>` — pairs of `(piece, position)` —
rather than the semicolon-delimited string used by the `/games/*`
endpoints. A game with moves played looks like:

```json
"history": [["wS1", ""], ["bG1", "-wS1"], ["wA1", "wS1/"]]
```

`game_control_history` is `Vec<(turn, GameControl)>`; a resign at turn 3
looks like `[[3, { "Resign": "Black" }]]`.

## User info

```
GET /api/v1/bot/user/{id}
```

`{id}` is a user UUID (use the bot's own `bot.id`, from the JWT, to fetch
its own profile).

```json
{
  "success": true,
  "data": {
    "bot": "bot1@bots.local",
    "user": {
      "username": "Bot1",
      "uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10",
      "patreon": false,
      "bot": true,
      "admin": false,
      "deleted": false,
      "ratings": {
        "Blitz": {
          "speed": "Blitz",
          "rating": 1400,
          "played": 10,
          "win": 5,
          "loss": 4,
          "draw": 1,
          "certainty": "Provisional",
          "user_uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10"
        },
        "Rapid": {
          "speed": "Rapid",
          "rating": 1452,
          "played": 40,
          "win": 22,
          "loss": 15,
          "draw": 3,
          "certainty": "Rankable",
          "user_uid": "b1d5e2b0-6e0a-4a2b-9c1d-2f6a7e8b9c10"
        }
      },
      "takeback": "Always",
      "lang": null
    }
  }
}
```

`ratings` only has entries for `GameSpeed`s the user has actually played
(`Bullet`, `Blitz`, `Rapid`, `Classic`, `Correspondence`, `Untimed`,
`Puzzle`); a speed with no games is simply absent from the map (treated as
rating 0 by clients).

## Typical bot loop

1. `POST /api/v1/auth/token` to get a bearer token; refresh it before it
   expires (it's a JWT — decode `exp` locally rather than guessing).
2. Poll `GET /api/v1/bot/games/pending` to find games needing attention.
3. For a new pairing, both sides call `POST /games/control` with
   `"start"`.
4. On the bot's turn, compute a move and call `POST /games/play`.
5. On game end, go back to step 2.
