"""
TicketBot – Bot ticket professionale per Discord (Versione PostgreSQL per Railway)
================================================================================
Versione con interfaccia migliorata: embed visivamente raffinati, pannello ticket
con sezioni ben organizzate, palette cromatica coerente per categoria.
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
from typing import Optional, Union

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
    GUILD_ID: int             = int(os.environ.get("GUILD_ID", "1517091767399350342"))
    LOG_CHANNEL_ID: int       = int(os.environ.get("LOG_CHANNEL_ID", "1517107696200061040"))
    CATEGORY_GENERAL: int     = int(os.environ.get("CATEGORY_GENERAL", "1517099911269580891"))
    STAFF_TICKET_ROLE_ID: int = int(os.environ.get("STAFF_TICKET_ROLE_ID", "1517123223836295188"))
    ADMIN_ROLE_ID: int        = int(os.environ.get("ADMIN_ROLE_ID", "1517123223836295188"))

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
# PALETTE COLORI & STILE PER CATEGORIA
# ══════════════════════════════════════════════════════════════════
CATEGORIA_STYLE: dict[str, dict] = {
    "Supporto Tecnico":  {"color": 0x5865F2, "emoji": "🔧", "icon": "🖥️"},
    "Report Utente":     {"color": 0xED4245, "emoji": "🚨", "icon": "📋"},
    "Candidatura Staff": {"color": 0x57F287, "emoji": "📄", "icon": "🌟"},
    "Unisciti al Team":  {"color": 0xFEE75C, "emoji": "🤝", "icon": "🏆"},
    "Altro":             {"color": 0x99AAB5, "emoji": "💬", "icon": "❓"},
}

def cat_color(categoria: str) -> int:
    return CATEGORIA_STYLE.get(categoria, CATEGORIA_STYLE["Altro"])["color"]

def cat_emoji(categoria: str) -> str:
    return CATEGORIA_STYLE.get(categoria, CATEGORIA_STYLE["Altro"])["emoji"]

def cat_icon(categoria: str) -> str:
    return CATEGORIA_STYLE.get(categoria, CATEGORIA_STYLE["Altro"])["icon"]


# ══════════════════════════════════════════════════════════════════
# ENUMS
# ══════════════════════════════════════════════════════════════════
class Priority(str, Enum):
    LOW    = "Bassa"
    MEDIUM = "Media"
    HIGH   = "Alta"
    URGENT = "Urgente"

    @property
    def icon(self) -> str:
        return {"Bassa": "🟢", "Media": "🟡", "Alta": "🔴", "Urgente": "🚨"}[self.value]

    @property
    def color(self) -> int:
        return {
            "Bassa":   0x57F287,
            "Media":   0xFEE75C,
            "Alta":    0xE67E22,
            "Urgente": 0xED4245,
        }[self.value]

    @property
    def rank(self) -> int:
        return {"Urgente": 0, "Alta": 1, "Media": 2, "Bassa": 3}[self.value]

    @property
    def bar(self) -> str:
        """Visual bar indicator for priority level."""
        bars = {"Bassa": "▓░░░", "Media": "▓▓░░", "Alta": "▓▓▓░", "Urgente": "▓▓▓▓"}
        return bars[self.value]


class TicketStatus(str, Enum):
    OPEN    = "open"
    CLOSING = "closing"
    CLOSED  = "closed"


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

def divider(label: str = "") -> str:
    """Returns a stylized section divider for embed descriptions."""
    if label:
        return f"\n─── {label} ───\n"
    return "\n──────────────────\n"


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
    if cfg.ADMIN_ROLE_ID != 0 and any(r.id == cfg.ADMIN_ROLE_ID for r in member.roles):
        return True
    if cfg.STAFF_TICKET_ROLE_ID != 0 and any(r.id == cfg.STAFF_TICKET_ROLE_ID for r in member.roles):
        return True
    cat_role_id = get_category_role_id(categoria)
    if cat_role_id and cat_role_id != 0 and any(r.id == cat_role_id for r in member.roles):
        return True
    return False

def _base_embed(title: str, description: str, color: Union[discord.Color, int]) -> discord.Embed:
    if isinstance(color, int):
        color = discord.Color(color)
    e = discord.Embed(title=title, description=description, color=color, timestamp=utcnow())
    return e


# ══════════════════════════════════════════════════════════════════
# EMBED BUILDERS  (tutta la parte estetica è qui)
# ══════════════════════════════════════════════════════════════════

def embed_ticket_panel() -> discord.Embed:
    """Embed principale nel canale pubblico per aprire ticket."""
    e = discord.Embed(
        title="🎫  Centro Assistenza",
        description=(
            "Hai bisogno di aiuto? Il nostro staff è qui per te.\n"
            "Clicca il pulsante qui sotto per aprire una richiesta privata."
        ),
        color=discord.Color(0x5865F2),
        timestamp=utcnow(),
    )
    e.add_field(
        name="📋  Categorie disponibili",
        value=(
            "🔧 **Supporto Tecnico** — problemi in-game o tecnici\n"
            "🚨 **Report Utente** — segnala comportamenti scorretti\n"
            "📄 **Candidatura Staff** — candidati per far parte del team\n"
            "🤝 **Unisciti al Team** — partnership o collaborazioni\n"
            "💬 **Altro** — qualsiasi altra richiesta"
        ),
        inline=False,
    )
    e.add_field(
        name="⏱️  Tempi di risposta",
        value="Di solito rispondiamo entro **24 ore**.",
        inline=True,
    )
    e.add_field(
        name="📌  Regole",
        value="Un ticket per problema. Non aprire duplicati.",
        inline=True,
    )
    e.set_footer(text="Usare il sistema ticket responsabilmente · Staff Team")
    return e


def embed_ticket_opened(
    user: discord.User | discord.Member,
    categoria: str,
    mc: str,
    subj: str,
    desc: str,
    ch_id: int,
    priority: Priority = Priority.LOW,
) -> discord.Embed:
    """Embed principale all'interno del canale ticket appena aperto."""
    color = cat_color(categoria)
    icon  = cat_icon(categoria)
    emoji = cat_emoji(categoria)

    e = discord.Embed(
        title=f"{emoji}  Ticket — {categoria}",
        description=(
            f"Benvenuto {user.mention}! Lo staff sarà con te a breve.\n"
            f"Nel frattempo, assicurati che la tua descrizione sia completa."
        ),
        color=discord.Color(color),
        timestamp=utcnow(),
    )

    # Blocco informazioni utente
    e.add_field(name="👤  Utente",        value=f"{user.mention}\n`{user.id}`",  inline=True)
    e.add_field(name="🎮  Minecraft",     value=f"```{mc}```",                   inline=True)
    e.add_field(name=f"{icon}  Categoria", value=f"`{categoria}`",               inline=True)

    # Divisore visivo tramite field vuoto (inline=False)
    e.add_field(name="\u200b", value="──────────────────────", inline=False)

    e.add_field(name="📌  Oggetto",           value=subj,        inline=False)
    e.add_field(name="📝  Descrizione",       value=f">>> {desc[:1000]}", inline=False)

    e.add_field(name="\u200b", value="──────────────────────", inline=False)

    e.add_field(
        name="🟢  Priorità",
        value=f"{priority.icon} **{priority.value}**  `{priority.bar}`",
        inline=True,
    )
    e.add_field(
        name="📊  Stato",
        value="🟦 **Aperto** — in attesa di staff",
        inline=True,
    )
    e.add_field(name="🆔  Canale ID", value=f"`{ch_id}`", inline=True)

    e.set_author(name=str(user), icon_url=user.display_avatar.url)
    e.set_footer(text="Ticket System · Rispondi qui per comunicare con lo staff")
    return e


