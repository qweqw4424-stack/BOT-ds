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
    GUILD_ID: int             = int(os.environ.get("GUILD_ID", "0"))
    LOG_CHANNEL_ID: int       = 1517107696200061040
    CATEGORY_GENERAL: int     = 1517099911269580891
    STAFF_TICKET_ROLE_ID: int = 1517123223836295188
    ADMIN_ROLE_ID: int        = 1517091767399350342

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
        "Supporto Tecnico":  1499713651576406020,
        "Report Utente":     1499713651576406020,
        "Candidatura Staff": 1499713651576406024,
        "Unisciti al Team":  1499713651576406024,
        "Altro":             1499713651576406020,
    })

    def validate(self) -> None:
        errors = []
        if not self.TOKEN or self.TOKEN == "IL_TUO_TOKEN_QUI":
            errors.append("TOKEN non impostato")
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL non impostato (Necessario per PostgreSQL su Railway)")
        if self.GUILD_ID == 0:
            errors.append("GUILD_ID non impostato")
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
    transcript   BYTEA -- Salvataggio del file transcript nel DB
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
        # Railway DATABASE_URL può iniziare con postgres://, asyncpg preferisce postgresql://
        dsn = self._dsn.replace("postgres://", "postgresql://", 1)
        self._pool = await asyncpg.create_pool(dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        log.info("Database PostgreSQL connesso con successo.")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def create_ticket(self, channel_id, user_id, categoria, mc_name, subject, description):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tickets (channel_id, user_id, categoria, mc_name, subject, description) VALUES ($1, $2, $3, $4, $5, $6)",
                channel_id, user_id, categoria, mc_name, subject, description
            )

    async def get_ticket(self, channel_id) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM tickets WHERE channel_id = $1", channel_id)
            return dict(row) if row else None

    async def update_ticket_status(self, channel_id, status, closed_by=None, close_reason=None):
        async with self._pool.acquire() as conn:
            if status == TicketStatus.CLOSED:
                await conn.execute(
                    "UPDATE tickets SET status = $1, closed_by = $2, closed_at = CURRENT_TIMESTAMP, close_reason = $3 WHERE channel_id = $4",
                    status.value, closed_by, close_reason, channel_id
                )
            else:
                await conn.execute("UPDATE tickets SET status = $1 WHERE channel_id = $2", status.value, channel_id)

    async def save_transcript(self, channel_id: int, content: bytes):
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET transcript = $1 WHERE channel_id = $2", content, channel_id)

    async def claim_ticket(self, channel_id, staff_id):
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET claimed_by = $1 WHERE channel_id = $2", staff_id, channel_id)

    async def set_priority(self, channel_id, priority):
        async with self._pool.acquire() as conn:
            await conn.execute("UPDATE tickets SET priority = $1 WHERE channel_id = $2", priority.value, channel_id)

    async def touch_last_message(self, channel_id):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tickets SET last_message = CURRENT_TIMESTAMP WHERE channel_id = $1 AND status = 'open'",
                channel_id
            )

    async def count_open_tickets(self, user_id):
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM tickets WHERE user_id = $1 AND status IN ('open','closing')", user_id)

    async def load_open_channel_ids(self):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_id FROM tickets WHERE status = 'open'")
            return {r['channel_id'] for r in rows}

    async def get_inactive_tickets(self, cutoff: datetime.datetime):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_id FROM tickets WHERE status = 'open' AND last_message < $1", cutoff)
            return rows

    async def get_open_tickets_by_priority(self):
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                """SELECT * FROM tickets WHERE status IN ('open', 'closing')
                   ORDER BY CASE priority WHEN 'Urgente' THEN 0 WHEN 'Alta' THEN 1 WHEN 'Media' THEN 2 WHEN 'Bassa' THEN 3 ELSE 4 END ASC, opened_at ASC"""
            )

    async def is_blacklisted(self, user_id):
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT 1 FROM blacklist WHERE user_id = $1", user_id) is not None

    async def blacklist_add(self, user_id, reason, added_by):
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO blacklist (user_id, reason, added_by) VALUES ($1, $2, $3)
                   ON CONFLICT(user_id) DO UPDATE SET reason = EXCLUDED.reason, added_by = EXCLUDED.added_by, added_at = CURRENT_TIMESTAMP""",
                user_id, reason, added_by
            )

    async def blacklist_remove(self, user_id):
        async with self._pool.acquire() as conn:
            res = await conn.execute("DELETE FROM blacklist WHERE user_id = $1", user_id)
            return "DELETE 1" in res

    async def blacklist_list(self):
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM blacklist ORDER BY added_at DESC LIMIT 25")

    async def check_cooldown(self, user_id):
        async with self._pool.acquire() as conn:
            last_open = await conn.fetchval("SELECT last_open FROM cooldowns WHERE user_id = $1", user_id)
            if not last_open:
                return 0
            now = datetime.datetime.now(datetime.timezone.utc)
            if last_open.tzinfo is None:
                last_open = last_open.replace(tzinfo=datetime.timezone.utc)
            elapsed = (now - last_open).total_seconds()
            return max(0, cfg.COOLDOWN_SECONDS - int(elapsed))

    async def update_cooldown(self, user_id):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cooldowns (user_id, last_open) VALUES ($1, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET last_open = CURRENT_TIMESTAMP",
                user_id
            )

    async def add_rating(self, channel_id, user_id, score, comment):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ratings (channel_id, user_id, score, comment) VALUES ($1, $2, $3, $4) ON CONFLICT(channel_id) DO NOTHING",
                channel_id, user_id, score, comment
            )

    async def add_note(self, channel_id, staff_id, note):
        async with self._pool.acquire() as conn:
            await conn.execute("INSERT INTO ticket_notes (channel_id, staff_id, note) VALUES ($1, $2, $3)", channel_id, staff_id, note)

    async def get_notes(self, channel_id):
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM ticket_notes WHERE channel_id = $1 ORDER BY created_at ASC", channel_id)

    async def bump_stat(self, staff_id, claimed=False, closed=False):
        async with self._pool.acquire() as conn:
            await conn.execute("INSERT INTO ticket_stats (staff_id) VALUES ($1) ON CONFLICT (staff_id) DO NOTHING", staff_id)
            if claimed:
                await conn.execute("UPDATE ticket_stats SET claimed_count = claimed_count + 1 WHERE staff_id = $1", staff_id)
            if closed:
                await conn.execute("UPDATE ticket_stats SET closed_count = closed_count + 1 WHERE staff_id = $1", staff_id)

    async def get_stats(self, staff_id):
        async with self._pool.acquire() as conn:
            return await conn.fetchrow("SELECT * FROM ticket_stats WHERE staff_id = $1", staff_id)

    async def get_top_staff(self, limit=10):
        async with self._pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM ticket_stats ORDER BY closed_count DESC LIMIT $1", limit)


# ══════════════════════════════════════════════════════════════════
# GLOBALS & HELPERS
# ══════════════════════════════════════════════════════════════════
_open_channel_ids: set[int] = set()
_close_tasks: dict[int, asyncio.Task] = {}

def get_category_role_id(cat_name: str) -> Optional[int]:
    return cfg.CATEGORIA_ROLES.get(cat_name)

def is_ticket_staff(member: discord.Member, categoria: str) -> bool:
    if member.guild_permissions.administrator:
        return True
    if any(r.id == cfg.ADMIN_ROLE_ID for r in member.roles):
        return True
    if any(r.id == cfg.STAFF_TICKET_ROLE_ID for r in member.roles):
        return True
    cat_role_id = get_category_role_id(categoria)
    if cat_role_id and any(r.id == cat_role_id for r in member.roles):
        return True
    return False

def _ticket_embed(title: str, description: str, color: Union[discord.Color, int]) -> discord.Embed:
    if isinstance(color, int):
        color = discord.Color(color)
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.datetime.now(datetime.timezone.utc)
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
    _open_channel_ids.discard(channel.id)
    _close_tasks.pop(channel.id, None)
    
    if isinstance(closer, discord.Member) and is_ticket_staff(closer, ticket["categoria"]):
        with contextlib.suppress(Exception):
            await db.bump_stat(closer.id, closed=True)
            
    await _send_transcript(db, channel)
    
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

async def _send_transcript(db: Database, channel: discord.TextChannel):
    if not HAS_CHAT_EXPORTER:
        return
    try:
        transcript = await chat_exporter.export(channel)
        if not transcript:
            return
            
        # SALVATAGGIO NEL DB (PostgreSQL BYTEA)
        await db.save_transcript(channel.id, transcript.encode())
        
        log_ch = channel.guild.get_channel(cfg.LOG_CHANNEL_ID)
        if not log_ch:
            return
            
        notes = await db.get_notes(channel.id)
        note_text = "\n".join(f"• [{r['created_at'].strftime('%Y-%m-%d %H:%M')}] <@{r['staff_id']}>: {r['note']}" for r in notes) or "Nessuna nota."
        
        embed = _ticket_embed(
            "📋 Transcript Ticket",
            f"**Canale:** `#{channel.name}` (`{channel.id}`)\n**Note staff:**\n{note_text}",
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
            ephemeral=True,
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
        if not _MC_NAME_RE.match(self.mc_name.value):
            return await interaction.response.send_message("❌ Nickname Minecraft non valido: usa solo lettere, numeri e `_` (3-16 caratteri).", ephemeral=True)
        if await db.is_blacklisted(user.id):
            return await interaction.response.send_message("🚫 Sei in blacklist e non puoi aprire ticket.", ephemeral=True)
        remaining = await db.check_cooldown(user.id)
        if remaining > 0:
            return await interaction.response.send_message(f"⏳ Attendi ancora **{format_delta(remaining)}** prima di aprire un altro ticket.", ephemeral=True)
        if await db.count_open_tickets(user.id) >= cfg.MAX_OPEN_TICKETS:
            return await interaction.response.send_message(f"⚠️ Hai già **{cfg.MAX_OPEN_TICKETS}** ticket aperti.", ephemeral=True)
        
        category = guild.get_channel(cfg.CATEGORY_GENERAL)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.response.send_message("❌ Categoria ticket non trovata.", ephemeral=True)
            
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True),
        }
        staff_role = guild.get_role(cfg.STAFF_TICKET_ROLE_ID)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True)
        
        category_role_id = get_category_role_id(self.categoria)
        category_role = guild.get_role(category_role_id) if category_role_id else None
        if category_role and (not staff_role or category_role.id != staff_role.id):
            overwrites[category_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True)
            
        admin_role = guild.get_role(cfg.ADMIN_ROLE_ID)
        if admin_role and admin_role.id not in (cfg.STAFF_TICKET_ROLE_ID, category_role_id):
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, manage_messages=True, manage_channels=True)
            
        channel_name = safe_channel_name(f"{user.name}-{self.categoria}")
        channel = await guild.create_text_channel(
            channel_name, category=category, overwrites=overwrites,
            topic=f"Ticket di {user.name} ({user.id}) | Categoria: {self.categoria} | MC: {self.mc_name.value}",
            reason=f"Ticket aperto da {user} ({user.id})",
        )
        
        await db.create_ticket(channel.id, user.id, self.categoria, self.mc_name.value, self.subject.value, self.description.value)
        _open_channel_ids.add(channel.id)
        await db.update_cooldown(user.id)
        
        mentions = []
        if staff_role: mentions.append(staff_role.mention)
        if category_role and (not staff_role or category_role.id != staff_role.id):
            mentions.append(category_role.mention)
            
        await channel.send(
            content=f"{user.mention} {' '.join(mentions)}",
            embed=ticket_open_embed(user, self.categoria, self.mc_name.value, self.subject.value, self.description.value, channel.id),
            view=TicketControlView(),
        )
        await interaction.response.send_message(f"✅ Ticket aperto in {channel.mention}!", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Chiudi", style=discord.ButtonStyle.red, emoji="🔒", custom_id="persistent:close", row=0)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("❌ Solo lo staff può chiudere i ticket.", ephemeral=True)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Questo ticket non è aperto.", ephemeral=True)
        await interaction.response.send_modal(CloseModal())

    @discord.ui.button(label="Prendi in Carico", style=discord.ButtonStyle.blurple, emoji="🙋", custom_id="persistent:claim", row=0)
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Ticket non trovato.", ephemeral=True)
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("❌ Solo lo staff può prendere in carico i ticket.", ephemeral=True)
        if ticket["claimed_by"]:
            claimer_id = ticket["claimed_by"]
            if claimer_id == interaction.user.id:
                return await interaction.response.send_message("ℹ️ Hai già preso in carico questo ticket.", ephemeral=True)
            return await interaction.response.send_message(f"⚠️ Già in carico a <@{claimer_id}>.", ephemeral=True)
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await db.bump_stat(interaction.user.id, claimed=True)
        await interaction.response.send_message(embed=_ticket_embed("🙋 Ticket Preso in Carico", f"{interaction.user.mention} ha preso in carico questo ticket.", discord.Color.blurple()))

    @discord.ui.button(label="Priorità", style=discord.ButtonStyle.gray, emoji="↕️", custom_id="persistent:priority", row=0)
    async def priority_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Ticket non trovato.", ephemeral=True)
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("❌ Solo lo staff può cambiare la priorità.", ephemeral=True)
        await interaction.response.send_message("Seleziona la nuova priorità:", view=PriorityView(interaction.channel_id), ephemeral=True)

    @discord.ui.button(label="Nota Interna", style=discord.ButtonStyle.gray, emoji="📝", custom_id="persistent:note", row=1)
    async def note_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("❌ Solo lo staff può aggiungere note.", ephemeral=True)
        await interaction.response.send_modal(AddNoteModal())

    @discord.ui.button(label="Aggiungi Utente", style=discord.ButtonStyle.green, emoji="➕", custom_id="persistent:add_user", row=1)
    async def add_user_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("❌ Solo lo staff può aggiungere utenti.", ephemeral=True)
        await interaction.response.send_modal(AddUserModal())

class CloseModal(discord.ui.Modal, title="🔒 Chiudi Ticket"):
    reason = discord.ui.TextInput(label="Motivo della chiusura", style=discord.TextStyle.long, required=False, placeholder="(Opzionale) Inserisci il motivo...", max_length=300)
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await schedule_close(interaction.client.db, interaction.channel, interaction.user, self.reason.value or None)
        await interaction.followup.send("✅ Chiusura avviata.", ephemeral=True)

class AddNoteModal(discord.ui.Modal, title="📝 Aggiungi Nota Interna"):
    note = discord.ui.TextInput(label="Nota", style=discord.TextStyle.long, placeholder="Inserisci una nota interna...", min_length=1, max_length=500)
    async def on_submit(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        await db.add_note(interaction.channel_id, interaction.user.id, self.note.value)
        await interaction.response.send_message("✅ Nota aggiunta.", ephemeral=True)

class AddUserModal(discord.ui.Modal, title="➕ Aggiungi Utente"):
    user_id = discord.ui.TextInput(label="ID Utente", placeholder="Inserisci l'ID dell'utente da aggiungere...", min_length=17, max_length=20)
    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value)
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
            await interaction.response.send_message(f"✅ {member.mention} aggiunto al ticket.", ephemeral=False)
        except Exception:
            await interaction.response.send_message("❌ Utente non trovato o ID non valido.", ephemeral=True)

class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=60)
        self.channel_id = channel_id
    @discord.ui.select(
        placeholder="Seleziona Priorità...",
        options=[discord.SelectOption(label=p.value, value=p.name, emoji=p.icon) for p in Priority]
    )
    async def select_priority(self, interaction: discord.Interaction, select: discord.ui.Select):
        db: Database = interaction.client.db
        p = Priority[select.values[0]]
        await db.set_priority(self.channel_id, p)
        await interaction.response.edit_message(content=f"✅ Priorità impostata a: **{p.icon} {p.value}**", view=None)

class RatingView(discord.ui.View):
    def __init__(self, db: Database, channel_id: int, user_id: int):
        super().__init__(timeout=300)
        self.db = db
        self.channel_id = channel_id
        self.user_id = user_id
    @discord.ui.select(
        placeholder="Valuta da 1 a 5 ⭐",
        options=[discord.SelectOption(label=f"{i} Stelle", value=str(i), emoji="⭐") for i in range(5, 0, -1)]
    )
    async def select_rating(self, interaction: discord.Interaction, select: discord.ui.Select):
        score = int(select.values[0])
        await self.db.add_rating(self.channel_id, self.user_id, score, "Nessun commento")
        await interaction.response.edit_message(content=f"✅ Grazie per il tuo feedback di **{score}** stelle!", view=None)


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
        if message.channel.id in _open_channel_ids:
            await self.bot.db.touch_last_message(message.channel.id)

    @tasks.loop(minutes=cfg.AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close_task(self):
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
        inactive = await self.bot.db.get_inactive_tickets(cutoff)
        for row in inactive:
            ch_id = row['channel_id']
            channel = self.bot.get_channel(ch_id)
            if isinstance(channel, discord.TextChannel):
                await schedule_close(self.bot.db, channel, self.bot.user, "Chiusura automatica per inattività")

    @app_commands.command(name="ticket_setup", description="Invia il messaggio persistente per l'apertura dei ticket.")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        embed = _ticket_embed(
            "🎫 Supporto Ticket",
            "Clicca il pulsante qui sotto per aprire un ticket di assistenza.\nLo staff ti risponderà il prima possibile.",
            discord.Color.blue()
        )
        await interaction.channel.send(embed=embed, view=MainPersistentView())
        await interaction.response.send_message("✅ Setup completato.", ephemeral=True)

    @app_commands.command(name="blacklist_add", description="Aggiunge un utente alla blacklist.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.User, reason: str = "Nessun motivo"):
        await self.bot.db.blacklist_add(user.id, reason, interaction.user.id)
        await interaction.response.send_message(f"✅ {user.mention} aggiunto alla blacklist.\n**Motivo:** {reason}")

    @app_commands.command(name="blacklist_remove", description="Rimuove un utente dalla blacklist.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.User):
        removed = await self.bot.db.blacklist_remove(user.id)
        if removed:
            await interaction.response.send_message(f"✅ {user.mention} rimosso dalla blacklist.")
        else:
            await interaction.response.send_message("⚠️ L'utente non è in blacklist.", ephemeral=True)


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
        try:
            await self.db.connect()
        except Exception:
            log.exception("Impossibile connettersi al database PostgreSQL.")
            raise
            
        _open_channel_ids.update(await self.db.load_open_channel_ids())
        
        self.add_view(MainPersistentView())
        self.add_view(TicketControlView())
        
        cog = TicketCog(self)
        await self.add_cog(cog)
        cog.auto_close_task.start()
        
        log.info("Sincronizzazione comandi slash sul server %s...", cfg.GUILD_ID)
        guild_obj = discord.Object(id=cfg.GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        synced = await self.tree.sync(guild=guild_obj)
        log.info("Sync completata: %d comandi registrati.", len(synced))

    async def on_ready(self) -> None:
        log.info("✅ Bot online come %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="i ticket | /ticket_setup"))

    async def close(self) -> None:
        for task in _close_tasks.values():
            task.cancel()
        await self.db.close()
        await super().close()

def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Errore slash '%s': %s", getattr(interaction.command, "name", "?"), error)
        msg = "⚠️ Si è verificato un errore durante l'esecuzione del comando."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
