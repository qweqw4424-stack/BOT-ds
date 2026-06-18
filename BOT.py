"""
TicketBot – Bot ticket professionale per Discord
=================================================
Modifica la sezione CONFIG qui sotto con i tuoi valori, poi avvia con:
    python bot.py
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
from typing import Optional

import aiosqlite
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
# ★  CONFIGURAZIONE  –  modifica questi valori
# ══════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Config:
    # ── Segreti ───────────────────────────────────────────────────
    TOKEN: str = os.environ.get("BOT_TOKEN", "")

    # ── IDs ───────────────────────────────────────────────────────
    GUILD_ID: int             = int(os.environ.get("GUILD_ID", "0"))  # ID del server Discord
    LOG_CHANNEL_ID: int       = 1517107696200061040   # Canale dove inviare i transcript
    CATEGORY_GENERAL: int     = 1517099911269580891   # Categoria Discord per i canali ticket
    STAFF_TICKET_ROLE_ID: int = 1517123223836295188   # Ruolo staff generale che gestisce tutti i ticket
    ADMIN_ROLE_ID: int        = 1517091767399350342   # Ruolo admin (ha tutti i permessi, come administrator)

    # ── Database ──────────────────────────────────────────────────
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "/app/tickets.db")

    # ── Comportamento ─────────────────────────────────────────────
    MAX_OPEN_TICKETS: int        = 2    # Ticket aperti contemporaneamente per utente
    COOLDOWN_SECONDS: int        = 30   # Cooldown (sec) tra un'apertura e la successiva
    AUTO_CLOSE_HOURS: int        = 48   # Ore di inattività prima della chiusura auto
    AUTO_CLOSE_CHECK_MINUTES: int = 30  # Ogni quanti minuti controllare i ticket inattivi
    CLOSE_DELAY: int             = 600  # Secondi di preavviso prima dell'eliminazione

    # ── Categorie ticket ──────────────────────────────────────────
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
        if self.GUILD_ID == 0:
            errors.append("GUILD_ID non impostato")
        if self.LOG_CHANNEL_ID == 0:
            errors.append("LOG_CHANNEL_ID non impostato")
        if self.CATEGORY_GENERAL == 0:
            errors.append("CATEGORY_GENERAL non impostato")
        if self.STAFF_TICKET_ROLE_ID == 0:
            errors.append("STAFF_TICKET_ROLE_ID non impostato")
        if self.ADMIN_ROLE_ID == 0:
            errors.append("ADMIN_ROLE_ID non impostato")
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


def utcnow_naive() -> datetime.datetime:
    return datetime.datetime.utcnow()


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
# DATABASE – repository pattern
# ══════════════════════════════════════════════════════════════════
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tickets (
    channel_id   INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    categoria    TEXT    NOT NULL DEFAULT 'Altro',
    priority     TEXT    NOT NULL DEFAULT 'Bassa',
    status       TEXT    NOT NULL DEFAULT 'open',
    mc_name      TEXT,
    subject      TEXT,
    description  TEXT,
    opened_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    last_message TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    claimed_by   INTEGER,
    closed_by    INTEGER,
    closed_at    TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS blacklist (
    user_id  INTEGER PRIMARY KEY,
    reason   TEXT    NOT NULL DEFAULT 'Nessun motivo',
    added_by INTEGER NOT NULL,
    added_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS ticket_stats (
    staff_id      INTEGER PRIMARY KEY,
    closed_count  INTEGER NOT NULL DEFAULT 0,
    claimed_count INTEGER NOT NULL DEFAULT 0,
    avg_close_min REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id   INTEGER PRIMARY KEY,
    last_open TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ratings (
    channel_id INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    score      INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
    comment    TEXT,
    rated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS ticket_notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES tickets(channel_id) ON DELETE CASCADE,
    staff_id   INTEGER NOT NULL,
    note       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
"""


