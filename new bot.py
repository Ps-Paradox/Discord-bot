import discord
from discord.ext import commands, tasks
import os
import json
import asyncio
import datetime
import re # For birthday date validation

TOKEN = os.getenv('DISCORD_BOT_TOKEN') 
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY') # New environment variable for Gemini

if TOKEN is None:
    print("Error: DISCORD_BOT_TOKEN environment variable not set.")
    print("Please set the environment variable or replace os.getenv() with your token (NOT RECOMMENDED for production).")
    # For quick local testing ONLY, uncomment and replace:
    # TOKEN = "YOUR_PASTE_YOUR_BOT_TOKEN_HERE" 

if GEMINI_API_KEY is None:
    print("Warning: GEMINI_API_KEY environment variable not set. AI features will use fallback/placeholder behaviors.")

# Persistent storage file paths (These will be ephemeral on Render's free tier without a mounted disk)
CONFIG_FILE = 'bot_config.json'
LEADERBOARD_FILE = 'leaderboard.json' # Stores XP and quiz scores
CHAT_LOG_FILE = 'chat_log.txt'
BIRTHDAYS_FILE = 'birthdays.json'

# --- Bot Setup ---
# Define Discord Intents: Crucial for your bot to receive certain events.
intents = discord.Intents.default()
intents.message_content = True # Required to read message content for chat/commands
intents.members = True       # Required for fetching member info, welcome/farewell, roles
intents.presences = True     # Useful for fetching full user/member objects

bot = commands.Bot(command_prefix='!', intents=intents)

# --- Data Storage (in-memory, loaded from/saved to files) ---
bot_config = {
    "response_channels": {},    # guild_id: channel_id for general chat responses
    "quiz_channels": {},        # guild_id: channel_id for quiz events
    "birthday_channels": {},    # guild_id: channel_id for birthday wishes
    "birthday_roles": {},       # guild_id: role_id for auto-assigned birthday role
    "welcome_channels": {},     # guild_id: channel_id for welcome messages
    "farewell_channels": {},    # guild_id: channel_id for farewell messages
    "welcome_messages": {},     # guild_id: message template for welcome
    "farewell_messages": {},    # guild_id: message template for farewell
    "rankup_channels": {}       # guild_id: channel_id for level-up announcements
}
# Stores user XP and quiz scores. {user_id: {"xp": int, "quiz_score": int}}
leaderboard_data = {} 
# Stores user birthdays globally (user_id: "MM-DD")
birthdays_data = {} 

# --- Helper Functions for Persistence ---
def load_data():
    """Loads configuration, leaderboard, and birthday data from JSON files."""
    global bot_config, leaderboard_data, birthdays_data
    
    # List of (file_path, data_dict, default_structure)
    data_files = [
        (CONFIG_FILE, bot_config, {"response_channels": {}, "quiz_channels": {}, "birthday_channels": {}, "birthday_roles": {}, "welcome_channels": {}, "farewell_channels": {}, "welcome_messages": {}, "farewell_messages": {}, "rankup_channels": {}}),
        (LEADERBOARD_FILE, leaderboard_data, {}),
        (BIRTHDAYS_FILE, birthdays_data, {})
    ]

    for file_path, data_dict, default_structure in data_files:
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r') as f:
                    loaded_data = json.load(f)
                    # For bot_config, ensure new keys are added if file is older version
                    if file_path == CONFIG_FILE:
                        for key, default_value in default_structure.items():
                            if key not in loaded_data:
                                loaded_data[key] = default_value
                    data_dict.update(loaded_data)
                print(f"Loaded {file_path}: {data_dict}")
            except json.JSONDecodeError:
                print(f"Error decoding {file_path}. Starting with empty data.")
                data_dict.clear() # Clear existing, then update with default
                data_dict.update(default_structure)
        else:
            print(f"File '{file_path}' not found, creating new.")
            with open(file_path, 'w') as f:
                json.dump(default_structure, f, indent=4)
            data_dict.update(default_structure) # Ensure in-memory also reflects defaults

