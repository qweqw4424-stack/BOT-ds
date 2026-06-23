"""
TicketBot v5 – Bot ticket professionale per Discord (PostgreSQL / Railway)
===========================================================================
CHANGELOG v5 (FIX BUG CRITICI + MIGLIORAMENTI):

1. FIX CRITICO – STAFF_TICKET_ROLE_IDS non era una tupla:
   `(1499713651576406020)` è solo un intero tra parentesi (manca la virgola
   finale), NON una tupla a un elemento. Qualsiasi `for ... in` o `set(...)`
   su questo valore lanciava TypeError. Questo rompeva:
     - l'apertura di QUALSIASI ticket (crash in build_channel_overwrites,
       chiamata da TicketModal.on_submit prima della creazione del canale),
     - is_staff() per chiunque non avesse un ruolo Admin (quindi quasi tutti
       i bottoni/comandi staff per lo staff "semplice"),
     - il task di sollecitazione automatica (staff_ping_task).
   FIX: ora è correttamente `(1499713651576406020,)`.

2. FIX – Log di chiusura incompleto:
   `_finalize` generava l'embed di log usando il dict `ticket` letto PRIMA
   dell'update su closed_by/close_reason/status, quindi "Chiuso da" e
   "Motivo" risultavano sempre vuoti nel transcript. Ora il dict locale
   viene aggiornato subito dopo l'update DB riuscito.

3. FIX – Rating DM non persistente tra riavvii:
   La RatingView persistente veniva registrata con channel_id=0 come
   placeholder. Dopo un riavvio del bot, qualunque valutazione su una DM
   inviata in una sessione precedente sarebbe stata salvata sul canale
   sbagliato (0). Aggiunta tabella `pending_ratings` (message_id -> 
   channel_id) per recuperare il canale corretto in modo affidabile,
   indipendentemente da riavvii.

4. FIX – Race condition su chiusura simultanea:
   Due membri dello staff che premevano "Chiudi" nello stesso istante
   potevano entrambi superare il controllo "status == OPEN" prima che il
   primo aggiornamento DB venisse scritto. La transizione open -> closing
   è ora una UPDATE SQL atomica con clausola WHERE status='open', che
   garantisce che solo una delle due richieste abbia successo.

5. FIX – Task in background non resilienti:
   auto_close_task e staff_ping_task non avevano un try/except a livello di
   intera iterazione: un singolo errore (DB irraggiungibile, ruolo
   cancellato, ecc.) interrompeva il loop per sempre fino al riavvio del
   processo. Ora ogni iterazione e ogni singolo ticket processato sono
   protetti, con logging dedicato e un .error() handler di sicurezza.

6. FIX – Modali senza controllo permessi proprio:
   CloseModal, AddNoteModal e AddUserModal si affidavano esclusivamente
   all'interaction_check della View che apre il modal — ma la submit di un
   Modal è un'interazione SEPARATA e l'interaction_check della View non
   viene rieseguito. Aggiunto un controllo is_staff esplicito in ciascun
   modal (defense-in-depth, rilevante anche per la confidenzialità dei
   ticket "Candidatura Staff").

7. Migliorie minori:
   - Fallback a guild.fetch_member() se il proprietario del ticket non è
     in cache al momento dell'invio della DM di valutazione.
   - Parsing sicuro di GUILD_ID da env (niente crash non gestito se non è
     un numero valido).
   - La conferma di cambio priorità è ora un messaggio pubblico nel
     canale (prima era ephemeral, visibile solo a chi l'aveva impostata).
   - Categorie (pannello, select del form, scelte di /move) generate
     dinamicamente da un'unica fonte (CATEGORIA_STYLE) per evitare che
     pannello, modulo e comando vadano fuori sincrono in futuro.
   - Ultimo followup di TicketModal.on_submit ora avvolto in try/except
     (il ticket è comunque già stato creato anche se questo fallisse).

CHANGELOG v4 (FUNZIONALITÀ):
- Sollecitazione automatica staff (ping orario), comando /move,
  comandi nascosti ai non-staff via default_member_permissions.

CHANGELOG v3.1 (FIX eliminazione canale):
- _finalize con fasi indipendenti in try/except, log/DM non bloccanti.
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
# CONFIGURAZIONE
# ══════════════════════════════════════════════════════════════════
def _int_env(name: str, default: str = "0") -> int:
    """Parsing sicuro di una variabile d'ambiente intera: non crasha mai,
    al massimo restituisce 0 (che viene poi intercettato da validate())."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        log.critical("Config: variabile d'ambiente %s non è un intero valido.", name)
        return 0


@dataclass
class Config:
    # ── Da variabili d'ambiente (Railway) ─────────────────────────
    TOKEN:        str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    GUILD_ID:     int = field(default_factory=lambda: _int_env("GUILD_ID"))
    DATABASE_URL: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    # ── Hardcoded – modifica questi valori ────────────────────────
    LOG_CHANNEL_ID:    int = 1517184033803604039
    CATEGORY_GENERAL:  int = 1499713653203533869

    # FIX v5: tupla VERA (nota la virgola finale). Prima era solo un intero
    # tra parentesi e qualunque iterazione su questo valore lanciava TypeError.
    STAFF_TICKET_ROLE_IDS: tuple[int, ...] = (1499713651576406020,)
    ADMIN_ROLE_IDS:        tuple[int, ...] = (1517123223836295188, 1517091767399350342)

    MAX_OPEN_TICKETS:        int = 200
    COOLDOWN_SECONDS:        int = 30
    AUTO_CLOSE_HOURS:        int = 48
    AUTO_CLOSE_CHECK_MINUTES: int = 30
    CLOSE_DELAY:             int = 60    # secondi prima della chiusura definitiva

    # Sollecitazione automatica
    PING_UNANSWERED_HOURS: int = 1   # ore senza risposta staff prima del ping
    PING_COOLDOWN_HOURS:   int = 2   # ore minime tra un ping e il successivo
    PING_CHECK_MINUTES:    int = 15  # frequenza del task di controllo

    CATEGORIE: tuple = field(default_factory=lambda: (
        "Supporto Tecnico",
        "Report Utente",
        "Candidatura Staff",
        "Unisciti al Team",
        "Altro",
    ))

    CATEGORIA_ROLES: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.CATEGORIA_ROLES:
            self.CATEGORIA_ROLES = {
                "Supporto Tecnico":  1499713651576406020,
                "Report Utente":     1499713651576406020,
                # Candidatura Staff → ruolo selezionatori (non staff generico)
                "Candidatura Staff": 1499713651576406024,
                "Unisciti al Team":  1499713651576406024,
                "Altro":             1499713651576406020,
            }

    def validate(self) -> None:
        errors: list[str] = []
        if not self.TOKEN:
            errors.append("BOT_TOKEN mancante")
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL mancante")
        if self.GUILD_ID == 0:
            errors.append("GUILD_ID non valido")
        if self.CATEGORY_GENERAL == 0:
            errors.append("CATEGORY_GENERAL non valido")
        if not isinstance(self.STAFF_TICKET_ROLE_IDS, tuple) or not self.STAFF_TICKET_ROLE_IDS:
            errors.append("STAFF_TICKET_ROLE_IDS deve essere una tupla non vuota di int")
        if errors:
            for e in errors:
                log.critical("Config: %s", e)
            sys.exit(1)


cfg = Config()
cfg.validate()


# ══════════════════════════════════════════════════════════════════
# STILE PER CATEGORIA (unica fonte di verità: pannello, select, /move)
# ══════════════════════════════════════════════════════════════════
CATEGORIA_STYLE: dict[str, dict] = {
    "Supporto Tecnico":  {"color": 0x5865F2, "emoji": "🔧", "icon": "🖥️",
                           "desc": "Problemi in-game, bug, errori tecnici"},
    "Report Utente":     {"color": 0xED4245, "emoji": "🚨", "icon": "📋",
                           "desc": "Segnala un utente per comportamento scorretto"},
    "Candidatura Staff": {"color": 0x57F287, "emoji": "📄", "icon": "🌟",
                           "desc": "Candidati per entrare nel team di staff"},
    "Unisciti al Team":  {"color": 0xFEE75C, "emoji": "🤝", "icon": "🏆",
                           "desc": "Partnership, collaborazioni, proposte"},
    "Altro":             {"color": 0x99AAB5, "emoji": "💬", "icon": "❓",
                           "desc": "Qualsiasi altra richiesta"},
}

def cat_color(c: str) -> int:  return CATEGORIA_STYLE.get(c, CATEGORIA_STYLE["Altro"])["color"]
def cat_emoji(c: str) -> str:  return CATEGORIA_STYLE.get(c, CATEGORIA_STYLE["Altro"])["emoji"]
def cat_icon(c: str)  -> str:  return CATEGORIA_STYLE.get(c, CATEGORIA_STYLE["Altro"])["icon"]
def cat_desc(c: str)  -> str:  return CATEGORIA_STYLE.get(c, CATEGORIA_STYLE["Altro"])["desc"]


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
        return {"Bassa": 0x57F287, "Media": 0xFEE75C, "Alta": 0xE67E22, "Urgente": 0xED4245}[self.value]

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
_MC_NAME_RE         = re.compile(r"^[A-Za-z0-9_]{3,16}$")
_CHANNEL_INVALID_RE = re.compile(r"[^a-z0-9]")