def embed_ticket_closing(
    closer: discord.User | discord.Member,
    delay: int,
    reason: Optional[str],
) -> discord.Embed:
    """Embed di avviso chiusura programmata."""
    desc_parts = [
        f"**Richiesta da:** {closer.mention}",
        f"**Chiusura tra:** `{format_delta(delay)}`",
    ]
    if reason:
        desc_parts.append(f"**Motivo:** {reason}")
    desc_parts.append(
        "\n> Il transcript verrà salvato automaticamente.\n"
        "> Potrai lasciare una valutazione al termine."
    )

    e = discord.Embed(
        title="🔒  Chiusura Pianificata",
        description="\n".join(desc_parts),
        color=discord.Color(0xFEE75C),
        timestamp=utcnow(),
    )
    e.set_footer(text="Lo staff può annullare la chiusura con /reopen")
    return e


def embed_ticket_claimed(claimer: discord.Member) -> discord.Embed:
    """Embed quando uno staff prende in carico il ticket."""
    e = discord.Embed(
        title="🙋  Ticket Preso in Carico",
        description=(
            f"{claimer.mention} ha assegnato questo ticket a sé.\n"
            f"Sarai assistito direttamente da **{claimer.display_name}**."
        ),
        color=discord.Color(0x57F287),
        timestamp=utcnow(),
    )
    e.set_thumbnail(url=claimer.display_avatar.url)
    e.set_footer(text="Solo uno staff per ticket — per chiarezza e velocità")
    return e


