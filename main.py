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

# In-memory storage for active game data
active_game = {
    "status": False,            # Whether a game has been started
    "players": set(),           # Discord user IDs who joined
    "track_pool": [],           # (track_id, track_name, artist_name, owner_ids) tuples
    "current_round": 0,         # Current round counter
    "round_in_progress": False, # For the timed guessing version
    "round_guesses": {}         # { guesser_discord_id: guessed_discord_id }
}

# Track where each user typed !join, so we can announce them as "ready" in that same channel
join_channels = {}  # { str(discord_user_id): int(channel_id) }

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

            print(f"DEBUG: Received token_info for Discord user {discord_user_id}: {token_info}")
            user_spotify_data[discord_user_id] = token_info
            print(f"DEBUG: user_spotify_data keys are now: {list(user_spotify_data.keys())}")

            # After successful authorization, post in the original channel
            def confirm_authorization():
                print(f"DEBUG: confirm_authorization triggered for user {discord_user_id}")
                # Let's see what channel ID we have stored
                channel_id = join_channels.get(discord_user_id)
                print(f"DEBUG: join_channels keys -> {join_channels.keys()}")
                print(f"DEBUG: channel_id from join_channels is {channel_id} for user {discord_user_id}")

                if channel_id is None:
                    print(f"DEBUG: No stored channel ID for user {discord_user_id}")
                    return

                channel = bot.get_channel(channel_id)
                print(f"DEBUG: get_channel({channel_id}) returned: {channel}")

                if channel is None:
                    print(f"DEBUG: Could not find channel object for channel_id {channel_id}. "
                          f"Make sure the bot has access to that channel, and that it wasn't a DM or thread.")
                    return

                user_obj = bot.get_user(int(discord_user_id))
                print(f"DEBUG: get_user({discord_user_id}) returned: {user_obj}")

                if user_obj is not None:
                    msg = f"{user_obj.mention} is now ready to play!"
                    coro = channel.send(msg)
                    asyncio.run_coroutine_threadsafe(coro, bot.loop)
                else:
                    print(f"DEBUG: Could not find user object for {discord_user_id} to mention.")

            # Schedule this function to run in the bot event loop
            bot.loop.call_soon_threadsafe(confirm_authorization)

            return "Authorization successful! You can close this tab and return to Discord."
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
    active_game["round_in_progress"] = False
    active_game["round_guesses"] = {}

    await ctx.send("A new game has started! Type `!join` to participate.")

@bot.command()
async def join(ctx):
    """
    User joins the game, we remember the channel they typed !join in,
    so we can announce them as "ready" after they finish authorizing.
    """
    if not active_game["status"]:
        await ctx.send("No game is currently running. Use `!start` to create a new game.")
        return

    user_id = str(ctx.author.id)
    if user_id in active_game["players"]:
        await ctx.send("You have already joined the game.")
        return

    # Store the channel ID where they typed !join
    join_channels[user_id] = ctx.channel.id
    active_game["players"].add(user_id)

    await ctx.send(f"{ctx.author.mention}, check your DMs to authorize Spotify.")

    sp_oauth = create_spotify_oauth(state=user_id)
    auth_url = sp_oauth.get_authorize_url()

    try:
        await ctx.author.send(
            "Click the link below to authorize with Spotify:\n" + auth_url
        )
    except discord.Forbidden:
        await ctx.send("I couldn't DM you. Please enable your DMs or add me as a friend.")

@bot.command()
async def play(ctx):
    """
    Fetch each player's recently played tracks, compile them,
    and start a 'guess who' round (timed).
    """
    if not active_game["status"]:
        await ctx.send("No game is active. Use `!start` to begin.")
        return
    if len(active_game["players"]) < 2:
        await ctx.send("Need at least 2 players to play!")
        return

    track_pool = []
    user_track_ids = {}

    for player_id in active_game["players"]:
        sp_client = get_spotify_client(player_id)
        if sp_client is None:
            print(f"DEBUG: No Spotify client for user {player_id}. Possibly not authorized.")
            continue

        # Check which Spotify account this user is on
        try:
            user_info = sp_client.me()
            spotify_user_id = user_info.get("id", "unknown_id")
            spotify_display_name = user_info.get("display_name", "Unknown Display Name")
            print(
                f"DEBUG: Discord user {player_id} is logged into Spotify as ID '{spotify_user_id}', "
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

                print(f"DEBUG: Processing track for user {player_id} -> {track.get('name')} (ID: {track_id})")

                if track_id in user_track_ids[player_id]:
                    continue

                user_track_ids[player_id].add(track_id)

                track_name = track["name"]
                artist_name = track["artists"][0]["name"]

                existing_track = next((t for t in track_pool if t[0] == track_id), None)
                if existing_track:
                    print(f"DEBUG: Merging track '{track_name}' (ID: {track_id})")
                    print(f"       Existing owners: {existing_track[3]}")
                    print(f"       Adding new owner: {player_id}")
                    existing_track[3].add(player_id)
                else:
                    print(f"DEBUG: New track '{track_name}' (ID: {track_id}), owned by {player_id}")
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
    Conduct a single guess round with a 10-second timer.
    """
    if active_game["current_round"] >= len(active_game["track_pool"]):
        await ctx.send("All tracks have been guessed! Game over.")
        active_game["status"] = False
        return

    track_id, track_name, artist_name, owner_ids = active_game["track_pool"][active_game["current_round"]]

    # Prompt the channel
    await ctx.send(
        f"**Guess Round {active_game['current_round']+1}:**\n"
        f"**Track:** {track_name} by {artist_name}\n"
        "You have 10 seconds! Type `!guess @username` to guess."
    )

    # Set round in progress and clear previous guesses
    active_game["round_in_progress"] = True
    active_game["round_guesses"] = {}

    # Wait 10 seconds
    await asyncio.sleep(10)

    # Round ended, evaluate guesses
    active_game["round_in_progress"] = False

    # Build a list of winners
    winners = []
    for guesser_id, guessed_user_id in active_game["round_guesses"].items():
        if guessed_user_id in owner_ids:
            winners.append(guesser_id)

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

    # Move on to the next round
    active_game["current_round"] += 1
    await do_guess_round(ctx)

@bot.command()
async def guess(ctx, user_mention: discord.User = None):
    """
    Each user can guess once per 10-second round.
    We'll record it, then check winners after the timer ends.
    """
    if not active_game["status"]:
        await ctx.send("No active game right now.")
        return

    if not active_game["round_in_progress"]:
        await ctx.send("No guessing period is active right now or time is up!")
        return

    if user_mention is None:
        await ctx.send("Please mention a user to guess. Example: `!guess @SomeUser`")
        return

    guesser_id = str(ctx.author.id)

    if guesser_id in active_game["round_guesses"]:
        await ctx.send("You have already guessed this round!")
        return

    guessed_user_id = str(user_mention.id)
    active_game["round_guesses"][guesser_id] = guessed_user_id
    await ctx.send(f"{ctx.author.mention} your guess has been recorded!")

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
    active_game["round_in_progress"] = False
    active_game["round_guesses"] = {}

    await ctx.send("The game has been ended. All data has been reset.")


# ----- RUN FLASK (SPOTIFY OAUTH) IN A SEPARATE THREAD -----
def run_flask():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

keep_alive()
bot.run(DISCORD_BOT_TOKEN)
