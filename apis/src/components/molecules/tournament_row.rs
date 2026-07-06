use crate::{
    components::{atoms::progress_bar::ProgressBar, molecules::time_row::TimeRow},
    responses::TournamentAbstractResponse,
};
use chrono::Local;
use leptos::prelude::*;
use shared_types::{PrettyString, TimeInfo, TournamentMode, TournamentStatus};

#[component]
pub fn TournamentRow(tournament: TournamentAbstractResponse) -> impl IntoView {
    let starts = move || {
        if matches!(tournament.status, TournamentStatus::NotStarted) {
            match tournament.starts_at {
                None => "Start up to organizer".to_string(),
                Some(time) => time
                    .with_timezone(&Local)
                    .format("Start: %d/%m/%Y %H:%M %Z")
                    .to_string(),
            }
        } else {
            tournament.status.pretty_string()
        }
    };
    let range = move || {
        let lower = match tournament.band_lower {
            None => "any".to_string(),
            Some(lower) => lower.to_string(),
        };

        let upper = match tournament.band_upper {
            None => "any".to_string(),
            Some(upper) => upper.to_string(),
        };

        format!("Elo: {lower}-{upper}")
    };

    let seats_taken = format!("{}/{} players", tournament.players, tournament.seats);
    let time_info = TimeInfo {
        mode: tournament.time_mode,
        base: tournament.time_base,
        increment: tournament.time_increment,
    };
    let total_games = tournament.games_total;
    let finished_games = Signal::derive(move || tournament.games_played);
    let mode_label = move || {
        tournament
            .mode
            .parse::<TournamentMode>()
            .map(|mode| match mode {
                TournamentMode::RoundRobin => {
                    let pairs = tournament.round_robin_pairs;
                    if pairs == 1 {
                        String::from("Round robin (1 pair)")
                    } else {
                        format!("Round robin ({pairs} pairs)")
                    }
                }
                _ => mode.pretty_string(),
            })
            .unwrap_or_default()
    };
    view! {
        <article class="flex relative flex-col gap-3 p-4 w-full ui-card-row">
            <div class="w-full text-lg font-bold text-gray-900 break-words dark:text-gray-100">
                {tournament.name}
            </div>
            <div class="grid gap-3 text-sm text-gray-700 sm:grid-cols-2 dark:text-gray-300">
                <div class="flex flex-col gap-1">
                    <div class="flex gap-1">
                        <div>{mode_label}</div>
                    </div>
                    <TimeRow time_info />
                    <div>{seats_taken}</div>
                </div>
                <div class="flex flex-col gap-1 sm:text-right">
                    <div>{range}</div>
                    <Show when=move || tournament.invite_only>
                        <div>Invite only</div>
                    </Show>
                    <div>{starts}</div>
                </div>
            </div>
            <ProgressBar current=finished_games total=total_games />
            <a
                class="absolute top-0 left-0 z-10 size-full"
                href=format!("/tournament/{}", tournament.tournament_id.0)
            ></a>
        </article>
    }
}
