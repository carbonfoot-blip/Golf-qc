"""
main.py — Application FastAPI pour golf-alert.

Routes :
  GET  /api/courses          — Liste des terrains (avec filtre région)
  GET  /api/courses/{id}     — Détail d'un terrain
  POST /api/alerts           — Créer une alerte SMS
  GET  /api/alerts           — Lister toutes les alertes
  DELETE /api/alerts/{id}    — Supprimer une alerte
  GET  /api/alerts/{id}/logs — Logs de polling d'une alerte
  POST /api/check/{alert_id} — Forcer un check immédiat
  GET  /api/search           — Recherche de disponibilités (sans alerte)
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

# Charger .env
load_dotenv(Path(__file__).parent.parent / ".env")

from database import (
    init_db, create_alert, get_all_alerts, get_active_alerts,
    delete_alert, get_poll_logs,
)
from scheduler import start_scheduler, stop_scheduler
from scraper import get_available_tee_times

# ─── Logging ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("golf-alert")

# ─── Données terrains ──────────────────────────────────
COURSES_PATH = Path(__file__).parent / "courses.json"

def load_courses() -> list[dict]:
    with open(COURSES_PATH, encoding="utf-8") as f:
        return json.load(f)

COURSES = load_courses()
COURSES_BY_ID = {c["id"]: c for c in COURSES}


# ─── Lifespan (startup / shutdown) ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🏌️ Démarrage de golf-alert")
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Golf-alert arrêté")


# ─── App ──────────────────────────────────────────────
app = FastAPI(
    title="Golf Alert API",
    description="Réservation et alertes de départs de golf au Québec",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Modèles Pydantic ─────────────────────────────────
class AlerteCreate(BaseModel):
    terrain_id: str
    date: str            # YYYY-MM-DD
    heure_debut: str     # HH:MM
    heure_fin: str       # HH:MM
    nb_joueurs: int      # 1–4
    email: str           # email de notification
    intervalle: int = 15 # minutes

    @field_validator("nb_joueurs")
    @classmethod
    def valider_joueurs(cls, v):
        if not 1 <= v <= 4:
            raise ValueError("Le nombre de joueurs doit être entre 1 et 4")
        return v

    @field_validator("intervalle")
    @classmethod
    def valider_intervalle(cls, v):
        if v not in [5, 10, 15, 30, 60]:
            raise ValueError("Intervalle doit être 5, 10, 15, 30 ou 60 minutes")
        return v

    @field_validator("terrain_id")
    @classmethod
    def valider_terrain(cls, v):
        if v not in COURSES_BY_ID:
            raise ValueError(f"Terrain '{v}' introuvable")
        return v


# ─── Routes : Terrains ────────────────────────────────
@app.get("/api/courses")
async def get_courses(
    region: Optional[str] = None,
    systeme: Optional[str] = None,
    apex: Optional[bool] = None,
):
    """Liste des terrains avec filtres optionnels."""
    results = COURSES

    if region:
        results = [c for c in results if c["region"].lower() == region.lower()]
    if systeme:
        results = [c for c in results if c["systeme"] == systeme]
    if apex is not None:
        results = [c for c in results if c["apex"] == apex]

    # Enrichir avec le statut de la fenêtre de réservation
    today = date.today()
    enriched = []
    for course in results:
        c = dict(course)
        fenetre = course["fenetreReservation"]
        c["date_ouverture_max"] = (today + timedelta(days=fenetre)).isoformat()
        enriched.append(c)

    return enriched


@app.get("/api/courses/regions")
async def get_regions():
    """Liste des régions disponibles."""
    regions = sorted(set(c["region"] for c in COURSES))
    return regions


@app.get("/api/courses/{course_id}")
async def get_course(course_id: str):
    """Détail d'un terrain."""
    if course_id not in COURSES_BY_ID:
        raise HTTPException(status_code=404, detail="Terrain introuvable")
    return COURSES_BY_ID[course_id]