class Database:
    def __init__(self, path: str):
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        log.info("Database connesso: %s", self._path)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    @contextlib.asynccontextmanager
    async def transaction(self):
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def create_ticket(self, channel_id, user_id, categoria, mc_name, subject, description):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO tickets (channel_id, user_id, categoria, mc_name, subject, description) VALUES (?, ?, ?, ?, ?, ?)",
                (channel_id, user_id, categoria, mc_name, subject, description),
            )

    async def get_ticket(self, channel_id):
        async with self._conn.execute("SELECT * FROM tickets WHERE channel_id = ?", (channel_id,)) as cur:
            return await cur.fetchone()

    async def update_ticket_status(self, channel_id, status, closed_by=None, close_reason=None):
        async with self.transaction():
            if status == TicketStatus.CLOSED:
                await self._conn.execute(
                    "UPDATE tickets SET status = ?, closed_by = ?, closed_at = strftime('%Y-%m-%dT%H:%M:%S','now'), close_reason = ? WHERE channel_id = ?",
                    (status.value, closed_by, close_reason, channel_id),
                )
            else:
                await self._conn.execute("UPDATE tickets SET status = ? WHERE channel_id = ?", (status.value, channel_id))

    async def claim_ticket(self, channel_id, staff_id):
        async with self.transaction():
            await self._conn.execute("UPDATE tickets SET claimed_by = ? WHERE channel_id = ?", (staff_id, channel_id))

    async def set_priority(self, channel_id, priority):
        async with self.transaction():
            await self._conn.execute("UPDATE tickets SET priority = ? WHERE channel_id = ?", (priority.value, channel_id))

    async def touch_last_message(self, channel_id):
        await self._conn.execute(
            "UPDATE tickets SET last_message = strftime('%Y-%m-%dT%H:%M:%S','now') WHERE channel_id = ? AND status = 'open'",
            (channel_id,),
        )
        await self._conn.commit()

    async def count_open_tickets(self, user_id):
        async with self._conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE user_id = ? AND status IN ('open','closing')", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def load_open_channel_ids(self):
        async with self._conn.execute("SELECT channel_id FROM tickets WHERE status = 'open'") as cur:
            rows = await cur.fetchall()
        return {r[0] for r in rows}

    async def get_inactive_tickets(self, cutoff):
        async with self._conn.execute(
            "SELECT channel_id FROM tickets WHERE status = 'open' AND last_message < ?",
            (cutoff.isoformat(timespec="seconds"),),
        ) as cur:
            return await cur.fetchall()

    async def get_open_tickets_by_priority(self):
        async with self._conn.execute(
            """SELECT * FROM tickets WHERE status IN ('open', 'closing')
               ORDER BY CASE priority WHEN 'Urgente' THEN 0 WHEN 'Alta' THEN 1 WHEN 'Media' THEN 2 WHEN 'Bassa' THEN 3 ELSE 4 END ASC, opened_at ASC"""
        ) as cur:
            return await cur.fetchall()

    async def is_blacklisted(self, user_id):
        async with self._conn.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

    async def blacklist_add(self, user_id, reason, added_by):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO blacklist (user_id, reason, added_by) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET reason = excluded.reason, added_by = excluded.added_by, added_at = strftime('%Y-%m-%dT%H:%M:%S','now')",
                (user_id, reason, added_by),
            )

    async def blacklist_remove(self, user_id):
        async with self.transaction():
            cur = await self._conn.execute("DELETE FROM blacklist WHERE user_id = ?", (user_id,))
        return cur.rowcount > 0

    async def blacklist_list(self):
        async with self._conn.execute("SELECT * FROM blacklist ORDER BY added_at DESC LIMIT 25") as cur:
            return await cur.fetchall()

    async def check_cooldown(self, user_id):
        async with self._conn.execute("SELECT last_open FROM cooldowns WHERE user_id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return 0
        last = datetime.datetime.fromisoformat(row[0])
        elapsed = (utcnow_naive() - last).total_seconds()
        return max(0, int(cfg.COOLDOWN_SECONDS - elapsed))

    async def update_cooldown(self, user_id):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO cooldowns (user_id, last_open) VALUES (?, strftime('%Y-%m-%dT%H:%M:%S','now')) ON CONFLICT(user_id) DO UPDATE SET last_open = strftime('%Y-%m-%dT%H:%M:%S','now')",
                (user_id,),
            )

    async def bump_stat(self, staff_id, *, closed=False, claimed=False):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO ticket_stats (staff_id, closed_count, claimed_count) VALUES (?, ?, ?) ON CONFLICT(staff_id) DO UPDATE SET closed_count = ticket_stats.closed_count + excluded.closed_count, claimed_count = ticket_stats.claimed_count + excluded.claimed_count",
                (staff_id, int(closed), int(claimed)),
            )

    async def get_stats(self):
        async with self._conn.execute("SELECT * FROM ticket_stats ORDER BY closed_count DESC LIMIT 10") as cur:
            return await cur.fetchall()

    async def save_rating(self, channel_id, user_id, score, comment=None):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO ratings (channel_id, user_id, score, comment) VALUES (?, ?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET score = excluded.score, comment = excluded.comment, rated_at = strftime('%Y-%m-%dT%H:%M:%S','now')",
                (channel_id, user_id, score, comment),
            )

    async def get_ratings_summary(self):
        async with self._conn.execute("SELECT COUNT(*) as total, ROUND(AVG(score),2) as avg_score FROM ratings") as cur:
            return await cur.fetchone()

    async def add_note(self, channel_id, staff_id, note):
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO ticket_notes (channel_id, staff_id, note) VALUES (?, ?, ?)", (channel_id, staff_id, note)
            )

    async def get_notes(self, channel_id):
        async with self._conn.execute(
            "SELECT * FROM ticket_notes WHERE channel_id = ? ORDER BY created_at", (channel_id,)
        ) as cur:
            return await cur.fetchall()


