use crate::{
    api::v1::{auth::Auth, messages::send::send_messages_batch},
    responses::TournamentResponse,
    websocket::{server_handlers::chat::handler::ChatHandler, WsHub},
};
use actix_web::{
    get,
    post,
    web::{Data, Json, Path},
    HttpResponse,
};
use anyhow::{anyhow, Result};
use db_lib::{
    get_conn,
    models::{Tournament, User},
    schema::{tournaments_organizers, tournaments_users},
    DbPool,
};
use diesel::prelude::*;
use diesel_async::RunQueryDsl;
use serde::{Deserialize, Serialize};
use serde_json::json;
use shared_types::{ChatDestination, ChatMessage, ChatMessageContainer, TournamentId};
use std::sync::Arc;
use uuid::Uuid;

#[derive(Serialize, Deserialize)]
struct ChatPostRequest {
    message: String,
}

#[get("/api/v1/bot/tournament/{tournament_id}")]
pub async fn api_get_tournament(
    tournament_id: Path<String>,
    Auth(bot): Auth,
    pool: Data<DbPool>,
) -> HttpResponse {
    match get_tournament_response(&tournament_id.into_inner(), &bot, pool).await {
        Ok(tournament) => HttpResponse::Ok().json(json!({
            "success": true,
            "data": {
                "tournament": tournament,
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

#[get("/api/v1/bot/tournament/{tournament_id}/chat")]
pub async fn api_get_tournament_chat(
    tournament_id: Path<String>,
    Auth(bot): Auth,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> HttpResponse {
    match get_tournament_chat(&tournament_id.into_inner(), &bot, pool, hub).await {
        Ok(messages) => HttpResponse::Ok().json(json!({
            "success": true,
            "data": {
                "messages": messages,
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

#[post("/api/v1/bot/tournament/{tournament_id}/chat")]
pub async fn api_post_tournament_chat(
    tournament_id: Path<String>,
    Json(req): Json<ChatPostRequest>,
    Auth(bot): Auth,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> HttpResponse {
    match post_tournament_chat(&tournament_id.into_inner(), req, &bot, pool, hub).await {
        Ok(message) => HttpResponse::Ok().json(json!({
            "success": true,
            "data": {
                "message": message,
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

async fn get_tournament_response(
    tournament_ref: &str,
    bot: &User,
    pool: Data<DbPool>,
) -> Result<TournamentResponse> {
    let mut conn = get_conn(&pool).await?;
    let tournament = find_tournament(tournament_ref, &mut conn).await?;
    ensure_tournament_access(&tournament, bot, &mut conn).await?;
    Ok(*TournamentResponse::from_model(&tournament, &mut conn).await?)
}

async fn get_tournament_chat(
    tournament_ref: &str,
    bot: &User,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> Result<Vec<ChatMessageContainer>> {
    let mut conn = get_conn(&pool).await?;
    let tournament = find_tournament(tournament_ref, &mut conn).await?;
    ensure_tournament_access(&tournament, bot, &mut conn).await?;

    Ok(hub
        .data
        .chat_storage
        .tournament
        .read()
        .map_err(|_| anyhow!("Could not read tournament chat"))?
        .get(&TournamentId(tournament.nanoid))
        .cloned()
        .unwrap_or_default())
}

async fn post_tournament_chat(
    tournament_ref: &str,
    req: ChatPostRequest,
    bot: &User,
    pool: Data<DbPool>,
    hub: Data<Arc<WsHub>>,
) -> Result<ChatMessageContainer> {
    let mut conn = get_conn(&pool).await?;
    let tournament = find_tournament(tournament_ref, &mut conn).await?;
    ensure_tournament_access(&tournament, bot, &mut conn).await?;

    let destination = ChatDestination::TournamentLobby(TournamentId(tournament.nanoid.clone()));
    let message = ChatMessage::new(bot.username.clone(), bot.id, &req.message, None, None);
    let container = ChatMessageContainer::new(destination, &message);
    let messages = ChatHandler::new(container.clone(), hub.data.clone()).handle();
    send_messages_batch(hub.as_ref(), messages).await;

    Ok(container)
}

async fn find_tournament(
    tournament_ref: &str,
    conn: &mut db_lib::DbConn<'_>,
) -> Result<Tournament> {
    if let Ok(uuid) = Uuid::parse_str(tournament_ref) {
        return Ok(Tournament::find_by_uuid(uuid, conn).await?);
    }

    Ok(Tournament::find_by_tournament_id(&TournamentId(tournament_ref.to_string()), conn).await?)
}

async fn ensure_tournament_access(
    tournament: &Tournament,
    bot: &User,
    conn: &mut db_lib::DbConn<'_>,
) -> Result<()> {
    let joined = diesel::select(diesel::dsl::exists(
        tournaments_users::table.find((tournament.id, bot.id)),
    ))
    .get_result::<bool>(conn)
    .await?;

    if joined {
        return Ok(());
    }

    let organizer = diesel::select(diesel::dsl::exists(
        tournaments_organizers::table.find((tournament.id, bot.id)),
    ))
    .get_result::<bool>(conn)
    .await?;

    if organizer {
        return Ok(());
    }

    Err(anyhow!(
        "Bot is not joined to this tournament and is not an organizer"
    ))
}