def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def safe_channel_name(username: str, categoria: str) -> str:
    u = _CHANNEL_INVALID_RE.sub("-", username.lower())[:20].strip("-")
    c = _CHANNEL_INVALID_RE.sub("-", categoria.lower())[:15].strip("-")
    name = f"ticket-{u}-{c}"
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name[:100] or f"ticket-{utcnow().timestamp():.0f}"

def format_delta(seconds: int) -> str:
    if seconds < 60:   return f"{seconds}s"
    if seconds < 3600: return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"

def ensure_tz(dt: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    if dt is None: return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)

def get_category_role_id(cat: str) -> Optional[int]:
    return cfg.CATEGORIA_ROLES.get(cat)

def is_staff(member: discord.Member, categoria: str = "Altro") -> bool:
    """Verifica se un membro è considerato staff (admin, staff generico, o
    ruolo specifico per la categoria del ticket)."""
    member_role_ids = {r.id for r in member.roles}
    if member_role_ids & set(cfg.ADMIN_ROLE_IDS):
        return True
    if member_role_ids & set(cfg.STAFF_TICKET_ROLE_IDS):
        return True
    crid = get_category_role_id(categoria)
    return bool(crid and crid in member_role_ids)

def is_admin(member: discord.Member) -> bool:
    """Verifica se un membro ha un ruolo admin."""
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(cfg.ADMIN_ROLE_IDS))