def embed_priority_updated(priority: Priority, setter: discord.Member) -> discord.Embed:
    """Embed quando viene aggiornata la priorità."""
    e = discord.Embed(
        title="↕️  Priorità Aggiornata",
        description=(
            f"**Nuova priorità:** {priority.icon} **{priority.value}**  `{priority.bar}`\n"
            f"**Impostata da:** {setter.mention}"
        ),
        color=discord.Color(priority.color),
        timestamp=utcnow(),
    )
    return e


def embed_transcript_log(
    channel: discord.TextChannel,
    ticket: dict,
    notes: list,
) -> discord.Embed:
    """Embed inviato nel canale log con i dettagli del transcript."""
    opened_at = ticket.get("opened_at")
    closed_at = ticket.get("closed_at") or utcnow()

    if opened_at and closed_at:
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=datetime.timezone.utc)
        duration = closed_at - opened_at
        h, rem = divmod(int(duration.total_seconds()), 3600)
        m = rem // 60
        duration_str = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration_str = "—"

    note_lines = []
    for r in notes:
        ts = r["created_at"].strftime("%d/%m %H:%M")
        note_lines.append(f"`{ts}` <@{r['staff_id']}>: {r['note']}")
    note_text = "\n".join(note_lines) if note_lines else "*Nessuna nota registrata.*"

    e = discord.Embed(
        title="📋  Transcript Ticket",
        color=discord.Color(0x5865F2),
        timestamp=utcnow(),
    )
    e.add_field(name="📁  Canale",    value=f"`#{channel.name}`",               inline=True)
    e.add_field(name="👤  Utente",    value=f"<@{ticket['user_id']}>",          inline=True)
    e.add_field(name="📌  Categoria", value=ticket["categoria"],                inline=True)
    e.add_field(name="📝  Oggetto",   value=ticket.get("subject") or "—",       inline=False)
    e.add_field(name="⏱️  Durata",    value=duration_str,                       inline=True)
    e.add_field(
        name="🔒  Chiuso da",
        value=f"<@{ticket['closed_by']}>" if ticket.get("closed_by") else "—",
        inline=True,
    )
    reason = ticket.get("close_reason")
    e.add_field(name="💬  Motivo",    value=reason or "*Non specificato*",       inline=True)
    e.add_field(name="📒  Note Staff", value=note_text,                         inline=False)
    e.set_footer(text=f"ID Canale: {channel.id}")
    return e


def embed_rating_request() -> discord.Embed:
    """DM inviato all'utente dopo la chiusura per raccogliere il feedback."""
    e = discord.Embed(
        title="⭐  Come ti abbiamo aiutato?",
        description=(
            "Il tuo ticket è stato chiuso.\n"
            "Dedica **10 secondi** a valutare il supporto ricevuto: "
            "il tuo feedback aiuta il nostro staff a migliorare.\n\n"
            "Seleziona un punteggio dal menu qui sotto."
        ),
        color=discord.Color(0xFEE75C),
        timestamp=utcnow(),
    )
    e.set_footer(text="La valutazione è anonima · Grazie per il tuo tempo")
    return e