# ─── Routes : Recherche ───────────────────────────────
@app.get("/api/search")
async def search_tee_times(
    terrain_id: str = Query(..., description="ID du terrain"),
    date: str = Query(..., description="Date YYYY-MM-DD"),
    heure_debut: str = Query(..., description="Heure début HH:MM"),
    heure_fin: str = Query(..., description="Heure fin HH:MM"),
    nb_joueurs: int = Query(default=2, ge=1, le=4),
):
    """Recherche immédiate de départs disponibles (sans créer d'alerte)."""
    if terrain_id not in COURSES_BY_ID:
        raise HTTPException(status_code=404, detail="Terrain introuvable")

    terrain = COURSES_BY_ID[terrain_id]

    # Vérifier la fenêtre de réservation
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Format de date invalide (YYYY-MM-DD)")

    today_date = datetime.today().date()
    jours_avant = (target_date - today_date).days

    if jours_avant < 0:
        raise HTTPException(status_code=400, detail="La date est dans le passé")

    fenetre = terrain["fenetreReservation"]
    if jours_avant > fenetre:
        return {
            "terrain": terrain,
            "disponible": False,
            "jours_avant_ouverture": jours_avant - fenetre,
            "date_ouverture": (today_date + timedelta(days=fenetre - jours_avant + jours_avant)).isoformat(),
            "tee_times": [],
            "message": (
                f"La réservation pour {terrain['nom']} ouvre dans "
                f"{jours_avant - fenetre} jour(s)"
            ),
        }

    tee_times = await get_available_tee_times(
        terrain=terrain,
        date=date,
        heure_debut=heure_debut,
        heure_fin=heure_fin,
        nb_joueurs=nb_joueurs,
    )

    return {
        "terrain": terrain,
        "disponible": len(tee_times) > 0,
        "jours_avant_ouverture": 0,
        "tee_times": tee_times,
        "message": (
            f"{len(tee_times)} départ(s) disponible(s)" if tee_times
            else "Aucun départ disponible dans cette plage"
        ),
    }


# ─── Routes : Alertes ─────────────────────────────────
@app.post("/api/alerts", status_code=201)
async def creer_alerte(alerte: AlerteCreate):
    """Crée une nouvelle alerte SMS."""
    terrain = COURSES_BY_ID[alerte.terrain_id]

    # Vérifier que la date n'est pas passée
    try:
        target_date = datetime.strptime(alerte.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Format de date invalide")

    if target_date < date.today():
        raise HTTPException(status_code=400, detail="La date est dans le passé")

    alert_id = await create_alert(
        terrain_id=alerte.terrain_id,
        terrain_nom=terrain["nom"],
        date=alerte.date,
        heure_debut=alerte.heure_debut,
        heure_fin=alerte.heure_fin,
        nb_joueurs=alerte.nb_joueurs,
        telephone=alerte.telephone,
        intervalle=alerte.intervalle,
    )

    logger.info(
        f"📲 Alerte #{alert_id} créée — {terrain['nom']} le {alerte.date} "
        f"({alerte.heure_debut}–{alerte.heure_fin}), {alerte.nb_joueurs}j, "
        f"tél: {alerte.telephone}, intervalle: {alerte.intervalle}min"
    )

    return {
        "id": alert_id,
        "message": (
            f"Alerte créée. Vous serez notifié au {alerte.telephone} "
            f"dès qu'un départ se libère au {terrain['nom']} "
            f"le {alerte.date} entre {alerte.heure_debut} et {alerte.heure_fin}."
        ),
        "terrain": terrain,
    }


@app.get("/api/alerts")
async def lister_alertes():
    """Liste toutes les alertes."""
    return await get_all_alerts()


@app.delete("/api/alerts/{alert_id}")
async def supprimer_alerte(alert_id: int):
    """Désactive une alerte."""
    await delete_alert(alert_id)
    return {"message": f"Alerte #{alert_id} supprimée"}


@app.get("/api/alerts/{alert_id}/logs")
async def logs_alerte(alert_id: int, limit: int = 20):
    """Logs de polling d'une alerte."""
    return await get_poll_logs(alert_id, limit)


@app.post("/api/check/{alert_id}")
async def forcer_check(alert_id: int):
    """Force un check immédiat pour une alerte (utile pour debug)."""
    from database import get_active_alerts
    from scheduler import _check_single_alert

    alerts = await get_active_alerts()
    alert = next((a for a in alerts if a["id"] == alert_id), None)
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable ou déjà notifiée")

    terrain = COURSES_BY_ID.get(alert["terrain_id"])
    if not terrain:
        raise HTTPException(status_code=404, detail="Terrain introuvable")

    await _check_single_alert(alert, terrain)
    return {"message": f"Check forcé pour alerte #{alert_id}"}


# ─── Servir le frontend ───────────────────────────────
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(FRONTEND_DIR / "index.html"))

    @app.get("/alerts")
    async def serve_alerts():
        return FileResponse(str(FRONTEND_DIR / "alerts.html"))


# ─── Démarrage direct ─────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True)