# ══════════════════════════════════════════════════════════════════
# CHECKS
# ══════════════════════════════════════════════════════════════════
def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == cfg.ADMIN_ROLE_ID for r in member.roles)


def is_staff(member: discord.Member) -> bool:
    if is_admin(member):
        return True
    return any(r.id == cfg.STAFF_TICKET_ROLE_ID for r in member.roles)


def get_category_role_id(categoria: Optional[str]) -> int:
    if not categoria:
        return 0
    return cfg.CATEGORIA_ROLES.get(categoria, 0)


def is_ticket_staff(member: discord.Member, categoria: Optional[str] = None) -> bool:
    if is_staff(member):
        return True
    role_id = get_category_role_id(categoria)
    if role_id:
        return any(r.id == role_id for r in member.roles)
    return False


def staff_only(*, category_aware: bool = False):
    async def predicate(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Questo comando funziona solo all'interno di un server.", ephemeral=True)
            return False
        categoria = None
        if category_aware:
            db: Database = interaction.client.db
            ticket = await db.get_ticket(interaction.channel_id)
            if ticket:
                categoria = ticket["categoria"]
        if is_ticket_staff(member, categoria):
            return True
        await interaction.response.send_message("❌ Non hai i permessi per usare questo comando.", ephemeral=True)
        return False
    return app_commands.check(predicate)


# ══════════════════════════════════════════════════════════════════
# STATE GLOBALE
# ══════════════════════════════════════════════════════════════════
_open_channel_ids: set[int] = set()
_close_tasks: dict[int, asyncio.Task] = {}


# ══════════════════════════════════════════════════════════════════
# EMBED HELPERS
# ══════════════════════════════════════════════════════════════════
def _ticket_embed(title, description, color, *, footer=None, thumbnail_url=None):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=utcnow())
    if footer:
        embed.set_footer(text=footer)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    return embed


def ticket_open_embed(user, categoria, mc_name, subject, description, ticket_id):
    priority = Priority.LOW
    embed = _ticket_embed(
        title=f"{priority.icon} Ticket: {categoria}",
        description=(
            f"**Utente:** {user.mention}\n"
            f"**Nickname MC:** `{mc_name}`\n"
            f"**Oggetto:** {subject}\n\n"
            f"**Descrizione:**\n{description}"
        ),
        color=priority.color,
        footer=f"Ticket #{ticket_id}  •  Priorità: {priority.value}",
        thumbnail_url=user.display_avatar.url,
    )
    return embed