def embed_stats(member: discord.Member | discord.User, data: dict) -> discord.Embed:
    """Embed statistiche staff."""
    closed  = data["closed_count"]
    claimed = data["claimed_count"]

    # Medaglie visuali
    if closed >= 100:
        badge = "🥇 Esperto"
    elif closed >= 50:
        badge = "🥈 Veterano"
    elif closed >= 10:
        badge = "🥉 Attivo"
    else:
        badge = "🌱 Novizio"

    e = discord.Embed(
        title=f"📊  Statistiche — {member.display_name}",
        description=f"**Badge:** {badge}",
        color=discord.Color(0x57F287),
        timestamp=utcnow(),
    )
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="🙋  Presi in carico", value=f"```{claimed}```", inline=True)
    e.add_field(name="🔒  Chiusi",          value=f"```{closed}```",  inline=True)
    e.set_footer(text="Statistiche aggiornate in tempo reale")
    return e


def embed_top_staff(rows: list) -> discord.Embed:
    """Embed classifica top staff."""
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, row in enumerate(rows, 1):
        medal = medals[i - 1] if i <= 3 else f"`#{i}`"
        lines.append(f"{medal} <@{row['staff_id']}> — **{row['closed_count']}** chiusi")

    e = discord.Embed(
        title="🏆  Classifica Staff",
        description="\n".join(lines) or "*Nessun dato disponibile.*",
        color=discord.Color(0xFEE75C),
        timestamp=utcnow(),
    )
    e.set_footer(text="Basata sui ticket chiusi · Aggiornata in tempo reale")
    return e


# ══════════════════════════════════════════════════════════════════
# LOGICA CHIUSURA & TRANSCRIPT
# ══════════════════════════════════════════════════════════════════
async def schedule_close(
    db: Database,
    channel: discord.TextChannel,
    closer: discord.User | discord.Member,
    reason: Optional[str] = None,
):
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        return
    if ticket["status"] == TicketStatus.CLOSING.value:
        with contextlib.suppress(discord.HTTPException):
            await channel.send("⚠️ Questo ticket è già in fase di chiusura.", delete_after=10)
        return

    await db.update_ticket_status(channel.id, TicketStatus.CLOSING)
    await channel.send(embed=embed_ticket_closing(closer, cfg.CLOSE_DELAY, reason))

    async def _do_close():
        await asyncio.sleep(cfg.CLOSE_DELAY)
        await _finalize_ticket(db, channel, closer, reason)

    task = asyncio.create_task(_do_close())
    _close_tasks[channel.id] = task


async def _finalize_ticket(
    db: Database,
    channel: discord.TextChannel,
    closer: discord.User | discord.Member,
    reason: Optional[str],
):
    ticket = await db.get_ticket(channel.id)
    if not ticket:
        return

    await db.update_ticket_status(
        channel.id, TicketStatus.CLOSED, closed_by=closer.id, close_reason=reason
    )
    _close_tasks.pop(channel.id, None)

    if isinstance(closer, discord.Member) and is_ticket_staff(closer, ticket["categoria"]):
        with contextlib.suppress(Exception):
            await db.bump_stat(closer.id, closed=True)

    await _send_transcript(db, channel, ticket)

    target = channel.guild.get_member(ticket["user_id"])
    if target:
        with contextlib.suppress(discord.Forbidden):
            await target.send(
                embed=embed_rating_request(),
                view=RatingView(db, channel.id, target.id),
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
        file  = discord.File(
            io.BytesIO(transcript.encode()),
            filename=f"transcript-{channel.name}.html",
        )
        with contextlib.suppress(discord.HTTPException):
            await log_ch.send(
                embed=embed_transcript_log(channel, ticket_data, notes),
                file=file,
            )
    except Exception:
        log.exception("Transcript export fallito per #%s", channel.name)


# ══════════════════════════════════════════════════════════════════
# VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════

class MainPersistentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Apri un Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="persistent:open_ticket",
    )
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button):
        e = discord.Embed(
            title="📂  Seleziona una Categoria",
            description=(
                "Scegli la categoria più adatta alla tua richiesta.\n"
                "Poi clicca **Apri Ticket →** per procedere."
            ),
            color=discord.Color(0x5865F2),
        )
        e.set_footer(text="Puoi avere al massimo 2 ticket aperti contemporaneamente.")
        await interaction.response.send_message(embed=e, view=CategorySelectView(), ephemeral=True)


class CategorySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self._categoria: Optional[str] = None

    @discord.ui.select(
        placeholder="📁  Scegli la categoria...",
        options=[
            discord.SelectOption(
                label=c,
                value=c,
                emoji=CATEGORIA_STYLE[c]["emoji"],
                description={
                    "Supporto Tecnico":  "Problemi tecnici o in-game",
                    "Report Utente":     "Segnala un comportamento scorretto",
                    "Candidatura Staff": "Vuoi entrare nello staff?",
                    "Unisciti al Team":  "Partnership o collaborazioni",
                    "Altro":             "Qualsiasi altra richiesta",
                }[c],
            )
            for c in cfg.CATEGORIE
        ],
        custom_id="cat_select",
    )
    async def select_cat(self, interaction: discord.Interaction, select: discord.ui.Select):
        self._categoria = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self._categoria

        e = discord.Embed(
            title=f"{cat_emoji(self._categoria)}  Categoria: {self._categoria}",
            description="Ottima scelta! Clicca **Apri Ticket →** per compilare il modulo.",
            color=discord.Color(cat_color(self._categoria)),
        )
        await interaction.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="Apri Ticket →", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._categoria:
            return await interaction.response.send_message(
                "⚠️ Seleziona prima una categoria dal menu.", ephemeral=True
            )
        await interaction.response.send_modal(TicketModal(self._categoria))


