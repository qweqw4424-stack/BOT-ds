"""
TicketBot – Bot ticket professionale per Discord (Versione PostgreSQL per Railway)
================================================================================
Modificato per utilizzare PostgreSQL e salvare i transcript direttamente nel DB.
Risolve l'errore 'unable to open database file' su Railway.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import io
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any, Union

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import chat_exporter
    HAS_CHAT_EXPORTER = True
except ImportError:
    HAS_CHAT_EXPORTER = False

# ══════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("ticketbot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("TicketBot")


# ══════════════════════════════════════════════════════════════════
# ★  CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Config:
    TOKEN: str = os.environ.get("BOT_TOKEN", "")
    GUILD_ID: int             = int(os.environ.get("GUILD_ID", "1517091767399350342")) # Ripristinato ID originale
    LOG_CHANNEL_ID: int       = int(os.environ.get("LOG_CHANNEL_ID", "1517107696200061040"))
    CATEGORY_GENERAL: int     = int(os.environ.get("CATEGORY_GENERAL", "1517099911269580891"))
    STAFF_TICKET_ROLE_ID: int = int(os.environ.get("STAFF_TICKET_ROLE_ID", "1517123223836295188"))
    ADMIN_ROLE_ID: int        = int(os.environ.get("ADMIN_ROLE_ID", "1517123223836295188"))

    # DATABASE_URL fornito da Railway
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

    MAX_OPEN_TICKETS: int        = 2
    COOLDOWN_SECONDS: int        = 30
    AUTO_CLOSE_HOURS: int        = 48
    AUTO_CLOSE_CHECK_MINUTES: int = 30
    CLOSE_DELAY: int             = 600

    CATEGORIE: tuple[str, ...] = (
        "Supporto Tecnico",
        "Report Utente",
        "Candidatura Staff",
        "Unisciti al Team",
        "Altro",
    )

    CATEGORIA_ROLES: dict[str, int] = field(default_factory=lambda: {
        "Supporto Tecnico":  int(os.environ.get("ROLE_SUPPORT_ID", "1499713651576406020")),
        "Report Utente":     int(os.environ.get("ROLE_REPORT_ID", "1499713651576406020")),
        "Candidatura Staff": int(os.environ.get("ROLE_STAFF_CANDIDATE_ID", "1499713651576406024")),
        "Unisciti al Team":  int(os.environ.get("ROLE_JOIN_TEAM_ID", "1499713651576406024")),
        "Altro":             int(os.environ.get("ROLE_OTHER_ID", "1499713651576406020")),
    })

    def validate(self) -> None:
        errors = []
        if not self.TOKEN or self.TOKEN == "IL_TUO_TOKEN_QUI":
            errors.append("TOKEN non impostato")
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL non impostato (Necessario per PostgreSQL su Railway)")
        if self.GUILD_ID == 0:
            errors.append("GUILD_ID non impostato o non valido")
        
        if errors:
            for e in errors:
                log.critical("Configurazione mancante: %s", e)
            sys.exit(1)

cfg = Config()
cfg.validate()


# ══════════════════════════════════════════════════════════════════
# ENUMS
# ══════════════════════════════════════════════════════════════════
class Priority(str, Enum):
    LOW = "Bassa"
    MEDIUM = "Media"
    HIGH = "Alta"
    URGENT = "Urgente"

    @property
    def icon(self) -> str:
        return {"Bassa": "🟢", "Media": "🟡", "Alta": "🔴", "Urgente": "🚨"}[self.value]

    @property
    def color(self) -> int:
        return {
            "Bassa": 0x2ECC71,
            "Media": 0xF39C12,
            "Alta": 0xE67E22,
            "Urgente": 0xE74C3C,
        }[self.value]

    @property
    def rank(self) -> int:
        return {"Urgente": 0, "Alta": 1, "Media": 2, "Bassa": 3}[self.value]


class TicketStatus(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


# ══════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════
_MC_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
_CHANNEL_INVALID_RE = re.compile(r"[^a-z0-9-]")

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def safe_channel_name(raw: str) -> str:
    base = _CHANNEL_INVALID_RE.sub("-", raw.lower())
    base = re.sub(r"-{2,}", "-", base).strip("-") or "utente"
    return f"ticket-{base}"[:100]

def format_delta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# ══════════════════════════════════════════════════════════════════
# DATABASE – PostgreSQL (asyncpg)
# ══════════════════════════════════════════════════════════════════
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    channel_id   BIGINT PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    categoria    TEXT NOT NULL DEFAULT 'Altro',
    priority     TEXT NOT NULL DEFAULT 'Bassa',
    status       TEXT NOT NULL DEFAULT 'open',
    mc_name      TEXT,
    subject      TEXT,
    description  TEXT,
    opened_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_message TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    claimed_by   BIGINT,
    closed_by    BIGINT,
    closed_at    TIMESTAMP WITH TIME ZONE,
    close_reason TEXT,
    transcript   BYTEA
);

CREATE TABLE IF NOT EXISTS blacklist (
    user_id  BIGINT PRIMARY KEY,
    reason   TEXT NOT NULL DEFAULT 'Nessun motivo',
    added_by BIGINT NOT NULL,
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_stats (
    staff_id      BIGINT PRIMARY KEY,
    closed_count  INTEGER NOT NULL DEFAULT 0,
    claimed_count INTEGER NOT NULL DEFAULT 0,
    avg_close_min REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id   BIGINT PRIMARY KEY,
    last_open TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE IF NOT EXISTS ratings (
    channel_id BIGINT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    score      INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
    comment    TEXT,
    rated_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    id         SERIAL PRIMARY KEY,
    channel_id BIGINT NOT NULL REFERENCES tickets(channel_id) ON DELETE CASCADE,
    staff_id   BIGINT NOT NULL,
    note       TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
"""