def save_data():
    """Saves configuration, leaderboard, and birthday data to JSON files."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(bot_config, f, indent=4)
        with open(LEADERBOARD_FILE, 'w') as f:
            json.dump(leaderboard_data, f, indent=4)
        with open(BIRTHDAYS_FILE, 'w') as f:
            json.dump(birthdays_data, f, indent=4)
        print("Data saved successfully.")
    except Exception as e:
        print(f"Error saving data: {e}")

# --- Bot Events ---
@bot.event
async def on_ready():
    """Event that fires when the bot successfully connects to Discord."""
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('------')
    load_data() # Load persistent data when the bot starts
    
    # Sync slash commands:
    # A global sync can take up to an hour to propagate.
    # For faster testing, you can sync to a specific guild:
    # guild_id_for_testing = 123456789012345678 # Replace with your test guild ID
    # guild = discord.Object(id=guild_id_for_testing)
    # bot.tree.copy_global_commands(guild=guild)
    # await bot.tree.sync(guild=guild)
    await bot.tree.sync() 
    print('Slash commands synced.')

    # Start background tasks
    check_birthdays_daily.start()
    print('Birthday checker task started.')

@bot.event
async def on_message(message):
    """Event that fires when a message is sent in any channel the bot can see."""
    # Ignore messages from the bot itself to prevent infinite loops.
    if message.author == bot.user:
        return

    # --- Chat Logging ---
    log_entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "guild_id": str(message.guild.id) if message.guild else "DM",
        "channel_id": str(message.channel.id),
        "content_length": len(message.content),
        "content_sample": message.content[:100] + "..." if len(message.content) > 100 else message.content, 
    }
    try:
        with open(CHAT_LOG_FILE, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception as e:
        print(f"Error writing to chat log: {e}")

    # --- XP System (for general messages) ---
    if message.guild:
        user_id_str = str(message.author.id)
        if user_id_str not in leaderboard_data:
            leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
        
        old_xp = leaderboard_data[user_id_str]["xp"]
        old_level = old_xp // 100 + 1 # Assuming 100 XP per level

        # Give XP for every message in the designated response channel
        guild_id_str = str(message.guild.id)
        response_channel_id = bot_config["response_channels"].get(guild_id_str)
        if response_channel_id and message.channel.id == int(response_channel_id):
            leaderboard_data[user_id_str]["xp"] += 1 # 1 XP per message
            save_data() # Save after XP update

            new_xp = leaderboard_data[user_id_str]["xp"]
            new_level = new_xp // 100 + 1

            if new_level > old_level:
                await send_levelup_message(message.author, old_level, new_level, message.guild)
    
    # --- AI Chat Interface & Contextual Image Generation (Mee6-like chat feature) ---
    guild_id_str = str(message.guild.id) if message.guild else None
    response_channel_id = bot_config["response_channels"].get(guild_id_str)

    is_in_designated_channel = (message.guild is None) or \
                               (response_channel_id and message.channel.id == int(response_channel_id))

    if not message.content.startswith('!') and not message.content.startswith('/') and is_in_designated_channel:
        lower_msg = message.content.lower()
        response_text = None
        image_prompt = None

        # Specific keyword-based AI chat responses (prioritized)
        if "hello bot" in lower_msg or "hi bot" in lower_msg or "hey bot" in lower_msg:
            response_text = f"Hello {message.author.display_name}! How can I assist you today?"
        elif "how are you" in lower_msg:
            response_text = "I'm a bot, so I don't have feelings, but I'm ready to help you!"
        elif "tell me about anime" in lower_msg:
            response_text = "Anime is a diverse and fascinating world of Japanese animation! Do you have a favorite genre or series?"
        elif "thank you" in lower_msg or "thanks bot" in lower_msg:
            response_text = "You're welcome! Happy to help."
        
        # Contextual image generation trigger (prioritized)
        if any(trigger in lower_msg for trigger in ["draw a", "generate an image of", "show me a picture of"]):
            if "draw a " in lower_msg:
                image_prompt = lower_msg.split("draw a ", 1)[1].strip()
            elif "generate an image of " in lower_msg:
                image_prompt = lower_msg.split("generate an image of ", 1)[1].strip()
            elif "show me a picture of " in lower_msg:
                image_prompt = lower_msg.split("show me a picture of ", 1)[1].strip()
            
            if image_prompt:
                response_text = f"Alright, generating an image of: **{image_prompt}** for you using AI..."
            else:
                response_text = "What would you like me to draw? Try 'draw a [something]' or 'generate an image of [something]'."

        if response_text:
            await message.channel.send(response_text)
        
        if image_prompt: # If an image was triggered, send it
            try:
                async with message.channel.typing():
                    generated_image_url = await generate_image_with_gemini(image_prompt)
                
                embed = discord.Embed(
                    title="AI Generated Image from Chat",
                    description=f"Prompt: `{image_prompt}`",
                    color=discord.Color.green()
                )
                embed.set_image(url=generated_image_url)
                await message.channel.send(embed=embed)
            except Exception as e:
                await message.channel.send(f"Sorry, I couldn't generate the image: {e}")
        elif not response_text: # If no specific keyword response or image trigger, use general AI chat
            try:
                async with message.channel.typing():
                    ai_chat_response = await get_ai_chat_response(message.content)
                await message.channel.send(ai_chat_response)
            except Exception as e:
                print(f"Error during general AI chat response: {e}")
                await message.channel.send("I'm having a bit of trouble understanding right now. Please try again later!")


    await bot.process_commands(message)

@bot.event
async def on_member_join(member):
    """Sends a welcome message when a new member joins."""
    guild_id_str = str(member.guild.id)
    welcome_channel_id = bot_config["welcome_channels"].get(guild_id_str)
    welcome_message_template = bot_config["welcome_messages"].get(guild_id_str)

    if welcome_channel_id and welcome_message_template:
        channel = member.guild.get_channel(int(welcome_channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            message = welcome_message_template.format(
                user_mention=member.mention,
                user_name=member.display_name,
                server_name=member.guild.name
            )
            try:
                await channel.send(message)
            except discord.Forbidden:
                print(f"Error: Missing permissions to send welcome message in '{channel.name}' in guild '{member.guild.name}'.")
            except Exception as e:
                print(f"Error sending welcome message for {member.display_name}: {e}")

@bot.event
async def on_member_remove(member):
    """Sends a farewell message when a member leaves."""
    guild_id_str = str(member.guild.id)
    farewell_channel_id = bot_config["farewell_channels"].get(guild_id_str)
    farewell_message_template = bot_config["farewell_messages"].get(guild_id_str)

    if farewell_channel_id and farewell_message_template:
        channel = member.guild.get_channel(int(farewell_channel_id))
        if channel and isinstance(channel, discord.TextChannel):
            message = farewell_message_template.format(
                user_name=member.display_name,
                server_name=member.guild.name
            )
            try:
                await channel.send(message)
            except discord.Forbidden:
                print(f"Error: Missing permissions to send farewell message in '{channel.name}' in guild '{member.guild.name}'.")
            except Exception as e:
                print(f"Error sending farewell message for {member.display_name}: {e}")

# --- Admin-only Channel & Message Configuration Commands (Slash Commands) ---

@bot.tree.command(name="set_response_channel", description="Sets the channel where the bot will respond to general chat.")
@commands.has_permissions(manage_channels=True)
async def set_response_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["response_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"Bot will now respond in {channel.mention} for general queries.", ephemeral=True)

@set_response_channel.error
async def set_response_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_quiz_channel", description="Sets the channel where anime quiz events will be hosted.")
@commands.has_permissions(manage_channels=True)
async def set_quiz_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["quiz_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"Anime quiz events will now be hosted in {channel.mention}.", ephemeral=True)

@set_quiz_channel.error
async def set_quiz_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_birthday_channel", description="Admin: Sets the channel for automatic birthday wishes.")
@commands.has_permissions(manage_channels=True)
async def set_birthday_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["birthday_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"Birthday wishes will now be sent in {channel.mention}.", ephemeral=True)

@set_birthday_channel.error
async def set_birthday_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_birthday_role", description="Admin: Sets the role to be given to users on their birthday.")
@commands.has_permissions(manage_roles=True)
async def set_birthday_role(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["birthday_roles"][str(interaction.guild.id)] = str(role.id); save_data()
    await interaction.response.send_message(f"The role {role.mention} will now be given to users on their birthday.", ephemeral=True)

@set_birthday_role.error
async def set_birthday_role_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Roles' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_welcome_channel", description="Admin: Sets the channel for new member welcome messages.")
@commands.has_permissions(manage_channels=True)
async def set_welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["welcome_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"New member welcome messages will now be sent in {channel.mention}.", ephemeral=True)

@set_welcome_channel.error
async def set_welcome_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_welcome_message", description="Admin: Sets the custom welcome message. Use {user.mention}, {user.name}, {server.name}.")
@commands.has_permissions(manage_guild=True)
async def set_welcome_message(interaction: discord.Interaction, message_template: str):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["welcome_messages"][str(interaction.guild.id)] = message_template; save_data()
    await interaction.response.send_message(f"Welcome message set to: `{message_template}`", ephemeral=True)

@set_welcome_message.error
async def set_welcome_message_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Server' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_farewell_channel", description="Admin: Sets the channel for member farewell messages.")
@commands.has_permissions(manage_channels=True)
async def set_farewell_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["farewell_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"Member farewell messages will now be sent in {channel.mention}.", ephemeral=True)

@set_farewell_channel.error
async def set_farewell_channel_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="set_farewell_message", description="Admin: Sets the custom farewell message. Use {user.name}, {server.name}.")
@commands.has_permissions(manage_guild=True)
async def set_farewell_message(interaction: discord.Interaction, message_template: str):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["farewell_messages"][str(interaction.guild.id)] = message_template; save_data()
    await interaction.response.send_message(f"Farewell message set to: `{message_template}`", ephemeral=True)

@set_farewell_message.error
async def set_farewell_message_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Server' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

@bot.tree.command(name="rank_settings", description="Admin: Sets the channel for level-up announcements.")
@commands.has_permissions(manage_channels=True)
async def rank_settings(interaction: discord.Interaction, channel: discord.TextChannel):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    bot_config["rankup_channels"][str(interaction.guild.id)] = str(channel.id); save_data()
    await interaction.response.send_message(f"Level-up announcements will now be sent in {channel.mention}.", ephemeral=True)

@rank_settings.error
async def rank_settings_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Channels' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)

# --- General Utility Commands (Slash Commands) ---

@bot.tree.command(name="ping", description="Checks bot latency.")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f'Pong! {round(bot.latency * 1000)}ms', ephemeral=True)
    # XP for using a simple command
    user_id_str = str(interaction.user.id)
    if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
    leaderboard_data[user_id_str]["xp"] += 3 
    save_data()


@bot.tree.command(name="mention", description="Mentions a user in the current channel.")
async def mention_user(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_message(f"{user.mention} you have been mentioned by {interaction.user.display_name}!")
    # XP for using a command
    user_id_str = str(interaction.user.id)
    if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
    leaderboard_data[user_id_str]["xp"] += 5 
    save_data()

# --- AI Image Generation (with Gemini Placeholder) ---
async def generate_image_with_gemini(prompt: str) -> str:
    """
    Placeholder for AI image generation using Google Gemini API or other image AI.
    In a real bot, you would integrate with `google-generativeai` library or another image AI.
    Example with `google-generativeai` (conceptual, as direct image generation might require more):
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-pro-vision') # Or a model capable of image generation
    # response = await model.generate_content(prompt)
    # return response.images[0].url # if a direct image URL is returned

    For this demo, it returns a placeholder image URL.
    """
    print(f"DEBUG (Gemini): Attempting to generate image for prompt: '{prompt}'")
    await asyncio.sleep(3) # Simulate API call delay
    
    # Simulate different images for different prompts for better demonstration
    if "cat" in prompt.lower():
        return "https://via.placeholder.com/500x300.png?text=AI+Cat+Art"
    elif "dog" in prompt.lower():
        return "https://via.placeholder.com/500x300.png?text=AI+Dog+Art"
    elif "anime character" in prompt.lower() or "anime girl" in prompt.lower() or "anime boy" in prompt.lower():
        return "https://via.placeholder.com/500x300.png?text=AI+Anime+Character"
    elif "futuristic city" in prompt.lower():
        return "https://via.placeholder.com/500x300.png?text=AI+Futuristic+City"
    elif "level up" in prompt.lower() or "rank up" in prompt.lower() or "celebration" in prompt.lower():
        return "https://via.placeholder.com/500x300.png?text=Level+Up+Celebration"
    return "https://via.placeholder.com/500x300.png?text=AI+Generated+Image"

async def get_ai_chat_response(message_content: str) -> str:
    """
    Placeholder for general AI chat response using Google Gemini API.
    In a real bot, you would integrate with `google-generativeai` library here.
    Example:
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-pro')
    try:
        response = await model.generate_content(message_content)
        return response.text
    except Exception as e:
        print(f"Gemini chat error: {e}")
        return "I'm having a bit of a brain glitch right now, please try again."
    """
    print(f"DEBUG (Gemini Chat): Responding to: '{message_content}'")
    await asyncio.sleep(1.5) # Simulate API call delay
    
    # Simple placeholder for general AI chat responses
    lower_msg = message_content.lower()
    if "favorite anime" in lower_msg:
        return "As an AI, I don't have favorites, but I find the storytelling in 'Fullmetal Alchemist: Brotherhood' quite remarkable!"
    elif "weather" in lower_msg:
        return "I can't check the weather, but I hope it's sunny wherever you are!"
    elif "tell me a joke" in lower_msg:
        return "Why don't scientists trust atoms? Because they make up everything!"
    else:
        return "That's an interesting thought! What else would you like to talk about?"


@bot.tree.command(name="generate_image", description="Generates an image based on a text prompt using AI (Gemini).")
async def generate_image_command(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)
    try:
        image_url = await generate_image_with_gemini(prompt)
        embed = discord.Embed(
            title="Generated Image",
            description=f"**Prompt:** `{prompt}`",
            color=discord.Color.blue()
        )
        embed.set_image(url=image_url)
        await interaction.followup.send(embed=embed)
        # XP for using a complex command
        user_id_str = str(interaction.user.id)
        if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
        leaderboard_data[user_id_str]["xp"] += 10 
        save_data()
    except Exception as e:
        await interaction.followup.send(f"An error occurred during image generation: {e}")

# --- Anime Quiz Event Implementation (Enhanced with Buttons and AI Quiz Generation Placeholder) ---
quiz_active_guilds = {} 

# Custom View for Quiz Buttons
class QuizAnswerView(discord.ui.View):
    def __init__(self, correct_answer, quiz_state, channel_id, timeout=30):
        super().__init__(timeout=timeout)
        self.correct_answer = correct_answer.lower().strip()
        self.quiz_state = quiz_state
        self.channel_id = channel_id
        self.answered = False 

        # Generate a list of options (one correct, others incorrect)
        # In a real AI quiz, the LLM would provide multiple plausible incorrect options.
        all_options = [correct_answer] 
        # Add some dummy incorrect options for demonstration
        dummy_incorrect_options = [
            "Wrong Answer A", "Incorrect Choice B", "Not This One C", "False Option D"
        ]
        # Make sure not to accidentally add the correct answer as a dummy incorrect one
        dummy_incorrect_options = [opt for opt in dummy_incorrect_options if opt.lower() != self.correct_answer]

        # Combine, shuffle, and take up to 4 options (including the correct one)
        import random
        random.shuffle(dummy_incorrect_options)
        options_to_display = all_options + dummy_incorrect_options[:3]
        random.shuffle(options_to_display)

        for option in options_to_display:
            self.add_item(discord.ui.Button(label=option, style=discord.ButtonStyle.primary, custom_id=f"quiz_option_{option.replace(' ', '_').replace('.', '').lower()}")) # Sanitize custom_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.channel.id != self.channel_id:
            await interaction.response.send_message("Please answer in the quiz channel!", ephemeral=True)
            return False
        
        if self.answered: 
            await interaction.response.send_message("This question has already been answered!", ephemeral=True)
            return False

        if not interaction.custom_id.startswith("quiz_option_"):
            return False 

        selected_answer_raw = interaction.custom_id.replace("quiz_option_", "").replace('_', ' ') # Revert sanitization
        selected_answer = selected_answer_raw.lower().strip()

        if selected_answer == self.correct_answer:
            self.answered = True
            await interaction.response.send_message(f"🎉 Correct! {interaction.user.mention} got it right! The answer was: **{self.correct_answer.title()}**")
            
            user_id = str(interaction.user.id)
            if user_id not in self.quiz_state.scores:
                self.quiz_state.scores[user_id] = 0
            self.quiz_state.scores[user_id] += 1

            # Update global leaderboard (persistent data)
            if user_id not in leaderboard_data: leaderboard_data[user_id] = {"xp": 0, "quiz_score": 0}
            leaderboard_data[user_id]["quiz_score"] += 1 # 1 point to quiz score
            leaderboard_data[user_id]["xp"] += 20 # 20 XP for correct answer
            save_data()

            for item in self.children: # Disable all buttons after correct answer
                item.disabled = True
            await self.message.edit(view=self) # Update the message with disabled buttons
            
            self.stop() 
            return True 
        else:
            await interaction.response.send_message("❌ Incorrect answer! Try again.", ephemeral=True)
            return False

    async def on_timeout(self):
        if not self.answered:
            await self.message.channel.send(f"⏱️ Time's up! No one got it this time. The answer was: **{self.correct_answer.title()}**")
        for item in self.children:
            item.disabled = True
        await self.message.edit(view=self)


class QuizState:
    """Manages the state of an ongoing anime quiz in a guild."""
    def __init__(self, guild_id, channel_id, questions):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.questions = questions
        self.current_question_index = 0
        self.scores = {} # user_id: score for current quiz
        self.is_running = False
        self.message_to_delete = None 

    async def start_quiz(self, channel: discord.TextChannel):
        self.is_running = True
        self.current_question_index = 0
        self.scores = {}
        await channel.send("🎉 **Anime Quiz starting soon! Get ready!** 🎉")
        await asyncio.sleep(3)
        await self.send_next_question(channel)

    async def send_next_question(self, channel: discord.TextChannel):
        if self.current_question_index >= len(self.questions):
            await self.end_quiz(channel)
            return

        if self.message_to_delete:
            try: await self.message_to_delete.delete()
            except discord.NotFound: pass 

        question_data = self.questions[self.current_question_index]
        question_text = f"**Question {self.current_question_index + 1}/{len(self.questions)}:** {question_data['question']}"
        image_url = question_data.get('image_url')

        embed = discord.Embed(
            title="❓ Anime Quiz Time! ❓",
            description=question_text,
            color=discord.Color.purple()
        )
        if image_url: embed.set_image(url=image_url)
        embed.set_footer(text="Choose your answer below! You have 30 seconds!")

        view = QuizAnswerView(question_data['answer'], self, channel.id)
        self.message_to_delete = await channel.send(embed=embed, view=view)

        await view.wait() # Wait for the quiz question to be answered or timeout

        await asyncio.sleep(5) 
        self.current_question_index += 1
        await self.send_next_question(channel)

    async def end_quiz(self, channel: discord.TextChannel):
        self.is_running = False
        quiz_active_guilds.pop(str(self.guild_id), None) 

        sorted_scores = sorted(self.scores.items(), key=lambda item: item[1], reverse=True)
        results_message = "🏆 **Quiz Over! Final Scores:** 🏆\n"
        if not sorted_scores:
            results_message += "No one scored points this round!"
        else:
            for user_id, score in sorted_scores:
                member = channel.guild.get_member(int(user_id))
                if member: results_message += f"{member.display_name}: {score} points\n"
                else: results_message += f"User ({user_id}): {score} points\n" 

        await channel.send(results_message)
        if self.message_to_delete:
            try: await self.message_to_delete.delete()
            except discord.NotFound: pass 

# Placeholder for AI Quiz Generation (using Gemini to generate questions and images)
async def generate_ai_quiz_questions(topic: str, num_questions: int) -> list:
    """
    Simulates using Gemini to generate unique anime quiz questions and images.
    """
    print(f"DEBUG: Generating {num_questions} AI quiz questions for topic: '{topic}'")
    await asyncio.sleep(5) # Simulate AI processing time

    dynamic_questions = []
    base_questions = [
        {"question": "Who is this iconic character?", "answer": "Goku", "image_prompt": "super saiyan goku, epic pose, battle aura"},
        {"question": "From which anime is this scene?", "answer": "Spirited Away", "image_prompt": "spirited away bathhouse night, fantasy anime"},
        {"question": "Which anime features the power of Nen?", "answer": "Hunter x Hunter", "image_prompt": "hunter x hunter logo, nen ability visual"},
        {"question": "What is the name of the main character's guardian spirit in Shaman King?", "answer": "Amidamaru", "image_prompt": "amidamaru spirit, shaman king anime"},
        {"question": "In Fullmetal Alchemist, what is the Law of Equivalent Exchange?", "answer": "You must give something of equal value to obtain something.", "image_prompt": "fullmetal alchemist symbols, alchemy circle, edward elric"},
        {"question": "Who is known as the 'Hero Killer' in My Hero Academia?", "answer": "Stain", "image_prompt": "hero killer stain, my hero academia, dark atmosphere"},
        {"question": "What is the name of the hidden village Naruto belongs to?", "answer": "Hidden Leaf Village", "image_prompt": "naruto hidden leaf village gate, konoha village"},
        {"question": "Which anime focuses on competitive card games with powerful monsters?", "answer": "Yu-Gi-Oh!", "image_prompt": "yugioh dark magician, card game duel, anime battle"}
    ]
    import random
    random.shuffle(base_questions)

    for i in range(min(num_questions, len(base_questions))):
        q = base_questions[i]
        try:
            image_url = await generate_image_with_gemini(q["image_prompt"])
            dynamic_questions.append({
                "question": q["question"],
                "answer": q["answer"],
                "image_url": image_url
            })
        except Exception as e:
            print(f"Error generating image for quiz: {e}")
            dynamic_questions.append({
                "question": q["question"],
                "answer": q["answer"],
                "image_url": "https://via.placeholder.com/500x300.png?text=Image+Error"
            })

    return dynamic_questions

@bot.tree.command(name="animequiz", description="Starts a pre-defined anime quiz event.")
async def anime_quiz_event(interaction: discord.Interaction):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    guild_id = str(interaction.guild.id); quiz_channel = interaction.guild.get_channel(int(bot_config["quiz_channels"].get(guild_id))) if bot_config["quiz_channels"].get(guild_id) else None
    if not quiz_channel: await interaction.response.send_message("Please set a quiz channel first using `/set_quiz_channel`.", ephemeral=True); return
    if guild_id in quiz_active_guilds and quiz_active_guilds[guild_id].is_running: await interaction.response.send_message("A quiz is already running! Wait for it to finish.", ephemeral=True); return

    await interaction.response.defer(thinking=True) 

    standard_questions = [
        {"question": "Who is the protagonist of 'Attack on Titan'?", "answer": "Eren Yeager", "image_url": "https://via.placeholder.com/500x300.png?text=Attack+on+Titan"},
        {"question": "What is the name of the main character in 'One-Punch Man'?", "answer": "Saitama", "image_url": "https://via.placeholder.com/500x300.png?text=One-Punch+Man"},
        {"question": "Which anime features a notebook that can kill anyone whose name is written in it?", "answer": "Death Note", "image_url": "https://via.placeholder.com/500x300.png?text=Death+Note"}
    ]
    quiz_state = QuizState(guild_id, quiz_channel.id, standard_questions)
    quiz_active_guilds[guild_id] = quiz_state
    await interaction.followup.send(f"Starting a standard anime quiz in {quiz_channel.mention}!", ephemeral=True)
    await quiz_state.start_quiz(quiz_channel)
    
    user_id_str = str(interaction.user.id)
    if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
    leaderboard_data[user_id_str]["xp"] += 10 
    save_data()

@bot.tree.command(name="animequiz_generate", description="Admin: Starts an AI-generated anime quiz with images.")
@commands.has_permissions(manage_guild=True)
async def anime_quiz_generate_event(interaction: discord.Interaction, topic: str, num_questions: int = 3):
    if interaction.guild is None: await interaction.response.send_message("This command can only be used in a server.", ephemeral=True); return
    guild_id = str(interaction.guild.id); quiz_channel = interaction.guild.get_channel(int(bot_config["quiz_channels"].get(guild_id))) if bot_config["quiz_channels"].get(guild_id) else None
    if not quiz_channel: await interaction.response.send_message("Please set a quiz channel first using `/set_quiz_channel`.", ephemeral=True); return
    if guild_id in quiz_active_guilds and quiz_active_guilds[guild_id].is_running: await interaction.response.send_message("A quiz is already running! Wait for it to finish.", ephemeral=True); return
    if not (1 <= num_questions <= 10): await interaction.response.send_message("Number of questions must be between 1 and 10.", ephemeral=True); return

    await interaction.response.defer(thinking=True) 

    try:
        generated_questions = await generate_ai_quiz_questions(topic, num_questions)
        if not generated_questions:
            await interaction.followup.send("Could not generate quiz questions for that topic. Please try another.", ephemeral=True)
            return

        quiz_state = QuizState(guild_id, quiz_channel.id, generated_questions)
        quiz_active_guilds[guild_id] = quiz_state
        await interaction.followup.send(f"Starting an AI-generated anime quiz about '{topic}' in {quiz_channel.mention}!", ephemeral=True)
        await quiz_state.start_quiz(quiz_channel)
        
        user_id_str = str(interaction.user.id)
        if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
        leaderboard_data[user_id_str]["xp"] += 25 
        save_data()

    except Exception as e:
        await interaction.followup.send(f"An error occurred while generating the AI quiz: {e}", ephemeral=True)

@anime_quiz_generate_event.error
async def anime_quiz_generate_error(interaction: discord.Interaction, error):
    if isinstance(error, commands.MissingPermissions): await interaction.response.send_message("You don't have permission to use this command. You need 'Manage Server' permission.", ephemeral=True)
    else: await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)


# --- User Leaderboard & Rank ---
@bot.tree.command(name="leaderboard", description="Displays the top users by their total XP.")
async def show_leaderboard(interaction: discord.Interaction):
    if not leaderboard_data:
        await interaction.response.send_message("No one is on the leaderboard yet! Interact with the bot to earn XP.", ephemeral=True)
        return

    filtered_users = {uid: data for uid, data in leaderboard_data.items() if data.get("xp", 0) > 0}
    if not filtered_users:
        await interaction.response.send_message("No one has earned XP yet!", ephemeral=True)
        return

    sorted_users = sorted(filtered_users.items(), key=lambda item: item[1].get("xp", 0), reverse=True)

    leaderboard_message = "👑 **Bot Interaction Leaderboard (Top XP)** 👑\n"
    rank = 1
    for user_id, data in sorted_users:
        user = bot.get_user(int(user_id)) 
        xp = data.get("xp", 0)
        quiz_score = data.get("quiz_score", 0)
        
        if user:
            leaderboard_message += f"**{rank}.** {user.display_name}: {xp} XP ({quiz_score} quiz points)\n"
        else:
            leaderboard_message += f"**{rank}.** User (ID: {user_id}): {xp} XP ({quiz_score} quiz points)\n"
        
        rank += 1
        if rank > 10: break 

    await interaction.response.send_message(leaderboard_message)

@bot.tree.command(name="rank", description="Shows your current XP and level.")
async def show_rank(interaction: discord.Interaction):
    user_id_str = str(interaction.user.id)
    user_data = leaderboard_data.get(user_id_str, {"xp": 0, "quiz_score": 0})
    xp = user_data.get("xp", 0)
    quiz_score = user_data.get("quiz_score", 0)

    level = xp // 100 + 1 
    xp_to_next_level = (level * 100) - xp

    rank_message = f"🌟 **{interaction.user.display_name}'s Rank** 🌟\n" \
                   f"**XP:** {xp}\n" \
                   f"**Level:** {level}\n" \
                   f"**Quiz Points:** {quiz_score}\n"
    if xp_to_next_level > 0:
        rank_message += f"You need {xp_to_next_level} more XP to reach Level {level + 1}!"
    else:
        rank_message += "You've reached the current max level or are ready to level up!"

    await interaction.response.send_message(rank_message, ephemeral=True)

async def send_levelup_message(user: discord.Member, old_level: int, new_level: int, guild: discord.Guild):
    """Sends a level-up message to the configured channel with an AI image."""
    guild_id_str = str(guild.id)
    rankup_channel_id = bot_config["rankup_channels"].get(guild_id_str)

    if not rankup_channel_id:
        print(f"No rank-up channel set for guild '{guild.name}'. Skipping level-up message.")
        return

    channel = guild.get_channel(int(rankup_channel_id))
    if not channel or not isinstance(channel, discord.TextChannel):
        print(f"Rank-up channel {rankup_channel_id} not found or not a text channel in guild '{guild.name}'. Skipping level-up message.")
        return

    try:
        image_url = await generate_image_with_gemini(f"anime character leveling up, celebratory, new rank, fireworks, confetti, digital art, level {new_level}")
        
        embed = discord.Embed(
            title="🎉 Level Up! 🎉",
            description=f"Congratulations {user.mention}! You've reached **Level {new_level}**!",
            color=discord.Color.gold()
        )
        embed.set_thumbnail(url=user.avatar.url if user.avatar else discord.Embed.Empty)
        embed.set_image(url=image_url)
        embed.set_footer(text=f"Keep chatting to earn more XP!")
        
        await channel.send(embed=embed)
        print(f"Sent level-up message for {user.display_name} to Level {new_level} in '{guild.name}'.")
    except discord.Forbidden:
        print(f"Error: Missing permissions to send level-up message in '{channel.name}' in guild '{guild.name}'.")
    except Exception as e:
        print(f"An error occurred sending level-up message for {user.display_name}: {e}")

# --- Birthday Wish Commands ---

@bot.tree.command(name="set_birthday", description="Sets your birthday (MM-DD) for birthday wishes and role.")
async def set_birthday(interaction: discord.Interaction, month_day: str):
    user_id = str(interaction.user.id)
    if not re.match(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$", month_day):
        await interaction.response.send_message("Invalid format. Please use `MM-DD` (e.g., `01-15` for January 15th).", ephemeral=True); return
    try: 
        month, day = map(int, month_day.split('-')); datetime.date(2000, month, day) 
    except ValueError:
        await interaction.response.send_message("Invalid date provided. Please check the month and day (e.g., no 31st in April).", ephemeral=True); return

    birthdays_data[user_id] = month_day; save_data()
    await interaction.response.send_message(
        f"Your birthday has been set to **{month_day}**! 🎉 I'll make sure to wish you well and assign your birthday role on your special day!\n"
        "*(Note: Birthday wishes are sent based on UTC time.)*", ephemeral=True
    )

@bot.tree.command(name="remove_birthday", description="Removes your birthday from the bot's memory.")
async def remove_birthday(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    if user_id in birthdays_data: del birthdays_data[user_id]; save_data()
    else: await interaction.response.send_message("You haven't set a birthday with me yet!", ephemeral=True)


# --- Anime Info Search (Placeholder for External API) ---
async def get_anime_info_from_api(title: str) -> dict | None:
    """
    Placeholder for fetching anime information from an external API (e.g., Jikan API).
    """
    print(f"DEBUG: Searching anime info for '{title}'")
    await asyncio.sleep(2) 

    lower_title = title.lower()
    if "naruto" in lower_title:
        return {
            "title": "Naruto",
            "synopsis": "Naruto Uzumaki, a mischievous adolescent ninja, struggles as he searches for recognition and dreams of becoming the Hokage, the village's leader and strongest ninja.",
            "genres": ["Action", "Adventure", "Comedy", "Supernatural"],
            "episodes": 220,
            "rating": "PG-13",
            "image_url": "https://via.placeholder.com/200x300.png?text=Naruto"
        }
    elif "one piece" in lower_title:
        return {
            "title": "One Piece",
            "synopsis": "Monkey D. Luffy, a boy whose body gained the properties of rubber after unintentionally eating a Devil Fruit, journeys with his diverse crew of pirates, named the Straw Hat Pirates, in search of the world's ultimate treasure, the One Piece.",
            "genres": ["Action", "Adventure", "Fantasy"],
            "episodes": "1000+",
            "rating": "PG-13",
            "image_url": "https://via.placeholder.com/200x300.png?text=One+Piece"
        }
    else:
        return None

@bot.tree.command(name="animeinfo", description="Get information about an anime series.")
async def anime_info_command(interaction: discord.Interaction, title: str):
    await interaction.response.defer(thinking=True)
    try:
        anime_data = await get_anime_info_from_api(title)
        if anime_data:
            embed = discord.Embed(
                title=anime_data.get("title", "Anime Info"),
                description=anime_data.get("synopsis", "No synopsis available."),
                color=discord.Color.blue()
            )
            embed.add_field(name="Genres", value=", ".join(anime_data.get("genres", ["N/A"])), inline=True)
            embed.add_field(name="Episodes", value=str(anime_data.get("episodes", "N/A")), inline=True)
            embed.add_field(name="Rating", value=anime_data.get("rating", "N/A"), inline=True)
            if anime_data.get("image_url"):
                embed.set_thumbnail(url=anime_data["image_url"])
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Could not find information for anime: **{title}**")
        
        user_id_str = str(interaction.user.id)
        if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
        leaderboard_data[user_id_str]["xp"] += 5 
        save_data()

    except Exception as e:
        await interaction.followup.send(f"An error occurred while fetching anime info: {e}")

# --- Anime Quote Generator ---
ANIME_QUOTES = [
    "\"If you don't take risks, you can't create a future!\" - Monkey D. Luffy (One Piece)",
    "\"The world isn't perfect. But it's there for us, doing the best it can. That's what makes it so damn beautiful.\" - Roy Mustang (Fullmetal Alchemist)",
    "\"Knowing what it feels like to be in pain is exactly why we try to be kind to others.\" - Jiraiya (Naruto)",
    "\"Bang.\" - Spike Spiegel (Cowboy Bebop)",
    "\"People's lives don't end when they die. It ends when they lose faith.\" - Itachi Uchiha (Naruto)",
    "\"Fear is not evil. It tells you what your weakness is. And once you know your weakness, you can become stronger as well as kinder.\" - Gildarts Clive (Fairy Tail)",
    "\"To know sorrow is not evil. But not to know joy is.\" - Jellal Fernandes (Fairy Tail)",
    "\"It's not about whether you can or can't. It's about whether you do or don't.\" - Gintoki Sakata (Gintama)",
    "\"Hard work is worthless for those that don't believe in themselves.\" - Naruto Uzumaki (Naruto)"
]

@bot.tree.command(name="animequote", description="Get a random inspiring anime quote.")
async def anime_quote_command(interaction: discord.Interaction):
    import random
    quote = random.choice(ANIME_QUOTES)
    await interaction.response.send_message(f"Here's an anime quote for you:\n\n{quote}")
    user_id_str = str(interaction.user.id)
    if user_id_str not in leaderboard_data: leaderboard_data[user_id_str] = {"xp": 0, "quiz_score": 0}
    leaderboard_data[user_id_str]["xp"] += 3 
    save_data()


# --- Daily Birthday Check Task ---
@tasks.loop(time=datetime.time(hour=0, minute=0, tzinfo=datetime.timezone.utc)) # Runs daily at 00:00 UTC
async def check_birthdays_daily():
    print(f"[{datetime.datetime.now()}] Checking for birthdays and managing roles...")
    today_mm_dd = datetime.datetime.now(datetime.timezone.utc).strftime("%m-%d")
    yesterday_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    yesterday_mm_dd = yesterday_date.strftime("%m-%d")
    
    # Iterate through all guilds to check for channels/roles and members
    for guild_id_str in bot_config["birthday_channels"].keys(): # Use keys to iterate
        guild = bot.get_guild(int(guild_id_str))
        if not guild:
            print(f"Warning: Guild {guild_id_str} not found for birthday check. Skipping.")
            continue

        birthday_channel = guild.get_channel(int(bot_config["birthday_channels"][guild_id_str])) if bot_config["birthday_channels"].get(guild_id_str) else None
        birthday_role_id = bot_config["birthday_roles"].get(guild_id_str)
        birthday_role = guild.get_role(int(birthday_role_id)) if birthday_role_id else None

        # --- Role Removal for Past Birthdays ---
        if birthday_role:
            print(f"Checking members in '{guild.name}' for birthday role removal...")
            for member in guild.members:
                if member.bot: continue # Skip bots
                user_birthday = birthdays_data.get(str(member.id))
                if birthday_role in member.roles: # If member has the birthday role
                    if not user_birthday or user_birthday != today_mm_dd: # If it's not their birthday today or no birthday set
                        try:
                            await member.remove_roles(birthday_role, reason="Birthday passed.")
                            print(f"Removed birthday role from {member.display_name} in '{guild.name}'.")
                        except discord.Forbidden:
                            print(f"Error: Missing permissions to remove role from {member.display_name} in '{guild.name}'.")
                        except Exception as e:
                            print(f"Error removing birthday role from {member.display_name} in '{guild.name}': {e}")
        
        # --- Birthday Wishes and Role Assignment for Today's Birthdays ---
        if birthday_channel and isinstance(birthday_channel, discord.TextChannel):
            print(f"Checking for today's birthdays in '{guild.name}' channel '{birthday_channel.name}'...")
            for user_id, birthday_date in birthdays_data.items():
                if birthday_date == today_mm_dd:
                    member_on_server = guild.get_member(int(user_id)) # Get as member to add role
                    if member_on_server:
                        # 1. Send Birthday Wish
                        try:
                            wish_message = (
                                f"🎉🎂 Happy Birthday to {member_on_server.mention}! 🥳✨\n"
                                f"The entire server wishes you an amazing day filled with joy, laughter, and all your favorite anime! "
                                f"May your year be as fantastic as a shonen protagonist's journey! 🌟"
                            )
                            embed = discord.Embed(
                                title="Happy Birthday!",
                                description=wish_message,
                                color=discord.Color.gold()
                            )
                            embed.set_thumbnail(url=member_on_server.avatar.url if member_on_server.avatar else discord.Embed.Empty)
                            await birthday_channel.send(embed=embed)
                            print(f"Sent birthday wish for {member_on_server.display_name} in '{guild.name}'.")
                        except discord.Forbidden:
                            print(f"Error: Missing permissions to send message in '{birthday_channel.name}' in guild '{guild.name}'.")
                        except Exception as e:
                            print(f"Error sending birthday message for {member_on_server.display_name} in '{guild.name}': {e}")

                        # 2. Assign Birthday Role
                        if birthday_role and birthday_role not in member_on_server.roles:
                            try:
                                await member_on_server.add_roles(birthday_role, reason="It's their birthday!")
                                print(f"Assigned birthday role to {member_on_server.display_name} in '{guild.name}'.")
                            except discord.Forbidden:
                                print(f"Error: Missing permissions to add role to {member_on_name.display_name} in '{guild.name}'.")
                            except Exception as e:
                                print(f"Error assigning birthday role to {member_on_server.display_name} in '{guild.name}': {e}")
                    else:
                        print(f"Birthday for user ID {user_id} detected, but user not found/accessible in guild '{guild.name}'.")
        else:
            print(f"Birthday channel not configured or invalid for guild '{guild.name}'. Skipping birthday wishes.")


@check_birthdays_daily.before_loop
async def before_check_birthdays_daily():
    """Waits for the bot to be ready before starting the daily birthday check loop."""
    await bot.wait_until_ready()
    print("Waiting for bot to be ready before starting birthday check loop...")


# --- Run the Bot ---
if TOKEN:
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("Bot token is invalid. Please check your DISCORD_BOT_TOKEN.")
    except Exception as e:
        print(f"An unexpected error occurred while running the bot: {e}")
else:
    print("Bot cannot start because the token is not set.")