class TicketModal(discord.ui.Modal):
    mc_name = discord.ui.TextInput(
        label="Nickname Minecraft",
        placeholder="Es. Steve123  (3–16 caratteri, lettere/numeri/_)",
        min_length=3,
        max_length=16,
    )
    subject = discord.ui.TextInput(
        label="Oggetto",
        placeholder="Breve titolo della tua richiesta (es. 'Bug nel rank')",
        min_length=5,
        max_length=60,
    )
    description = discord.ui.TextInput(
        label="Descrizione dettagliata",
        style=discord.TextStyle.long,
        placeholder=(
            "Descrivi il problema nel dettaglio:\n"
            "— Cosa stavi facendo?\n"
            "— Quando è successo?\n"
            "— Hai già provato qualcosa?"
        ),
        min_length=20,
        max_length=1000,
    )

    def __init__(self, categoria: str):
        super().__init__(title=f"{cat_emoji(categoria)}  Nuovo Ticket — {categoria}", timeout=300)
        self.categoria = categoria

    async def on_submit(self, interaction: discord.Interaction):
        db: Database = interaction.client.db
        user  = interaction.user
        guild = interaction.guild

        if not guild:
            return await interaction.response.send_message("❌ Disponibile solo nei server.", ephemeral=True)
        if not _MC_NAME_RE.match(self.mc_name.value):
            return await interaction.response.send_message(
                "❌ **Nickname non valido.** Usa solo lettere, numeri e _ (3–16 caratteri).", ephemeral=True
            )
        if await db.is_blacklisted(user.id):
            return await interaction.response.send_message(
                "🚫 Sei in blacklist e non puoi aprire ticket.", ephemeral=True
            )
        remaining = await db.check_cooldown(user.id)
        if remaining > 0:
            return await interaction.response.send_message(
                f"⏳ Attendi ancora **{format_delta(remaining)}** prima di aprire un nuovo ticket.", ephemeral=True
            )
        if await db.count_open_tickets(user.id) >= cfg.MAX_OPEN_TICKETS:
            return await interaction.response.send_message(
                f"⚠️ Hai già **{cfg.MAX_OPEN_TICKETS}** ticket aperti. Chiudine uno prima.", ephemeral=True
            )

        category_channel = guild.get_channel(cfg.CATEGORY_GENERAL)
        if not isinstance(category_channel, discord.CategoryChannel):
            return await interaction.response.send_message(
                "❌ Errore di configurazione del server. Contatta un amministratore.", ephemeral=True
            )

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                attach_files=True, read_message_history=True,
            ),
        }
        staff_role    = guild.get_role(cfg.STAFF_TICKET_ROLE_ID)
        cat_role_id   = get_category_role_id(self.categoria)
        category_role = guild.get_role(cat_role_id) if cat_role_id else None
        admin_role    = guild.get_role(cfg.ADMIN_ROLE_ID)

        staff_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, manage_messages=True,
        )
        if staff_role:
            overwrites[staff_role] = staff_perms
        if category_role and category_role.id != 0:
            overwrites[category_role] = staff_perms
        if admin_role and admin_role.id != 0:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                attach_files=True, manage_messages=True, manage_channels=True,
            )

        channel_name = safe_channel_name(f"{user.name}-{self.categoria}")

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channel = await guild.create_text_channel(
                channel_name,
                category=category_channel,
                overwrites=overwrites,
                topic=f"Ticket di {user.name} | Categoria: {self.categoria} | MC: {self.mc_name.value}",
                reason=f"Ticket aperto da {user}",
            )
            await db.create_ticket(
                channel.id, user.id, self.categoria,
                self.mc_name.value, self.subject.value, self.description.value,
            )
            await db.update_cooldown(user.id)

            mentions = []
            if staff_role:
                mentions.append(staff_role.mention)
            if category_role and category_role.id != 0 and (not staff_role or category_role.id != staff_role.id):
                mentions.append(category_role.mention)

            await channel.send(
                content=f"{user.mention} {' '.join(mentions)}",
                embed=embed_ticket_opened(
                    user, self.categoria,
                    self.mc_name.value, self.subject.value, self.description.value,
                    channel.id,
                ),
                view=TicketControlView(),
            )

            confirm_embed = discord.Embed(
                title="✅  Ticket aperto con successo!",
                description=f"Il tuo ticket è disponibile in {channel.mention}.\nLo staff ti risponderà al più presto.",
                color=discord.Color(cat_color(self.categoria)),
            )
            await interaction.followup.send(embed=confirm_embed, ephemeral=True)

        except Exception:
            log.exception("Errore apertura ticket.")
            await interaction.followup.send(
                "❌ Si è verificato un errore durante l'apertura del ticket. Riprova tra poco.", ephemeral=True
            )


