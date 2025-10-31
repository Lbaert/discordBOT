import asyncio
import os
import random
import sqlite3
import time
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

import gspread
from google.oauth2.service_account import Credentials

# ============================= CONFIG ==================================
TEXT_XP_MIN = 10
TEXT_XP_MAX = 20
TEXT_COOLDOWN_S = 10
VOICE_XP_PER_MIN = 6  # XP par minute en vocal

# --- Dossier persistant (Render redémarre parfois) ---
os.makedirs("data", exist_ok=True)
DB_PATH = os.path.join("data", "levelbot.sqlite3")

# ======================== GOOGLE SHEETS SETUP ==========================
SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("/etc/secrets/credentials.json", scopes=SCOPE)
client = gspread.authorize(creds)

SHEET_NAME = "LevelBot XP"  # 🔧 Mets le nom exact de ton Google Sheet
sheet = client.open(SHEET_NAME).sheet1

def save_xp_to_sheets(user_id, username, level, xp):
    """Sauvegarde le profil utilisateur sur Google Sheets"""
    try:
        all_users = sheet.col_values(1)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if str(user_id) in all_users:
            row_index = all_users.index(str(user_id)) + 1
            sheet.update(f"B{row_index}:E{row_index}", [[username, level, xp, now]])
        else:
            sheet.append_row([user_id, username, level, xp, now])
    except Exception as e:
        print(f"[Google Sheets] Erreur de sauvegarde pour {username}: {e}")