# ══════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    channel_id             BIGINT PRIMARY KEY,
    user_id                BIGINT NOT NULL,
    categoria              TEXT NOT NULL DEFAULT 'Altro',
    priority               TEXT NOT NULL DEFAULT 'Bassa',
    status                 TEXT NOT NULL DEFAULT 'open',
    mc_name                TEXT,
    subject                TEXT,
    description            TEXT,
    opened_at              TIMESTAMPTZ DEFAULT NOW(),
    last_message           TIMESTAMPTZ DEFAULT NOW(),
    last_message_author_id BIGINT,
    last_ping              TIMESTAMPTZ,
    claimed_by             BIGINT,
    closed_by              BIGINT,
    closed_at              TIMESTAMPTZ,
    close_reason           TEXT,
    transcript             BYTEA
);
-- Aggiungi colonne se tabella esiste già (upgrade da versioni precedenti)
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_message_author_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS last_ping TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS blacklist (
    user_id  BIGINT PRIMARY KEY,
    reason   TEXT NOT NULL DEFAULT 'Nessun motivo',
    added_by BIGINT NOT NULL,
    added_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ticket_stats (
    staff_id      BIGINT PRIMARY KEY,
    closed_count  INTEGER NOT NULL DEFAULT 0,
    claimed_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cooldowns (
    user_id   BIGINT PRIMARY KEY,
    last_open TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    channel_id BIGINT PRIMARY KEY,
    user_id    BIGINT NOT NULL,
    score      INTEGER NOT NULL CHECK(score BETWEEN 1 AND 5),
    comment    TEXT,
    rated_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS ticket_notes (
    id         SERIAL PRIMARY KEY,
    channel_id BIGINT NOT NULL REFERENCES tickets(channel_id) ON DELETE CASCADE,
    staff_id   BIGINT NOT NULL,
    note       TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- v5: traccia le DM di valutazione in attesa, per attribuirle al canale
-- corretto anche dopo un riavvio del bot (vedi RatingView).
CREATE TABLE IF NOT EXISTS pending_ratings (
    message_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    user_id    BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn  = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        dsn = self._dsn.replace("postgres://", "postgresql://", 1)
        self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        async with self._pool.acquire() as c:
            await c.execute(_SCHEMA)
        log.info("DB connesso.")

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    # ── Tickets ───────────────────────────────────────────────────
    async def create_ticket(self, channel_id: int, user_id: int, categoria: str,
                             mc_name: str, subject: str, description: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO tickets(channel_id,user_id,categoria,mc_name,subject,description,"
                "last_message_author_id) VALUES($1,$2,$3,$4,$5,$6,$7)",
                channel_id, user_id, categoria, mc_name, subject, description, user_id,
            )

    async def get_ticket(self, channel_id: int) -> Optional[dict]:
        async with self._pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM tickets WHERE channel_id=$1", channel_id)
            return dict(row) if row else None

    async def update_status(self, channel_id: int, status: TicketStatus,
                             closed_by: Optional[int] = None,
                             close_reason: Optional[str] = None) -> None:
        async with self._pool.acquire() as c:
            if status == TicketStatus.CLOSED:
                await c.execute(
                    "UPDATE tickets SET status=$1,closed_by=$2,closed_at=NOW(),close_reason=$3"
                    " WHERE channel_id=$4",
                    status.value, closed_by, close_reason, channel_id,
                )
            else:
                await c.execute("UPDATE tickets SET status=$1 WHERE channel_id=$2",
                                status.value, channel_id)

    async def try_start_closing(self, channel_id: int) -> bool:
        """FIX v5: transizione atomica open -> closing.
        Ritorna True solo se QUESTA chiamata ha effettivamente eseguito la
        transizione, eliminando la race condition di due 'Chiudi' premuti
        in contemporanea (prima si faceva un get_ticket + update separati,
        con una finestra in cui entrambe le richieste vedevano status=open)."""
        async with self._pool.acquire() as c:
            result = await c.execute(
                "UPDATE tickets SET status=$1 WHERE channel_id=$2 AND status=$3",
                TicketStatus.CLOSING.value, channel_id, TicketStatus.OPEN.value,
            )
            return result == "UPDATE 1"

    async def claim_ticket(self, channel_id: int, staff_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute("UPDATE tickets SET claimed_by=$1 WHERE channel_id=$2",
                            staff_id, channel_id)

    async def set_priority(self, channel_id: int, priority: Priority) -> None:
        async with self._pool.acquire() as c:
            await c.execute("UPDATE tickets SET priority=$1 WHERE channel_id=$2",
                            priority.value, channel_id)

    async def touch(self, channel_id: int, author_id: int) -> None:
        """Aggiorna last_message e l'autore dell'ultimo messaggio."""
        async with self._pool.acquire() as c:
            await c.execute(
                "UPDATE tickets SET last_message=NOW(), last_message_author_id=$1"
                " WHERE channel_id=$2",
                author_id, channel_id,
            )

    async def update_last_ping(self, channel_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute("UPDATE tickets SET last_ping=NOW() WHERE channel_id=$1", channel_id)

    async def update_categoria(self, channel_id: int, categoria: str) -> None:
        """Aggiorna la categoria di un ticket (usato da /move)."""
        async with self._pool.acquire() as c:
            await c.execute("UPDATE tickets SET categoria=$1 WHERE channel_id=$2",
                            categoria, channel_id)

    async def count_open(self, user_id: int) -> int:
        async with self._pool.acquire() as c:
            n = await c.fetchval(
                "SELECT COUNT(*) FROM tickets WHERE user_id=$1 AND status='open'", user_id)
            return n or 0

    async def load_open_ids(self) -> set[int]:
        async with self._pool.acquire() as c:
            rows = await c.fetch(
                "SELECT channel_id FROM tickets WHERE status IN ('open','closing')")
            return {r["channel_id"] for r in rows}

    async def get_inactive(self, cutoff: datetime.datetime) -> list[asyncpg.Record]:
        async with self._pool.acquire() as c:
            return await c.fetch(
                "SELECT channel_id FROM tickets WHERE status='open' AND last_message<$1", cutoff)

    async def get_unanswered_tickets(self, unanswered_cutoff: datetime.datetime,
                                      ping_cutoff: datetime.datetime) -> list[asyncpg.Record]:
        """
        Restituisce i ticket aperti dove:
        - last_message_author_id == user_id (ultimo messaggio è dell'utente)
        - last_message < unanswered_cutoff (da più di PING_UNANSWERED_HOURS)
        - (last_ping IS NULL OR last_ping < ping_cutoff) (non pingato di recente)
        """
        async with self._pool.acquire() as c:
            return await c.fetch(
                """
                SELECT channel_id, user_id, categoria, claimed_by
                FROM tickets
                WHERE status = 'open'
                  AND last_message_author_id = user_id
                  AND last_message < $1
                  AND (last_ping IS NULL OR last_ping < $2)
                """,
                unanswered_cutoff, ping_cutoff,
            )

    async def reopen(self, channel_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "UPDATE tickets SET status='open',closed_by=NULL,closed_at=NULL,"
                "close_reason=NULL WHERE channel_id=$1", channel_id)

    # ── Blacklist ─────────────────────────────────────────────────
    async def is_blacklisted(self, user_id: int) -> bool:
        async with self._pool.acquire() as c:
            return bool(await c.fetchrow("SELECT 1 FROM blacklist WHERE user_id=$1", user_id))

    async def blacklist_add(self, user_id: int, reason: str, added_by: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO blacklist(user_id,reason,added_by) VALUES($1,$2,$3)"
                " ON CONFLICT(user_id) DO UPDATE SET reason=$2,added_by=$3,added_at=NOW()",
                user_id, reason, added_by)

    async def blacklist_remove(self, user_id: int) -> bool:
        async with self._pool.acquire() as c:
            r = await c.execute("DELETE FROM blacklist WHERE user_id=$1", user_id)
            return r == "DELETE 1"

    async def blacklist_list(self) -> list[asyncpg.Record]:
        async with self._pool.acquire() as c:
            return await c.fetch("SELECT * FROM blacklist ORDER BY added_at DESC")

    # ── Cooldowns ─────────────────────────────────────────────────
    async def check_cooldown(self, user_id: int) -> int:
        async with self._pool.acquire() as c:
            last = await c.fetchval("SELECT last_open FROM cooldowns WHERE user_id=$1", user_id)
            if last:
                elapsed = (utcnow() - ensure_tz(last)).total_seconds()
                return max(0, int(cfg.COOLDOWN_SECONDS - elapsed))
            return 0

    async def update_cooldown(self, user_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO cooldowns(user_id,last_open) VALUES($1,NOW())"
                " ON CONFLICT(user_id) DO UPDATE SET last_open=NOW()", user_id)

    # ── Notes ─────────────────────────────────────────────────────
    async def add_note(self, channel_id: int, staff_id: int, note: str) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO ticket_notes(channel_id,staff_id,note) VALUES($1,$2,$3)",
                channel_id, staff_id, note)

    async def get_notes(self, channel_id: int) -> list[asyncpg.Record]:
        async with self._pool.acquire() as c:
            return await c.fetch(
                "SELECT * FROM ticket_notes WHERE channel_id=$1 ORDER BY created_at", channel_id)

    # ── Ratings ───────────────────────────────────────────────────
    async def add_rating(self, channel_id: int, user_id: int, score: int,
                          comment: Optional[str]) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO ratings(channel_id,user_id,score,comment) VALUES($1,$2,$3,$4)"
                " ON CONFLICT(channel_id) DO UPDATE SET score=$3,comment=$4,rated_at=NOW()",
                channel_id, user_id, score, comment)

    # ── Pending ratings (v5) ──────────────────────────────────────
    async def add_pending_rating(self, message_id: int, channel_id: int, user_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO pending_ratings(message_id,channel_id,user_id) VALUES($1,$2,$3)"
                " ON CONFLICT(message_id) DO UPDATE SET channel_id=$2,user_id=$3,created_at=NOW()",
                message_id, channel_id, user_id)

    async def get_pending_rating(self, message_id: int) -> Optional[dict]:
        async with self._pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM pending_ratings WHERE message_id=$1", message_id)
            return dict(row) if row else None

    async def remove_pending_rating(self, message_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute("DELETE FROM pending_ratings WHERE message_id=$1", message_id)

    # ── Stats ─────────────────────────────────────────────────────
    async def bump_closed(self, staff_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO ticket_stats(staff_id,closed_count) VALUES($1,1)"
                " ON CONFLICT(staff_id) DO UPDATE SET closed_count=ticket_stats.closed_count+1",
                staff_id)

    async def bump_claimed(self, staff_id: int) -> None:
        async with self._pool.acquire() as c:
            await c.execute(
                "INSERT INTO ticket_stats(staff_id,claimed_count) VALUES($1,1)"
                " ON CONFLICT(staff_id) DO UPDATE SET claimed_count=ticket_stats.claimed_count+1",
                staff_id)

    async def get_stats(self, staff_id: int) -> Optional[dict]:
        async with self._pool.acquire() as c:
            row = await c.fetchrow("SELECT * FROM ticket_stats WHERE staff_id=$1", staff_id)
            return dict(row) if row else None

    async def get_top_staff(self, limit: int = 10) -> list[asyncpg.Record]:
        async with self._pool.acquire() as c:
            return await c.fetch(
                "SELECT * FROM ticket_stats ORDER BY closed_count DESC LIMIT $1", limit)


# ══════════════════════════════════════════════════════════════════
# STATO GLOBALE
# ══════════════════════════════════════════════════════════════════
_close_tasks: dict[int, asyncio.Task] = {}
_open_channels: set[int] = set()


# ══════════════════════════════════════════════════════════════════
# PERMESSI CANALE – helper condiviso
# ══════════════════════════════════════════════════════════════════
def build_channel_overwrites(
    guild: discord.Guild,
    user: discord.Member,
    categoria: str,
) -> dict:
    """
    Costruisce i permission overwrites per un canale ticket.

    Se la categoria è "Candidatura Staff", lo staff generico viene ESCLUSO
    esplicitamente (view_channel=False) per garantire la privacy delle
    candidature. Solo admin e il ruolo selezionatori vedono il canale.
    """
    staff_perms = discord.PermissionOverwrite(
        view_channel=True, send_messages=True,
        attach_files=True, manage_messages=True,
    )
    admin_perms = discord.PermissionOverwrite(
        view_channel=True, send_messages=True,
        attach_files=True, manage_messages=True, manage_channels=True,
    )
    deny_perms = discord.PermissionOverwrite(view_channel=False)

    crid     = get_category_role_id(categoria)
    cat_role = guild.get_role(crid) if crid else None

    overwrites: dict = {
        guild.default_role: deny_perms,
        user: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            attach_files=True, read_message_history=True,
        ),
    }

    # Ruoli admin → sempre accesso pieno
    for aid in cfg.ADMIN_ROLE_IDS:
        role = guild.get_role(aid)
        if role:
            overwrites[role] = admin_perms

    is_candidatura = (categoria == "Candidatura Staff")

    # Staff generico
    for sid in cfg.STAFF_TICKET_ROLE_IDS:
        role = guild.get_role(sid)
        if not role:
            continue
        # Salta se è già stato gestito come admin
        if role in overwrites:
            continue
        if is_candidatura:
            # Nega esplicitamente lo staff generico per le candidature
            overwrites[role] = deny_perms
        else:
            overwrites[role] = staff_perms

    # Ruolo specifico della categoria (es. selezionatori per Candidatura Staff)
    if cat_role and cat_role not in overwrites:
        overwrites[cat_role] = staff_perms

    return overwrites


# ══════════════════════════════════════════════════════════════════
# EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════
def embed_panel() -> discord.Embed:
    cat_lines = "\n".join(
        f"{cat_emoji(c)} **{c}** — {cat_desc(c)}" for c in cfg.CATEGORIE
    )
    e = discord.Embed(
        title="🎫  Centro Assistenza",
        description=(
            "Hai bisogno di aiuto? Il nostro staff è qui per te.\n"
            "**Seleziona una categoria dal menu** per aprire il tuo ticket privato."
        ),
        color=discord.Color(0x5865F2), timestamp=utcnow(),
    )
    e.add_field(name="📋  Categorie", value=cat_lines, inline=False)
    e.add_field(name="⏱️  Risposta", value="Entro **24 ore**", inline=True)
    e.add_field(name="📌  Regola",   value="Un ticket per problema", inline=True)
    e.set_footer(text="Ticket System · Staff Team")
    return e


def embed_opened(user: Union[discord.User, discord.Member], categoria: str,
                  mc: str, subj: str, desc: str, ch_id: int,
                  priority: Priority = Priority.LOW) -> discord.Embed:
    e = discord.Embed(
        title=f"{cat_emoji(categoria)}  Ticket — {categoria}",
        description=f"Benvenuto {user.mention}! Lo staff sarà con te a breve.",
        color=discord.Color(cat_color(categoria)), timestamp=utcnow(),
    )
    e.add_field(name="👤  Utente",                 value=f"{user.mention}\n`{user.id}`", inline=True)
    e.add_field(name="🎮  Minecraft",              value=f"`{mc}`",                      inline=True)
    e.add_field(name=f"{cat_icon(categoria)}  Cat", value=f"`{categoria}`",              inline=True)
    e.add_field(name="📌  Oggetto",  value=subj,              inline=False)
    e.add_field(name="📝  Descrizione", value=f">>> {desc[:1000]}", inline=False)
    e.add_field(name="🎯  Priorità", value=f"{priority.icon} **{priority.value}** `{priority.bar}`", inline=True)
    e.add_field(name="📊  Stato",    value="🟦 Aperto",       inline=True)
    e.add_field(name="🆔  ID",       value=f"`{ch_id}`",      inline=True)
    e.set_author(name=str(user), icon_url=user.display_avatar.url)
    e.set_footer(text="Rispondi qui per comunicare con lo staff")
    return e


def embed_closing(closer: Union[discord.User, discord.Member],
                   delay: int, reason: Optional[str]) -> discord.Embed:
    desc = (
        f"**Richiesta da:** {closer.mention}\n"
        f"**Chiusura tra:** `{format_delta(delay)}`"
    )
    if reason:
        desc += f"\n**Motivo:** {reason}"
    desc += "\n\n> Il transcript verrà salvato automaticamente."
    e = discord.Embed(title="🔒  Chiusura Pianificata", description=desc,
                       color=discord.Color(0xFEE75C), timestamp=utcnow())
    e.set_footer(text="Lo staff può annullare con /reopen")
    return e


def embed_reopened(who: Union[discord.User, discord.Member]) -> discord.Embed:
    e = discord.Embed(
        title="🔓  Ticket Riaperto",
        description=f"{who.mention} ha riaperto questo ticket.",
        color=discord.Color(0x57F287), timestamp=utcnow(),
    )
    return e


def embed_claimed(claimer: discord.Member) -> discord.Embed:
    e = discord.Embed(
        title="🙋  Ticket Preso in Carico",
        description=(
            f"{claimer.mention} ha preso in carico questo ticket.\n"
            f"Sarai assistito da **{claimer.display_name}**."
        ),
        color=discord.Color(0x57F287), timestamp=utcnow(),
    )
    e.set_thumbnail(url=claimer.display_avatar.url)
    return e


def embed_priority_set(p: Priority, setter: discord.Member) -> discord.Embed:
    e = discord.Embed(
        title="↕️  Priorità Aggiornata",
        description=f"**Nuova:** {p.icon} **{p.value}** `{p.bar}`\n**Da:** {setter.mention}",
        color=discord.Color(p.color), timestamp=utcnow(),
    )
    return e


def embed_moved(mover: discord.Member, old_cat: str, new_cat: str) -> discord.Embed:
    """Embed di notifica spostamento categoria."""
    e = discord.Embed(
        title="🔀  Ticket Spostato",
        description=(
            f"**Da:** {cat_emoji(old_cat)} {old_cat}\n"
            f"**A:** {cat_emoji(new_cat)} {new_cat}\n"
            f"**Da:** {mover.mention}"
        ),
        color=discord.Color(cat_color(new_cat)), timestamp=utcnow(),
    )
    e.set_footer(text="I permessi del canale sono stati aggiornati.")
    return e


def embed_staff_ping(unanswered_since: datetime.datetime) -> discord.Embed:
    """Embed inviato quando il bot sollecita lo staff per mancanza di risposta."""
    delta = int((utcnow() - unanswered_since).total_seconds())
    e = discord.Embed(
        title="⏰  Ticket in Attesa di Risposta",
        description=(
            f"L'utente aspetta risposta da **{format_delta(delta)}**.\n"
            "Si prega di rispondere al più presto."
        ),
        color=discord.Color(0xE67E22), timestamp=utcnow(),
    )
    e.set_footer(text="Sollecitazione automatica · TicketBot")
    return e


def embed_log(channel: discord.TextChannel, ticket: dict, notes: list) -> discord.Embed:
    opened = ensure_tz(ticket.get("opened_at"))
    closed = ensure_tz(ticket.get("closed_at")) or utcnow()
    if opened:
        sec = int((closed - opened).total_seconds())
        h, r = divmod(sec, 3600)
        dur = f"{h}h {r//60}m" if h else f"{r//60}m"
    else:
        dur = "—"
    note_txt = "\n".join(
        f"`{r['created_at'].strftime('%d/%m %H:%M')}` <@{r['staff_id']}>: {r['note']}"
        for r in notes
    ) or "*Nessuna.*"
    e = discord.Embed(title="📋  Transcript", color=discord.Color(0x5865F2), timestamp=utcnow())
    e.add_field(name="Canale",    value=f"`#{channel.name}`",          inline=True)
    e.add_field(name="Utente",    value=f"<@{ticket['user_id']}>",     inline=True)
    e.add_field(name="Categoria", value=ticket["categoria"],           inline=True)
    e.add_field(name="Oggetto",   value=ticket.get("subject") or "—",  inline=False)
    e.add_field(name="Durata",    value=dur,                           inline=True)
    e.add_field(name="Chiuso da", value=f"<@{ticket['closed_by']}>" if ticket.get("closed_by") else "—", inline=True)
    e.add_field(name="Motivo",    value=ticket.get("close_reason") or "—", inline=True)
    e.add_field(name="Note",      value=note_txt,                      inline=False)
    e.set_footer(text=f"ID: {channel.id}")
    return e


def embed_rating_dm() -> discord.Embed:
    e = discord.Embed(
        title="⭐  Valuta il Supporto",
        description=(
            "Il tuo ticket è stato chiuso.\n"
            "Seleziona un punteggio dal menu qui sotto per valutare l'assistenza ricevuta.\n"
            "Il tuo feedback aiuta lo staff a migliorare!"
        ),
        color=discord.Color(0xFEE75C), timestamp=utcnow(),
    )
    e.set_footer(text="La valutazione è anonima")
    return e


def embed_stats(member: Union[discord.Member, discord.User], data: dict) -> discord.Embed:
    closed  = data["closed_count"]
    claimed = data["claimed_count"]
    badge = "🥇 Esperto" if closed>=100 else "🥈 Veterano" if closed>=50 else "🥉 Attivo" if closed>=10 else "🌱 Novizio"
    e = discord.Embed(title=f"📊  Stats — {member.display_name}",
                       description=f"**Badge:** {badge}",
                       color=discord.Color(0x57F287), timestamp=utcnow())
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Presi in carico", value=f"`{claimed}`", inline=True)
    e.add_field(name="Chiusi",          value=f"`{closed}`",  inline=True)
    return e


def embed_top(rows: list) -> discord.Embed:
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i-1] if i<=3 else f'`#{i}`'} <@{r['staff_id']}> — **{r['closed_count']}** chiusi"
        for i, r in enumerate(rows, 1)
    ]
    return discord.Embed(title="🏆  Top Staff",
                          description="\n".join(lines) or "*Vuota.*",
                          color=discord.Color(0xFEE75C), timestamp=utcnow())


def embed_blacklist(rows: list) -> discord.Embed:
    lines = [
        f"<@{r['user_id']}> — `{r['reason']}` "
        f"*(da <@{r['added_by']}> il {ensure_tz(r['added_at']).strftime('%d/%m/%Y') if r['added_at'] else '—'})*"
        for r in rows
    ]
    return discord.Embed(title="🚫  Blacklist",
                          description="\n".join(lines) or "*Vuota.*",
                          color=discord.Color(0xED4245), timestamp=utcnow())


# ══════════════════════════════════════════════════════════════════
# CLOSE / FINALIZE
# ══════════════════════════════════════════════════════════════════
async def schedule_close(db: Database, channel: discord.TextChannel,
                          closer: Union[discord.User, discord.Member],
                          reason: Optional[str] = None) -> bool:
    """Avvia la chiusura pianificata. Ritorna True se la chiusura è stata
    effettivamente avviata da QUESTA chiamata (vedi try_start_closing)."""
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] == TicketStatus.CLOSED.value:
        return False

    started = await db.try_start_closing(channel.id)
    if not started:
        with contextlib.suppress(discord.HTTPException):
            await channel.send("⚠️ Chiusura già in corso (o ticket non più aperto).", delete_after=8)
        return False

    await channel.send(embed=embed_closing(closer, cfg.CLOSE_DELAY, reason))

    async def _do() -> None:
        try:
            await asyncio.sleep(cfg.CLOSE_DELAY)
            await _finalize(db, channel, closer, reason)
        except asyncio.CancelledError:
            log.info("Close annullato: %d", channel.id)

    old = _close_tasks.pop(channel.id, None)
    if old and not old.done():
        old.cancel()
    _close_tasks[channel.id] = asyncio.create_task(_do())
    return True


async def cancel_close(channel_id: int) -> bool:
    task = _close_tasks.pop(channel_id, None)
    if task and not task.done():
        task.cancel()
        return True
    return False


async def _finalize(db: Database, channel: discord.TextChannel,
                     closer: Union[discord.User, discord.Member],
                     reason: Optional[str]) -> None:
    ticket = await db.get_ticket(channel.id)
    if not ticket or ticket["status"] not in (TicketStatus.CLOSING.value, TicketStatus.OPEN.value):
        return

    closer_id = closer.id
    closed_at = utcnow()

    # ── 1. Aggiorna DB e cache ────────────────────────────────────
    try:
        await db.update_status(channel.id, TicketStatus.CLOSED, closer_id, reason)
        _open_channels.discard(channel.id)
        # FIX v5: il dict 'ticket' è stato letto PRIMA di questo update.
        # Lo aggiorniamo qui per riflettere i nuovi valori, altrimenti
        # embed_log (sotto) mostrerebbe sempre "Chiuso da: —" e "Motivo: —".
        ticket["status"]       = TicketStatus.CLOSED.value
        ticket["closed_by"]    = closer_id
        ticket["close_reason"] = reason
        ticket["closed_at"]    = closed_at
    except asyncpg.exceptions.PostgresError:
        log.exception("Errore DB update_status in _finalize (canale %s)", channel.name)
        with contextlib.suppress(discord.HTTPException):
            await channel.send("❌ Errore DB durante la chiusura. Riprova.")
        return

    # ── 2. Log + transcript ───────────────────────────────────────
    try:
        log_ch = channel.guild.get_channel(cfg.LOG_CHANNEL_ID)
        if isinstance(log_ch, discord.TextChannel):
            notes   = await db.get_notes(channel.id)
            log_emb = embed_log(channel, ticket, notes)

            if HAS_CHAT_EXPORTER:
                export = await chat_exporter.export(
                    channel, limit=None, tz_info="Europe/Rome",
                    guild=channel.guild, bot=channel.guild.me,
                )
                if export:
                    await log_ch.send(
                        embed=log_emb,
                        file=discord.File(export, f"transcript-{channel.id}.html"),
                    )
                else:
                    await log_ch.send(embed=log_emb, content="⚠️ Export HTML fallito.")
            else:
                msgs = [
                    f"[{m.created_at:%Y-%m-%d %H:%M:%S}] {m.author}: {m.content}"
                    async for m in channel.history(limit=None, oldest_first=True)
                ]
                buf = io.BytesIO("\n".join(msgs).encode())
                await log_ch.send(
                    embed=log_emb,
                    file=discord.File(buf, f"transcript-{channel.id}.txt"),
                )
        else:
            log.warning("Canale log non trovato (ID %d)", cfg.LOG_CHANNEL_ID)
    except Exception:
        log.exception("Errore log/transcript (canale %s) — la chiusura continua", channel.name)

    # ── 3. DM rating all'utente ───────────────────────────────────
    try:
        owner = channel.guild.get_member(ticket["user_id"])
        if owner is None:
            # FIX v5: fallback se l'utente non è nella cache dei membri.
            with contextlib.suppress(discord.HTTPException):
                owner = await channel.guild.fetch_member(ticket["user_id"])
        if owner:
            dm_msg: Optional[discord.Message] = None
            with contextlib.suppress(discord.Forbidden):
                dm_msg = await owner.send(embed=embed_rating_dm(), view=RatingView(channel.id))
            if dm_msg:
                # FIX v5: registra la DM come "in attesa di voto" così, anche
                # dopo un riavvio del bot, sapremo a quale canale corrisponde.
                with contextlib.suppress(Exception):
                    await db.add_pending_rating(dm_msg.id, channel.id, owner.id)
    except Exception:
        log.exception("Errore DM rating (canale %s) — la chiusura continua", channel.name)

    # ── 4. Statistiche staff ──────────────────────────────────────
    try:
        await db.bump_closed(closer_id)
    except Exception:
        log.exception("Errore bump_closed staff_id=%d — la chiusura continua", closer_id)

    # ── 5. ELIMINA IL CANALE ──────────────────────────────────────
    try:
        await channel.delete(reason=f"Ticket chiuso da closer_id={closer_id}")
        log.info("Ticket #%s eliminato correttamente (closer_id=%d).", channel.name, closer_id)
    except discord.NotFound:
        log.warning("Canale %d già eliminato manualmente, nessun problema.", channel.id)
    except discord.Forbidden:
        log.error("Permessi insufficienti per eliminare il canale %d.", channel.id)
        with contextlib.suppress(discord.HTTPException):
            await channel.send(
                "❌ Non ho i permessi per eliminare questo canale. "
                "Contatta un amministratore."
            )
    except discord.HTTPException as ex:
        log.exception("HTTPException eliminando canale %d: %s", channel.id, ex)
    except Exception:
        log.exception("Errore inatteso eliminando canale %d", channel.id)


# ══════════════════════════════════════════════════════════════════
# VIEWS & MODALS
# ══════════════════════════════════════════════════════════════════

class MainPersistentView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="📂  Seleziona categoria e apri il ticket...",
        min_values=1, max_values=1,
        custom_id="persistent:cat_select",
        options=[
            discord.SelectOption(label=c, emoji=cat_emoji(c), value=c, description=cat_desc(c))
            for c in cfg.CATEGORIE
        ],
    )
    async def select_cat(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        categoria = select.values[0]
        log.info("Utente %s ha selezionato categoria: %s", interaction.user, categoria)
        try:
            await interaction.response.send_modal(TicketModal(categoria))
        except Exception:
            log.exception("Errore send_modal per utente %s", interaction.user)
            with contextlib.suppress(discord.HTTPException):
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Errore nell'apertura del form. Riprova tra qualche secondo.",
                        ephemeral=True,
                    )


class TicketModal(discord.ui.Modal):
    def __init__(self, categoria: str) -> None:
        super().__init__(title=f"{cat_emoji(categoria)} Ticket — {categoria}", timeout=300)
        self.categoria = categoria

        self.mc = discord.ui.TextInput(
            label="Nickname Minecraft",
            placeholder="Es. Steve123  (3–16 caratteri, lettere/numeri/_)",
            min_length=3, max_length=16,
            required=True,
        )
        self.subj = discord.ui.TextInput(
            label="Oggetto della richiesta",
            placeholder="Breve titolo (es. 'Non riesco ad accedere al rank')",
            min_length=5, max_length=60,
            required=True,
        )
        self.desc = discord.ui.TextInput(
            label="Descrizione dettagliata",
            style=discord.TextStyle.long,
            placeholder=(
                "Descrivi il problema:\n"
                "• Cosa stavi facendo?\n"
                "• Quando è successo?\n"
                "• Hai già provato qualcosa?"
            ),
            min_length=20, max_length=1000,
            required=True,
        )
        self.add_item(self.mc)
        self.add_item(self.subj)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        db: Database  = interaction.client.db
        user          = interaction.user
        guild         = interaction.guild

        async def reply(msg: str) -> None:
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(msg, ephemeral=True)

        if not guild:
            return await reply("❌ Disponibile solo nei server Discord.")

        mc_val   = self.mc.value.strip()
        subj_val = self.subj.value.strip()
        desc_val = self.desc.value.strip()

        if not _MC_NAME_RE.match(mc_val):
            return await reply("❌ **Nickname Minecraft non valido.** Usa 3–16 caratteri: lettere, numeri, _")

        try:
            if await db.is_blacklisted(user.id):
                return await reply("🚫 Sei in blacklist e non puoi aprire ticket.")
        except Exception:
            log.exception("Errore DB check_blacklist per %s", user)
            return await reply("❌ Errore interno. Riprova tra poco.")

        try:
            remaining = await db.check_cooldown(user.id)
            if remaining > 0:
                return await reply(f"⏳ Attendi **{format_delta(remaining)}** prima di aprire un nuovo ticket.")
        except Exception:
            log.exception("Errore DB check_cooldown per %s", user)

        try:
            if await db.count_open(user.id) >= cfg.MAX_OPEN_TICKETS:
                return await reply(f"⚠️ Hai già **{cfg.MAX_OPEN_TICKETS}** ticket aperti. Chiudine uno prima.")
        except Exception:
            log.exception("Errore DB count_open per %s", user)
            return await reply("❌ Errore interno. Riprova tra poco.")

        cat_ch = guild.get_channel(cfg.CATEGORY_GENERAL)
        if not isinstance(cat_ch, discord.CategoryChannel):
            log.error("CATEGORY_GENERAL %d non trovato o non è CategoryChannel", cfg.CATEGORY_GENERAL)
            return await reply("❌ Configurazione errata: categoria canali non trovata. Contatta un admin.")

        try:
            overwrites = build_channel_overwrites(guild, user, self.categoria)
        except Exception:
            log.exception("Errore build_channel_overwrites per utente %s, categoria %s", user, self.categoria)
            return await reply("❌ Errore interno nel calcolo dei permessi. Contatta un admin.")

        ch_name = safe_channel_name(user.name, self.categoria)

        try:
            channel = await guild.create_text_channel(
                ch_name,
                category=cat_ch,
                overwrites=overwrites,
                topic=f"Ticket di {user.name} | {self.categoria} | MC: {mc_val}",
                reason=f"Ticket aperto da {user} ({user.id})",
            )
            log.info("Canale ticket creato: #%s (ID %d)", channel.name, channel.id)
        except discord.Forbidden:
            log.error("Permessi mancanti per creare canale ticket (utente %s)", user)
            return await reply("❌ Il bot non ha i permessi per creare canali. Contatta un admin.")
        except discord.HTTPException as ex:
            log.exception("HTTPException creando canale per %s: %s", user, ex)
            return await reply("❌ Errore Discord durante la creazione del canale. Riprova.")
        except Exception:
            log.exception("Errore inatteso creando canale per %s", user)
            return await reply("❌ Errore inatteso. Riprova tra poco.")

        try:
            await db.create_ticket(channel.id, user.id, self.categoria, mc_val, subj_val, desc_val)
            await db.update_cooldown(user.id)
            _open_channels.add(channel.id)
        except Exception:
            log.exception("Errore DB dopo creazione canale %d", channel.id)
            with contextlib.suppress(Exception):
                await channel.delete(reason="Rollback: errore DB post-creazione")
            return await reply("❌ Errore DB durante la creazione del ticket. Riprova.")

        # Costruisce le menzioni per il messaggio di apertura
        mention_ids: set[int] = set()
        crid = get_category_role_id(self.categoria)
        if crid:
            mention_ids.add(crid)
        # Per categorie non-candidatura, pinga anche lo staff generico
        if self.categoria != "Candidatura Staff":
            mention_ids.update(cfg.STAFF_TICKET_ROLE_IDS)
        mentions = " ".join(
            role.mention
            for rid in mention_ids
            if (role := guild.get_role(rid))
        )

        try:
            await channel.send(
                content=f"{user.mention} {mentions}".strip(),
                embed=embed_opened(user, self.categoria, mc_val, subj_val, desc_val, channel.id),
                view=TicketControlView(),
            )
        except Exception:
            log.exception("Errore inviando embed nel canale ticket %d", channel.id)

        e = discord.Embed(
            title="✅  Ticket aperto!",
            description=f"Il tuo ticket è in {channel.mention}.\nLo staff ti risponderà al più presto.",
            color=discord.Color(cat_color(self.categoria)),
        )
        try:
            await interaction.followup.send(embed=e, ephemeral=True)
        except discord.HTTPException:
            # FIX v5: il ticket è comunque già stato creato con successo,
            # quindi un fallimento qui (es. token interazione scaduto) non
            # deve essere trattato come un errore bloccante.
            log.warning("Followup finale fallito per canale %d (ticket comunque creato)", channel.id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Eccezione non gestita in TicketModal (utente %s):", interaction.user)
        with contextlib.suppress(discord.HTTPException):
            msg = "❌ Si è verificato un errore inatteso. Riprova tra qualche secondo."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            await interaction.response.send_message("❌ Canale non ticket.", ephemeral=True)
            return False
        if not is_staff(interaction.user, ticket["categoria"]):
            await interaction.response.send_message("🔒 Solo lo staff.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Chiudi",           style=discord.ButtonStyle.red,     emoji="🔒", custom_id="tc:close",    row=0)
    async def close_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Ticket non trovato.", ephemeral=True)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Ticket non in stato Aperto.", ephemeral=True)
        await interaction.response.send_modal(CloseModal())

    @discord.ui.button(label="Riapri",           style=discord.ButtonStyle.green,   emoji="🔓", custom_id="tc:reopen",  row=0)
    async def reopen_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Ticket non trovato.", ephemeral=True)
        if ticket["status"] == TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Già aperto.", ephemeral=True)
        if ticket["status"] == TicketStatus.CLOSED.value:
            return await interaction.response.send_message("❌ Ticket già chiuso definitivamente.", ephemeral=True)
        await cancel_close(interaction.channel_id)
        await db.reopen(interaction.channel_id)
        _open_channels.add(interaction.channel_id)
        await interaction.response.send_message(embed=embed_reopened(interaction.user))

    @discord.ui.button(label="Prendi in Carico", style=discord.ButtonStyle.blurple, emoji="🙋", custom_id="tc:claim",   row=0)
    async def claim_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        db: Database = interaction.client.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Ticket non trovato.", ephemeral=True)
        if ticket.get("claimed_by"):
            return await interaction.response.send_message(
                f"⚠️ Già gestito da <@{ticket['claimed_by']}>.", ephemeral=True)
        await db.claim_ticket(interaction.channel_id, interaction.user.id)
        await db.bump_claimed(interaction.user.id)
        await interaction.response.send_message(embed=embed_claimed(interaction.user))

    @discord.ui.button(label="Priorità",         style=discord.ButtonStyle.gray,    emoji="↕️", custom_id="tc:priority", row=1)
    async def priority_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Seleziona la priorità:", view=PriorityView(interaction.channel_id), ephemeral=True)

    @discord.ui.button(label="Nota Interna",     style=discord.ButtonStyle.gray,    emoji="📝", custom_id="tc:note",    row=1)
    async def note_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddNoteModal())

    @discord.ui.button(label="Aggiungi Utente",  style=discord.ButtonStyle.green,   emoji="➕", custom_id="tc:adduser", row=1)
    async def add_user_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddUserModal())