def closing_embed(closer, delay, reason):
    embed = _ticket_embed(
        title="🔒 Ticket in Chiusura",
        description=f"Questo ticket verrà eliminato tra **{format_delta(delay)}**.",
        color=discord.Color.orange(),
        footer=f"Chiuso da: {closer.name}",
    )
    if reason:
        embed.add_field(name="Motivo", value=reason, inline=False)
    return embed


# ══════════════════════════════════════════════════════════════════
# TICKET LIFECYCLE
# ══════════════════════════════════════════════════════════════════
async def schedule_close(db, channel, closer, reason=None):
    if channel.id in _close_tasks:
        _close_tasks[channel.id].cancel()
        del _close_tasks[channel.id]
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        return
    if ticket["status"] == TicketStatus.CLOSING.value:
        await channel.send("⚠️ Questo ticket è già in fase di chiusura.", delete_after=10)
        return
    await db.update_ticket_status(channel.id, TicketStatus.CLOSING)
    await channel.send(embed=closing_embed(closer, cfg.CLOSE_DELAY, reason))

    async def _do_close():
        await asyncio.sleep(cfg.CLOSE_DELAY)
        await _finalize_ticket(db, channel, closer, reason)

    task = asyncio.create_task(_do_close())
    _close_tasks[channel.id] = task


async def _finalize_ticket(db, channel, closer, reason):
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


async def _send_transcript(db, channel):
    if not HAS_CHAT_EXPORTER:
        return
    try:
        transcript = await chat_exporter.export(channel)
    except Exception:
        log.exception("Transcript export fallito per #%s", channel.name)
        return
    if not transcript:
        return
    log_ch = channel.guild.get_channel(cfg.LOG_CHANNEL_ID)
    if not log_ch:
        return
    notes = await db.get_notes(channel.id)
    note_text = "\n".join(f"• [{r['created_at'][:16]}] <@{r['staff_id']}>: {r['note']}" for r in notes) or "Nessuna nota."
    embed = _ticket_embed(
        "📋 Transcript Ticket",
        f"**Canale:** `#{channel.name}` (`{channel.id}`)\n**Note staff:**\n{note_text}",
        discord.Color.blurple(),
    )
    file = discord.File(io.BytesIO(transcript.encode()), filename=f"transcript-{channel.name}.html")
    with contextlib.suppress(discord.HTTPException):
        await log_ch.send(embed=embed, file=file)


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
        if staff_role:
            mentions.append(staff_role.mention)
        if category_role and (not staff_role or category_role.id != staff_role.id):
            mentions.append(category_role.mention)
        await channel.send(
            content=f"{user.mention} {' '.join(mentions)}",
            embed=ticket_open_embed(user, self.categoria, self.mc_name.value, self.subject.value, self.description.value, channel.id),
            view=TicketControlView(),
        )
        await interaction.response.send_message(f"✅ Ticket aperto in {channel.mention}!", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Errore TicketModal: %s", error)
        msg = "Si è verificato un errore durante l'apertura del ticket."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


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
        await interaction.response.send_message(
            embed=_ticket_embed("📝 Nota Aggiunta", f"{interaction.user.mention} ha aggiunto una nota interna.\n\n> {self.note.value}", discord.Color.dark_gray()),
            ephemeral=True,
        )


class AddUserModal(discord.ui.Modal, title="➕ Aggiungi Utente al Ticket"):
    user_id_input = discord.ui.TextInput(label="ID Utente Discord", placeholder="Es. 123456789012345678", min_length=17, max_length=20)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id_input.value)
        except ValueError:
            return await interaction.response.send_message("❌ ID non valido.", ephemeral=True)
        member = interaction.guild.get_member(uid)
        if not member:
            try:
                member = await interaction.guild.fetch_member(uid)
            except discord.NotFound:
                return await interaction.response.send_message("❌ Utente non trovato nel server.", ephemeral=True)
        await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(f"✅ {member.mention} è stato aggiunto al ticket.", ephemeral=False)