# =========================== DATABASE SETUP ============================
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    guild_id INTEGER,
    user_id  INTEGER,
    xp       INTEGER DEFAULT 0,
    level    INTEGER DEFAULT 0,
    last_msg_ts INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS voice_sessions (
    guild_id INTEGER,
    user_id  INTEGER,
    start_ts INTEGER,
    PRIMARY KEY (guild_id, user_id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS level_roles (
    guild_id INTEGER,
    level    INTEGER,
    role_id  INTEGER,
    PRIMARY KEY (guild_id, level)
);
""")
conn.commit()

# ============================== UTILITAIRES =============================
def required_xp(level: int) -> int:
    return 100 + 50 * level + 5 * (level ** 2)

def get_profile(guild_id: int, user_id: int):
    cur.execute(
        "SELECT xp, level, last_msg_ts FROM users WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users(guild_id, user_id, xp, level, last_msg_ts) VALUES(?,?,?,?,?)",
            (guild_id, user_id, 0, 0, 0),
        )
        conn.commit()
        return 0, 0, 0
    return row

def update_profile(guild_id: int, user_id: int, xp: int, level: int, last_msg_ts=None):
    if last_msg_ts is None:
        cur.execute(
            "UPDATE users SET xp=?, level=? WHERE guild_id=? AND user_id=?",
            (xp, level, guild_id, user_id),
        )
    else:
        cur.execute(
            "UPDATE users SET xp=?, level=?, last_msg_ts=? WHERE guild_id=? AND user_id=?",
            (xp, level, last_msg_ts, guild_id, user_id),
        )
    conn.commit()

def add_voice_session_start(guild_id: int, user_id: int):
    cur.execute(
        "REPLACE INTO voice_sessions(guild_id, user_id, start_ts) VALUES(?,?,?)",
        (guild_id, user_id, int(time.time())),
    )
    conn.commit()

def pop_voice_session(guild_id: int, user_id: int):
    cur.execute(
        "SELECT start_ts FROM voice_sessions WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        return None
    cur.execute(
        "DELETE FROM voice_sessions WHERE guild_id=? AND user_id=?",
        (guild_id, user_id),
    )
    conn.commit()
    return row[0]

def list_top(guild_id: int, limit: int = 10):
    cur.execute(
        "SELECT user_id, xp, level FROM users WHERE guild_id=? ORDER BY xp DESC LIMIT ?",
        (guild_id, limit),
    )
    return cur.fetchall()

def set_level_role(guild_id: int, level: int, role_id: int):
    cur.execute(
        "REPLACE INTO level_roles(guild_id, level, role_id) VALUES(?,?,?)",
        (guild_id, level, role_id),
    )
    conn.commit()

def get_level_roles(guild_id: int):
    cur.execute(
        "SELECT level, role_id FROM level_roles WHERE guild_id=? ORDER BY level ASC",
        (guild_id,),
    )
    return cur.fetchall()

def fetch_role_for_level(guild_id: int, level: int):
    cur.execute(
        "SELECT role_id FROM level_roles WHERE guild_id=? AND level=?",
        (guild_id, level),
    )
    row = cur.fetchone()
    return row[0] if row else None

# ============================ DISCORD BOT ===============================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
text_cooldowns = {}

async def grant_xp_and_handle_levelup(member: discord.Member, amount: int):
    guild_id = member.guild.id
    user_id = member.id
    xp, level, last_ts = get_profile(guild_id, user_id)
    xp += amount
    leveled_up = False

    while xp >= required_xp(level):
        xp -= required_xp(level)
        level += 1
        leveled_up = True
        role_id = fetch_role_for_level(guild_id, level)
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason=f"Level {level} reached")
                except discord.Forbidden:
                    pass

    update_profile(guild_id, user_id, xp, level)
    save_xp_to_sheets(user_id, member.name, level, xp)
    return leveled_up, level, xp

@bot.event
async def on_ready():
    print(f"✅ Connecté en tant que {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    now = int(time.time())
    key = (message.guild.id, message.author.id)
    last_ts = text_cooldowns.get(key, 0)

    if now - last_ts >= TEXT_COOLDOWN_S:
        text_cooldowns[key] = now
        amount = random.randint(TEXT_XP_MIN, TEXT_XP_MAX)
        leveled_up, level, xp = await grant_xp_and_handle_levelup(message.author, amount)
        if leveled_up:
            await message.channel.send(f"🎉 {message.author.mention} passe **niveau {level}** !")

    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    joined = after.channel and (not before.channel or before.channel.id != after.channel.id)
    left = before.channel and (not after.channel or before.channel.id != after.channel.id)

    if joined and not after.self_mute and not after.self_deaf:
        add_voice_session_start(member.guild.id, member.id)

    if left:
        start_ts = pop_voice_session(member.guild.id, member.id)
        if start_ts:
            duration = int(time.time()) - start_ts
            minutes = duration // 60
            if minutes > 0:
                amount = minutes * VOICE_XP_PER_MIN
                await grant_xp_and_handle_levelup(member, amount)

    if after.channel and (after.self_mute or after.self_deaf):
        pop_voice_session(member.guild.id, member.id)

    if after.channel and (not after.self_mute and not after.self_deaf):
        cur.execute(
            "SELECT 1 FROM voice_sessions WHERE guild_id=? AND user_id=?",
            (member.guild.id, member.id),
        )
        if cur.fetchone() is None:
            add_voice_session_start(member.guild.id, member.id)

# =============================== COMMANDES ==============================
@bot.command(name="rank")
async def rank(ctx, member: discord.Member = None):
    member = member or ctx.author
    xp, level, _ = get_profile(ctx.guild.id, member.id)
    await ctx.send(f"📊 {member.mention} — Niveau **{level}**, XP **{xp}/{required_xp(level)}**")

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    rows = list_top(ctx.guild.id, 10)
    if not rows:
        await ctx.send("Aucun classement pour l’instant.")
        return
    lines = []
    for i, (uid, xp, level) in enumerate(rows, start=1):
        user = ctx.guild.get_member(uid) or await ctx.guild.fetch_member(uid)
        name = user.display_name if user else f"<@{uid}>"
        lines.append(f"**#{i}** {name} — Lv {level} • {xp} XP")
    await ctx.send("\n".join(lines))

@commands.has_permissions(manage_roles=True)
@bot.command(name="setrole")
async def setrole(ctx, level: int, role: discord.Role):
    set_level_role(ctx.guild.id, level, role.id)
    await ctx.send(f"✅ Rôle {role.mention} attribué au **niveau {level}**.")

@bot.command(name="roles")
async def roles(ctx):
    pairs = get_level_roles(ctx.guild.id)
    if not pairs:
        await ctx.send("Aucun palier défini. Utilise `!setrole <niveau> @role`.")
        return
    lines = [f"🎯 Paliers de rôles pour {ctx.guild.name} :"]
    for level, role_id in pairs:
        role = ctx.guild.get_role(role_id)
        lines.append(f"• Lv {level} → {role.mention if role else role_id}")
    await ctx.send("\n".join(lines))

# ============================= RENDER SERVER ===========================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN manquant dans .env")

    async def health(_):
        return web.Response(text="ok")

    async def start_web():
        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/healthz", health)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.getenv("PORT", "10000"))
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

    asyncio.get_event_loop().create_task(start_web())
    bot.run(TOKEN)