class Database:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        dsn = self._dsn.replace("postgres://", "postgresql://", 1)
        self._pool = await asyncpg.create_pool(dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        log.info("Database PostgreSQL connesso con successo.")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def create_ticket(self, channel_id: int, user_id: int, categoria: str, mc_name: str, subject: str, description: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tickets (channel_id, user_id, categoria, mc_name, subject, description) VALUES ($1, $2, $3, $4, $5, $6)",
                channel_id, user_id, categoria, mc_name, subject, description
            )

    async def get_ticket(self, channel_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tickets WHERE channel_id = $1", channel_id)
            return dict(row) if row else None

    async def update_ticket_status(self, channel_id: int, status: TicketStatus, closed_by: Optional[int] = None, close_reason: Optional[str] = None) -> None:
        async with self._pool.acquire() as conn:
            if status == TicketStatus.CLOSED:
                await conn.execute(
                    "UPDATE tickets SET status = $1, closed_by = $2, closed_at = CURRENT_TIMESTAMP, close_reason = $3 WHERE channel_id = $4",
                    status.value, closed_by, close_reason, channel_id
                )
            else:
                await conn.execute("UPDATE tickets SET status = $1 WHERE channel_id = $2", status.value, channel_id)

    async def save_transcript(self, channel_id: int, content: bytes) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET transcript = $1 WHERE channel_id = $2", content, channel_id)

    async def claim_ticket(self, channel_id: int, staff_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET claimed_by = $1 WHERE channel_id = $2", staff_id, channel_id)

    async def set_priority(self, channel_id: int, priority: Priority) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET priority = $1 WHERE channel_id = $2", priority.value, channel_id)

    async def touch_last_message(self, channel_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tickets SET last_message = CURRENT_TIMESTAMP WHERE channel_id = $1 AND status = 'open'",
                channel_id
            )

    async def count_open_tickets(self, user_id: int) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE user_id = $1 AND status IN ('open','closing')", user_id)

    async def load_open_channel_ids(self) -> set[int]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_id FROM tickets WHERE status = 'open'")
            return {r['channel_id'] for r in rows}

    async def get_inactive_tickets(self, cutoff: datetime.datetime) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT channel_id FROM tickets WHERE status = 'open' AND last_message < $1", cutoff)

    async def is_blacklisted(self, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT 1 FROM blacklist WHERE user_id = $1", user_id) is not None

    async def blacklist_add(self, user_id: int, reason: str, added_by: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO blacklist (user_id, reason, added_by) VALUES ($1, $2, $3)
                   ON CONFLICT(user_id) DO UPDATE SET reason = EXCLUDED.reason, added_by = EXCLUDED.added_by, added_at = CURRENT_TIMESTAMP""",
                user_id, reason, added_by
            )

    async def blacklist_remove(self, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM blacklist WHERE user_id = $1", user_id)
            return "DELETE 1" in res

    async def blacklist_list(self) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM blacklist ORDER BY added_at DESC LIMIT 25")

    async def check_cooldown(self, user_id: int) -> int:
        async with self._pool.acquire() as conn:
            last_open = await conn.fetchval("SELECT last_open FROM cooldowns WHERE user_id = $1", user_id)
            if not last_open:
                return 0
            now = utcnow()
            if last_open.tzinfo is None:
                last_open = last_open.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now - last_open).total_seconds()
            return max(0, cfg.COOLDOWN_SECONDS - int(elapsed))

    async def update_cooldown(self, user_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cooldowns (user_id, last_open) VALUES ($1, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET last_open = CURRENT_TIMESTAMP",
                user_id
            )

    async def add_rating(self, channel_id: int, user_id: int, score: int, comment: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ratings (channel_id, user_id, score, comment) VALUES ($1, $2, $3, $4) ON CONFLICT(channel_id) DO NOTHING",
                channel_id, user_id, score, comment
            )

    async def add_note(self, channel_id: int, staff_id: int, note: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("INSERT INTO ticket_notes (channel_id, staff_id, note) VALUES ($1, $2, $3)", channel_id, staff_id, note)

    async def get_notes(self, channel_id: int) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM ticket_notes WHERE channel_id = $1 ORDER BY created_at ASC", channel_id)

    async def bump_stat(self, staff_id: int, claimed: bool = False, closed: bool = False) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute("INSERT INTO ticket_stats (staff_id) VALUES ($1) ON CONFLICT (staff_id) DO NOTHING", staff_id)
            if claimed:
                await conn.execute("UPDATE ticket_stats SET claimed_count = claimed_count + 1 WHERE staff_id = $1", staff_id)
            if closed:
                await conn.execute("UPDATE ticket_stats SET closed_count = closed_count + 1 WHERE staff_id = $1", staff_id)

    async def get_stats(self, staff_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM ticket_stats WHERE staff_id = $1", staff_id)
            return dict(row) if row else None

    async def get_top_staff(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM ticket_stats ORDER BY closed_count DESC LIMIT $1", limit)


# ══════════════════════════════════════════════════════════════════
# GLOBALS & HELPERS
# ══════════════════════════════════════════════════════════════════
_close_tasks: dict[int, asyncio.Task] = {}

def get_category_role_id(cat_name: str) -> Optional[int]:
    return cfg.CATEGORIA_ROLES.get(cat_name)

def is_ticket_staff(member: discord.Member, categoria: str) -> bool:
    # L'admin è definito solo dal ruolo specifico, non dal permesso amministratore del server
    if cfg.ADMIN_ROLE_ID != 0 and any(r.id == cfg.ADMIN_ROLE_ID for r in member.roles):
        return True
    if cfg.STAFF_TICKET_ROLE_ID != 0 and any(r.id == cfg.STAFF_TICKET_ROLE_ID for r in member.roles):
        return True
    cat_role_id = get_category_role_id(categoria)
    if cat_role_id and cat_role_id != 0 and any(r.id == cat_role_id for r in member.roles):
        return True
    return False

def _ticket_embed(title: str, description: str, color: Union[discord.Color, int]) -> discord.Embed:
    if isinstance(color, int):
        color = discord.Color(color)
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=utcnow()
    )

def ticket_open_embed(user: discord.User | discord.Member, categoria: str, mc: str, subj: str, desc: str, ch_id: int) -> discord.Embed:
    e = _ticket_embed("🎫 Nuovo Ticket Aperto", f"Grazie per aver aperto un ticket, {user.mention}!", discord.Color.blue())
    e.add_field(name="👤 Utente", value=f"{user} (`{user.id}`)", inline=True)
    e.add_field(name="📁 Categoria", value=categoria, inline=True)
    e.add_field(name="🎮 Minecraft", value=f"`{mc}`", inline=True)
    e.add_field(name="📌 Oggetto", value=subj, inline=False)
    e.add_field(name="📝 Descrizione", value=desc[:1024], inline=False)
    e.set_footer(text=f"ID Canale: {ch_id} | Attendi lo staff.")
    return e

def closing_embed(closer: discord.User | discord.Member, delay: int, reason: Optional[str]) -> discord.Embed:
    desc = f"Il ticket verrà chiuso tra **{format_delta(delay)}**.\n**Chiuso da:** {closer.mention}"
    if reason:
        desc += f"\n**Motivo:** {reason}"
    return _ticket_embed("🔒 Chiusura Programmata", desc, discord.Color.orange())


# ══════════════════════════════════════════════════════════════════
# LOGICA CHIUSURA & TRANSCRIPT
# ══════════════════════════════════════════════════════════════════
async def schedule_close(db: Database, channel: discord.TextChannel, closer: discord.User | discord.Member, reason: Optional[str] = None):
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        return
    if ticket["status"] == TicketStatus.CLOSING.value:
        with contextlib.suppress(discord.HTTPException):
            await channel.send("⚠️ Questo ticket è già in fase di chiusura.", delete_after=10)
        return

    await db.update_ticket_status(channel.id, TicketStatus.CLOSING)
    await channel.send(embed=closing_embed(closer, cfg.CLOSE_DELAY, reason))

    async def _do_close():
        await asyncio.sleep(cfg.CLOSE_DELAY)
        await _finalize_ticket(db, channel, closer, reason)

    task = asyncio.create_task(_do_close())
    _close_tasks[channel.id] = task

async def _finalize_ticket(db: Database, channel: discord.TextChannel, closer: discord.User | discord.Member, reason: Optional[str]):
    ticket = await db.get_ticket(channel.id)
    if not ticket:
        return

    await db.update_ticket_status(channel.id, TicketStatus.CLOSED, closed_by=closer.id, close_reason=reason)
    _close_tasks.pop(channel.id, None)
    
    if isinstance(closer, discord.Member) and is_ticket_staff(closer, ticket["categoria"]):
        with contextlib.suppress(Exception):
            await db.bump_stat(closer.id, closed=True)
            
    await _send_transcript(db, channel, ticket)
    
    target = channel.guild.get_member(ticket["user_id"])
    if target:
        with contextlib.suppress(discord.Forbidden):
            view = RatingView(db, channel.id, target.id)
            await target.send(
                embed=_ticket_embed("⭐ Come ti siamo stati utili?", "Valuta la tua esperienza con il nostro staff.", discord.Color.gold()),
                view=view,
            )
    with contextlib.suppress(discord.NotFound):
        await channel.delete(reason=f"Ticket chiuso da {closer}")

async def _send_transcript(db: Database, channel: discord.TextChannel, ticket_data: dict):
    if not HAS_CHAT_EXPORTER or cfg.LOG_CHANNEL_ID == 0:
        return

    try:
        transcript = await chat_exporter.export(channel)
        if not transcript:
            return
            
        await db.save_transcript(channel.id, transcript.encode())
        
        log_ch = channel.guild.get_channel(cfg.LOG_CHANNEL_ID)
        if not log_ch:
            return
            
        notes = await db.get_notes(channel.id)
        note_text = "\n".join(f"• [{r['created_at'].strftime('%Y-%m-%d %H:%M')}] <@{r['staff_id']}>: {r['note']}" for r in notes) or "Nessuna nota."
        
        embed = _ticket_embed(
            "📋 Transcript Ticket",
            f"**Canale:** `#{channel.name}`\n**Utente:** <@{ticket_data['user_id']}>\n**Categoria:** {ticket_data['categoria']}\n**Oggetto:** {ticket_data['subject']}\n**Note staff:**\n{note_text}",
            discord.Color.blurple(),
        )
        file = discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{channel.name}.html")
        with contextlib.suppress(discord.HTTPException):
            await log_ch.send(embed=embed, file=file)
    except Exception:
        log.exception("Transcript export fallito per #%s", channel.name)


# ══════════════════════════════════════════════════════════════════
# VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════
class MainPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apri un Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="persistent:open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=_ticket_embed("📂 Scegli la Categoria", "Seleziona la categoria del tuo ticket e poi clicca **Conferma**.", discord.Color.blue()),
            view=CategorySelectView(),
            ephemeral=True
        )

class CategorySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self._categoria: Optional[str] = None

    @discord.ui.select(
        placeholder="📁 Categoria...",
        options=[discord.SelectOption(label=c, value=c, emoji="📌") for c in cfg.CATEGORIE],
        custom_id="cat_select",
    )
    async def select_cat(self, interaction: discord.Interaction, select: discord.ui.Select):
        self._categoria = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self._categoria
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Conferma e Apri →", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._categoria:
            return await interaction.response.send_message("Seleziona prima una categoria.", ephemeral=True)
        await interaction.response.send_modal(TicketModal(self._categoria))

class TicketModal(discord.ui.Modal):
    mc_name = discord.ui.TextInput(label="Nickname Minecraft", placeholder="Es. Steve123", min_length=3, max_length=16)
    subject = discord.ui.TextInput(label="Oggetto", placeholder="Breve descrizione del problema", min_length=5, max_length=60)
    description = discord.ui.TextInput(label="Descrizione dettagliata", style=discord.TextStyle.long, placeholder="Spiega il problema nel dettaglio...", min_length=20, max_length=1000)

    def __init__(self, categoria: str):
        super().__init__(title=f"📋 Ticket – {categoria}", timeout=300)
        self.categoria = categoria

    async def on_submit(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        user = interaction.user
        guild = interaction.guild

        if not guild:
            return await interaction.response.send_message("❌ Solo nei server.", ephemeral=True)

        if not _MC_NAME_RE.match(self.mc_name.value):
            return await interaction.response.send_message("❌ Nickname Minecraft non valido.", ephemeral=True)
        if await db.is_blacklisted(user.id):
            return await interaction.response.send_message("🚫 Sei in blacklist.", ephemeral=True)
        remaining = await db.check_cooldown(user.id)
        if remaining > 0:
            return await interaction.response.send_message(f"⏳ Attendi {format_delta(remaining)}.", ephemeral=True)
        if await db.count_open_tickets(user.id) >= cfg.MAX_OPEN_TICKETS:
            return await interaction.response.send_message(f"⚠️ Hai già {cfg.MAX_OPEN_TICKETS} ticket aperti.", ephemeral=True)
        
        category_channel = guild.get_channel(cfg.CATEGORY_GENERAL)
        if not isinstance(category_channel, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Errore configurazione categoria.", ephemeral=True)
            
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }

        staff_role = guild.get_role(cfg.STAFF_TICKET_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True)
        
        category_role_id = get_category_role_id(self.categoria)
        category_role = guild.get_role(category_role_id) if category_role_id else None
        if category_role and category_role.id != 0:
            overwrites[category_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True)
            
        admin_role = guild.get_role(cfg.ADMIN_ROLE_ID)
        if admin_role and admin_role.id != 0:
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True, manage_channels=True)
            
        channel_name = safe_channel_name(f"{user.name}-{self.categoria}")
        
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channel = await guild.create_text_channel(
                channel_name, category=category_channel, overwrites=overwrites,
                topic=f"Ticket di {user.name} | Categoria: {self.categoria} | MC: {self.mc_name.value}",
                reason=f"Ticket aperto da {user}",
            )
            
            await db.create_ticket(channel.id, user.id, self.categoria, self.mc_name.value, self.subject.value, self.description.value)
            await db.update_cooldown(user.id)
            
            mentions = []
            if staff_role: mentions.append(staff_role.mention)
            if category_role and category_role.id != 0 and (not staff_role or category_role.id != staff_role.id):
                mentions.append(category_role.mention)
                
            await channel.send(
                content=f"{user.mention} {' '.join(mentions)}",
                embed=ticket_open_embed(user, self.categoria, self.mc_name.value, self.subject.value, self.description.value, channel.id),
                view=TicketControlView(),
            )
            await interaction.followup.send(f"✅ Ticket aperto in {channel.mention}!", ephemeral=True)
        except Exception:
            log.exception("Errore apertura ticket.")
            await interaction.followup.send("❌ Errore durante l'apertura.", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Non un ticket.", ephemeral=True)
            return False
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            await interaction.response.send_message("❌ Solo staff.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Chiudi", style=discord.ButtonStyle.red, emoji="🔒", custom_id="persistent:close", row=0)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Non aperto.", ephemeral=True)
        await interaction.response.send_modal(CloseModal())

    @discord.ui.button(label="Prendi in Carico", style=discord.ButtonStyle.blurple, emoji="🙋", custom_id="persistent:claim", row=0)
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["claimed_by"]:
            return await interaction.response.send_message(f"⚠️ Già in carico a <@{ticket['claimed_by']}>.", ephemeral=True)
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await db.bump_stat(interaction.user.id, claimed=True)
        await interaction.response.send_message(embed=_ticket_embed("🙋 Preso in Carico", f"{interaction.user.mention} ha preso in carico il ticket.", discord.Color.blurple()))

    @discord.ui.button(label="Priorità", style=discord.ButtonStyle.gray, emoji="↕️", custom_id="persistent:priority", row=0)
    async def priority_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message("Seleziona Priorità:", view=PriorityView(interaction.channel_id), ephemeral=True)

    @discord.ui.button(label="Nota Interna", style=discord.ButtonStyle.gray, emoji="📝", custom_id="persistent:note", row=1)
    async def note_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddNoteModal())

    @discord.ui.button(label="Aggiungi Utente", style=discord.ButtonStyle.green, emoji="➕", custom_id="persistent:add_user", row=1)
    async def add_user_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddUserModal())

class CloseModal(discord.ui.Modal, title="🔒 Chiudi Ticket"):
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.long, required=False, placeholder="Opzionale...", max_length=300)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await schedule_close(interaction.client.db, interaction.channel, interaction.user, self.reason.value or None)
        await interaction.followup.send("✅ Chiusura avviata.", ephemeral=True)

class AddNoteModal(discord.ui.Modal, title="📝 Nota Interna"):
    note = discord.ui.TextInput(label="Nota", style=discord.TextStyle.long, min_length=1, max_length=500)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.client.db.add_note(interaction.channel_id, interaction.user.id, self.note.value)
        await interaction.response.send_message("✅ Nota aggiunta.", ephemeral=True)

class AddUserModal(discord.ui.Modal, title="➕ Aggiungi Utente"):
    user_id = discord.ui.TextInput(label="ID Utente", min_length=17, max_length=20)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id.value)
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
            await interaction.followup.send(f"✅ {member.mention} aggiunto.", ephemeral=False)
        except Exception:
            await interaction.followup.send("❌ Utente non trovato.", ephemeral=True)

class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=60)
        self.channel_id = channel_id
    @discord.ui.select(
        placeholder="Priorità...",
        options=[discord.SelectOption(label=p.value, value=p.name, emoji=p.icon) for p in Priority]
    )
    async def select_priority(self, interaction: discord.Interaction, select: discord.ui.Select):
        p = Priority[select.values[0]]
        await interaction.client.db.set_priority(self.channel_id, p)
        await interaction.response.edit_message(content=f"✅ Priorità: **{p.icon} {p.value}**", view=None)

class RatingView(discord.ui.View):
    def __init__(self, db: Database, channel_id: int, user_id: int):
        super().__init__(timeout=300)
        self.db = db
        self.channel_id = channel_id
        self.user_id = user_id
    @discord.ui.select(
        placeholder="Valuta ⭐",
        options=[discord.SelectOption(label=f"{i} Stelle", value=str(i), emoji="⭐") for i in range(5, 0, -1)]
    )
    async def select_rating(self, interaction: discord.Interaction, select: discord.ui.Select):
        score = int(select.values[0])
        await self.db.add_rating(self.channel_id, self.user_id, score, "Nessun commento")
        await interaction.response.edit_message(content=f"✅ Feedback di **{score}** stelle ricevuto!", view=None)


# ══════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════
class TicketCog(commands.Cog):
    def __init__(self, bot: TicketBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        ticket = await self.bot.db.get_ticket(message.channel.id)
        if ticket and ticket["status"] == TicketStatus.OPEN.value:
            await self.bot.db.touch_last_message(message.channel.id)

    @tasks.loop(minutes=cfg.AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close_task(self):
        cutoff = utcnow() - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
        inactive = await self.bot.db.get_inactive_tickets(cutoff)
        for row in inactive:
            channel = self.bot.get_channel(row['channel_id'])
            if isinstance(channel, discord.TextChannel):
                await schedule_close(self.bot.db, channel, self.bot.user, "Inattività")

    @app_commands.command(name="ticket_setup", description="Invia il messaggio di apertura ticket.")
    async def ticket_setup(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user, "Altro"): # Controllo tramite ruolo Admin/Staff
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        embed = _ticket_embed("🎫 Supporto Ticket", "Clicca sotto per aprire un ticket.", discord.Color.blue())
        await interaction.channel.send(embed=embed, view=MainPersistentView())
        await interaction.response.send_message("✅ Setup completato.", ephemeral=True)

    @app_commands.command(name="blacklist_add", description="Blacklist utente.")
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.User, reason: str = "Nessun motivo"):
        if not is_ticket_staff(interaction.user, "Altro"):
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        await self.bot.db.blacklist_add(user.id, reason, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} in blacklist.")

    @app_commands.command(name="blacklist_remove", description="Rimuovi blacklist.")
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.User):
        if not is_ticket_staff(interaction.user, "Altro"):
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        if await self.bot.db.blacklist_remove(user.id):
            await interaction.response.send_message(f"✅ {user.mention} rimosso.")
        else:
            await interaction.response.send_message("⚠️ Non in blacklist.", ephemeral=True)

    @app_commands.command(name="stats", description="Mostra le tue statistiche staff.")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        data = await self.bot.db.get_stats(target.id)
        if not data:
            return await interaction.response.send_message("Nessuna statistica trovata.", ephemeral=True)
        
        embed = _ticket_embed(f"📊 Statistiche Staff: {target.display_name}", "", discord.Color.green())
        embed.add_field(name="🙋 Presi in carico", value=str(data['claimed_count']))
        embed.add_field(name="🔒 Chiusi", value=str(data['closed_count']))
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="topstaff", description="Classifica staff per ticket chiusi.")
    async def topstaff(self, interaction: discord.Interaction):
        top = await self.bot.db.get_top_staff(10)
        if not top:
            return await interaction.response.send_message("Classifica vuota.", ephemeral=True)
        
        desc = ""
        for i, row in enumerate(top, 1):
            desc += f"{i}. <@{row['staff_id']}> — **{row['closed_count']}** chiusi\n"
        
        embed = _ticket_embed("🏆 Top Staff", desc, discord.Color.gold())
        await interaction.response.send_message(embed=embed)


# ══════════════════════════════════════════════════════════════════
# BOT CLASS
# ══════════════════════════════════════════════════════════════════
class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db: Database = Database(cfg.DATABASE_URL)

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.add_view(MainPersistentView())
        self.add_view(TicketControlView())
        
        cog = TicketCog(self)
        await self.add_cog(cog)
        cog.auto_close_task.start()
        
        log.info("Sincronizzazione comandi slash...")
        guild_obj = discord.Object(id=cfg.GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        synced = await self.tree.sync(guild=guild_obj)
        log.info("Sync completata: %d comandi.", len(synced))

    async def on_ready(self) -> None:
        log.info("✅ Bot online: %s", self.user)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="i ticket"))

    async def close(self) -> None:
        for task in _close_tasks.values():
            task.cancel()
        await self.db.close()
        await super().close()

def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ Non hai i permessi necessari."
        else:
            log.exception("Errore slash:")
            msg = "⚠️ Errore durante l'esecuzione."
        
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
