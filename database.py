"""
database.py — SQLite avec email au lieu de telephone.
Logique : 1 notification max par jour/heure/terrain.
"""

import os
import aiosqlite
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "alerts.db"))


async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                terrain_id  TEXT NOT NULL,
                terrain_nom TEXT NOT NULL,
                date        TEXT NOT NULL,
                heure_debut TEXT NOT NULL,
                heure_fin   TEXT NOT NULL,
                nb_joueurs  INTEGER NOT NULL,
                email       TEXT NOT NULL,
                intervalle  INTEGER NOT NULL DEFAULT 15,
                actif       INTEGER NOT NULL DEFAULT 1,
                notifie     INTEGER NOT NULL DEFAULT 0,
                cree_le     TEXT NOT NULL,
                notifie_le  TEXT
            )
        """)
        # Migration: ajouter colonne email si elle n'existe pas (ancienne DB avec telephone)
        try:
            await db.execute("ALTER TABLE alerts ADD COLUMN email TEXT NOT NULL DEFAULT ''")
            logger.info("Migration: colonne email ajoutée")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS poll_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id    INTEGER NOT NULL,
                verifie_le  TEXT NOT NULL,
                statut      TEXT NOT NULL,
                details     TEXT,
                FOREIGN KEY (alert_id) REFERENCES alerts(id)
            )
        """)

        # Table pour tracker les notifications déjà envoyées (1 par jour/heure/terrain)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notifications_sent (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                terrain_id  TEXT NOT NULL,
                date        TEXT NOT NULL,
                heure       TEXT NOT NULL,
                email       TEXT NOT NULL,
                envoye_le   TEXT NOT NULL,
                UNIQUE(terrain_id, date, heure, email)
            )
        """)

        await db.commit()
    logger.info(f"Base de données initialisée: {DB_PATH}")


async def create_alert(
    terrain_id: str, terrain_nom: str, date: str,
    heure_debut: str, heure_fin: str, nb_joueurs: int,
    email: str, intervalle: int = 15,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO alerts
               (terrain_id, terrain_nom, date, heure_debut, heure_fin,
                nb_joueurs, email, intervalle, actif, notifie, cree_le)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)""",
            (terrain_id, terrain_nom, date, heure_debut, heure_fin,
             nb_joueurs, email, intervalle, datetime.now().isoformat()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_alerts() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM alerts WHERE actif = 1 AND notifie = 0") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def get_all_alerts() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM alerts ORDER BY cree_le DESC") as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def mark_alert_notified(alert_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET notifie = 1, notifie_le = ? WHERE id = ?",
            (datetime.now().isoformat(), alert_id),
        )
        await db.commit()


async def delete_alert(alert_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alerts SET actif = 0 WHERE id = ?", (alert_id,))
        await db.commit()


async def log_poll(alert_id: int, statut: str, details: Optional[str] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO poll_logs (alert_id, verifie_le, statut, details) VALUES (?, ?, ?, ?)",
            (alert_id, datetime.now().isoformat(), statut, details),
        )
        await db.commit()


async def get_poll_logs(alert_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poll_logs WHERE alert_id = ? ORDER BY verifie_le DESC LIMIT ?",
            (alert_id, limit),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def notification_deja_envoyee(terrain_id: str, date: str, heure: str, email: str) -> bool:
    """Vérifie si une notification a déjà été envoyée pour ce terrain/date/heure/email aujourd'hui."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM notifications_sent WHERE terrain_id=? AND date=? AND heure=? AND email=?",
            (terrain_id, date, heure, email),
        ) as cursor:
            return await cursor.fetchone() is not None


async def marquer_notification_envoyee(terrain_id: str, date: str, heure: str, email: str):
    """Enregistre qu'une notification a été envoyée pour éviter les doublons."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT OR IGNORE INTO notifications_sent (terrain_id, date, heure, email, envoye_le) VALUES (?, ?, ?, ?, ?)",
                (terrain_id, date, heure, email, datetime.now().isoformat()),
            )
            await db.commit()
        except Exception as e:
            logger.warning(f"marquer_notification_envoyee: {e}")
