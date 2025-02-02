import os
import random
import asyncio
import discord
from discord.ext import commands

from flask import Flask, request
from threading import Thread

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Environment variables in Replit secrets:
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# ----- CONFIGURATIONS -----
BOT_PREFIX = '!'
REDIRECT_URI = "https://web-production-b04e.up.railway.app/callback"
SCOPES = "user-read-recently-played"

# ----- MULTI-SERVER GAME STATES -----
active_games = {}  # { guild_id: { ...gameState... } }

def get_game_state(ctx):
    """
    Returns the game state dict for this server/guild.
    If none exists, creates one.
    """
    guild_id = ctx.guild.id
    if guild_id not in active_games:
        active_games[guild_id] = {
            "status": False,
            "players": set(),
            "track_pool": [],
            "current_round": 0,
            "round_in_progress": False,
            "round_guesses": {},
            "points": {}
        }
    return active_games[guild_id]

# For each user ID, we store (guild_id, channel_id) to announce "ready" post-auth
join_channels = {}  # { user_id: (guild_id, channel_id) }

# In-memory storage for user Spotify data
user_spotify_data = {}

# ----- FLASK APP FOR SPOTIFY OAUTH -----
app = Flask(__name__)
app.secret_key = "some-random-secret-key"

def create_spotify_oauth(state=None):
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        show_dialog=True,
        state=state
    )

@app.route("/")
def index():
    return "Spotify Guessing Game Bot is running!"

@app.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return f"There was an error during Spotify authorization: {error}", 400

    if code:
        try:
            sp_oauth = create_spotify_oauth(state=state)
            token_info = sp_oauth.get_access_token(code, check_cache=False)
        except Exception as e:
            return f"Error obtaining access token: {e}", 400

        if token_info:
            discord_user_id = str(state)
            print(f"DEBUG: Received token_info for Discord user {discord_user_id}: {token_info}")
            user_spotify_data[discord_user_id] = token_info
            print(f"DEBUG: user_spotify_data keys are now: {list(user_spotify_data.keys())}")

            def confirm_authorization():
                async def confirm_authorization_async():
                    print(f"DEBUG: confirm_authorization triggered for user {discord_user_id}")
                    guild_channel = join_channels.get(discord_user_id)
                    if not guild_channel:
                        print(f"DEBUG: No stored guild/channel for user {discord_user_id}")
                        return

                    guild_id, channel_id = guild_channel
                    print(f"DEBUG: For user {discord_user_id}, got guild_id={guild_id}, channel_id={channel_id}")

                    channel = bot.get_channel(channel_id)
                    print(f"DEBUG: get_channel({channel_id}) returned: {channel}")
                    if channel is None:
                        print("DEBUG: Could not find channel object (check permissions or if it's valid).")
                        return

                    try:
                        user_obj = await bot.fetch_user(int(discord_user_id))
                        print(f"DEBUG: fetch_user({discord_user_id}) returned: {user_obj}")
                    except Exception as ex:
                        print(f"DEBUG: Exception fetching user {discord_user_id}: {ex}")
                        return

                    if user_obj is not None:
                        msg = f"{user_obj.mention} is now ready to play!"
                        await channel.send(msg)
                    else:
                        print(f"DEBUG: Could not fetch user object for {discord_user_id} to mention.")

                asyncio.run_coroutine_threadsafe(confirm_authorization_async(), bot.loop)

            bot.loop.call_soon_threadsafe(confirm_authorization)
            return "Authorization successful! You can close this tab and return to Discord."
        else:
            return "Could not get token info from Spotify.", 400
    else:
        return "No code returned from Spotify.", 400

def get_spotify_client(discord_user_id):
    token_info = user_spotify_data.get(str(discord_user_id))
    if not token_info:
        return None

    sp_oauth = create_spotify_oauth(state=str(discord_user_id))
    if sp_oauth.is_token_expired(token_info):
        try:
            token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
            user_spotify_data[str(discord_user_id)] = token_info
        except Exception as e:
            print(f"DEBUG: Error refreshing token for Discord user {discord_user_id}: {e}")
            return None

    return spotipy.Spotify(auth=token_info["access_token"])

# ----- DISCORD BOT SETUP -----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents)

@bot.event
async def on_ready():
    print(f"Bot logged in as {bot.user}")