async def _check_staff_for_modal(interaction: discord.Interaction) -> Optional[dict]:
    """Helper condiviso dai Modal aperti dai bottoni di TicketControlView.

    FIX v5: la submit di un Modal è un'interazione SEPARATA da quella che lo
    ha aperto (il click sul bottone), quindi l'interaction_check della View
    NON viene rieseguito qui. Senza questo controllo esplicito, i modal
    Chiudi/Nota/AggiungiUtente non avevano alcuna verifica di permesso
    propria. Ritorna il dict del ticket se l'utente è staff, altrimenti
    None (e ha già risposto con un messaggio di errore)."""
    if not isinstance(interaction.user, discord.Member) or not interaction.channel_id:
        await interaction.response.send_message("❌ Disponibile solo nei canali dei server.", ephemeral=True)
        return None
    db: Database = interaction.client.db
    ticket = await db.get_ticket(interaction.channel_id)
    if not ticket:
        await interaction.response.send_message("❌ Non è un canale ticket.", ephemeral=True)
        return None
    if not is_staff(interaction.user, ticket["categoria"]):
        await interaction.response.send_message("🔒 Solo lo staff.", ephemeral=True)
        return None
    return ticket


class CloseModal(discord.ui.Modal, title="🔒 Chiudi Ticket"):
    reason = discord.ui.TextInput(
        label="Motivo (opzionale)", style=discord.TextStyle.long,
        required=False, max_length=300,
        placeholder="Spiega brevemente perché viene chiuso...",
    )
    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket = await _check_staff_for_modal(interaction)
        if ticket is None:
            return
        await interaction.response.defer(ephemeral=True)
        ok = await schedule_close(
            interaction.client.db, interaction.channel,
            interaction.user, self.reason.value or None,
        )
        if ok:
            await interaction.followup.send("✅ Chiusura avviata.", ephemeral=True)
        else:
            await interaction.followup.send(
                "⚠️ Chiusura non avviata: il ticket era già in chiusura o non più aperto.", ephemeral=True)


