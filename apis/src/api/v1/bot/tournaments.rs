use crate::{api::v1::auth::Auth, responses::TournamentResponse};
use actix_web::{
    get,
    web::{Data, Path},
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
use serde_json::json;
use shared_types::TournamentId;
use uuid::Uuid;

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