class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=60)
        self.channel_id = channel_id

    @discord.ui.select(
        placeholder="Seleziona priorità…",
        options=[discord.SelectOption(label=p.value, value=p.value, emoji=p.icon) for p in Priority],
    )
    async def select_priority(self, interaction: discord.Interaction, select: discord.ui.Select):
        db: Database = interaction.client.db
        priority = Priority(select.values[0])
        await db.set_priority(self.channel_id, priority)
        with contextlib.suppress(discord.HTTPException):
            topic = interaction.channel.topic or ""
            topic = re.sub(r"Priorità: \w+", f"Priorità: {priority.value}", topic)
            if "Priorità:" not in topic:
                topic = f"{topic} | Priorità: {priority.value}"
            await interaction.channel.edit(topic=topic[:1024])
        await interaction.response.edit_message(content=f"{priority.icon} Priorità aggiornata a **{priority.value}**.", view=None)


class RatingView(discord.ui.View):
    def __init__(self, db: Database, channel_id: int, user_id: int):
        super().__init__(timeout=86400)
        self._db = db
        self.channel_id = channel_id
        self.user_id = user_id
        for score in range(1, 6):
            btn = discord.ui.Button(label="⭐" * score, style=discord.ButtonStyle.secondary, custom_id=f"rating:{channel_id}:{score}")
            btn.callback = self._make_callback(score)
            self.add_item(btn)

    def _make_callback(self, score: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("Solo l'utente che ha aperto il ticket può lasciare una valutazione.", ephemeral=True)
            await self._db.save_rating(self.channel_id, self.user_id, score)
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(
                embed=_ticket_embed("Grazie per la valutazione!", f"Hai valutato la tua esperienza con: **{'⭐' * score}** ({score}/5)", discord.Color.gold()),
                view=self,
            )
        return callback


# ══════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════
class TicketCog(commands.Cog, name="Ticket"):
    def __init__(self, bot: "TicketBot"):
        self.bot = bot

    @property
    def db(self) -> Database:
        return self.bot.db

    @app_commands.command(name="ticket_setup", description="[ADMIN] Invia il pannello per aprire ticket")
    async def ticket_setup(self, interaction: discord.Interaction):
        if not (isinstance(interaction.user, discord.Member) and is_admin(interaction.user)):
            return await interaction.response.send_message("❌ Solo gli amministratori.", ephemeral=True)
        embed = _ticket_embed(
            "🎫 Centro Supporto",
            "Benvenuto nel centro supporto!\n\nClicca il pulsante qui sotto per aprire un ticket e ricevere assistenza dallo staff.\n\n**Prima di aprire un ticket:**\n• Assicurati di non avere già ticket aperti\n• Descrivi il problema in modo dettagliato\n• Sii paziente, lo staff risponderà il prima possibile",
            discord.Color.blue(),
        )
        await interaction.channel.send(embed=embed, view=MainPersistentView())
        await interaction.response.send_message("✅ Pannello ticket inviato.", ephemeral=True)

    @app_commands.command(name="blacklist_add", description="[STAFF] Aggiunge un utente alla blacklist")
    @app_commands.describe(user="Utente da bloccare", reason="Motivo")
    @staff_only()
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Nessun motivo"):
        await self.db.blacklist_add(user.id, reason, interaction.user.id)
        await interaction.response.send_message(
            embed=_ticket_embed("🚫 Blacklist Aggiornata", f"{user.mention} aggiunto alla blacklist.\n**Motivo:** {reason}", discord.Color.red(), footer=f"Aggiunto da {interaction.user.name}"),
            ephemeral=True,
        )

    @app_commands.command(name="blacklist_remove", description="[STAFF] Rimuove un utente dalla blacklist")
    @app_commands.describe(user="Utente da sbloccare")
    @staff_only()
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.Member):
        removed = await self.db.blacklist_remove(user.id)
        msg = f"✅ {user.mention} rimosso dalla blacklist." if removed else f"ℹ️ {user.mention} non era in blacklist."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="blacklist_list", description="[STAFF] Mostra la blacklist")
    @staff_only()
    async def blacklist_list(self, interaction: discord.Interaction):
        rows = await self.db.blacklist_list()
        if not rows:
            return await interaction.response.send_message("ℹ️ La blacklist è vuota.", ephemeral=True)
        embed = _ticket_embed("🚫 Blacklist Ticket", "", discord.Color.red())
        for row in rows:
            embed.add_field(name=f"ID: {row['user_id']}", value=f"**Motivo:** {row['reason']}\n**Aggiunto da:** <@{row['added_by']}>\n**Data:** {row['added_at'][:10]}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_stats", description="[STAFF] Mostra statistiche dello staff")
    @staff_only()
    async def ticket_stats(self, interaction: discord.Interaction):
        rows = await self.db.get_stats()
        rating_row = await self.db.get_ratings_summary()
        if not rows:
            return await interaction.response.send_message("ℹ️ Nessuna statistica disponibile.", ephemeral=True)
        embed = _ticket_embed("📊 Statistiche Staff Ticket", "", discord.Color.blue())
        medals = ["🥇", "🥈", "🥉"]
        for i, row in enumerate(rows):
            medal = medals[i] if i < len(medals) else f"#{i+1}"
            embed.add_field(name=f"{medal} <@{row['staff_id']}>", value=f"Chiusi: **{row['closed_count']}** | Presi in carico: **{row['claimed_count']}**", inline=False)
        if rating_row and rating_row["total"] > 0:
            embed.add_field(name="⭐ Valutazioni", value=f"Media: **{rating_row['avg_score']}/5** su {rating_row['total']} valutazioni", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_priority_list", description="[STAFF] Mostra tutti i ticket aperti ordinati per priorità")
    @staff_only()
    async def ticket_priority_list(self, interaction: discord.Interaction):
        rows = await self.db.get_open_tickets_by_priority()
        if not rows:
            return await interaction.response.send_message("ℹ️ Non ci sono ticket aperti al momento.", ephemeral=True)
        embed = _ticket_embed("📈 Ticket Aperti per Priorità", "Elenco dei ticket aperti, dal più urgente al meno urgente.", discord.Color.red())
        for row in rows[:25]:
            priority = Priority(row["priority"])
            claimed = f"<@{row['claimed_by']}>" if row["claimed_by"] else "Nessuno"
            embed.add_field(
                name=f"{priority.icon} #{row['channel_id']}",
                value=f"**Canale:** <#{row['channel_id']}>\n**Categoria:** {row['categoria']}\n**Priorità:** {priority.value}\n**In carico a:** {claimed}\n**Aperto da:** <@{row['user_id']}>",
                inline=False,
            )
        if len(rows) > 25:
            embed.set_footer(text=f"Mostrati 25 di {len(rows)} ticket aperti.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_notes", description="[STAFF] Mostra le note interne del ticket corrente")
    @staff_only(category_aware=True)
    async def ticket_notes(self, interaction: discord.Interaction):
        ticket = await self.db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        notes = await self.db.get_notes(interaction.channel_id)
        if not notes:
            return await interaction.response.send_message("ℹ️ Nessuna nota interna per questo ticket.", ephemeral=True)
        embed = _ticket_embed(f"📝 Note Interne – Ticket #{ticket['channel_id']}", f"**Categoria:** {ticket['categoria']}", discord.Color.dark_gray())
        for note in notes[-25:]:
            embed.add_field(name=f"🗒️ {note['created_at'][:16].replace('T', ' ')}", value=f"<@{note['staff_id']}>: {note['note']}", inline=False)
        if len(notes) > 25:
            embed.set_footer(text=f"Mostrate le ultime 25 di {len(notes)} note.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_info", description="[STAFF] Mostra informazioni sul ticket corrente")
    @staff_only(category_aware=True)
    async def ticket_info(self, interaction: discord.Interaction):
        ticket = await self.db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        priority = Priority(ticket["priority"])
        embed = _ticket_embed(f"{priority.icon} Info Ticket #{ticket['channel_id']}", "", priority.color)
        embed.add_field(name="Utente", value=f"<@{ticket['user_id']}>", inline=True)
        embed.add_field(name="Categoria", value=ticket["categoria"], inline=True)
        embed.add_field(name="Priorità", value=f"{priority.icon} {priority.value}", inline=True)
        embed.add_field(name="Stato", value=ticket["status"].upper(), inline=True)
        embed.add_field(name="Nickname MC", value=ticket["mc_name"] or "N/A", inline=True)
        if ticket["claimed_by"]:
            embed.add_field(name="In carico a", value=f"<@{ticket['claimed_by']}>", inline=True)
        embed.add_field(name="Aperto il", value=ticket["opened_at"][:16].replace("T", " "), inline=True)
        embed.add_field(name="Ultimo msg", value=ticket["last_message"][:16].replace("T", " "), inline=True)
        if ticket["subject"]:
            embed.add_field(name="Oggetto", value=ticket["subject"], inline=False)
        notes = await self.db.get_notes(interaction.channel_id)
        if notes:
            note_lines = "\n".join(f"• `{r['created_at'][:16]}` <@{r['staff_id']}>: {r['note']}" for r in notes)
            embed.add_field(name="📝 Note Staff", value=note_lines[:1024], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="ticket_close", description="[STAFF] Chiude il ticket corrente")
    @app_commands.describe(reason="Motivo della chiusura")
    @staff_only(category_aware=True)
    async def ticket_close(self, interaction: discord.Interaction, reason: str = ""):
        ticket = await self.db.get_ticket(interaction.channel_id)
        if not ticket or ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Questo canale non è un ticket aperto.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await schedule_close(self.db, interaction.channel, interaction.user, reason or None)
        await interaction.followup.send("✅ Chiusura avviata.", ephemeral=True)

    @app_commands.command(name="ticket_transfer", description="[STAFF] Trasferisce il ticket a un altro membro dello staff")
    @app_commands.describe(member="Nuovo responsabile")
    @staff_only(category_aware=True)
    async def ticket_transfer(self, interaction: discord.Interaction, member: discord.Member):
        db = self.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Questo canale non è un ticket.", ephemeral=True)
        if not is_ticket_staff(member, ticket["categoria"]):
            return await interaction.response.send_message("❌ Il membro selezionato non è staff per questo ticket.", ephemeral=True)
        await db.claim_ticket(interaction.channel_id, member.id)
        await interaction.response.send_message(embed=_ticket_embed("🔄 Ticket Trasferito", f"Questo ticket è stato trasferito a {member.mention} da {interaction.user.mention}.", discord.Color.blurple()))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.channel.id in _open_channel_ids:
            await self.db.touch_last_message(message.channel.id)

    @tasks.loop(minutes=cfg.AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close_task(self):
        cutoff = utcnow_naive() - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
        rows = await self.db.get_inactive_tickets(cutoff)
        for row in rows:
            channel = self.bot.get_channel(row[0])
            if channel is None:
                await self.db.update_ticket_status(row[0], TicketStatus.CLOSED)
                _open_channel_ids.discard(row[0])
                continue
            log.info("Auto-chiusura per inattività: canale %s", channel.id)
            await schedule_close(self.db, channel, self.bot.user)

    @auto_close_task.before_loop
    async def before_auto_close(self):
        await self.bot.wait_until_ready()

    @auto_close_task.error
    async def auto_close_error(self, error: Exception):
        log.exception("Errore nel task auto_close: %s", error)


# ══════════════════════════════════════════════════════════════════
# BOT
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
            log.exception("Impossibile connettersi al database.")
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
        for cmd in synced:
            log.info("  /%s", cmd.name)

    async def on_ready(self) -> None:
        log.info("✅ Bot online come %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="i ticket | /ticket_setup"))

    async def close(self) -> None:
        for task in _close_tasks.values():
            task.cancel()
        await self.db.close()
        await super().close()


# ══════════════════════════════════════════════════════════════════
# ERROR HANDLER GLOBALE
# ══════════════════════════════════════════════════════════════════
def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Errore slash '%s': %s", getattr(interaction.command, "name", "?"), error)
        msg = "⚠️ Si è verificato un errore. Riprova o contatta un amministratore."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
