import os
import random
import discord
from discord.ext import commands

from flask import Flask, request, session
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

# In-memory storage for active game data
active_game = {
    "status": False,     # Whether a game has been started
    "players": set(),    # Discord user IDs who joined
    "track_pool": [],    # (track_id, track_name, artist_name, owner_ids) tuples
    "current_round": 0   # Current round counter
}

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
    """
    Spotify redirects here after user authorizes.
    We exchange the authorization code for an access token.
    The `state` we sent contains the Discord user ID, 
    so we know which user to associate with the token.
    """
    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error:
        return f"There was an error during Spotify authorization: {error}", 400

    if code:
        try:
            # Create a new SpotifyOAuth instance for this specific user
            sp_oauth = create_spotify_oauth(state=state)
            token_info = sp_oauth.get_access_token(code, check_cache=False)
        except Exception as e:
            return f"Error obtaining access token: {e}", 400

        if token_info:
            discord_user_id = str(state)

            # --- Debug prints to avoid accidental overwriting ---
            print(f"DEBUG: Received token_info for Discord user {discord_user_id}: {token_info}")
            user_spotify_data[discord_user_id] = token_info
            print(f"DEBUG: user_spotify_data keys are now: {list(user_spotify_data.keys())}")
            # ---------------------------------------------------

            return (
                "Authorization successful! You can close this tab and return to Discord."
            )
        else:
            return "Could not get token info from Spotify.", 400
    else:
        return "No code returned from Spotify.", 400

def get_spotify_client(discord_user_id):
    """
    Returns a Spotipy client for the given Discord user ID,
    automatically refreshing the token if it's expired.
    """
    token_info = user_spotify_data.get(str(discord_user_id))
    if not token_info:
        return None

    # Create a new OAuth object specifically for this user
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
    """
    Start a new game session. Resets state, etc.
    """
    if active_game["status"]:
        await ctx.send("A game is already in progress.")
        return

    active_game["status"] = True
    active_game["players"] = set()
    active_game["track_pool"] = []
    active_game["current_round"] = 0

    await ctx.send("A new game has started! Type `!join` to participate.")

@bot.command()
async def join(ctx):
    """
    User joins the game. We provide a link to authorize with Spotify.
    """
    if not active_game["status"]:
        await ctx.send("No game is currently running. Use `!start` to create a new game.")
        return

    user_id = str(ctx.author.id)
    if user_id in active_game["players"]:
        await ctx.send("You have already joined the game.")
        return

    active_game["players"].add(user_id)
    
    # Create a new OAuth object with the user's Discord ID as state
    sp_oauth = create_spotify_oauth(state=user_id)
    auth_url = sp_oauth.get_authorize_url()
    
    # DM the authorization link
    await ctx.author.send(
        "Click the link below to authorize with Spotify:\n" + auth_url
    )
    await ctx.send(f"{ctx.author.mention} has joined the game! Check your DMs to authorize Spotify.")

@bot.command()
async def play(ctx):
    """
    Fetch each player's recently played tracks, compile them,
    and start a 'guess who' round.
    """
    if not active_game["status"]:
        await ctx.send("No game is active. Use `!start` to begin.")
        return
    if len(active_game["players"]) < 2:
        await ctx.send("Need at least 2 players to play!")
        return

    track_pool = []
    # We'll keep track of which track IDs have already been added for each user
    user_track_ids = {}

    for player_id in active_game["players"]:
        sp_client = get_spotify_client(player_id)
        if sp_client is None:
            print(f"DEBUG: No Spotify client for user {player_id}. Possibly not authorized.")
            continue

        # FIRST: Check which Spotify account this user is on
        try:
            user_info = sp_client.me()
            spotify_user_id = user_info.get("id", "unknown_id")
            spotify_display_name = user_info.get("display_name", "Unknown Display Name")
            print(
                f"DEBUG: Discord user {player_id} is logged into Spotify as ID '{spotify_user_id}', "
                f"display name: '{spotify_display_name}'"
            )
            print(f"DEBUG: sp_client.auth -> {sp_client.auth}")  # Debug: Print the actual token being used
        except Exception as e:
            print(f"DEBUG: Error calling sp_client.me() for {player_id}: {e}")
            continue

        # Debug: Show we are fetching data for this user
        print(f"DEBUG: Fetching recently played tracks for user {player_id} ...")

        # Keep a set of track IDs we've already added for this user
        user_track_ids[player_id] = set()

        try:
            results = sp_client.current_user_recently_played(limit=20)
            items_count = len(results['items'])
            print(f"DEBUG: Found {items_count} recently played items for user {player_id}.")

            for item in results["items"]:
                track = item["track"]
                track_id = track["id"]

                # skip if there's no valid track ID (local songs, etc.)
                if not track_id:
                    continue

                # Debug: Show track being processed
                print(f"DEBUG: Processing track for user {player_id} -> {track.get('name')} (ID: {track_id})")

                if track_id in user_track_ids[player_id]:
                    # Already added this track for the user
                    continue

                user_track_ids[player_id].add(track_id)

                track_name = track["name"]
                artist_name = track["artists"][0]["name"]

                # Check if this track is already in our global pool
                existing_track = next((t for t in track_pool if t[0] == track_id), None)
                if existing_track:
                    # ── DEBUG PRINT ─────────────────────────────────────────────
                    print(f"DEBUG: Merging track '{track_name}' (ID: {track_id})")
                    print(f"       Existing owners: {existing_track[3]}")
                    print(f"       Adding new owner: {player_id}")
                    # ────────────────────────────────────────────────────────────
                    existing_track[3].add(player_id)
                else:
                    # ── DEBUG PRINT ─────────────────────────────────────────────
                    print(f"DEBUG: New track '{track_name}' (ID: {track_id}), owned by {player_id}")
                    # ────────────────────────────────────────────────────────────
                    track_pool.append((track_id, track_name, artist_name, {player_id}))

        except Exception as e:
            print(f"Error fetching recent tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or players not authorized. Make sure everyone has connected their Spotify.")
        return

    random.shuffle(track_pool)
    active_game["track_pool"] = track_pool
    active_game["current_round"] = 0

    await ctx.send("Track pool compiled! Starting the game...")
    await do_guess_round(ctx)