class AddNoteModal(discord.ui.Modal, title="📝 Nota Interna"):
    note = discord.ui.TextInput(
        label="Nota (visibile solo allo staff)", style=discord.TextStyle.long,
        min_length=1, max_length=500,
        placeholder="Scrivi la nota qui...",
    )
    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket = await _check_staff_for_modal(interaction)
        if ticket is None:
            return
        await interaction.client.db.add_note(interaction.channel_id, interaction.user.id, self.note.value)
        await interaction.response.send_message("✅ Nota aggiunta.", ephemeral=True)


class AddUserModal(discord.ui.Modal, title="➕ Aggiungi Utente"):
    user_id_input = discord.ui.TextInput(
        label="ID Utente o @Menzione",
        placeholder="123456789012345678",
        min_length=1, max_length=50,
    )
    async def on_submit(self, interaction: discord.Interaction) -> None:
        ticket = await _check_staff_for_modal(interaction)
        if ticket is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        raw = self.user_id_input.value.strip()
        m = re.match(r"<@!?(\d+)>", raw)
        try:
            tid = int(m.group(1) if m else raw)
        except ValueError:
            return await interaction.followup.send("❌ ID non valido.", ephemeral=True)

        if not interaction.guild:
            return await interaction.followup.send("❌ Solo nei server.", ephemeral=True)
        member = interaction.guild.get_member(tid)
        if not member:
            return await interaction.followup.send("❌ Utente non trovato.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.followup.send("❌ Solo nei canali ticket.", ephemeral=True)

        try:
            await interaction.channel.set_permissions(
                member, view_channel=True, send_messages=True,
                attach_files=True, read_message_history=True)
            await interaction.followup.send(f"✅ {member.mention} aggiunto.")
            await interaction.channel.send(f"👋 Benvenuto {member.mention}! Sei stato aggiunto al ticket.")
        except discord.Forbidden:
            await interaction.followup.send("❌ Permessi insufficienti.", ephemeral=True)


class PriorityView(discord.ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=120)
        self.channel_id = channel_id

    @discord.ui.select(
        placeholder="Seleziona priorità...", min_values=1, max_values=1,
        options=[
            discord.SelectOption(label="Urgente", emoji="🚨", value="Urgente"),
            discord.SelectOption(label="Alta",    emoji="🔴", value="Alta"),
            discord.SelectOption(label="Media",   emoji="🟡", value="Media"),
            discord.SelectOption(label="Bassa",   emoji="🟢", value="Bassa"),
        ],
    )
    async def sel(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        p = Priority(select.values[0])
        await interaction.client.db.set_priority(self.channel_id, p)
        # FIX v5: prima era ephemeral (visibile solo a chi l'ha impostata).
        # Un cambio di priorità riguarda tutti nel ticket, quindi ora è un
        # messaggio pubblico nel canale.
        await interaction.response.send_message(embed=embed_priority_set(p, interaction.user), ephemeral=False)
        self.stop()


_STARS = {1: "⭐☆☆☆☆", 2: "⭐⭐☆☆☆", 3: "⭐⭐⭐☆☆", 4: "⭐⭐⭐⭐☆", 5: "⭐⭐⭐⭐⭐"}


class RatingView(discord.ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.select(
        placeholder="Valuta il supporto (1–5 stelle)...",
        min_values=1, max_values=1,
        custom_id="persistent:rating",
        options=[
            discord.SelectOption(label=f"{_STARS[5]} Eccellente", value="5"),
            discord.SelectOption(label=f"{_STARS[4]} Buono",      value="4"),
            discord.SelectOption(label=f"{_STARS[3]} Neutro",     value="3"),
            discord.SelectOption(label=f"{_STARS[2]} Scarso",     value="2"),
            discord.SelectOption(label=f"{_STARS[1]} Pessimo",    value="1"),
        ],
    )
    async def sel(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        score      = int(select.values[0])
        db: Database = interaction.client.db

        # FIX v5: la view persistente registrata all'avvio del bot usa
        # channel_id=0 come placeholder (non può conoscere in anticipo a
        # quale ticket corrisponderà ogni DM futura). Per recuperare il
        # canale corretto, anche dopo un riavvio, consultiamo la tabella
        # pending_ratings usando l'ID del messaggio della DM cliccata.
        channel_id = self.channel_id
        msg_id = interaction.message.id if interaction.message else None
        if msg_id:
            with contextlib.suppress(Exception):
                pending = await db.get_pending_rating(msg_id)
                if pending:
                    channel_id = pending["channel_id"]

        class CommentModal(discord.ui.Modal, title="💬 Commento (opzionale)"):
            comment = discord.ui.TextInput(
                label="Cosa potremmo migliorare?", style=discord.TextStyle.long,
                required=False, max_length=500,
            )
            async def on_submit(inner, mi: discord.Interaction) -> None:
                try:
                    await db.add_rating(channel_id, mi.user.id, score, inner.comment.value or None)
                    if msg_id:
                        with contextlib.suppress(Exception):
                            await db.remove_pending_rating(msg_id)
                except Exception:
                    log.exception("Errore salvataggio rating channel=%d", channel_id)
                await mi.response.send_message(f"✅ Grazie! Hai votato {_STARS[score]}.", ephemeral=True)

        await interaction.response.send_modal(CommentModal())


# ══════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════

# Shorthand per i permessi nativi Discord (chi vede il comando nel suggeritore)
_STAFF_PERMS = discord.Permissions(manage_messages=True)


class TicketCog(commands.Cog):
    def __init__(self, bot: "TicketBot") -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if message.channel.id in _open_channels:
            await self.bot.db.touch(message.channel.id, message.author.id)

    # ── Task: auto-close ──────────────────────────────────────────
    @tasks.loop(minutes=cfg.AUTO_CLOSE_CHECK_MINUTES)
    async def auto_close_task(self) -> None:
        if not self.bot.user:
            return
        try:
            cutoff   = utcnow() - datetime.timedelta(hours=cfg.AUTO_CLOSE_HOURS)
            inactive = await self.bot.db.get_inactive(cutoff)
        except Exception:
            # FIX v5: senza questo try/except, un errore DB qui terminava
            # per sempre il loop (fino al riavvio del processo).
            log.exception("Errore nel task auto_close_task durante il recupero ticket inattivi")
            return

        for row in inactive:
            try:
                ch = self.bot.get_channel(row["channel_id"])
                if isinstance(ch, discord.TextChannel):
                    log.info("Auto-close: #%s", ch.name)
                    await schedule_close(self.bot.db, ch, self.bot.user, "Chiusura automatica per inattività")
            except Exception:
                log.exception("Errore auto-close canale %d — continuo con gli altri", row["channel_id"])

    @auto_close_task.before_loop
    async def _wait_auto_close(self) -> None:
        await self.bot.wait_until_ready()

    @auto_close_task.error
    async def _auto_close_error(self, error: Exception) -> None:
        log.exception("Eccezione non gestita in auto_close_task: %s", error)

    # ── Task: ping automatico staff ────────────────────────────────
    @tasks.loop(minutes=cfg.PING_CHECK_MINUTES)
    async def staff_ping_task(self) -> None:
        """
        Ogni PING_CHECK_MINUTES minuti controlla i ticket aperti dove:
        - l'ultimo messaggio è dell'utente
        - non c'è stata risposta da più di PING_UNANSWERED_HOURS
        - non è già stato pingato nelle ultime PING_COOLDOWN_HOURS

        Se le condizioni sono soddisfatte, pinga nel canale il ruolo staff
        responsabile della categoria di quel ticket.
        """
        if not self.bot.user:
            return

        unanswered_cutoff = utcnow() - datetime.timedelta(hours=cfg.PING_UNANSWERED_HOURS)
        ping_cutoff       = utcnow() - datetime.timedelta(hours=cfg.PING_COOLDOWN_HOURS)

        try:
            rows = await self.bot.db.get_unanswered_tickets(unanswered_cutoff, ping_cutoff)
        except Exception:
            log.exception("Errore nel task staff_ping_task durante il recupero ticket")
            return

        for row in rows:
            # FIX v5: tutto il corpo del singolo ticket è ora protetto, così
            # un problema su UN ticket (es. ruolo cancellato, permessi) non
            # blocca l'elaborazione degli altri né uccide il loop.
            try:
                ch = self.bot.get_channel(row["channel_id"])
                if not isinstance(ch, discord.TextChannel):
                    continue

                categoria = row["categoria"]
                guild     = ch.guild

                crid = get_category_role_id(categoria)
                role = guild.get_role(crid) if crid else None
                if not role:
                    for sid in cfg.STAFF_TICKET_ROLE_IDS:
                        role = guild.get_role(sid)
                        if role:
                            break

                mention = role.mention if role else "@staff"

                ticket = await self.bot.db.get_ticket(row["channel_id"])
                last_msg_time = ensure_tz(ticket.get("last_message")) if ticket else None

                await ch.send(
                    content=mention,
                    embed=embed_staff_ping(last_msg_time or unanswered_cutoff),
                )
                await self.bot.db.update_last_ping(row["channel_id"])
                log.info("Staff ping inviato per canale %d (categoria: %s)", row["channel_id"], categoria)
            except discord.Forbidden:
                log.warning("Permessi insufficienti per inviare ping nel canale %d", row["channel_id"])
            except Exception:
                log.exception("Errore durante il ping nel canale %d", row["channel_id"])

    @staff_ping_task.before_loop
    async def _wait_ping(self) -> None:
        await self.bot.wait_until_ready()

    @staff_ping_task.error
    async def _staff_ping_error(self, error: Exception) -> None:
        log.exception("Eccezione non gestita in staff_ping_task: %s", error)

    # ── Slash commands ─────────────────────────────────────────────

    @app_commands.command(name="ticket_setup", description="Invia il pannello ticket nel canale corrente.")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_setup(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        await interaction.channel.send(embed=embed_panel(), view=MainPersistentView())
        await interaction.response.send_message("✅ Pannello inviato.", ephemeral=True)

    @app_commands.command(name="reopen", description="Riapre un ticket in chiusura, annullando il timer.")
    @app_commands.default_permissions(manage_messages=True)
    async def reopen_cmd(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member):
            return
        db     = self.bot.db
        ticket = await db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Non è un canale ticket.", ephemeral=True)
        if not is_staff(interaction.user, ticket["categoria"]):
            return await interaction.response.send_message("🔒 Solo lo staff.", ephemeral=True)
        if ticket["status"] == TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Già aperto.", ephemeral=True)
        if ticket["status"] == TicketStatus.CLOSED.value:
            return await interaction.response.send_message("❌ Già chiuso definitivamente.", ephemeral=True)
        await cancel_close(interaction.channel_id)
        await db.reopen(interaction.channel_id)
        _open_channels.add(interaction.channel_id)
        await interaction.response.send_message(embed=embed_reopened(interaction.user))

    @app_commands.command(name="adduser", description="Aggiungi un utente al ticket corrente.")
    @app_commands.default_permissions(manage_messages=True)
    async def adduser_cmd(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Solo nei canali ticket.", ephemeral=True)
        try:
            await interaction.channel.set_permissions(
                user, view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
            await interaction.response.send_message(f"✅ {user.mention} aggiunto.")
            await interaction.channel.send(f"👋 Benvenuto {user.mention}!")
        except discord.Forbidden:
            await interaction.response.send_message("❌ Permessi insufficienti.", ephemeral=True)

    @app_commands.command(name="removeuser", description="Rimuovi un utente dal ticket corrente.")
    @app_commands.default_permissions(manage_messages=True)
    async def removeuser_cmd(self, interaction: discord.Interaction, user: discord.Member) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Solo nei canali ticket.", ephemeral=True)
        try:
            await interaction.channel.set_permissions(user, overwrite=None)
            await interaction.response.send_message(f"✅ {user.mention} rimosso.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Permessi insufficienti.", ephemeral=True)

    @app_commands.command(name="ticket_info", description="Mostra le informazioni del ticket corrente.")
    @app_commands.default_permissions(manage_messages=True)
    async def ticket_info(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        ticket = await self.bot.db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("❌ Non è un canale ticket.", ephemeral=True)
        e = discord.Embed(title="🎫  Info Ticket", color=discord.Color(cat_color(ticket["categoria"])),
                           timestamp=utcnow())
        e.add_field(name="Utente",    value=f"<@{ticket['user_id']}>",   inline=True)
        e.add_field(name="Categoria", value=ticket["categoria"],         inline=True)
        e.add_field(name="Stato",     value=ticket["status"],            inline=True)
        e.add_field(name="Priorità",  value=ticket["priority"],          inline=True)
        e.add_field(name="Oggetto",   value=ticket.get("subject") or "—", inline=False)
        e.add_field(name="MC Nick",   value=ticket.get("mc_name") or "—", inline=True)
        e.add_field(name="Claimed",   value=f"<@{ticket['claimed_by']}>" if ticket.get("claimed_by") else "—", inline=True)
        opened = ensure_tz(ticket.get("opened_at"))
        e.add_field(name="Aperto il", value=opened.strftime("%d/%m/%Y %H:%M") if opened else "—", inline=True)
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ── /move – Trasferimento ticket tra categorie ─────────────────
    @app_commands.command(
        name="move",
        description="Sposta il ticket in un'altra categoria, aggiornando permessi e ruoli.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(nuova_categoria="La nuova categoria per questo ticket")
    @app_commands.choices(nuova_categoria=[
        app_commands.Choice(name=c, value=c) for c in cfg.CATEGORIE
    ])
    async def move_cmd(self, interaction: discord.Interaction, nuova_categoria: str) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)

        db     = self.bot.db
        ticket = await db.get_ticket(interaction.channel_id)

        if not ticket:
            return await interaction.response.send_message("❌ Non è un canale ticket.", ephemeral=True)
        if ticket["status"] != TicketStatus.OPEN.value:
            return await interaction.response.send_message("⚠️ Solo i ticket aperti possono essere spostati.", ephemeral=True)

        old_categoria = ticket["categoria"]
        if old_categoria == nuova_categoria:
            return await interaction.response.send_message(
                f"⚠️ Il ticket è già in categoria **{nuova_categoria}**.", ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)

        guild   = interaction.guild
        channel = interaction.channel

        if not guild or not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send("❌ Errore: canale o server non valido.", ephemeral=True)

        # Recupera il membro proprietario del ticket (fallback sul bot se ha lasciato il server)
        ticket_owner = guild.get_member(ticket["user_id"])
        fake_user = ticket_owner or guild.me

        new_overwrites = build_channel_overwrites(guild, fake_user, nuova_categoria)

        try:
            await channel.edit(
                overwrites=new_overwrites,
                name=safe_channel_name(
                    ticket_owner.name if ticket_owner else str(ticket["user_id"]),
                    nuova_categoria,
                ),
                topic=(
                    f"Ticket di {ticket_owner.name if ticket_owner else ticket['user_id']} "
                    f"| {nuova_categoria} | MC: {ticket.get('mc_name', '?')}"
                ),
                reason=f"Ticket spostato da {interaction.user} ({old_categoria} → {nuova_categoria})",
            )
        except discord.Forbidden:
            return await interaction.followup.send("❌ Permessi insufficienti per modificare il canale.", ephemeral=True)
        except discord.HTTPException as ex:
            log.exception("HTTPException durante /move canale %d: %s", channel.id, ex)
            return await interaction.followup.send("❌ Errore Discord. Riprova.", ephemeral=True)

        try:
            await db.update_categoria(channel.id, nuova_categoria)
        except Exception:
            log.exception("Errore DB aggiornamento categoria canale %d", channel.id)
            await interaction.followup.send(
                "⚠️ Canale aggiornato ma errore nel DB. Contatta un admin.", ephemeral=True)
            return

        crid     = get_category_role_id(nuova_categoria)
        new_role = guild.get_role(crid) if crid else None
        mention  = new_role.mention if new_role else ""

        await channel.send(
            content=mention,
            embed=embed_moved(interaction.user, old_categoria, nuova_categoria),
        )
        await interaction.followup.send(
            f"✅ Ticket spostato da **{old_categoria}** a **{nuova_categoria}**.", ephemeral=True)
        log.info(
            "Ticket %d spostato da '%s' a '%s' da %s",
            channel.id, old_categoria, nuova_categoria, interaction.user,
        )

    # ── Blacklist ──────────────────────────────────────────────────
    @app_commands.command(name="blacklist_add", description="Aggiungi utente alla blacklist ticket.")
    @app_commands.default_permissions(manage_messages=True)
    async def bl_add(self, interaction: discord.Interaction, user: discord.User,
                      reason: str = "Nessun motivo") -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        await self.bot.db.blacklist_add(user.id, reason, interaction.user.id)
        e = discord.Embed(title="🚫  Blacklist",
                           description=f"{user.mention} aggiunto.\n**Motivo:** {reason}",
                           color=discord.Color(0xED4245), timestamp=utcnow())
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="blacklist_remove", description="Rimuovi utente dalla blacklist.")
    @app_commands.default_permissions(manage_messages=True)
    async def bl_remove(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        ok = await self.bot.db.blacklist_remove(user.id)
        if ok:
            await interaction.response.send_message(
                embed=discord.Embed(title="✅  Rimosso", description=f"{user.mention} rimosso dalla blacklist.",
                                     color=discord.Color(0x57F287), timestamp=utcnow()))
        else:
            await interaction.response.send_message("⚠️ Utente non in blacklist.", ephemeral=True)

    @app_commands.command(name="blacklist_list", description="Lista utenti in blacklist.")
    @app_commands.default_permissions(manage_messages=True)
    async def bl_list(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Solo lo staff.", ephemeral=True)
        rows = await self.bot.db.blacklist_list()
        await interaction.response.send_message(embed=embed_blacklist(rows), ephemeral=True)

    @app_commands.command(name="stats", description="Statistiche di un membro dello staff.")
    @app_commands.default_permissions(manage_messages=True)
    async def stats_cmd(self, interaction: discord.Interaction,
                         member: Optional[discord.Member] = None) -> None:
        target = member or interaction.user
        data   = await self.bot.db.get_stats(target.id)
        if not data:
            return await interaction.response.send_message(
                f"Nessuna statistica per {target.mention}.", ephemeral=True)
        await interaction.response.send_message(embed=embed_stats(target, data))

    @app_commands.command(name="topstaff", description="Classifica staff per ticket chiusi.")
    @app_commands.default_permissions(manage_messages=True)
    async def topstaff_cmd(self, interaction: discord.Interaction) -> None:
        top = await self.bot.db.get_top_staff()
        if not top:
            return await interaction.response.send_message("Classifica vuota.", ephemeral=True)
        await interaction.response.send_message(embed=embed_top(top))


# ══════════════════════════════════════════════════════════════════
# BOT
# ══════════════════════════════════════════════════════════════════
class TicketBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.db = Database(cfg.DATABASE_URL)

    async def setup_hook(self) -> None:
        await self.db.connect()

        global _open_channels
        _open_channels = await self.db.load_open_ids()
        log.info("Cache ticket aperti: %d canali", len(_open_channels))

        self.add_view(MainPersistentView())
        self.add_view(TicketControlView())
        self.add_view(RatingView(channel_id=0))

        cog = TicketCog(self)
        await self.add_cog(cog)
        cog.auto_close_task.start()
        cog.staff_ping_task.start()

        guild_obj = discord.Object(id=cfg.GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        synced = await self.tree.sync(guild=guild_obj)
        log.info("Comandi sincronizzati: %d", len(synced))

    async def on_ready(self) -> None:
        if not self.user:
            log.error("on_ready: self.user è None")
            return
        log.info("✅ Online: %s (%d)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="i ticket 🎫"))

    async def close(self) -> None:
        for t in list(_close_tasks.values()):
            t.cancel()
        await asyncio.gather(*_close_tasks.values(), return_exceptions=True)
        _close_tasks.clear()
        await self.db.close()
        await super().close()


def attach_error_handler(bot: TicketBot) -> None:
    @bot.tree.error
    async def _err(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "❌ Permessi insufficienti."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Riprova tra `{error.retry_after:.1f}s`."
        else:
            log.exception("Errore slash command '%s':", interaction.command)
            msg = "⚠️ Errore inatteso. Riprova tra poco."
        with contextlib.suppress(discord.HTTPException):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    bot = TicketBot()
    attach_error_handler(bot)
    bot.run(cfg.TOKEN, log_handler=None)