# ----- DISCORD COMMANDS -----
@bot.command()
async def start(ctx):
    game_state = get_game_state(ctx)
    if game_state["status"]:
        await ctx.send("A game is already in progress in this server.")
        return

    game_state["status"] = True
    game_state["players"] = set()
    game_state["track_pool"] = []
    game_state["current_round"] = 0
    game_state["round_in_progress"] = False
    game_state["round_guesses"] = {}
    game_state["points"] = {}

    await ctx.send("A new game has started! Type `!join` to participate.")

@bot.command()
async def join(ctx):
    game_state = get_game_state(ctx)
    if not game_state["status"]:
        await ctx.send("No game is currently running. Use `!start` to create a new game.")
        return

    user_id = str(ctx.author.id)
    if user_id in game_state["players"]:
        await ctx.send("You have already joined the game.")
        return

    # Store the guild/channel ID for this user
    join_channels[user_id] = (ctx.guild.id, ctx.channel.id)
    game_state["players"].add(user_id)

    await ctx.send(f"{ctx.author.mention}, check your DMs to optionally authorize Spotify.")

    sp_oauth = create_spotify_oauth(state=user_id)
    auth_url = sp_oauth.get_authorize_url()

    try:
        await ctx.author.send(
            "Click the link below to authorize with Spotify:\n" + auth_url + "\n"
            "If you don't authorize, you can still guess, but no tracks will be added on your behalf."
        )
    except discord.Forbidden:
        await ctx.send("I couldn't DM you. Please enable your DMs or add me as a friend.")

@bot.command()
async def play(ctx):
    game_state = get_game_state(ctx)
    if not game_state["status"]:
        await ctx.send("No game is active. Use `!start` to begin.")
        return
    if len(game_state["players"]) < 2:
        await ctx.send("Need at least 2 players to play!")
        return

    track_pool = []
    user_track_ids = {}

    for player_id in game_state["players"]:
        sp_client = get_spotify_client(player_id)
        if sp_client is None:
            print(f"DEBUG: No Spotify client for user {player_id} (not authorized). Skipping tracks.")
            # They can guess, but no tracks from them
            continue

        try:
            user_info = sp_client.me()
            spotify_user_id = user_info.get("id", "unknown_id")
            spotify_display_name = user_info.get("display_name", "Unknown Display Name")
            print(
                f"DEBUG: (Guild {ctx.guild.id}) Discord user {player_id} => "
                f"Spotify ID '{spotify_user_id}', display name: '{spotify_display_name}'"
            )
        except Exception as e:
            print(f"DEBUG: Error calling sp_client.me() for {player_id}: {e}")
            continue

        user_track_ids[player_id] = set()
        try:
            results = sp_client.current_user_recently_played(limit=20)
            items_count = len(results['items'])
            print(f"DEBUG: Found {items_count} recently played items for user {player_id}.")

            for item in results["items"]:
                track = item["track"]
                track_id = track["id"]
                if not track_id:
                    continue

                if track_id in user_track_ids[player_id]:
                    continue

                user_track_ids[player_id].add(track_id)

                track_name = track["name"]
                artist_name = track["artists"][0]["name"]

                existing_track = next((t for t in track_pool if t[0] == track_id), None)
                if existing_track:
                    existing_track[3].add(player_id)
                else:
                    track_pool.append((track_id, track_name, artist_name, {player_id}))

        except Exception as e:
            print(f"Error fetching recent tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or nobody authorized. We'll still proceed, but there's nothing to guess!")
        # We won't return so they can do a 'round' if they want
        # but the track pool is empty => the game will end quickly

    random.shuffle(track_pool)
    game_state["track_pool"] = track_pool
    game_state["current_round"] = 0

    await ctx.send("Track pool compiled! Starting the game...")
    await do_guess_round(ctx)

