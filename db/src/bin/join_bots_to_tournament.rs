use anyhow::{anyhow, Result};
use db_lib::{
    config::DbConfig,
    get_conn, get_pool,
    models::{Tournament, User},
    schema::{tournaments_users, users},
};
use diesel::prelude::*;
use diesel_async::{scoped_futures::ScopedFutureExt, AsyncConnection, RunQueryDsl};
use shared_types::TournamentId;
use std::env;
use uuid::Uuid;

const DEFAULT_BOT_COUNT: usize = 4;
const DEFAULT_BOT_PREFIX: &str = "Bot";

#[tokio::main]
async fn main() -> Result<()> {
    let tournament_ref = env::var("TOURNAMENT_ID")
        .map_err(|_| anyhow!("TOURNAMENT_ID must be set to a tournament UUID or nanoid"))?;

    let config = DbConfig::from_env()?;
    let pool = get_pool(&config.database_url).await?;
    let mut conn = get_conn(&pool).await?;

    let bot_count = env::var("BOT_COUNT")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_BOT_COUNT);
    let bot_prefix = env::var("BOT_PREFIX").unwrap_or_else(|_| DEFAULT_BOT_PREFIX.to_string());

    let mut tournament = find_tournament(&tournament_ref, &mut conn).await?;

    for index in 1..=bot_count {
        let username = format!("{bot_prefix}{index}");
        let bot = users::table
            .filter(users::normalized_username.eq(username.to_lowercase()))
            .filter(users::bot.eq(true))
            .filter(users::deleted.eq(false))
            .first::<User>(&mut conn)
            .await
            .map_err(|_| anyhow!("{username} does not exist or is not marked as a bot"))?;

        let already_joined = diesel::select(diesel::dsl::exists(
            tournaments_users::table.find((tournament.id, bot.id)),
        ))
        .get_result::<bool>(&mut conn)
        .await?;

        if already_joined {
            println!("{username} already joined {}", tournament.nanoid);
            continue;
        }

        let tournament_to_join = tournament.clone();
        let bot_id = bot.id;
        tournament = conn
            .transaction::<_, anyhow::Error, _>(|tc| {
                async move { Ok(tournament_to_join.join(&bot_id, tc).await?) }.scope_boxed()
            })
            .await?;
        println!("joined {username} to {}", tournament.nanoid);
    }

    Ok(())
}

async fn find_tournament(tournament_ref: &str, conn: &mut db_lib::DbConn<'_>) -> Result<Tournament> {
    if let Ok(uuid) = Uuid::parse_str(tournament_ref) {
        return Ok(Tournament::find_by_uuid(uuid, conn).await?);
    }

    Ok(Tournament::find_by_tournament_id(&TournamentId(tournament_ref.to_string()), conn).await?)
}
