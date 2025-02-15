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
SCOPES = "user-read-recently-played user-library-read"

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
            "points": {},
            "round_task": None  # <--- We'll store the running round's Task here
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
                        print("DEBUG: Could not find channel object (check permissions).")
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

    # If a leftover round task was still stored, cancel it to be safe
    if game_state.get("round_task"):
        game_state["round_task"].cancel()
        game_state["round_task"] = None

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
    """
    Gathers tracks and starts the first round by scheduling do_guess_round in a Task.
    """
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
            continue

        try:
            user_info = sp_client.me()
            _spotify_user_id = user_info.get("id", "unknown_id")
            _spotify_display_name = user_info.get("display_name", "Unknown Display Name")
        except Exception as e:
            print(f"DEBUG: Error calling sp_client.me() for {player_id}: {e}")
            continue

        user_track_ids[player_id] = set()
        try:
            results = sp_client.current_user_recently_played(limit=20)
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
                # Get album cover URL
                album_cover_url = track["album"]["images"][0]["url"] if track["album"]["images"] else None

                existing_track = next((t for t in track_pool if t[0] == track_id), None)
                if existing_track:
                    existing_track[4].add(player_id)
                else:
                    track_pool.append((track_id, track_name, artist_name, album_cover_url, {player_id}))
        except Exception as e:
            print(f"Error fetching recent tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or nobody authorized. We'll proceed, but there's nothing to guess!")

    random.shuffle(track_pool)
    game_state["track_pool"] = track_pool
    game_state["current_round"] = 0

    # If a leftover round task existed, cancel it
    if game_state.get("round_task"):
        game_state["round_task"].cancel()

    # Create a new task that runs do_guess_round
    task = bot.loop.create_task(do_guess_round(ctx))
    game_state["round_task"] = task

    await ctx.send("Track pool compiled! Starting the game...")

@bot.command()
async def playlikes(ctx):
    """
    Similar to !play but uses random liked songs instead of recently played tracks.
    """
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
            continue

        try:
            user_info = sp_client.me()
            _spotify_user_id = user_info.get("id", "unknown_id")
            _spotify_display_name = user_info.get("display_name", "Unknown Display Name")
        except Exception as e:
            print(f"DEBUG: Error calling sp_client.me() for {player_id}: {e}")
            continue

        user_track_ids[player_id] = set()
        try:
            # First, get total number of liked songs
            initial_results = sp_client.current_user_saved_tracks(limit=1)
            total_tracks = initial_results["total"]
            
            if total_tracks == 0:
                print(f"User {player_id} has no liked songs")
                continue

            # Generate 20 random unique offsets within the user's library size
            max_offset = max(0, total_tracks - 1)  # -1 because offset is 0-based
            random_offsets = set()
            while len(random_offsets) < min(20, total_tracks):
                random_offsets.add(random.randint(0, max_offset))
            
            # Fetch each randomly selected track
            for offset in random_offsets:
                try:
                    result = sp_client.current_user_saved_tracks(limit=1, offset=offset)
                    if not result["items"]:
                        continue
                        
                    item = result["items"][0]
                    track = item["track"]
                    track_id = track["id"]
                    
                    if not track_id or track_id in user_track_ids[player_id]:
                        continue

                    user_track_ids[player_id].add(track_id)
                    track_name = track["name"]
                    artist_name = track["artists"][0]["name"]
                    album_cover_url = track["album"]["images"][0]["url"] if track["album"]["images"] else None

                    existing_track = next((t for t in track_pool if t[0] == track_id), None)
                    if existing_track:
                        existing_track[4].add(player_id)
                    else:
                        track_pool.append((track_id, track_name, artist_name, album_cover_url, {player_id}))
                except Exception as e:
                    print(f"Error fetching individual track at offset {offset} for user {player_id}: {e}")
                    continue
                    
        except Exception as e:
            print(f"Error fetching liked tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or nobody authorized. We'll proceed, but there's nothing to guess!")

    random.shuffle(track_pool)
    game_state["track_pool"] = track_pool
    game_state["current_round"] = 0

    # If a leftover round task existed, cancel it
    if game_state.get("round_task"):
        game_state["round_task"].cancel()

    # Create a new task that runs do_guess_round
    task = bot.loop.create_task(do_guess_round(ctx))
    game_state["round_task"] = task

    await ctx.send("Track pool compiled from random liked songs! Starting the game...")
    
