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
# Instead of one global dict, we store a dict of game states keyed by guild_id
# Each game state is the same structure we had before, but now per server
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

# We store for each user ID a (guild_id, channel_id) so we know where to announce "ready"
join_channels = {}  # { user_id: (guild_id, channel_id) }

# In-memory storage for user Spotify data: { discord_user_id: token_info }
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
                    # Retrieve (guild_id, channel_id) from join_channels
                    guild_channel = join_channels.get(discord_user_id)
                    if not guild_channel:
                        print(f"DEBUG: No stored guild/channel for user {discord_user_id}")
                        return

                    guild_id, channel_id = guild_channel
                    print(f"DEBUG: For user {discord_user_id}, got guild_id={guild_id}, channel_id={channel_id}")

                    channel = bot.get_channel(channel_id)
                    print(f"DEBUG: get_channel({channel_id}) returned: {channel}")
                    if channel is None:
                        print("DEBUG: Could not find channel object. Check bot permissions or if channel is valid.")
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

                # Schedule the async function
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

    # Store the guild/channel for the user
    join_channels[user_id] = (ctx.guild.id, ctx.channel.id)
    game_state["players"].add(user_id)

    await ctx.send(f"{ctx.author.mention}, check your DMs to authorize Spotify (optional).")

    sp_oauth = create_spotify_oauth(state=user_id)
    auth_url = sp_oauth.get_authorize_url()

    try:
        await ctx.author.send(
            "Click the link below to authorize with Spotify:\n" + auth_url + "\n"
            "If you choose not to authorize, you can still guess but won't add any tracks."
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
            print(f"DEBUG: No Spotify client for user {player_id}. Possibly not authorized.")
            # They can still play (guess), but we skip adding tracks
            continue

        try:
            user_info = sp_client.me()
            spotify_user_id = user_info.get("id", "unknown_id")
            spotify_display_name = user_info.get("display_name", "Unknown Display Name")
            print(
                f"DEBUG: (Guild {ctx.guild.id}) Discord user {player_id} => Spotify ID '{spotify_user_id}', "
                f"display name: '{spotify_display_name}'"
            )
        except Exception as e:
            print(f"DEBUG: Error calling sp_client.me() for {player_id}: {e}")
            continue

        print(f"DEBUG: Fetching recently played tracks for user {player_id} ...")
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

                # See if this track is already in the global pool
                existing_track = next((t for t in track_pool if t[0] == track_id), None)
                if existing_track:
                    existing_track[3].add(player_id)
                else:
                    track_pool.append((track_id, track_name, artist_name, {player_id}))

        except Exception as e:
            print(f"Error fetching recent tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or no one authorized Spotify. The game can still proceed, but there's nothing to guess!")
        # You could return or you can let them guess with an empty pool
        # We'll proceed so they can do a round with no songs if they want
        # return

    random.shuffle(track_pool)
    game_state["track_pool"] = track_pool
    game_state["current_round"] = 0

    await ctx.send("Track pool compiled! Starting the game...")
    await do_guess_round(ctx)

async def do_guess_round(ctx):
    game_state = get_game_state(ctx)

    # If we've used all tracks or we've hit 20 rounds, end the game
    if game_state["current_round"] >= len(game_state["track_pool"]) or game_state["current_round"] >= 20:
        await announce_winner_and_reset(ctx, game_finished=True)
        return

    # Grab the next track
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

    # Score correct guesses
    winners = []
    for guesser_id, guessed_user_id in game_state["round_guesses"].items():
        if guessed_user_id in owner_ids:
            # Skip awarding if guesser is the same user
            if guesser_id == guessed_user_id:
                continue
            winners.append(guesser_id)

    # Award points
    for w in winners:
        if w not in game_state["points"]:
            game_state["points"][w] = 0
        game_state["points"][w] += 1

    # Announce result
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

    # Check if the game is still active (maybe someone typed !end)
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
    Announces the scoreboard for the current server, then resets that server's game.
    """
    game_state = get_game_state(ctx)
    points_map = game_state["points"]
    players = game_state["players"]

    # Make sure every player is in points_map
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

    # Reset only this server's game
    # (Remove the entire state from active_games or reinitialize it)
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
