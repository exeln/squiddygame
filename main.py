###########################
# main.py
###########################

import os
import random
import discord
from discord.ext import commands

from flask import Flask, request, redirect, session
from threading import Thread

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Environment variables in Replit secrets:
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# ----- CONFIGURATIONS -----
# Adjust these as needed
BOT_PREFIX = '!'
REDIRECT_URI = "https://<YOUR-REPL-SUBDOMAIN>.<USERNAME>.repl.co/callback"  
# Replace <YOUR-REPL-SUBDOMAIN>.<USERNAME> with your actual Repl domain
SCOPES = "user-read-recently-played"

# In-memory storage for active game data
active_game = {
    "status": False,     # Whether a game has been started
    "players": set(),    # Discord user IDs who joined
    "track_pool": [],    # (track, owner_id) pairs for the current game
    "current_round": 0   # Example round counter
}

# In-memory storage for user Spotify data: { discord_user_id: token_info }
user_spotify_data = {}

# ----- FLASK APP FOR SPOTIFY OAUTH -----
app = Flask(__name__)
# We need a secret key for Flask sessions (use a random, strong key in production):
app.secret_key = "some-random-secret-key"

# Set up the Spotify Oauth
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
    sp_oauth = create_spotify_oauth()
    code = request.args.get("code")
    state = request.args.get("state")  # The Discord user ID we passed in
    error = request.args.get("error")

    if error:
        return f"There was an error during Spotify authorization: {error}", 400

    if code:
        # Exchange code for token
        token_info = sp_oauth.get_access_token(code)
        if token_info:
            # Store token_info in our in-memory dictionary
            discord_user_id = str(state)
            user_spotify_data[discord_user_id] = token_info
            return "Authorization successful! You can close this tab and return to Discord."
        else:
            return "Could not get token info from Spotify.", 400
    else:
        return "No code returned from Spotify.", 400

# Function to refresh token if needed
def get_spotify_client(discord_user_id):
    """
    Returns a Spotipy client for the given Discord user ID, 
    automatically refreshing the token if it's expired.
    """
    token_info = user_spotify_data.get(str(discord_user_id))
    if not token_info:
        return None  # User not authorized

    sp_oauth = create_spotify_oauth()
    
    # Check if token is expired
    if sp_oauth.is_token_expired(token_info):
        token_info = sp_oauth.refresh_access_token(token_info["refresh_token"])
        user_spotify_data[str(discord_user_id)] = token_info

    return spotipy.Spotify(auth=token_info["access_token"])


# ----- DISCORD BOT SETUP -----
intents = discord.Intents.default()
intents.message_content = True  # Make sure to enable this intent in your Discord bot settings
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

    # Add the user to the set of players
    active_game["players"].add(user_id)

    # Create an OAuth object with the state as the Discord user ID
    sp_oauth = create_spotify_oauth(state=user_id)
    auth_url = sp_oauth.get_authorize_url()
    
    # DM or public message with the authorization link:
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

    # Build track pool from each player's recently played
    track_pool = []
    for player_id in active_game["players"]:
        sp_client = get_spotify_client(player_id)
        if sp_client is None:
            # Player not authorized or no token
            continue
        # Fetch recently played tracks
        try:
            results = sp_client.current_user_recently_played(limit=20)
            for item in results["items"]:
                track = item["track"]
                track_id = track["id"]
                track_name = track["name"]
                artist_name = track["artists"][0]["name"]
                
                # We'll store the track as a tuple (track_id, track_name, artist_name, owner_id)
                if track_id not in [t[0] for t in track_pool]:
                    track_pool.append((track_id, track_name, artist_name, player_id))
        except Exception as e:
            print(f"Error fetching recent tracks for user {player_id}: {e}")

    if not track_pool:
        await ctx.send("No tracks found or players not authorized. Make sure everyone has connected their Spotify.")
        return

    # Shuffle the track pool to randomize order
    random.shuffle(track_pool)

    active_game["track_pool"] = track_pool
    active_game["current_round"] = 0

    await ctx.send("Track pool compiled! Starting the game...")

    # Let's show one track to guess as a demo
    await do_guess_round(ctx)


async def do_guess_round(ctx):
    """
    Conduct a single guess round:
    1. Pick the next track from the pool.
    2. Prompt the channel: "Who does this track belong to?"
    3. Wait for guesses.
    """
    if active_game["current_round"] >= len(active_game["track_pool"]):
        # No more tracks
        await ctx.send("All tracks have been guessed! Game over.")
        active_game["status"] = False
        return

    track_info = active_game["track_pool"][active_game["current_round"]]
    track_id, track_name, artist_name, owner_id = track_info

    # Prompt
    await ctx.send(f"**Guess Round {active_game['current_round']+1}:**\n"
                   f"**Track:** {track_name} by {artist_name}\n"
                   "Who does this track belong to? Type `!guess @username`.")

    # Increment round pointer
    active_game["current_round"] += 1


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

    # The track that was just posted
    current_round_index = active_game["current_round"] - 1  # Because we already incremented
    if current_round_index < 0:
        await ctx.send("No track is currently being guessed.")
        return

    track_info = active_game["track_pool"][current_round_index]
    _, track_name, artist_name, owner_id = track_info

    # Check if guess is correct
    if str(user_mention.id) == owner_id:
        await ctx.send(f"Correct! The track '{track_name}' by {artist_name} belongs to {user_mention.mention}!")
    else:
        # Let them know it's incorrect
        actual_owner = await bot.fetch_user(int(owner_id))
        await ctx.send(f"Wrong guess! The correct answer was {actual_owner.mention}.")

    # Proceed to next round (or end)
    if active_game["current_round"] < len(active_game["track_pool"]):
        # Move on to the next track
        await do_guess_round(ctx)
    else:
        # No more tracks
        await ctx.send("All tracks have been used! Game over.")
        active_game["status"] = False


# ----- RUN FLASK (SPOTIFY OAUTH) IN A SEPARATE THREAD -----
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)


def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# Keep the Flask server alive
keep_alive()

# ----- START THE DISCORD BOT -----
bot.run(DISCORD_BOT_TOKEN)
