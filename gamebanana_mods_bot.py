import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import json
import os
import random

# --- CONFIG ---
COMMAND_PREFIX = '!'
BOT_VERSION = '1.0'
DEFAULT_POST_INTERVAL_HOURS = 6
EMBED_COLOR_GAMEBANANA = discord.Color.from_rgb(255, 221, 51)  # GameBanana yellow
EMBED_COLOR_INFO = discord.Color.blue()
EMBED_COLOR_WARNING = discord.Color.orange()
EMBED_COLOR_SUCCESS = discord.Color.green()

CONFIG_FILE = 'gbbot_server_configs.json'

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
bot.http_session = None

# --- Server Config Management ---
def load_server_configs():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_server_configs():
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(server_configs, f, indent=2)

server_configs = load_server_configs()

# --- Helper: Get/Set Guild Config ---
def get_guild_config(guild_id):
    gid = str(guild_id)
    if gid not in server_configs:
        server_configs[gid] = {
            'auto_post_channel_id': None,
            'auto_post_enabled': False,
            'auto_post_interval': DEFAULT_POST_INTERVAL_HOURS,
            'posted_urls_cache': {},
        }
    return server_configs[gid]

# --- Helper: Create Embed ---
def create_gb_embed(title, description='', color=EMBED_COLOR_GAMEBANANA, instructions=None, mod_image=None):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if instructions:
        embed.add_field(name='Instructions', value=instructions, inline=False)
    if mod_image:
        embed.set_thumbnail(url=mod_image)
    embed.set_footer(text=f"GameBanana Bot v{BOT_VERSION} | {COMMAND_PREFIX}help")
    return embed

# --- GameBanana Scraping ---
async def fetch_gamebanana_mods_scrape(session, game_id, count=3, sort_criteria='new', keyword=None):
    sort_param_map = {'new': 'new', 'popular_alltime': 'DLPDA', 'likes_alltime': 'Likes'}
    sort_param = sort_param_map.get(sort_criteria, 'new')
    url = f"https://gamebanana.com/games/{game_id}?_sSort={sort_param}"
    headers = {"User-Agent": "Mozilla/5.0"}
    mods = []
    try:
        async with session.get(url, headers=headers, timeout=15) as response:
            if response.status == 200:
                html_content = await response.text(encoding='utf-8', errors='ignore')
                soup = BeautifulSoup(html_content, 'html.parser')
                mod_cards = soup.select('div.RecordList article.BananaCard')
                if not mod_cards:
                    mod_cards = soup.select('ul#ModIndexList > li.SubcategoryEntry')
                found_count = 0
                for card in mod_cards:
                    if found_count >= count * 3:  # Fetch more for keyword filtering
                        break
                    title_tag = card.select_one('a.Name')
                    if not title_tag:
                        continue
                    mod_name = title_tag.text.strip()
                    mod_link = title_tag['href']
                    if not mod_link.startswith('http'):
                        mod_link = 'https://gamebanana.com' + mod_link
                    uploader = 'Unknown'
                    uploader_tag = card.select_one('span.Contributors span.Username a')
                    if not uploader_tag:
                        uploader_tag = card.select_one('span.Username a')
                    if not uploader_tag:
                        uploader_tag = card.select_one('a[href*="/members/"]')
                    if uploader_tag:
                        uploader = uploader_tag.text.strip()
                    image_url = None
                    img_tag = card.select_one('img')
                    if img_tag and img_tag.get('src'):
                        image_url = img_tag['src']
                    summary = 'View on GameBanana for details.'
                    if keyword:
                        if keyword.lower() not in mod_name.lower():
                            continue
                    mods.append({
                        'name': mod_name,
                        'url': mod_link,
                        'uploader': uploader,
                        'summary': summary,
                        'image_url': image_url
                    })
                    found_count += 1
                    if len(mods) >= count:
                        break
    except Exception as e:
        print(f"GameBanana: Exception fetching mods: {e}")
    return mods[:count]

