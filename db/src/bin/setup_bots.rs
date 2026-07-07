use argon2::{
    password_hash::{rand_core::OsRng, PasswordHasher, SaltString},
    Argon2,
};
use anyhow::{anyhow, Result};
use chrono::Utc;
use db_lib::{
    config::DbConfig,
    get_conn, get_pool,
    models::{NewUser, User},
    schema::users,
};
use diesel::prelude::*;
use diesel_async::RunQueryDsl;
use std::env;

const DEFAULT_BOT_COUNT: usize = 4;
const DEFAULT_BOT_PREFIX: &str = "Bot";
const DEFAULT_BOT_PASSWORD: &str = "bot-password";

#[tokio::main]
async fn main() -> Result<()> {
    let config = DbConfig::from_env()?;
    let pool = get_pool(&config.database_url).await?;
    let mut conn = get_conn(&pool).await?;

    let bot_count = env::var("BOT_COUNT")
        .ok()
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(DEFAULT_BOT_COUNT);
    let bot_prefix = env::var("BOT_PREFIX").unwrap_or_else(|_| DEFAULT_BOT_PREFIX.to_string());
    let shared_password =
        env::var("BOT_PASSWORD").unwrap_or_else(|_| DEFAULT_BOT_PASSWORD.to_string());

    for index in 1..=bot_count {
        let username = format!("{bot_prefix}{index}");
        let password = env::var(format!("BOT{index}_PASSWORD"))
            .unwrap_or_else(|_| shared_password.clone());
        let password_hash = hash_password(&password)?;
        let email = format!("{}@bots.local", username.to_lowercase());

        let existing = users::table
            .filter(users::normalized_username.eq(username.to_lowercase()))
            .first::<User>(&mut conn)
            .await
            .optional()?;

        if let Some(user) = existing {
            diesel::update(users::table.find(user.id))
                .set((
                    users::password.eq(password_hash),
                    users::bot.eq(true),
                    users::updated_at.eq(Utc::now()),
                ))
                .execute(&mut conn)
                .await?;
            println!("updated {username}");
        } else {
            let mut new_user = NewUser::new(&username, &password_hash, &email)?;
            new_user.bot = true;
            User::create(new_user, &mut conn).await?;
            println!("created {username}");
        }
    }

    Ok(())
}

fn hash_password(password: &str) -> Result<String> {
    let salt = SaltString::generate(&mut OsRng);
    Ok(Argon2::default()
        .hash_password(password.as_bytes(), &salt)
        .map_err(|err| anyhow!(err.to_string()))?
        .to_string())
}