async def do_guess_round(ctx):
    """
    Conduct a single guess round:
    1. Pick the next track from the pool.
    2. Prompt the channel: "Who does this track belong to?"
    3. Wait for guesses via !guess.
    """
    if active_game["current_round"] >= len(active_game["track_pool"]):
        await ctx.send("All tracks have been guessed! Game over.")
        active_game["status"] = False
        return

    track_info = active_game["track_pool"][active_game["current_round"]]
    _, track_name, artist_name, owner_ids = track_info

    await ctx.send(
        f"**Guess Round {active_game['current_round']+1}:**\n"
        f"**Track:** {track_name} by {artist_name}\n"
        "Who does this track belong to? Type `!guess @username`."
    )

    active_game["current_round"] += 1

@bot.command()
async def end(ctx):
    """
    End the current game and reset all game data.
    """
    if not active_game["status"]:
        await ctx.send("No game is currently running.")
        return

    active_game["status"] = False
    active_game["players"].clear()
    active_game["track_pool"].clear()
    active_game["current_round"] = 0

    await ctx.send("The game has been ended. All data has been reset.")

@bot.command()
async def guess(ctx, user_mention: discord.User = None):
    """
    Player guesses which user the current track belongs to.
    Example usage: !guess @SomeUser
    """
    if not active_game["status"]:
        await ctx.send("No active game right now.")
        return

    if user_mention is None:
        await ctx.send("Please mention a user to guess. Example: `!guess @SomeUser`")
        return

    # The track that was just posted in the current round
    current_round_index = active_game["current_round"] - 1
    if current_round_index < 0:
        await ctx.send("No track is currently being guessed.")
        return

    _, track_name, artist_name, owner_ids = active_game["track_pool"][current_round_index]
    guessed_user_id = str(user_mention.id)

    # ── DEBUG PRINTS (GUESS COMMAND) ─────────────────────────────────────────
    print("DEBUG: ===== GUESS COMMAND TRIGGERED =====")
    print(f"DEBUG: Track name -> {track_name}")
    print(f"DEBUG: Artist -> {artist_name}")
    print(f"DEBUG: Track owner IDs -> {owner_ids}")
    print(f"DEBUG: Guessed user -> {guessed_user_id}")
    print(f"DEBUG: Is guessed_user_id in owner_ids? -> {guessed_user_id in owner_ids}")
    # ─────────────────────────────────────────────────────────────────────────

    # Check if the guessed user is one of the owners
    if guessed_user_id in owner_ids:
        # Only show all owners if there are multiple
        if len(owner_ids) > 1:
            owner_mentions = [f"<@{owner_id}>" for owner_id in owner_ids]
            owners_str = ", ".join(owner_mentions)
            await ctx.send(
                f"Correct! The track '{track_name}' by {artist_name} "
                f"was in multiple users' recently played: {owners_str}!"
            )
        else:
            await ctx.send(
                f"Correct! The track '{track_name}' by {artist_name} "
                f"belongs to {user_mention.mention}!"
            )
    else:
        owner_mentions = [f"<@{owner_id}>" for owner_id in owner_ids]
        owners_str = ", ".join(owner_mentions)
        await ctx.send(f"Wrong guess! The correct answer was {owners_str}.")

    # Move on to the next round, or end if no more tracks
    if active_game["current_round"] < len(active_game["track_pool"]):
        await do_guess_round(ctx)
    else:
        await ctx.send("All tracks have been used! Game over.")
        active_game["status"] = False

# ----- RUN FLASK (SPOTIFY OAUTH) IN A SEPARATE THREAD -----
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

keep_alive()
bot.run(DISCORD_BOT_TOKEN)