# --- Scheduled Posting Task ---
@tasks.loop(hours=DEFAULT_POST_INTERVAL_HOURS)
async def scheduled_mod_poster():
    if not bot.http_session or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()
    for guild_id, config in server_configs.items():
        if not config.get('auto_post_enabled') or not config.get('auto_post_channel_id'):
            continue
        channel = bot.get_channel(int(config['auto_post_channel_id']))
        if not channel or not isinstance(channel, discord.TextChannel):
            continue
        # For demo, use a default game (e.g., 6460 = "Friday Night Funkin'")
        game_id = '6460'
        mods_new = await fetch_gamebanana_mods_scrape(bot.http_session, game_id, count=3, sort_criteria='new')
        mods_top = await fetch_gamebanana_mods_scrape(bot.http_session, game_id, count=2, sort_criteria='popular_alltime')
        all_mods = mods_new + mods_top
        posted_urls = config['posted_urls_cache'].setdefault(game_id, [])
        new_mods = [m for m in all_mods if m['url'] not in posted_urls]
        if not new_mods:
            continue
        for mod in new_mods:
            embed = create_gb_embed(
                title=f"{mod['name']} (by {mod['uploader']})",
                description=f"[Direct Download & Details]({mod['url']})\n*{mod['summary']}*",
                color=EMBED_COLOR_GAMEBANANA,
                instructions="To install: Download from the link above and follow instructions on the mod page.",
                mod_image=mod.get('image_url')
            )
            try:
                await channel.send(embed=embed)
                posted_urls.append(mod['url'])
                config['posted_urls_cache'][game_id] = posted_urls[-50:]
            except Exception as e:
                print(f"Failed to send mod embed: {e}")
    save_server_configs()

# --- Bot Events ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if not bot.http_session or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()
    if not scheduled_mod_poster.is_running():
        scheduled_mod_poster.change_interval(hours=DEFAULT_POST_INTERVAL_HOURS)
        scheduled_mod_poster.start()

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if bot.user in message.mentions:
        await message.channel.send(f"Hi {message.author.mention}! I'm your GameBanana mod bot. Use `{COMMAND_PREFIX}help` for commands or `{COMMAND_PREFIX}gbsearch <gameid> <keyword>` to search mods!")
    await bot.process_commands(message)

# --- Admin Commands ---
@commands.has_permissions(manage_guild=True)
@bot.command(name='gbsetchannel', help='Set the channel for GameBanana mod posts.')
async def gbsetchannel(ctx, channel: discord.TextChannel):
    config = get_guild_config(ctx.guild.id)
    config['auto_post_channel_id'] = str(channel.id)
    config['auto_post_enabled'] = True
    save_server_configs()
    await ctx.send(f"GameBanana mod posts will now be sent to {channel.mention} every {config.get('auto_post_interval', DEFAULT_POST_INTERVAL_HOURS)} hours.")

@commands.has_permissions(manage_guild=True)
@bot.command(name='gbsetinterval', help='Set the posting interval in hours (min 2, max 24).')
async def gbsetinterval(ctx, hours: int):
    if not (2 <= hours <= 24):
        await ctx.send("Interval must be between 2 and 24 hours.")
        return
    config = get_guild_config(ctx.guild.id)
    config['auto_post_interval'] = hours
    scheduled_mod_poster.change_interval(hours=hours)
    save_server_configs()
    await ctx.send(f"GameBanana mod posting interval set to {hours} hours.")

# --- User Command: Search Mods ---
@bot.command(name='gbsearch', help='Search GameBanana mods by game ID and keyword. Usage: !gbsearch <gameid> <keyword> [count]')
async def gbsearch(ctx, gameid: str = None, keyword: str = None, count: int = 3):
    if not gameid or not keyword:
        await ctx.send(f"Usage: `{COMMAND_PREFIX}gbsearch <gameid> <keyword> [count]`")
        return
    if not bot.http_session or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()
    mods = await fetch_gamebanana_mods_scrape(bot.http_session, gameid, count=count, sort_criteria='new', keyword=keyword)
    if not mods:
        await ctx.send(f"No mods found for game `{gameid}` with keyword `{keyword}`.")
        return
    for mod in mods:
        embed = create_gb_embed(
            title=f"{mod['name']} (by {mod['uploader']})",
            description=f"[Direct Download & Details]({mod['url']})\n*{mod['summary']}*",
            color=EMBED_COLOR_GAMEBANANA,
            instructions="To install: Download from the link above and follow instructions on the mod page.",
            mod_image=mod.get('image_url')
        )
        await ctx.send(embed=embed)

# --- Run Bot ---
if __name__ == '__main__':
    import sys
    TOKEN = os.getenv('DISCORD_TOKEN') or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not TOKEN:
        print('Please provide your Discord bot token as DISCORD_TOKEN env or argument.')
        exit(1)
    bot.run(TOKEN) 