class TicketControlView(discord.ui.View):
    """Pannello di controllo visibile nello staff all'interno del ticket."""

    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Questo canale non è un ticket valido.", ephemeral=True)
            return False
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            await interaction.response.send_message("🔒 Solo i membri dello staff possono usare questi controlli.", ephemeral=True)
            return False
        return True

    # ── Riga 0: azioni principali ──────────────────────────────────
    @discord.ui.button(
        label="Chiudi Ticket",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="persistent:close",
        row=0,
    )
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message(
                "⚠️ Il ticket non è nello stato **Aperto**.", ephemeral=True
            )
        await interaction.response.send_modal(CloseModal())

    @discord.ui.button(
        label="Prendi in Carico",
        style=discord.ButtonStyle.blurple,
        emoji="🙋",
        custom_id="persistent:claim",
        row=0,
    )
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["claimed_by"]:
            return await interaction.response.send_message(
                f"⚠️ Questo ticket è già gestito da <@{ticket['claimed_by']}>.", ephemeral=True
            )
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await db.bump_stat(interaction.user.id, claimed=True)
        await interaction.response.send_message(embed=embed_ticket_claimed(interaction.user))

    @discord.ui.button(
        label="Priorità",
        style=discord.ButtonStyle.gray,
        emoji="↕️",
        custom_id="persistent:priority",
        row=0,
    )
    async def priority_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "Seleziona la nuova priorità per questo ticket:",
            view=PriorityView(interaction.channel_id),
            ephemeral=True,
        )

    # ── Riga 1: strumenti secondari ────────────────────────────────
    @discord.ui.button(
        label="Nota Interna",
        style=discord.ButtonStyle.gray,
        emoji="📝",
        custom_id="persistent:note",
        row=1,
    )
    async def note_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddNoteModal())

    @discord.ui.button(
        label="Aggiungi Utente",
        style=discord.ButtonStyle.green,
        emoji="➕",
        custom_id="persistent:add_user",
        row=1,
    )
    async def add_user_btn(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_modal(AddUserModal())


class CloseModal(discord.ui.Modal, title="🔒  Chiudi Ticket"):
    reason = discord.ui.TextInput(
        label="Motivo della chiusura",
        style=discord.TextStyle.long,
        required=False,
        placeholder="(opzionale) Spiega brevemente perché viene chiuso...",
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await schedule_close(
            interaction.client.db,
            interaction.channel,
            interaction.user,
            self.reason.value or None,
        )
        await interaction.followup.send("✅ Chiusura avviata.", ephemeral=True)


class AddNoteModal(discord.ui.Modal, title="📝  Aggiungi Nota Interna"):
    note = discord.ui.TextInput(
        label="Nota (visibile solo allo staff)",
        style=discord.TextStyle.long,
        min_length=1,
        max_length=500,
        placeholder="Scrivi qui le tue osservazioni interne...",
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.client.db.add_note(interaction.channel_id, interaction.user.id, self.note.value)
        e = discord.Embed(
            title="📝  Nota Aggiunta",
            description=f"> {self.note.value}",
            color=discord.Color(0x99AAB5),
            timestamp=utcnow(),
        )
        e.set_footer(text=f"Nota di {interaction.user.display_name} · solo staff")
        await interaction.response.send_message(embed=e, ephemeral=True)


class AddUserModal(discord.ui.Modal, title="➕  Aggiungi Utente al Ticket"):
    user_id = discord.ui.TextInput(
        label="ID Discord dell'utente",
        min_length=17,
        max_length=20,
        placeholder="Es. 123456789012345678",
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            uid    = int(self.user_id.value)
            member = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            await interaction.channel.set_permissions(
                member,
                view_channel=True, send_messages=True,
                attach_files=True, read_message_history=True,
            )
            e = discord.Embed(
                title="➕  Utente Aggiunto",
                description=f"{member.mention} può ora accedere a questo ticket.",
                color=discord.Color(0x57F287),
                timestamp=utcnow(),
            )
            await interaction.followup.send(embed=e)
        except Exception:
            await interaction.followup.send("❌ Utente non trovato. Controlla l'ID e riprova.", ephemeral=True)


class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=60)
        self.channel_id = channel_id

    @discord.ui.select(
        placeholder="Seleziona la priorità...",
        options=[
            discord.SelectOption(
                label=p.value,
                value=p.name,
                emoji=p.icon,
                description=f"Livello {p.rank + 1} di 4  |  Barra: {p.bar}",
            )
            for p in Priority
        ],
    )
    async def select_priority(self, interaction: discord.Interaction, select: discord.ui.Select):
        p = Priority[select.values[0]]
        await interaction.client.db.set_priority(self.channel_id, p)
        await interaction.response.edit_message(
            content=None,
            embed=embed_priority_updated(p, interaction.user),
            view=None,
        )


_STAR_LABELS = {5: "Eccellente 🌟", 4: "Ottimo 👍", 3: "Nella media 😐", 2: "Da migliorare 😕", 1: "Scadente 👎"}

class RatingView(discord.ui.View):
    def __init__(self, db: Database, channel_id: int, user_id: int):
        super().__init__(timeout=300)
        self.db         = db
        self.channel_id = channel_id
        self.user_id    = user_id

    @discord.ui.select(
        placeholder="⭐  Seleziona il tuo voto...",
        options=[
            discord.SelectOption(
                label=f"{'⭐' * i}  {_STAR_LABELS[i]}",
                value=str(i),
            )
            for i in range(5, 0, -1)
        ],
    )
    async def select_rating(self, interaction: discord.Interaction, select: discord.ui.Select):
        score = int(select.values[0])
        await self.db.add_rating(self.channel_id, self.user_id, score, "Nessun commento")
        stars = "⭐" * score
        e = discord.Embed(
            title="✅  Grazie per il tuo feedback!",
            description=f"Hai valutato il supporto **{score}/5** {stars}\n*{_STAR_LABELS[score]}*",
            color=discord.Color(0xFEE75C),
            timestamp=utcnow(),
        )
        e.set_footer(text="Il tuo feedback è stato registrato.")
        await interaction.response.edit_message(embed=e, view=None)


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
        cutoff   = utcnow() - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
        inactive = await self.bot.db.get_inactive_tickets(cutoff)
        for row in inactive:
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                await schedule_close(self.bot.db, channel, self.bot.user, "Chiusura automatica per inattività")

    # ── Comandi slash ──────────────────────────────────────────────
    @app_commands.command(name="ticket_setup", description="Invia il pannello di apertura ticket nel canale corrente.")
    async def ticket_setup(self, interaction: discord.Interaction):
        if not is_ticket_staff(interaction.user, "Altro"):
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        await interaction.channel.send(embed=embed_ticket_panel(), view=MainPersistentView())
        await interaction.response.send_message("✅ Pannello ticket inviato.", ephemeral=True)

    @app_commands.command(name="blacklist_add", description="Aggiungi un utente alla blacklist.")
    async def blacklist_add(self, interaction: discord.Interaction, user: discord.User, reason: str = "Nessun motivo"):
        if not is_ticket_staff(interaction.user, "Altro"):
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        await self.bot.db.blacklist_add(user.id, reason, interaction.user.id)
        e = discord.Embed(
            title="🚫  Utente in Blacklist",
            description=f"{user.mention} non potrà più aprire ticket.\n**Motivo:** {reason}",
            color=discord.Color(0xED4245),
            timestamp=utcnow(),
        )
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="blacklist_remove", description="Rimuovi un utente dalla blacklist.")
    async def blacklist_remove(self, interaction: discord.Interaction, user: discord.User):
        if not is_ticket_staff(interaction.user, "Altro"):
            return await interaction.response.send_message("❌ Solo lo staff può usare questo comando.", ephemeral=True)
        if await self.bot.db.blacklist_remove(user.id):
            e = discord.Embed(
                title="✅  Blacklist Rimossa",
                description=f"{user.mention} può nuovamente aprire ticket.",
                color=discord.Color(0x57F287),
                timestamp=utcnow(),
            )
            await interaction.response.send_message(embed=e)
        else:
            await interaction.response.send_message("⚠️ Questo utente non è in blacklist.", ephemeral=True)

    @app_commands.command(name="stats", description="Mostra le statistiche ticket di un membro dello staff.")
    async def stats(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        data   = await self.bot.db.get_stats(target.id)
        if not data:
            return await interaction.response.send_message(
                f"Nessuna statistica trovata per {target.mention}.", ephemeral=True
            )
        await interaction.response.send_message(embed=embed_stats(target, data))

    @app_commands.command(name="topstaff", description="Classifica dello staff per ticket chiusi.")
    async def topstaff(self, interaction: discord.Interaction):
        top = await self.bot.db.get_top_staff(10)
        if not top:
            return await interaction.response.send_message("La classifica è ancora vuota.", ephemeral=True)
        await interaction.response.send_message(embed=embed_top_staff(top))


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
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="i ticket 🎫")
        )

    async def close(self) -> None:
        for task in _close_tasks.values():
            task.cancel()
        await self.db.close()
        await super().close()


def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ Non hai i permessi necessari per questo comando."
        else:
            log.exception("Errore slash:")
            msg = "⚠️ Si è verificato un errore inatteso. Riprova tra poco."

        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
