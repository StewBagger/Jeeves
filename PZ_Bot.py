import os, discord, asyncio, rcon, time, datetime, socket, subprocess, httpx, json, threading
from rcon.source import Client
from discord import app_commands
from discord.ext import commands, tasks

#-----Configuration-----#
#RCON
RC_HOST = '127.0.0.1'
RC_PORT = 27015
RC_PASSWORD = 'ReforgedOdyn1010'
#Discord
CHANNEL_ID = 1372533858381336718
GUILD_ID = 299334817336786946
TOKEN = 'MTI2NDIwNjU1NTQ5OTQ2Njg2NA.Ggs5Y7.5iMhndnubOUV5J8FSIg4d2bE8Z8MBNHVYEx7t8'
#Misc
CHECK_INTERVAL = 30
SERVER_BATCH = 'C:\\SteamCMD\\steamapps\\common\\Project Zomboid Dedicated Server\\StartServer64.bat'
RESTARTNOW_SCRIPT = 'C:\\SteamCMD\\steamapps\\common\\Project Zomboid Dedicated Server\\RestartServerNow.exe'
#Mod Checker
STEAM_API_KEY = "FBD9437843663697867D510838940D4F"
SERVER_INI_PATH = r"C:\\Users\\ut2k3\\Zomboid\\Server\\servertest.ini"
UPDATE_LOG_PATH = r"C:\\Users\\ut2k3\\Desktop\\Mod Update Log\\mod_update_log.json"
API_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
#-----------------------#

intents = discord.Intents.all()
intents.message_content = True

#---Global Variables----#
UP_MODS = None
FIRST_START = True
AUTO_RST = False
PLAYERS_ONLINE = False
MR_TASK = None
RESTARTING = False
#-----------------------#

class PZBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        MY_GUILD = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=MY_GUILD)
        await self.tree.sync(guild=MY_GUILD)
        await self.load_extension('auto_restart')
        await self.load_extension('mod_check_timer')
        print(f"Synced commands to guild {MY_GUILD.id}")

bot = PZBot()

@bot.tree.command(name="hello", description="Returns Hello!")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Hello!", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
@bot.tree.command(name="players", description="Returns a list of players currently connected to the server.")
async def ping(interaction: discord.Interaction):
    response = await rcon.source.rcon( 'players', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    embed = discord.Embed(title=f"{response}", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
@bot.tree.command(name="online", description="Checks if game server is online..")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Checking on server...", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await is_online()
  
@bot.tree.command(name="restart", description="Restarts the game server.")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Restarting server, this may take several minutes...", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed)
    await stop_server()
    await asyncio.sleep(5)
    await start_server()
    
@bot.tree.command(name="start", description="Starts the game server if it is not currently running.")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Attempting to start the server, this may take several minutes...", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed)
    await start_online()
    
@bot.tree.command(name="stop", description="Stops the game server.")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Shutting down the server...", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed)
    await stop_server()
    await is_online()

@bot.tree.command(name="teleport", description="Teleports player1 to player2's location.")
async def teleport_info(interaction: discord.Interaction, player1: str, player2: str):
    response = await rcon.source.rcon(f'teleport ("{player1}", "{player2}")', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD)
    embed = discord.Embed(title=f"Attempting to teleport {player1} to {player2}'s location.", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="msg", description="Broadcasts a message to the server.")
async def msg_info(interaction: discord.Interaction, message: str):
    response = await rcon.source.rcon(f'servermsg "{message}"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD)
    embed = discord.Embed(title=f"{response}", colour=discord.Colour.purple())    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="mod", description="Checks if the server's mods are up to date.")
async def ping(interaction: discord.Interaction):
    embed = discord.Embed(title="Checking mods...", colour=discord.Colour.purple())
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await check_mod_main()
    
@bot.event
async def on_ready():
    await jeeves_online()

@bot.event  
async def server_online():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:pzhappy:1467224964829544449> Server is Online!", colour=discord.Colour.green())
    await channel.send(embed=embed)
    
@bot.event  
async def already_online():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:pzdizzy:1467224682720923759> Server is already Online!", colour=discord.Colour.green())
    await channel.send(embed=embed)
    
@bot.event  
async def server_offline():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title=f"<:pzpanic:1467224381385085263> Server is Offline! Checking again in {CHECK_INTERVAL}s...", colour=discord.Colour.red())
    await channel.send(embed=embed)
    
@bot.event  
async def server_offline_2():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title=f"<:pzpanic:1467224381385085263> Server is Offline!", colour=discord.Colour.red())
    await channel.send(embed=embed)

@bot.event  
async def jeeves_online():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:jeeves:1468333299524042895> Checking on the server...", colour=discord.Colour.purple())
    await channel.send(embed=embed)
    await monitor_until_online()
    