async def do_guess_round(ctx):
    """
    The main round logic. If we've used all tracks or hit 20 rounds, end the game.
    Otherwise, show the next track, wait 10s for guesses, and award points.
    """
    game_state = get_game_state(ctx)

    if game_state["current_round"] >= len(game_state["track_pool"]) or game_state["current_round"] >= 20:
        # Enough rounds or no more tracks
        await announce_winner_and_reset(ctx, game_finished=True)
        return

    track_id, track_name, artist_name, owner_ids = game_state["track_pool"][game_state["current_round"]]

    await ctx.send(
        f"**Guess Round {game_state['current_round']+1}:**\n"
        f"**Track:** {track_name} by {artist_name}\n"
        "You have 10 seconds! Type `!guess @username` to guess."
    )

    game_state["round_in_progress"] = True
    game_state["round_guesses"] = {}

    await asyncio.sleep(10)

    game_state["round_in_progress"] = False

    winners = []
    for guesser_id, guessed_user_id in game_state["round_guesses"].items():
        if guessed_user_id in owner_ids:
            # skip awarding if guesser is the same user
            if guesser_id == guessed_user_id:
                continue
            winners.append(guesser_id)

    # Award points
    for w in winners:
        if w not in game_state["points"]:
            game_state["points"][w] = 0
        game_state["points"][w] += 1

    # Announce results of this round
    if winners:
        owner_mentions = ", ".join(f"<@{o}>" for o in owner_ids)
        winner_mentions = ", ".join(f"<@{w}>" for w in winners)
        await ctx.send(
            f"Time's up! The correct owner(s) for '{track_name}' was {owner_mentions}.\n"
            f"Congrats to {winner_mentions} for guessing correctly!"
        )
    else:
        owner_mentions = ", ".join(f"<@{o}>" for o in owner_ids)
        await ctx.send(
            f"Time's up! No one guessed correctly.\n"
            f"The track '{track_name}' belongs to {owner_mentions}."
        )

    # If the game was ended while we were sleeping, skip continuing
    if not game_state["status"]:
        return

    game_state["current_round"] += 1
    await do_guess_round(ctx)

@bot.command()
async def guess(ctx, user_mention: discord.User = None):
    game_state = get_game_state(ctx)

    if not game_state["status"]:
        await ctx.send("No active game in this server right now.")
        return
    if not game_state["round_in_progress"]:
        await ctx.send("No guessing period is active right now or time is up!")
        return
    if user_mention is None:
        await ctx.send("Please mention a user to guess. Example: `!guess @SomeUser`")
        return

    guesser_id = str(ctx.author.id)
    if guesser_id in game_state["round_guesses"]:
        await ctx.send("You have already guessed this round!")
        return

    guessed_user_id = str(user_mention.id)
    game_state["round_guesses"][guesser_id] = guessed_user_id
    await ctx.send(f"{ctx.author.mention} your guess has been recorded!")

@bot.command()
async def end(ctx):
    """
    Ends the current game prematurely in this server. We still show the scoreboard.
    """
    game_state = get_game_state(ctx)
    if not game_state["status"]:
        await ctx.send("No game is currently running in this server.")
        return

    await announce_winner_and_reset(ctx, game_finished=False)

async def announce_winner_and_reset(ctx, game_finished=True):
    """
    Announces the scoreboard for this server, then resets that server's game state.
    """
    game_state = get_game_state(ctx)
    points_map = game_state["points"]
    players = game_state["players"]

    # Make sure all players appear in points_map
    for pid in players:
        if pid not in points_map:
            points_map[pid] = 0

    scoreboard = [(pid, points_map[pid]) for pid in players]
    scoreboard.sort(key=lambda x: x[1], reverse=True)

    if scoreboard:
        top_score = scoreboard[0][1]
        winners = [uid for (uid, pts) in scoreboard if pts == top_score]

        scoreboard_lines = [f"<@{uid}>: {pts} points" for (uid, pts) in scoreboard]
        scoreboard_str = "\n".join(scoreboard_lines)

        if len(winners) == 1:
            await ctx.send("Game over!" if game_finished else "Game ended prematurely!")
            await ctx.send(
                f"**Final Scores:**\n{scoreboard_str}\n\n"
                f"**Winner:** <@{winners[0]}> with {top_score} points!"
            )
        else:
            tie_mentions = ", ".join(f"<@{w}>" for w in winners)
            await ctx.send("Game over!" if game_finished else "Game ended prematurely!")
            await ctx.send(
                f"**Final Scores:**\n{scoreboard_str}\n\n"
                f"**Winners (tie):** {tie_mentions} with {top_score} points each!"
            )
    else:
        await ctx.send("No one scored any points, so no winner. Maybe no guesses?")

    # Reset only this server
    guild_id = ctx.guild.id
    active_games[guild_id] = {
        "status": False,
        "players": set(),
        "track_pool": [],
        "current_round": 0,
        "round_in_progress": False,
        "round_guesses": {},
        "points": {}
    }

# ----- RUN FLASK (SPOTIFY OAUTH) IN A SEPARATE THREAD -----
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

keep_alive()
bot.run(DISCORD_BOT_TOKEN)
