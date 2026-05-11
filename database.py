"""
database.py — Gestion SQLite des alertes et des logs de polling.
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
    """Crée les tables si elles n'existent pas."""
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
                telephone   TEXT NOT NULL,
                intervalle  INTEGER NOT NULL DEFAULT 15,
                actif       INTEGER NOT NULL DEFAULT 1,
                notifie     INTEGER NOT NULL DEFAULT 0,
                cree_le     TEXT NOT NULL,
                notifie_le  TEXT
            )
        """)
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
        await db.commit()
    logger.info(f"Base de données initialisée : {DB_PATH}")


async def create_alert(
    terrain_id: str,
    terrain_nom: str,
    date: str,
    heure_debut: str,
    heure_fin: str,
    nb_joueurs: int,
    telephone: str,
    intervalle: int = 15,
) -> int:
    """Crée une alerte et retourne son ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            INSERT INTO alerts
                (terrain_id, terrain_nom, date, heure_debut, heure_fin,
                 nb_joueurs, telephone, intervalle, actif, notifie, cree_le)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?)
            """,
            (
                terrain_id, terrain_nom, date, heure_debut, heure_fin,
                nb_joueurs, telephone, intervalle,
                datetime.now().isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_alerts() -> list[dict]:
    """Retourne toutes les alertes actives et non encore notifiées."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alerts WHERE actif = 1 AND notifie = 0"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_all_alerts() -> list[dict]:
    """Retourne toutes les alertes (pour l'UI)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM alerts ORDER BY cree_le DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def mark_alert_notified(alert_id: int):
    """Marque une alerte comme notifiée."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET notifie = 1, notifie_le = ? WHERE id = ?",
            (datetime.now().isoformat(), alert_id),
        )
        await db.commit()


async def delete_alert(alert_id: int):
    """Supprime (désactive) une alerte."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET actif = 0 WHERE id = ?",
            (alert_id,),
        )
        await db.commit()


async def log_poll(alert_id: int, statut: str, details: Optional[str] = None):
    """Enregistre le résultat d'un cycle de polling."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO poll_logs (alert_id, verifie_le, statut, details)
            VALUES (?, ?, ?, ?)
            """,
            (alert_id, datetime.now().isoformat(), statut, details),
        )
        await db.commit()


async def get_poll_logs(alert_id: int, limit: int = 20) -> list[dict]:
    """Retourne les derniers logs de polling pour une alerte."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM poll_logs WHERE alert_id = ? ORDER BY verifie_le DESC LIMIT ?",
            (alert_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
