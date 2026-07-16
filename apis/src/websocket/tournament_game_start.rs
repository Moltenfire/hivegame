use anyhow::{anyhow, Result};
use chrono::{DateTime, Utc};
use db_lib::models::Game;
use shared_types::GameId;
use std::{collections::HashMap, sync::RwLock};
use uuid::Uuid;

pub const REQUEST_TTL_SECONDS: i64 = 35;

#[derive(Clone, Debug)]
pub struct PendingGameRequest {
    pub game_id: GameId,
    pub proposer_id: Uuid,
    pub white_id: Uuid,
    pub black_id: Uuid,
    pub created_at: DateTime<Utc>,
    accepted_by: Option<Uuid>,
}

impl PendingGameRequest {
    pub fn involves(&self, user_id: Uuid) -> bool {
        self.white_id == user_id || self.black_id == user_id
    }

    pub fn expires_at(&self) -> DateTime<Utc> {
        self.created_at + chrono::Duration::seconds(REQUEST_TTL_SECONDS)
    }
}

#[derive(Debug)]
pub struct TournamentGameStart {
    pub games_date: RwLock<HashMap<GameId, PendingGameRequest>>,
}

impl TournamentGameStart {
    pub fn new() -> Self {
        Self {
            games_date: RwLock::new(HashMap::new()),
        }
    }

    fn prune_expired(games: &mut HashMap<GameId, PendingGameRequest>, now: DateTime<Utc>) {
        games.retain(|_, request| request.expires_at() > now);
    }

    pub fn live_requests(&self) -> Result<Vec<PendingGameRequest>> {
        let now = Utc::now();
        let mut games = self
            .games_date
            .write()
            .map_err(|_| anyhow!("Could not acquire game request write lock"))?;
        Self::prune_expired(&mut games, now);
        Ok(games
            .values()
            .filter(|request| request.accepted_by.is_none())
            .cloned()
            .collect())
    }

    pub fn complete_request(&self, game_id: &GameId) -> Result<()> {
        let mut games = self
            .games_date
            .write()
            .map_err(|_| anyhow!("Could not acquire game request write lock"))?;
        games.remove(game_id);
        Ok(())
    }

    pub fn should_start(&self, game: &Game, user_id: Uuid) -> Result<bool> {
        self.should_start_inner(game, user_id, false)
    }

    pub fn should_start_exclusive(&self, game: &Game, user_id: Uuid) -> Result<bool> {
        self.should_start_inner(game, user_id, true)
    }

    fn should_start_inner(&self, game: &Game, user_id: Uuid, exclusive: bool) -> Result<bool> {
        if game.black_id != user_id && game.white_id != user_id {
            return Err(anyhow!("Not your game to start"));
        }
        if let Ok(mut games_date) = self.games_date.try_write() {
            let now = Utc::now();
            Self::prune_expired(&mut games_date, now);
            let game_id = GameId(game.nanoid.clone());

            if let Some(request) = games_date.get_mut(&game_id) {
                if request.proposer_id == user_id {
                    if request.accepted_by.is_some() {
                        return Err(anyhow!("Game request is already being accepted"));
                    }
                    request.created_at = now;
                    return Ok(false);
                }
                if exclusive {
                    if request.accepted_by.is_some() {
                        return Err(anyhow!("Game request is already being accepted"));
                    }
                    request.accepted_by = Some(user_id);
                    request.created_at = now;
                }
                return Ok(true);
            }

            if exclusive
                && games_date.values().any(|request| {
                    request.involves(game.white_id) || request.involves(game.black_id)
                })
            {
                return Err(anyhow!("A player already has a pending game request"));
            }

            games_date.insert(
                game_id.clone(),
                PendingGameRequest {
                    game_id,
                    proposer_id: user_id,
                    white_id: game.white_id,
                    black_id: game.black_id,
                    created_at: now,
                    accepted_by: None,
                },
            );
            Ok(false)
        } else {
            println!("Could not acquire write lock for TournamentGameStart");
            Err(anyhow!(
                "Could not acquire write lock for TournamentGameStart"
            ))
        }
    }
}