async def do_guess_round(ctx):
    """
    The main loop for each round.
    We'll do the 10-second wait, awarding points, and move to next round
    until we reach 20 or run out of tracks.
    """
    try:
        while True:
            game_state = get_game_state(ctx)

            # If the game is ended externally, bail out
            if not game_state["status"]:
                return

            # If we've used all tracks or hit 20 rounds, end game
            if game_state["current_round"] >= len(game_state["track_pool"]) or game_state["current_round"] >= 20:
                await announce_winner_and_reset(ctx, game_finished=True)
                return

            track_id, track_name, artist_name, album_cover_url, owner_ids = game_state["track_pool"][game_state["current_round"]]
            
            # Create Spotify track URL
            spotify_track_url = f"https://open.spotify.com/track/{track_id}"
            
            # Create embed with cover art, Spotify link, and attribution
            embed = discord.Embed(
                title=f"Guess Round {game_state['current_round']+1}",
                description=f"**Track:** [{track_name}]({spotify_track_url})\n**Artist:** {artist_name}\n\nYou have 10 seconds! Type `!guess @username` to guess.",
                color=0x1DB954  # Spotify's brand green color
            )
            if album_cover_url:
                embed.set_thumbnail(url=album_cover_url)
            
            # Add Spotify attribution
            embed.set_footer(text="Powered by Spotify", icon_url="https://storage.googleapis.com/pr-newsroom-wp/1/2018/11/Spotify_Logo_RGB_Green.png")
            
            await ctx.send(embed=embed)

            game_state["round_in_progress"] = True
            game_state["round_guesses"] = {}

            # Sleep 10s
            await asyncio.sleep(10)

            # If the game is ended while we slept, bail out
            if not game_state["status"]:
                return

            game_state["round_in_progress"] = False

            # Tally winners
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

            if winners:
                owner_mentions = ", ".join(f"<@{o}>" for o in owner_ids)
                winner_mentions = ", ".join(f"<@{w}>" for w in winners)
                await ctx.send(
                    f"Time's up! The correct owner(s) for '[{track_name}]({spotify_track_url})' was {owner_mentions}.\n"
                    f"Congrats to {winner_mentions} for guessing correctly!"
                )
            else:
                owner_mentions = ", ".join(f"<@{o}>" for o in owner_ids)
                await ctx.send(
                    f"Time's up! No one guessed correctly.\n"
                    f"The track '[{track_name}]({spotify_track_url})' belongs to {owner_mentions}."
                )

            # Move to the next round
            game_state["current_round"] += 1

    except asyncio.CancelledError:
        # Round was cancelled (e.g., someone did !end or a new !play)
        return

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
    Also cancel any ongoing round Task so it can't finish and cause a second scoreboard.
    """
    game_state = get_game_state(ctx)
    if not game_state["status"]:
        await ctx.send("No game is currently running in this server.")
        return

    # Cancel any ongoing round Task
    if game_state.get("round_task"):
        game_state["round_task"].cancel()
        game_state["round_task"] = None

    await announce_winner_and_reset(ctx, game_finished=False)

async def announce_winner_and_reset(ctx, game_finished=True):
    """
    Announces the scoreboard for this server, then resets that server's game state.
    """
    game_state = get_game_state(ctx)
    points_map = game_state["points"]
    players = game_state["players"]

    # Mark the status as False, so no new rounds continue
    game_state["status"] = False

    for pid in players:
        points_map.setdefault(pid, 0)

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

    # Fully reset this server's state
    guild_id = ctx.guild.id
    active_games[guild_id] = {
        "status": False,
        "players": set(),
        "track_pool": [],
        "current_round": 0,
        "round_in_progress": False,
        "round_guesses": {},
        "points": {},
        "round_task": None
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