@bot.event
async def start_server():
    p = subprocess.Popen(SERVER_BATCH,creationflags=subprocess.CREATE_NEW_CONSOLE)
    await asyncio.sleep(60)
    await jeeves_online()
    
@bot.event
async def stop_server():
    await rcon.source.rcon( 'save', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    await asyncio.sleep(8)
    await rcon.source.rcon( 'quit', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    await asyncio.sleep(8)
    os.system("taskkill /f /im cmd.exe")
    
@bot.event  
async def retries():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:pzangry:1467224612440903683> Retry limit exceeded!", colour=discord.Colour.red())
    await channel.send(embed=embed)

@bot.event    
async def auto_restart_main():
    global AUTO_RST
    AUTO_RST = True
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<a:spiffopop:1467224894193275116> Automatic Restart Initiated, this may take several minutes...", colour=discord.Colour.yellow())
    await channel.send(embed=embed)
    await stop_server()
    await asyncio.sleep(5)
    await start_server()
    
async def mod_restart_task():
    global MR_TASK
    MR_TASK = asyncio.create_task(mod_restart())
   
async def cancel_task():
    global MR_TASK
    if MR_TASK and not MR_TASK.done():
        MR_TASK.cancel()
        print("Restart task cancelled.")
    else:
        print("No active restart task found to cancel.")
  
async def mod_restart():
    try:
        channel = bot.get_channel(CHANNEL_ID)
        embed = discord.Embed(title="<:jeeves:1468333299524042895> Mod update detected!", description=f"{UP_MODS}", colour=discord.Colour.purple())
        await rcon.source.rcon( 'servermsg "Mod update detected!"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
        await channel.send(embed=embed)
        if PLAYERS_ONLINE:
            await asyncio.sleep(10)
            await server_restart_10m()
            await mod_restart_10m()
            await server_restart_5m()
            await mod_restart_5m()
            await server_restart_1m()
            await mod_restart_1m()
            await server_restart_10s()
            await asyncio.sleep(10)
            await stop_server()
            await asyncio.sleep(5)
            await start_server()
        else:
            await mod_restart_now()
    except asyncio.CancelledError:
        print ("Mod update countdown cancelled, switching to immediate restart.")
        
async def mod_restart_10m(max_retries=10):
    global PLAYERS_ONLINE    
    for attempt in range(max_retries):
        if PLAYERS_ONLINE:
            await asyncio.sleep(30)
            continue
        asyncio.create_task(mod_restart_now())
        await cancel_task()
        return
        
async def mod_restart_5m(max_retries=8):
    global PLAYERS_ONLINE    
    for attempt in range(max_retries):
        if PLAYERS_ONLINE:
            await asyncio.sleep(30)
            continue
        asyncio.create_task(mod_restart_now())
        await cancel_task()
        return
        
async def mod_restart_1m(max_retries=1):
    global PLAYERS_ONLINE    
    for attempt in range(max_retries):
        if PLAYERS_ONLINE:
            await asyncio.sleep(50)
            continue
        asyncio.create_task(mod_restart_now())
        await cancel_task()
        return
   
async def mod_restart_now():
    global RESTARTING
    if RESTARTING: 
        return
    RESTARTING = True
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:jeeves:1468333299524042895> No players online, restarting immediately!", colour=discord.Colour.purple())
    await channel.send(embed=embed)
    await stop_server()
    await asyncio.sleep(5)
    await start_server()
    RESTARTING = False
        
@bot.event  
async def server_restart_10m():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:spiffowave:1467226773824733226> Server will automatically restart in 10 Minutes!", colour=discord.Colour.yellow())
    await channel.send(embed=embed)
    await rcon.source.rcon( 'servermsg "Server will automatically restart in 10 Minutes!"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    
@bot.event  
async def server_restart_5m():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:spiffoeducate:1467226965789376552> Server will automatically restart in 5 Minutes!", colour=discord.Colour.yellow())
    await channel.send(embed=embed)
    await rcon.source.rcon( 'servermsg "Server will automatically restart in 5 Minutes!"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    
@bot.event  
async def server_restart_1m():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:spiffokatana:1467227039336763475> Server will automatically restart in 1 Minute!", colour=discord.Colour.yellow())
    await channel.send(embed=embed)
    await rcon.source.rcon( 'servermsg "Server will automatically restart in 1 Minute!"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    
@bot.event  
async def server_restart_10s():
    channel = bot.get_channel(CHANNEL_ID)
    embed = discord.Embed(title="<:spiffostop:1467226688017400023> Server will automatically restart in 10 Seconds!", colour=discord.Colour.yellow())
    await channel.send(embed=embed)
    await rcon.source.rcon( 'servermsg "Server will automatically restart in 10 Seconds!"', host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD )
    
@bot.event  
async def player_check_main(max_retries=1):
    global PLAYERS_ONLINE
    for attempt in range(max_retries):
        try:
            with Client(host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD, timeout=5) as client:
                response = client.run('players')
                if "Players connected (0)" in response:
                    PLAYERS_ONLINE = False
                else:    
                    PLAYERS_ONLINE = True               
        except (socket.timeout, ConnectionRefusedError, OSError) as e: 
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
            else:
                return

async def monitor_until_online(max_retries=12):
    global FIRST_START
    for attempt in range(max_retries):
        try:
            with Client(host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD, timeout=5) as client:
                client.run('players')
                await server_online()
                if FIRST_START:
                    FIRST_START = False
                else:
                    try:
                        await bot.reload_extension('mod_check_timer')
                    except discord.ext.commands.ExtensionNotLoaded:
                        await bot.load_extension('mod_check_timer')   
                return       
        except (socket.timeout, ConnectionRefusedError, OSError) as e: 
            if attempt < max_retries - 1:
                await server_offline()
                await asyncio.sleep(CHECK_INTERVAL)
            else:
                await retries()
                raise

async def is_online(max_retries=1):  
    for attempt in range(max_retries):
        try:
            with Client(host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD, timeout=5) as client:
                client.run('players')
                await server_online()
                return       
        except (socket.timeout, ConnectionRefusedError, OSError) as e: 
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            else:
                await server_offline_2()
                return
        
async def start_online(max_retries=1):  
    for attempt in range(max_retries):
        try:
            with Client(host=RC_HOST, port=RC_PORT, passwd=RC_PASSWORD, timeout=5) as client:
                client.run('players')
                await already_online()
                return
        except (socket.timeout, ConnectionRefusedError, OSError) as e: 
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
            else:
                await start_server()
                return
                
async def get_workshop_ids_from_ini(ini_path):
    try:
        with open(ini_path, 'r') as f:
            for line in f:
                if line.strip().startswith("WorkshopItems="):
                    val = line.split('=')[1].strip()
                    return [i for i in val.split(';') if i.strip()]
        return []
    except Exception as e:
        print(f"Error reading .ini: {e}")
        return []

async def get_steam_workshop_update_times(workshop_ids, api_key):
    data = {'itemcount': len(workshop_ids), 'format': 'json', 'key': api_key}
    for i, item_id in enumerate(workshop_ids):
        data[f'publishedfileids[{i}]'] = item_id
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(API_URL, data=data)
            response.raise_for_status()
            result = response.json()
            details = {}
            for item in result.get('response', {}).get('publishedfiledetails', []):
                fid = str(item.get('publishedfileid'))
                details[fid] = {'time': item.get('time_updated', 0), 'title': item.get('title', 'Unknown Mod')}
            return details
        except httpx.HTTPStatusError as e:
            print(f"HTTP error {e.response.status_code} from {e.request.url}")
            return {}
        except Exception as e:
            print(f"Async API Request Failed: {e}")
            return {}
            
@bot.event
async def check_mod_main():
    global AUTO_RST
    print("🚀 Starting mod update check...")
    x = datetime.datetime.now()
    print(x)
    workshop_ids = await get_workshop_ids_from_ini(SERVER_INI_PATH)
    if not workshop_ids:
        print("No Workshop IDs found. Check your .ini file path.")
        return
    previous_state = {}
    if os.path.exists(UPDATE_LOG_PATH):
        with open(UPDATE_LOG_PATH, 'r') as f:
            try:
                previous_state = json.load(f)
            except: pass
    current_state = await get_steam_workshop_update_times(workshop_ids, STEAM_API_KEY)
    if not current_state:
        print("Failed to fetch mod data from Steam.")
        return
    updated_mods = [
        info['title'] for mid, info in current_state.items()
        if mid in previous_state and info['time'] > previous_state[mid]['time']
    ]
    
    os.makedirs(os.path.dirname(UPDATE_LOG_PATH), exist_ok=True)
    with open(UPDATE_LOG_PATH, 'w') as f:
        json.dump(current_state, f, indent=4)
        
    if updated_mods:
        if AUTO_RST == False:
            print(f"🚨 Updates detected: {', '.join(updated_mods)}")
            globals()["UP_MODS"] = f"{updated_mods}"
            print("Restart Logic Initiated")
            await mod_restart_task()
        else:
            print(f"🚨 Updates detected: {', '.join(updated_mods)}")
            globals()["UP_MODS"] = f"{updated_mods}"
            print("Auto Restart should have caught this, No Restart Required.")
    else:
        print("✅ All mods are current.")
        print("No Restart Required")
    AUTO_RST = False
            
bot.run(TOKEN)

