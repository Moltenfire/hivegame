use crate::{api::v1::auth::Auth, websocket::WsHub};
use actix_web::{
    get,
    web::{Data, Path},
    HttpResponse,
};
use anyhow::Result;
use db_lib::{
    get_conn,
    models::{Game, User},
    DbPool,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use shared_types::GameId;
use std::sync::Arc;
use uuid::Uuid;

#[derive(Serialize, Deserialize)]
pub enum GameSelector {
    Ongoing,
    Pending,
    Specific(GameId),
}

#[derive(Serialize)]
struct BotGameRequest {
    game_id: GameId,
    proposer_id: Uuid,
    proposer_username: String,
    created_at: chrono::DateTime<chrono::Utc>,
    expires_at: chrono::DateTime<chrono::Utc>,
    outgoing: bool,
}

#[get("/api/v1/bot/game/{nanoid}")]
pub async fn api_get_game(
    nanoid: Path<GameId>,
    Auth(bot): Auth,
    pool: Data<DbPool>,
) -> HttpResponse {
    let nanoid = nanoid.into_inner();
    match get_games(bot.clone(), GameSelector::Specific(nanoid), pool).await {
        Ok(games) => HttpResponse::Ok().json(json!({
          "success": true,
          "data": {
            "bot": bot.email,
            "bot_username": bot.username,
            "games": games,
          }
        })),
        Err(e) => HttpResponse::Ok().json(json!({
          "success": false,
          "data": {
            "error": e.to_string(),
          }
        })),
    }
}

#[get("/api/v1/bot/games/ongoing")]
pub async fn api_get_ongoing_games(Auth(bot): Auth, pool: Data<DbPool>) -> HttpResponse {
    match get_games(bot.clone(), GameSelector::Ongoing, pool).await {
        Ok(games) => HttpResponse::Ok().json(json!({
          "success": true,
          "data": {
            "bot": bot.email,
            "bot_username": bot.username,
            "games": games,
          }
        })),
        Err(e) => HttpResponse::Ok().json(json!({
          "success": false,
          "data": {
            "error": e.to_string(),
          }
        })),
    }
}

#[get("/api/v1/bot/games/pending")]
pub async fn api_get_pending_games(Auth(bot): Auth, pool: Data<DbPool>) -> HttpResponse {
    match get_games(bot.clone(), GameSelector::Pending, pool).await {
        Ok(games) => HttpResponse::Ok().json(json!({
          "success": true,
          "data": {
            "bot": bot.email,
            "bot_username": bot.username,
            "games": games,
          }
        })),
        Err(e) => HttpResponse::Ok().json(json!({
          "success": false,
          "data": {
            "error": e.to_string(),
          }
        })),
    }
}

#[get("/api/v1/bot/games/requests")]
pub async fn api_get_game_requests(
    Auth(bot): Auth,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> HttpResponse {
    match get_game_requests(&bot, pool, hub).await {
        Ok(requests) => HttpResponse::Ok().json(json!({
          "success": true,
          "data": {
            "bot": bot.email,
            "bot_username": bot.username,
            "requests": requests,
          }
        })),
        Err(e) => HttpResponse::Ok().json(json!({
          "success": false,
          "data": {
            "error": e.to_string(),
          }
        })),
    }
}

async fn get_game_requests(
    bot: &User,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> Result<Vec<BotGameRequest>> {
    let requests = hub.data.game_start.live_requests()?;
    let mut conn = get_conn(&pool).await?;
    let mut responses = Vec::new();

    for request in requests {
        if !request.involves(bot.id) {
            continue;
        }
        let game = Game::find_by_game_id(&request.game_id, &mut conn).await?;
        if game.finished || game.game_status != "NotStarted" || game.tournament_id.is_none() {
            continue;
        }
        let proposer = User::find_by_uuid(&request.proposer_id, &mut conn).await?;
        let expires_at = request.expires_at();
        responses.push(BotGameRequest {
            game_id: request.game_id,
            proposer_id: request.proposer_id,
            proposer_username: proposer.username,
            created_at: request.created_at,
            expires_at,
            outgoing: request.proposer_id == bot.id,
        });
    }

    responses.sort_by(|a, b| {
        a.created_at
            .cmp(&b.created_at)
            .then(a.game_id.0.cmp(&b.game_id.0))
    });
    Ok(responses)
}

async fn get_games(bot: User, selector: GameSelector, pool: Data<DbPool>) -> Result<Vec<Game>> {
    let mut conn = get_conn(&pool).await?;
    // Specific lookups self-finalize via find_by_game_id, so return directly.
    let raw = match selector {
        GameSelector::Specific(id) => {
            return Ok(vec![Game::find_by_game_id(&id, &mut conn).await?]);
        }
        GameSelector::Ongoing => bot.get_ongoing_games(&mut conn).await?,
        GameSelector::Pending => bot.get_games_with_notifications(&mut conn).await?,
    };
    // Between sweep ticks a row can be past timeout but not yet finalized;
    // check_time settles it so the bot never sees a stale ongoing/pending game.
    let mut out = Vec::with_capacity(raw.len());
    for game in raw {
        let g = game.check_time(&mut conn).await?;
        if !g.finished {
            out.push(g);
        }
    }
    Ok(out)
}