impl Default for TournamentGameStart {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use hive_lib::GameType;
    use shared_types::GameStart;

    fn game(game_id: &str, white_id: Uuid, black_id: Uuid) -> Game {
        let mut game: Game = serde_json::from_value(serde_json::json!({
            "id": Uuid::new_v4(),
            "nanoid": game_id,
            "current_player_id": white_id,
            "black_id": black_id,
            "finished": false,
            "game_status": "NotStarted",
            "game_type": GameType::Base.to_string(),
            "history": "",
            "game_control_history": "",
            "rated": false,
            "tournament_queen_rule": true,
            "turn": 0,
            "white_id": white_id,
            "white_rating": null,
            "black_rating": null,
            "white_rating_change": null,
            "black_rating_change": null,
            "created_at": Utc::now(),
            "updated_at": Utc::now(),
            "time_mode": "Untimed",
            "time_base": null,
            "time_increment": null,
            "last_interaction": null,
            "black_time_left": null,
            "white_time_left": null,
            "speed": "Untimed",
            "hashes": [],
            "conclusion": "Unknown",
            "tournament_id": Uuid::new_v4(),
            "tournament_game_result": "Unknown",
            "game_start": GameStart::Ready.to_string(),
            "move_times": [],
            "timeout_at": null
        }))
        .expect("test game");
        game.nanoid = game_id.to_string();
        game
    }

    #[test]
    fn request_is_accepted_by_the_other_player() {
        let registry = TournamentGameStart::new();
        let white = Uuid::new_v4();
        let black = Uuid::new_v4();
        let game = game("g1", white, black);

        assert!(!registry.should_start_exclusive(&game, white).unwrap());
        assert!(registry.should_start_exclusive(&game, black).unwrap());
        assert!(registry.live_requests().unwrap().is_empty());
    }

    #[test]
    fn completed_request_releases_both_players() {
        let registry = TournamentGameStart::new();
        let white = Uuid::new_v4();
        let black = Uuid::new_v4();
        let other = Uuid::new_v4();
        let first = game("g1", white, black);

        assert!(!registry.should_start_exclusive(&first, white).unwrap());
        assert!(registry.should_start_exclusive(&first, black).unwrap());
        registry
            .complete_request(&GameId(first.nanoid.clone()))
            .unwrap();

        assert!(!registry
            .should_start_exclusive(&game("g2", white, other), white)
            .unwrap());
    }

    #[test]
    fn exclusive_requests_reserve_both_players() {
        let registry = TournamentGameStart::new();
        let white = Uuid::new_v4();
        let black = Uuid::new_v4();
        let other = Uuid::new_v4();

        assert!(!registry
            .should_start_exclusive(&game("g1", white, black), white)
            .unwrap());
        assert!(registry
            .should_start_exclusive(&game("g2", other, black), other)
            .is_err());
    }

    #[test]
    fn exclusive_request_can_only_be_accepted_once() {
        let registry = TournamentGameStart::new();
        let white = Uuid::new_v4();
        let black = Uuid::new_v4();
        let game = game("g1", white, black);

        assert!(!registry.should_start_exclusive(&game, white).unwrap());
        assert!(registry.should_start_exclusive(&game, black).unwrap());
        assert!(registry.should_start_exclusive(&game, black).is_err());
    }

    #[test]
    fn expired_requests_are_removed() {
        let registry = TournamentGameStart::new();
        let white = Uuid::new_v4();
        let black = Uuid::new_v4();
        let game = game("g1", white, black);
        assert!(!registry.should_start_exclusive(&game, white).unwrap());
        registry
            .games_date
            .write()
            .unwrap()
            .get_mut(&GameId("g1".into()))
            .unwrap()
            .created_at = Utc::now() - chrono::Duration::seconds(REQUEST_TTL_SECONDS + 1);

        assert!(registry.live_requests().unwrap().is_empty());
    }
}
