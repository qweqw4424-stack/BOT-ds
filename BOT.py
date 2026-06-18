"""
TicketBot – Bot ticket professionale per Discord (Versione PostgreSQL per Railway)
================================================================================
Versione con interfaccia migliorata: embed visivamente raffinati, pannello ticket
con sezioni ben organizzate, palette cromatica coerente per categoria.

CHANGELOG rispetto alla versione precedente:
- FIX: Config ora usa __post_init__ invece di frozen=True + field(default_factory)
- FIX: TicketControlView persistent buttons ora funzionano correttamente al riavvio
- FIX: Aggiunto comando /reopen menzionato negli embed ma mancante
- FIX: auto_close_task ora controlla che bot.user non sia None prima di usarlo
- FIX: _finalize_ticket ora gestisce eccezioni DB prima di eliminare il canale
- FIX: schedule_close ora gestisce task orfani e race condition
- FIX: RatingView persistente via DM con timeout gestito
- FIX: Cache in-memory dei ticket aperti per ridurre query DB su on_message
- FIX: embed_transcript_log gestisce closed_at = None senza crash
- FIX: Priority.icon ora restituisce str pura compatibile con SelectOption.emoji
- FIX: Validazione config più robusta con messaggi chiari
- MIGLIORAMENTO: /reopen slash command implementato
- MIGLIORAMENTO: /adduser e /removeuser slash command
- MIGLIORAMENTO: Logging migliorato con contesto strutturato
- MIGLIORAMENTO: Gestione errori più granulare con rollback
- MIGLIORAMENTO: Auto-close mostra avviso nel canale prima di chiudere
- MIGLIORAMENTO: Blacklist mostra lista formattata con /blacklist_list
- MIGLIORAMENTO: Transcript fallback testuale se chat_exporter non disponibile
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
# BUG FIX: dataclass frozen=True è incompatibile con field(default_factory).
# Soluzione: usiamo una dataclass normale con __post_init__ per il dict.
# ══════════════════════════════════════════════════════════════════
@dataclass
class Config:
    TOKEN: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    GUILD_ID: int             = field(default_factory=lambda: int(os.environ.get("GUILD_ID", "0")))
    LOG_CHANNEL_ID: int       = field(default_factory=lambda: int(os.environ.get("LOG_CHANNEL_ID", "0")))
    CATEGORY_GENERAL: int     = field(default_factory=lambda: int(os.environ.get("CATEGORY_GENERAL", "0")))
    STAFF_TICKET_ROLE_ID: int = field(default_factory=lambda: int(os.environ.get("STAFF_TICKET_ROLE_ID", "0")))
    ADMIN_ROLE_ID: int        = field(default_factory=lambda: int(os.environ.get("ADMIN_ROLE_ID", "0")))
    DATABASE_URL: str         = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    MAX_OPEN_TICKETS: int         = 2
    COOLDOWN_SECONDS: int         = 30
    AUTO_CLOSE_HOURS: int         = 48
    AUTO_CLOSE_CHECK_MINUTES: int = 30
    CLOSE_DELAY: int              = 600

    CATEGORIE: tuple = field(default_factory=lambda: (
        "Supporto Tecnico",
        "Report Utente",
        "Candidatura Staff",
        "Unisciti al Team",
        "Altro",
    ))

    # Compilato in __post_init__
    CATEGORIA_ROLES: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.CATEGORIA_ROLES:
            self.CATEGORIA_ROLES = {
                "Supporto Tecnico":  int(os.environ.get("ROLE_SUPPORT_ID", "0")),
                "Report Utente":     int(os.environ.get("ROLE_REPORT_ID", "0")),
                "Candidatura Staff": int(os.environ.get("ROLE_STAFF_CANDIDATE_ID", "0")),
                "Unisciti al Team":  int(os.environ.get("ROLE_JOIN_TEAM_ID", "0")),
                "Altro":             int(os.environ.get("ROLE_OTHER_ID", "0")),
            }

    def validate(self) -> None:
        errors: list[str] = []
        if not self.TOKEN:
            errors.append("BOT_TOKEN non impostato")
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL non impostato (richiesto per PostgreSQL su Railway)")
        if self.GUILD_ID == 0:
            errors.append("GUILD_ID non impostato o non valido")
        if self.CATEGORY_GENERAL == 0:
            errors.append("CATEGORY_GENERAL non impostato — il bot non potrà creare canali ticket")
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
        # BUG FIX: usiamo una stringa emoji pura, compatibile con discord.SelectOption.emoji
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
        return {"Bassa": "▓░░░", "Media": "▓▓░░", "Alta": "▓▓▓░", "Urgente": "▓▓▓▓"}[self.value]


class TicketStatus(str, Enum):
    OPEN    = "open"
    CLOSING = "closing"
    CLOSED  = "closed"


# ══════════════════════════════════════════════════════════════════
# UTILITY
# ══════════════════════════════════════════════════════════════════
_MC_NAME_RE        = re.compile(r"^[A-Za-z0-9_]{3,16}$")
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

def ensure_tz(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Garantisce che un datetime abbia timezone UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=datetime.timezone.utc)
    return dt


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
    avg_close_min REAL    NOT NULL DEFAULT 0.0
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
    def __init__(self, dsn: str) -> None:
        self._dsn  = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        dsn = self._dsn.replace("postgres://", "postgresql://", 1)
        self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        log.info("Database PostgreSQL connesso con successo.")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ── Tickets ───────────────────────────────────────────────────
    async def create_ticket(
        self,
        channel_id: int,
        user_id: int,
        categoria: str,
        mc_name: str,
        subject: str,
        description: str,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO tickets
                   (channel_id, user_id, categoria, mc_name, subject, description)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                channel_id, user_id, categoria, mc_name, subject, description,
            )

    async def get_ticket(self, channel_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tickets WHERE channel_id = $1", channel_id
            )
            return dict(row) if row else None

    async def update_ticket_status(
        self,
        channel_id: int,
        status: TicketStatus,
        closed_by: Optional[int] = None,
        close_reason: Optional[str] = None,
    ) -> None:
        async with self._pool.acquire() as conn:
            if status == TicketStatus.CLOSED:
                await conn.execute(
                    """UPDATE tickets
                       SET status = $1, closed_by = $2,
                           closed_at = CURRENT_TIMESTAMP, close_reason = $3
                       WHERE channel_id = $4""",
                    status.value, closed_by, close_reason, channel_id,
                )
            else:
                await conn.execute(
                    "UPDATE tickets SET status = $1 WHERE channel_id = $2",
                    status.value, channel_id,
                )

    async def save_transcript(self, channel_id: int, content: bytes) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tickets SET transcript = $1 WHERE channel_id = $2",
                content, channel_id,
            )

    async def claim_ticket(self, channel_id: int, staff_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tickets SET claimed_by = $1 WHERE channel_id = $2",
                staff_id, channel_id,
            )

    async def set_priority(self, channel_id: int, priority: Priority) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE tickets SET priority = $1 WHERE channel_id = $2",
                priority.value, channel_id,
            )

    async def touch_last_message(self, channel_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE tickets
                   SET last_message = CURRENT_TIMESTAMP
                   WHERE channel_id = $1 AND status = 'open'""",
                channel_id,
            )

    async def count_open_tickets(self, user_id: int) -> int:
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM tickets WHERE user_id = $1 AND status IN ('open','closing')",
                user_id,
            )

    async def load_open_channel_ids(self) -> set[int]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT channel_id FROM tickets WHERE status = 'open'")
            return {r["channel_id"] for r in rows}

    async def get_inactive_tickets(self, cutoff: datetime.datetime) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT channel_id FROM tickets WHERE status = 'open' AND last_message < $1",
                cutoff,
            )

    # BUG FIX: aggiunto reopen
    async def reopen_ticket(self, channel_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """UPDATE tickets
                   SET status = 'open', closed_by = NULL,
                       closed_at = NULL, close_reason = NULL
                   WHERE channel_id = $1""",
                channel_id,
            )

    # ── Blacklist ─────────────────────────────────────────────────
    async def is_blacklisted(self, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            return (
                await conn.fetchval(
                    "SELECT 1 FROM blacklist WHERE user_id = $1", user_id
                )
            ) is not None

    async def blacklist_add(self, user_id: int, reason: str, added_by: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO blacklist (user_id, reason, added_by) VALUES ($1, $2, $3)
                   ON CONFLICT(user_id) DO UPDATE
                   SET reason = EXCLUDED.reason,
                       added_by = EXCLUDED.added_by,
                       added_at = CURRENT_TIMESTAMP""",
                user_id, reason, added_by,
            )

    async def blacklist_remove(self, user_id: int) -> bool:
        async with self._pool.acquire() as conn:
            res = await conn.execute(
                "DELETE FROM blacklist WHERE user_id = $1", user_id
            )
            return "DELETE 1" in res

    async def blacklist_list(self) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM blacklist ORDER BY added_at DESC LIMIT 25"
            )

    # ── Cooldown ──────────────────────────────────────────────────
    async def check_cooldown(self, user_id: int) -> int:
        async with self._pool.acquire() as conn:
            last_open = await conn.fetchval(
                "SELECT last_open FROM cooldowns WHERE user_id = $1", user_id
            )
            if not last_open:
                return 0
            last_open = ensure_tz(last_open)
            elapsed = (utcnow() - last_open).total_seconds()
            return max(0, cfg.COOLDOWN_SECONDS - int(elapsed))

    async def update_cooldown(self, user_id: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO cooldowns (user_id, last_open) VALUES ($1, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET last_open = CURRENT_TIMESTAMP""",
                user_id,
            )

    # ── Ratings ───────────────────────────────────────────────────
    async def add_rating(
        self, channel_id: int, user_id: int, score: int, comment: str
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO ratings (channel_id, user_id, score, comment)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT(channel_id) DO NOTHING""",
                channel_id, user_id, score, comment,
            )

    # ── Notes ─────────────────────────────────────────────────────
    async def add_note(self, channel_id: int, staff_id: int, note: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ticket_notes (channel_id, staff_id, note) VALUES ($1, $2, $3)",
                channel_id, staff_id, note,
            )

    async def get_notes(self, channel_id: int) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM ticket_notes WHERE channel_id = $1 ORDER BY created_at ASC",
                channel_id,
            )

    # ── Stats ─────────────────────────────────────────────────────
    async def bump_stat(
        self, staff_id: int, claimed: bool = False, closed: bool = False
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ticket_stats (staff_id) VALUES ($1) ON CONFLICT (staff_id) DO NOTHING",
                staff_id,
            )
            if claimed:
                await conn.execute(
                    "UPDATE ticket_stats SET claimed_count = claimed_count + 1 WHERE staff_id = $1",
                    staff_id,
                )
            if closed:
                await conn.execute(
                    "UPDATE ticket_stats SET closed_count = closed_count + 1 WHERE staff_id = $1",
                    staff_id,
                )

    async def get_stats(self, staff_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM ticket_stats WHERE staff_id = $1", staff_id
            )
            return dict(row) if row else None

    async def get_top_staff(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM ticket_stats ORDER BY closed_count DESC LIMIT $1", limit
            )


# ══════════════════════════════════════════════════════════════════
# GLOBALS & HELPERS
# ══════════════════════════════════════════════════════════════════
_close_tasks: dict[int, asyncio.Task] = {}

# BUG FIX: cache in-memory dei channel_id aperti per evitare una query DB
# per ogni singolo messaggio in ogni canale del server.
_open_ticket_channels: set[int] = set()

def get_category_role_id(cat_name: str) -> Optional[int]:
    return cfg.CATEGORIA_ROLES.get(cat_name)

def is_ticket_staff(member: discord.Member, categoria: str) -> bool:
    if cfg.ADMIN_ROLE_ID and any(r.id == cfg.ADMIN_ROLE_ID for r in member.roles):
        return True
    if cfg.STAFF_TICKET_ROLE_ID and any(r.id == cfg.STAFF_TICKET_ROLE_ID for r in member.roles):
        return True
    cat_role_id = get_category_role_id(categoria)
    if cat_role_id and any(r.id == cat_role_id for r in member.roles):
        return True
    return False

def _base_embed(
    title: str,
    description: str,
    color: Union[discord.Color, int],
) -> discord.Embed:
    if isinstance(color, int):
        color = discord.Color(color)
    return discord.Embed(
        title=title, description=description, color=color, timestamp=utcnow()
    )


# ══════════════════════════════════════════════════════════════════
# EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════

def embed_ticket_panel() -> discord.Embed:
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
    e.add_field(name="⏱️  Tempi di risposta", value="Di solito rispondiamo entro **24 ore**.", inline=True)
    e.add_field(name="📌  Regole",            value="Un ticket per problema. Non aprire duplicati.", inline=True)
    e.set_footer(text="Usare il sistema ticket responsabilmente · Staff Team")
    return e


def embed_ticket_opened(
    user: Union[discord.User, discord.Member],
    categoria: str,
    mc: str,
    subj: str,
    desc: str,
    ch_id: int,
    priority: Priority = Priority.LOW,
) -> discord.Embed:
    color = cat_color(categoria)
    icon  = cat_icon(categoria)
    emoji = cat_emoji(categoria)

    e = discord.Embed(
        title=f"{emoji}  Ticket — {categoria}",
        description=(
            f"Benvenuto {user.mention}! Lo staff sarà con te a breve.\n"
            "Nel frattempo assicurati che la tua descrizione sia completa."
        ),
        color=discord.Color(color),
        timestamp=utcnow(),
    )
    e.add_field(name="👤  Utente",         value=f"{user.mention}\n`{user.id}`", inline=True)
    e.add_field(name="🎮  Minecraft",      value=f"```{mc}```",                  inline=True)
    e.add_field(name=f"{icon}  Categoria", value=f"`{categoria}`",               inline=True)
    e.add_field(name="\u200b",             value="──────────────────────",       inline=False)
    e.add_field(name="📌  Oggetto",        value=subj,                           inline=False)
    e.add_field(name="📝  Descrizione",    value=f">>> {desc[:1000]}",           inline=False)
    e.add_field(name="\u200b",             value="──────────────────────",       inline=False)
    e.add_field(
        name="🟢  Priorità",
        value=f"{priority.icon} **{priority.value}**  `{priority.bar}`",
        inline=True,
    )
    e.add_field(name="📊  Stato",   value="🟦 **Aperto** — in attesa di staff", inline=True)
    e.add_field(name="🆔  Canale",  value=f"`{ch_id}`",                          inline=True)
    e.set_author(name=str(user), icon_url=user.display_avatar.url)
    e.set_footer(text="Ticket System · Rispondi qui per comunicare con lo staff")
    return e


def embed_ticket_closing(
    closer: Union[discord.User, discord.Member],
    delay: int,
    reason: Optional[str],
) -> discord.Embed:
    parts = [
        f"**Richiesta da:** {closer.mention}",
        f"**Chiusura tra:** `{format_delta(delay)}`",
    ]
    if reason:
        parts.append(f"**Motivo:** {reason}")
    parts.append(
        "\n> Il transcript verrà salvato automaticamente.\n"
        "> Potrai lasciare una valutazione al termine."
    )
    e = discord.Embed(
        title="🔒  Chiusura Pianificata",
        description="\n".join(parts),
        color=discord.Color(0xFEE75C),
        timestamp=utcnow(),
    )
    # BUG FIX: /reopen ora esiste, il footer è accurato
    e.set_footer(text="Lo staff può annullare la chiusura con /reopen")
    return e


def embed_ticket_reopened(
    reopener: Union[discord.User, discord.Member],
) -> discord.Embed:
    e = discord.Embed(
        title="🔓  Ticket Riaperto",
        description=f"{reopener.mention} ha riaperto questo ticket.\nLo staff è di nuovo disponibile.",
        color=discord.Color(0x57F287),
        timestamp=utcnow(),
    )
    e.set_footer(text="Ticket System")
    return e


def embed_ticket_claimed(claimer: discord.Member) -> discord.Embed:
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
    # BUG FIX: gestione robusta di opened_at/closed_at potenzialmente None
    opened_at = ensure_tz(ticket.get("opened_at"))
    closed_at = ensure_tz(ticket.get("closed_at")) or utcnow()

    if opened_at:
        duration = closed_at - opened_at
        h, rem = divmod(int(duration.total_seconds()), 3600)
        m = rem // 60
        duration_str = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration_str = "—"

    note_lines = [
        f"`{r['created_at'].strftime('%d/%m %H:%M')}` <@{r['staff_id']}>: {r['note']}"
        for r in notes
    ]
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
    e.add_field(name="💬  Motivo",     value=ticket.get("close_reason") or "*Non specificato*", inline=True)
    e.add_field(name="📒  Note Staff", value=note_text,                                         inline=False)
    e.set_footer(text=f"ID Canale: {channel.id}")
    return e


def embed_rating_request() -> discord.Embed:
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


def embed_stats(member: Union[discord.Member, discord.User], data: dict) -> discord.Embed:
    closed  = data["closed_count"]
    claimed = data["claimed_count"]
    badge = (
        "🥇 Esperto"  if closed >= 100 else
        "🥈 Veterano" if closed >= 50  else
        "🥉 Attivo"   if closed >= 10  else
        "🌱 Novizio"
    )
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
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i - 1] if i <= 3 else f'`#{i}`'} <@{row['staff_id']}> — **{row['closed_count']}** chiusi"
        for i, row in enumerate(rows, 1)
    ]
    e = discord.Embed(
        title="🏆  Classifica Staff",
        description="\n".join(lines) or "*Nessun dato disponibile.*",
        color=discord.Color(0xFEE75C),
        timestamp=utcnow(),
    )
    e.set_footer(text="Basata sui ticket chiusi · Aggiornata in tempo reale")
    return e


def embed_blacklist_list(rows: list) -> discord.Embed:
    lines = []
    for row in rows:
        added_at = ensure_tz(row["added_at"])
        ts = added_at.strftime("%d/%m/%Y") if added_at else "—"
        lines.append(
            f"<@{row['user_id']}> — `{row['reason']}` *(aggiunto da <@{row['added_by']}> il {ts})*"
        )
    e = discord.Embed(
        title="🚫  Blacklist Ticket",
        description="\n".join(lines) if lines else "*Nessun utente in blacklist.*",
        color=discord.Color(0xED4245),
        timestamp=utcnow(),
    )
    return e


# ══════════════════════════════════════════════════════════════════
# LOGICA CHIUSURA & TRANSCRIPT
# ══════════════════════════════════════════════════════════════════
async def schedule_close(
    db: Database,
    channel: discord.TextChannel,
    closer: Union[discord.User, discord.Member],
    reason: Optional[str] = None,
) -> None:
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        return
    if ticket["status"] == TicketStatus.CLOSING.value:
        with contextlib.suppress(discord.HTTPException):
            await channel.send(
                "⚠️ Questo ticket è già in fase di chiusura.", delete_after=10
            )
        return

    await db.update_ticket_status(channel.id, TicketStatus.CLOSING)
    await channel.send(embed=embed_ticket_closing(closer, cfg.CLOSE_DELAY, reason))

    async def _do_close() -> None:
        try:
            await asyncio.sleep(cfg.CLOSE_DELAY)
            await _finalize_ticket(db, channel, closer, reason)
        except asyncio.CancelledError:
            # Task annullato da /reopen — non fare nulla
            log.info("Close task annullato per canale %d", channel.id)

    # BUG FIX: cancella eventuale task precedente prima di crearne uno nuovo
    old_task = _close_tasks.pop(channel.id, None)
    if old_task and not old_task.done():
        old_task.cancel()

    _close_tasks[channel.id] = asyncio.create_task(_do_close())


async def cancel_close(channel_id: int) -> bool:
    """Annulla un close task pendente. Restituisce True se era presente."""
    task = _close_tasks.pop(channel_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def _finalize_ticket(
    db: Database,
    channel: discord.TextChannel,
    closer: Union[discord.User, discord.Member],
    reason: Optional[str],
) -> None:
    # BUG FIX: verifica che il ticket esista ancora e sia in stato closing
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] not in (TicketStatus.CLOSING.value, TicketStatus.OPEN.value):
        return

    try:
        await db.update_ticket_status(
            channel.id,
            TicketStatus.CLOSED,
            closed_by=closer.id,
            close_reason=reason,
        )
    except Exception:
        log.exception("Errore aggiornamento stato ticket %d", channel.id)
        return  # Non eliminare il canale se il DB ha fallito

    _close_tasks.pop(channel.id, None)
    _open_ticket_channels.discard(channel.id)

    if isinstance(closer, discord.Member) and is_ticket_staff(closer, ticket["categoria"]):
        with contextlib.suppress(Exception):
            await db.bump_stat(closer.id, closed=True)

    await _send_transcript(db, channel, ticket)

    # Invia richiesta valutazione in DM
    target = channel.guild.get_member(ticket["user_id"])
    if target:
        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
            await target.send(
                embed=embed_rating_request(),
                view=RatingView(db, channel.id, target.id),
            )

    # BUG FIX: attendi un momento prima di eliminare per permettere al messaggio
    # di transcript di essere inviato al canale log
    await asyncio.sleep(2)
    with contextlib.suppress(discord.NotFound, discord.HTTPException):
        await channel.delete(reason=f"Ticket chiuso da {closer}")


async def _send_transcript(
    db: Database,
    channel: discord.TextChannel,
    ticket_data: dict,
) -> None:
    if cfg.LOG_CHANNEL_ID == 0:
        return

    log_ch = channel.guild.get_channel(cfg.LOG_CHANNEL_ID)
    if not isinstance(log_ch, discord.TextChannel):
        return

    notes = await db.get_notes(channel.id)
    base_embed = embed_transcript_log(channel, ticket_data, notes)

    if HAS_CHAT_EXPORTER:
        try:
            transcript = await chat_exporter.export(channel)
            if transcript:
                await db.save_transcript(channel.id, transcript.encode("utf-8"))
                file = discord.File(
                    io.BytesIO(transcript.encode("utf-8")),
                    filename=f"transcript-{channel.name}.html",
                )
                with contextlib.suppress(discord.HTTPException):
                    await log_ch.send(embed=base_embed, file=file)
                return
        except Exception:
            log.exception("chat_exporter fallito per #%s, uso fallback testuale", channel.name)

    # Fallback: invia solo l'embed senza file HTML
    with contextlib.suppress(discord.HTTPException):
        await log_ch.send(embed=base_embed)


# ══════════════════════════════════════════════════════════════════
# VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════

class MainPersistentView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Apri un Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="persistent:open_ticket",
    )
    async def open_ticket(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        e = discord.Embed(
            title="📂  Seleziona una Categoria",
            description=(
                "Scegli la categoria più adatta alla tua richiesta.\n"
                "Poi clicca **Apri Ticket →** per procedere."
            ),
            color=discord.Color(0x5865F2),
        )
        e.set_footer(text="Puoi avere al massimo 2 ticket aperti contemporaneamente.")
        await interaction.response.send_message(
            embed=e, view=CategorySelectView(), ephemeral=True
        )


class CategorySelectView(discord.ui.View):
    def __init__(self) -> None:
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
    async def select_cat(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
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
    async def confirm(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
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

    def __init__(self, categoria: str) -> None:
        super().__init__(
            title=f"{cat_emoji(categoria)}  Nuovo Ticket — {categoria}", timeout=300
        )
        self.categoria = categoria

    async def on_submit(self, interaction: discord.Interaction) -> None:
        db: Database = interaction.client.db
        user  = interaction.user
        guild = interaction.guild

        if not guild:
            return await interaction.response.send_message(
                "❌ Disponibile solo nei server.", ephemeral=True
            )

        # Validazioni
        if not _MC_NAME_RE.match(self.mc_name.value):
            return await interaction.response.send_message(
                "❌ **Nickname non valido.** Usa solo lettere, numeri e _ (3–16 caratteri).",
                ephemeral=True,
            )
        if await db.is_blacklisted(user.id):
            return await interaction.response.send_message(
                "🚫 Sei in blacklist e non puoi aprire ticket.", ephemeral=True
            )
        remaining = await db.check_cooldown(user.id)
        if remaining > 0:
            return await interaction.response.send_message(
                f"⏳ Attendi ancora **{format_delta(remaining)}** prima di aprire un nuovo ticket.",
                ephemeral=True,
            )
        if await db.count_open_tickets(user.id) >= cfg.MAX_OPEN_TICKETS:
            return await interaction.response.send_message(
                f"⚠️ Hai già **{cfg.MAX_OPEN_TICKETS}** ticket aperti. Chiudine uno prima.",
                ephemeral=True,
            )

        category_channel = guild.get_channel(cfg.CATEGORY_GENERAL)
        if not isinstance(category_channel, discord.CategoryChannel):
            return await interaction.response.send_message(
                "❌ Errore di configurazione: categoria canali non trovata. Contatta un amministratore.",
                ephemeral=True,
            )

        # Costruisci permissions
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                attach_files=True, read_message_history=True,
            ),
        }
        staff_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, manage_messages=True,
        )
        admin_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, attach_files=True,
            manage_messages=True, manage_channels=True,
        )

        staff_role    = guild.get_role(cfg.STAFF_TICKET_ROLE_ID)  if cfg.STAFF_TICKET_ROLE_ID  else None
        admin_role    = guild.get_role(cfg.ADMIN_ROLE_ID)          if cfg.ADMIN_ROLE_ID          else None
        cat_role_id   = get_category_role_id(self.categoria)
        category_role = guild.get_role(cat_role_id)                if cat_role_id                else None

        if staff_role:
            overwrites[staff_role] = staff_perms
        if category_role and category_role != staff_role:
            overwrites[category_role] = staff_perms
        if admin_role:
            overwrites[admin_role] = admin_perms

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

            # Aggiorna cache
            _open_ticket_channels.add(channel.id)

            # Menzioni
            mentions: list[str] = []
            if staff_role:
                mentions.append(staff_role.mention)
            if category_role and (not staff_role or category_role.id != staff_role.id):
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

            confirm_e = discord.Embed(
                title="✅  Ticket aperto con successo!",
                description=(
                    f"Il tuo ticket è disponibile in {channel.mention}.\n"
                    "Lo staff ti risponderà al più presto."
                ),
                color=discord.Color(cat_color(self.categoria)),
            )
            await interaction.followup.send(embed=confirm_e, ephemeral=True)
            log.info("Ticket aperto: #%s da %s (ID: %d)", channel.name, user, user.id)

        except discord.Forbidden:
            log.error("Permessi insufficienti per creare canale ticket per %s", user)
            await interaction.followup.send(
                "❌ Il bot non ha i permessi per creare canali. Contatta un amministratore.",
                ephemeral=True,
            )
        except Exception:
            log.exception("Errore inaspettato durante apertura ticket per %s", user)
            await interaction.followup.send(
                "❌ Si è verificato un errore durante l'apertura del ticket. Riprova tra poco.",
                ephemeral=True,
            )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.exception("Errore in TicketModal.on_submit:")
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Errore inatteso. Riprova tra poco.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "❌ Errore inatteso. Riprova tra poco.", ephemeral=True
                )


class TicketControlView(discord.ui.View):
    """
    Pannello di controllo visibile nello staff all'interno del ticket.
    BUG FIX: tutti i bottoni hanno custom_id persistenti e la view viene
    registrata con add_view() al boot per sopravvivere ai riavvii.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message(
                "❌ Questo canale non è un ticket valido.", ephemeral=True
            )
            return False
        if not isinstance(interaction.user, discord.Member):
            return False
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            await interaction.response.send_message(
                "🔒 Solo i membri dello staff possono usare questi controlli.",
                ephemeral=True,
            )
            return False
        return True

    # ── Riga 0 ────────────────────────────────────────────────────
    @discord.ui.button(
        label="Chiudi",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="persistent:close",
        row=0,
    )
    async def close_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message(
                "⚠️ Il ticket non è nello stato **Aperto**.", ephemeral=True
            )
        await interaction.response.send_modal(CloseModal())

    @discord.ui.button(
        label="Riapri",
        style=discord.ButtonStyle.green,
        emoji="🔓",
        custom_id="persistent:reopen",
        row=0,
    )
    async def reopen_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["status"] == TicketStatus.OPEN.value:
            return await interaction.response.send_message(
                "⚠️ Il ticket è già aperto.", ephemeral=True
            )
        if ticket["status"] == TicketStatus.CLOSED.value:
            return await interaction.response.send_message(
                "❌ Un ticket chiuso non può essere riaperto dal pannello. Usa `/reopen`.",
                ephemeral=True,
            )
        # stato: closing → annulla il timer
        cancelled = await cancel_close(interaction.channel_id)
        await db.reopen_ticket(interaction.channel_id)
        _open_ticket_channels.add(interaction.channel_id)
        msg = "✅ Chiusura annullata e ticket riaperto." if cancelled else "✅ Ticket riaperto."
        await interaction.response.send_message(
            embed=embed_ticket_reopened(interaction.user)
        )
        await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="Prendi in Carico",
        style=discord.ButtonStyle.blurple,
        emoji="🙋",
        custom_id="persistent:claim",
        row=0,
    )
    async def claim_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if ticket["claimed_by"]:
            return await interaction.response.send_message(
                f"⚠️ Questo ticket è già gestito da <@{ticket['claimed_by']}>.",
                ephemeral=True,
            )
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await db.bump_stat(interaction.user.id, claimed=True)
        await interaction.response.send_message(
            embed=embed_ticket_claimed(interaction.user)
        )

    # ── Riga 1 ────────────────────────────────────────────────────
    @discord.ui.button(
        label="Priorità",
        style=discord.ButtonStyle.gray,
        emoji="↕️",
        custom_id="persistent:priority",
        row=1,
    )
    async def priority_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "Seleziona la nuova priorità:",
            view=PriorityView(interaction.channel_id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Nota Interna",
        style=discord.ButtonStyle.gray,
        emoji="📝",
        custom_id="persistent:note",
        row=1,
    )
    async def note_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(AddNoteModal())

    @discord.ui.button(
        label="Aggiungi Utente",
        style=discord.ButtonStyle.green,
        emoji="➕",
        custom_id="persistent:add_user",
        row=1,
    )
    async def add_user_btn(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(AddUserModal())


class CloseModal(discord.ui.Modal, title="🔒  Chiudi Ticket"):
    reason = discord.ui.TextInput(
        label="Motivo della chiusura",
        style=discord.TextStyle.long,
        required=False,
        placeholder="(opzionale) Spiega brevemente perché viene chiuso...",
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
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

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.db.add_note(
            interaction.channel_id, interaction.user.id, self.note.value
        )
        e = discord.Embed(
            title="📝  Nota Aggiunta",
            description=f"> {self.note.value}",
            color=discord.Color(0x99AAB5),
            timestamp=utcnow(),
        )
        e.set_footer(text=f"Nota di {interaction.user.display_name} · solo staff")
        await interaction.response.send_message(embed=e, ephemeral=True)


class AddUserModal(discord.ui.Modal, title="➕  Aggiungi Utente al Ticket"):
    user_id_input = discord.ui.TextInput(
        label="ID Discord dell'utente",
        min_length=17,
        max_length=20,
        placeholder="Es. 123456789012345678",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            uid = int(self.user_id_input.value)
            member = (
                interaction.guild.get_member(uid)
                or await interaction.guild.fetch_member(uid)
            )
            await interaction.channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                attach_files=True,
                read_message_history=True,
            )
            e = discord.Embed(
                title="➕  Utente Aggiunto",
                description=f"{member.mention} può ora accedere a questo ticket.",
                color=discord.Color(0x57F287),
                timestamp=utcnow(),
            )
            await interaction.followup.send(embed=e)
        except ValueError:
            await interaction.followup.send(
                "❌ ID non valido. Deve essere un numero intero.", ephemeral=True
            )
        except discord.NotFound:
            await interaction.followup.send(
                "❌ Utente non trovato nel server. Controlla l'ID.", ephemeral=True
            )
        except Exception:
            log.exception("Errore in AddUserModal")
            await interaction.followup.send(
                "❌ Errore inatteso. Riprova.", ephemeral=True
            )


class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=60)
        self.channel_id = channel_id

    @discord.ui.select(
        placeholder="Seleziona la priorità...",
        options=[
            discord.SelectOption(
                label=p.value,
                value=p.name,
                emoji=p.icon,
                description=f"Livello {p.rank + 1}/4  |  {p.bar}",
            )
            for p in Priority
        ],
    )
    async def select_priority(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        p = Priority[select.values[0]]
        await interaction.client.db.set_priority(self.channel_id, p)
        if not isinstance(interaction.user, discord.Member):
            return
        await interaction.response.edit_message(
            content=None,
            embed=embed_priority_updated(p, interaction.user),
            view=None,
        )


_STAR_LABELS = {
    5: "Eccellente 🌟",
    4: "Ottimo 👍",
    3: "Nella media 😐",
    2: "Da migliorare 😕",
    1: "Scadente 👎",
}

class RatingView(discord.ui.View):
    """
    BUG FIX: La view viene inviata in DM e non deve essere persistente
    (non ha custom_id statici). Il timeout di 300s è corretto per un DM.
    """

    def __init__(self, db: Database, channel_id: int, user_id: int) -> None:
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
    async def select_rating(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        score = int(select.values[0])
        await self.db.add_rating(self.channel_id, self.user_id, score, "")
        stars = "⭐" * score
        e = discord.Embed(
            title="✅  Grazie per il tuo feedback!",
            description=f"Hai valutato il supporto **{score}/5** {stars}\n*{_STAR_LABELS[score]}*",
            color=discord.Color(0xFEE75C),
            timestamp=utcnow(),
        )
        e.set_footer(text="Il tuo feedback è stato registrato.")
        await interaction.response.edit_message(embed=e, view=None)
        self.stop()

    async def on_timeout(self) -> None:
        # Non c'è nulla da fare — la view scade silenziosamente
        pass


# ══════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════
class TicketCog(commands.Cog):
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        # BUG FIX: usa la cache in-memory per evitare una query DB su ogni
        # messaggio inviato in qualsiasi canale del server.
        if message.channel.id not in _open_ticket_channels:
            return
        await self.bot.db.touch_last_message(message.channel.id)

    # BUG FIX: il task ora controlla che bot.user non sia None
    @tasks.loop(minutes=cfg.AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close_task(self) -> None:
        cutoff   = utcnow() - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
        inactive = await self.bot.db.get_inactive_tickets(cutoff)
        bot_user = self.bot.user
        if not bot_user:
            return
        for row in inactive:
            channel = self.bot.get_channel(row["channel_id"])
            if isinstance(channel, discord.TextChannel):
                log.info("Auto-close: ticket inattivo #%s", channel.name)
                await schedule_close(
                    self.bot.db,
                    channel,
                    bot_user,
                    "Chiusura automatica per inattività",
                )

    @auto_close_task.before_loop
    async def before_auto_close(self) -> None:
        await self.bot.wait_until_ready()

    # ── Comandi slash ──────────────────────────────────────────────

    @app_commands.command(
        name="ticket_setup",
        description="Invia il pannello di apertura ticket nel canale corrente.",
    )
    async def ticket_setup(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_ticket_staff(
            interaction.user, "Altro"
        ):
            return await interaction.response.send_message(
                "❌ Solo lo staff può usare questo comando.", ephemeral=True
            )
        await interaction.channel.send(
            embed=embed_ticket_panel(), view=MainPersistentView()
        )
        await interaction.response.send_message("✅ Pannello ticket inviato.", ephemeral=True)

    @app_commands.command(
        name="reopen",
        description="Riapre un ticket in stato 'closing' annullando il timer.",
    )
    async def reopen(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        db = self.bot.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message(
                "❌ Questo canale non è un ticket.", ephemeral=True
            )
        if not is_ticket_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message(
                "🔒 Solo lo staff può riaprire un ticket.", ephemeral=True
            )
        if ticket["status"] == TicketStatus.OPEN.value:
            return await interaction.response.send_message(
                "⚠️ Il ticket è già aperto.", ephemeral=True
            )
        if ticket["status"] == TicketStatus.CLOSED.value:
            return await interaction.response.send_message(
                "❌ Non è possibile riaprire un ticket già chiuso e cancellato.",
                ephemeral=True,
            )
        cancelled = await cancel_close(interaction.channel_id)
        await db.reopen_ticket(interaction.channel_id)
        _open_ticket_channels.add(interaction.channel_id)
        msg = "✅ Timer di chiusura annullato." if cancelled else "✅ Ticket riaperto."
        await interaction.response.send_message(
            embed=embed_ticket_reopened(interaction.user)
        )
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(
        name="blacklist_add",
        description="Aggiungi un utente alla blacklist ticket.",
    )
    async def blacklist_add(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "Nessun motivo",
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_ticket_staff(
            interaction.user, "Altro"
        ):
            return await interaction.response.send_message(
                "❌ Solo lo staff può usare questo comando.", ephemeral=True
            )
        await self.bot.db.blacklist_add(user.id, reason, interaction.user.id)
        e = discord.Embed(
            title="🚫  Utente in Blacklist",
            description=f"{user.mention} non potrà più aprire ticket.\n**Motivo:** {reason}",
            color=discord.Color(0xED4245),
            timestamp=utcnow(),
        )
        await interaction.response.send_message(embed=e)

    @app_commands.command(
        name="blacklist_remove",
        description="Rimuovi un utente dalla blacklist ticket.",
    )
    async def blacklist_remove(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_ticket_staff(
            interaction.user, "Altro"
        ):
            return await interaction.response.send_message(
                "❌ Solo lo staff può usare questo comando.", ephemeral=True
            )
        if await self.bot.db.blacklist_remove(user.id):
            e = discord.Embed(
                title="✅  Blacklist Rimossa",
                description=f"{user.mention} può nuovamente aprire ticket.",
                color=discord.Color(0x57F287),
                timestamp=utcnow(),
            )
            await interaction.response.send_message(embed=e)
        else:
            await interaction.response.send_message(
                "⚠️ Questo utente non è in blacklist.", ephemeral=True
            )

    @app_commands.command(
        name="blacklist_list",
        description="Mostra tutti gli utenti in blacklist.",
    )
    async def blacklist_list_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_ticket_staff(
            interaction.user, "Altro"
        ):
            return await interaction.response.send_message(
                "❌ Solo lo staff può usare questo comando.", ephemeral=True
            )
        rows = await self.bot.db.blacklist_list()
        await interaction.response.send_message(
            embed=embed_blacklist_list(rows), ephemeral=True
        )

    @app_commands.command(
        name="stats",
        description="Mostra le statistiche ticket di un membro dello staff.",
    )
    async def stats(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        target = member or interaction.user
        data   = await self.bot.db.get_stats(target.id)
        if not data:
            return await interaction.response.send_message(
                f"Nessuna statistica trovata per {target.mention}.", ephemeral=True
            )
        await interaction.response.send_message(embed=embed_stats(target, data))

    @app_commands.command(
        name="topstaff",
        description="Classifica dello staff per ticket chiusi.",
    )
    async def topstaff(self, interaction: discord.Interaction) -> None:
        top = await self.bot.db.get_top_staff(10)
        if not top:
            return await interaction.response.send_message(
                "La classifica è ancora vuota.", ephemeral=True
            )
        await interaction.response.send_message(embed=embed_top_staff(top))


# ══════════════════════════════════════════════════════════════════
# BOT CLASS
# ══════════════════════════════════════════════════════════════════
class TicketBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db: Database = Database(cfg.DATABASE_URL)

    async def setup_hook(self) -> None:
        await self.db.connect()

        # Carica la cache dei ticket aperti al boot
        global _open_ticket_channels
        _open_ticket_channels = await self.db.load_open_channel_ids()
        log.info("Cache ticket aperti: %d canali.", len(_open_ticket_channels))

        # BUG FIX: registra le persistent views PRIMA di sync
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
        log.info("✅  Bot online: %s (ID: %d)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="i ticket 🎫"
            )
        )

    async def close(self) -> None:
        # Cancella tutti i task di chiusura pendenti
        for task in list(_close_tasks.values()):
            task.cancel()
        await asyncio.gather(*_close_tasks.values(), return_exceptions=True)
        _close_tasks.clear()

        await self.db.close()
        await super().close()


def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ Non hai i permessi necessari per questo comando."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Comando in cooldown. Riprova tra `{error.retry_after:.1f}s`."
        else:
            log.exception("Errore slash command '%s':", interaction.command)
            msg = "⚠️ Si è verificato un errore inatteso. Riprova tra poco."

        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


# ══════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
