"""
main.py — Application FastAPI pour golf-alert.

Fonctionne dans deux contextes :
  - Local (C:/Golf/)         : python main.py
  - Railway (racine du repo) : uvicorn main:app --host 0.0.0.0 --port $PORT

Routes :
  GET    /api/courses              — Liste des terrains
  GET    /api/courses/regions      — Régions disponibles
  GET    /api/courses/{id}         — Détail d'un terrain
  GET    /api/search               — Recherche de départs disponibles
  POST   /api/alerts               — Créer une alerte SMS
  GET    /api/alerts               — Lister toutes les alertes
  DELETE /api/alerts/{id}          — Supprimer une alerte
  GET    /api/alerts/{id}/logs     — Logs de polling
  POST   /api/check/{alert_id}     — Forcer un check immédiat
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

# ─── Résolution des chemins (local plat OU Railway) ───
# __file__ peut être C:\Golf\main.py ou /app/main.py
BASE_DIR = Path(__file__).parent.resolve()

# Chercher courses.json et le dossier frontend depuis BASE_DIR
def _find_file(filename: str) -> Path:
    """Cherche un fichier dans BASE_DIR ou ses sous-dossiers immédiats."""
    direct = BASE_DIR / filename
    if direct.exists():
        return direct
    # Chercher dans backend/ ou frontend/ si structure repo avec sous-dossiers
    for sub in ["backend", "frontend"]:
        candidate = BASE_DIR / sub / filename
        if candidate.exists():
            return candidate
    return direct  # Retourne le chemin direct même s'il n'existe pas encore

def _find_dir(dirname: str) -> Path:
    """Cherche un dossier frontend ou backend."""
    direct = BASE_DIR / dirname
    if direct.exists():
        return direct
    return BASE_DIR  # Fallback : tout est dans BASE_DIR (local plat)

# Charger .env (ignoré silencieusement si absent sur Railway)
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.example")  # Fallback pour les valeurs par défaut

# ─── Logging ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("golf-alert")

# ─── Import des modules locaux ─────────────────────────
# Ajouter BASE_DIR au path pour que les imports fonctionnent
# que les fichiers soient à plat ou dans backend/
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from database import init_db, create_alert, get_all_alerts, get_active_alerts, delete_alert, get_poll_logs
from scheduler import start_scheduler, stop_scheduler
from scraper import get_available_tee_times

# ─── Données terrains ──────────────────────────────────
COURSES_PATH = _find_file("courses.json")
logger.info(f"courses.json : {COURSES_PATH}")

def load_courses() -> list[dict]:
    with open(COURSES_PATH, encoding="utf-8") as f:
        return json.load(f)

COURSES = load_courses()
COURSES_BY_ID = {c["id"]: c for c in COURSES}

# ─── Lifespan ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🏌️ Démarrage de golf-alert")
    await init_db()
    start_scheduler()
    yield
    stop_scheduler()
    logger.info("Golf-alert arrêté")

# ─── App ───────────────────────────────────────────────
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

# ─── Modèles Pydantic ──────────────────────────────────
class AlerteCreate(BaseModel):
    terrain_id: str
    date: str
    heure_debut: str
    heure_fin: str
    nb_joueurs: int
    telephone: str
    intervalle: int = 15

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

# ─── Routes : Terrains ─────────────────────────────────
@app.get("/api/courses")
async def get_courses(
    region: Optional[str] = None,
    systeme: Optional[str] = None,
    apex: Optional[bool] = None,
):
    results = COURSES
    if region:
        results = [c for c in results if c["region"].lower() == region.lower()]
    if systeme:
        results = [c for c in results if c["systeme"] == systeme]
    if apex is not None:
        results = [c for c in results if c["apex"] == apex]

    today = date.today()
    enriched = []
    for course in results:
        c = dict(course)
        c["date_ouverture_max"] = (today + timedelta(days=course["fenetreReservation"])).isoformat()
        enriched.append(c)
    return enriched


@app.get("/api/courses/regions")
async def get_regions():
    return sorted(set(c["region"] for c in COURSES))


@app.get("/api/courses/{course_id}")
async def get_course(course_id: str):
    if course_id not in COURSES_BY_ID:
        raise HTTPException(status_code=404, detail="Terrain introuvable")
    return COURSES_BY_ID[course_id]


# ─── Routes : Recherche ────────────────────────────────
@app.get("/api/search")
async def search_tee_times(
    terrain_id: str = Query(...),
    date: str = Query(...),
    heure_debut: str = Query(...),
    heure_fin: str = Query(...),
    nb_joueurs: int = Query(default=2, ge=1, le=4),
):
    if terrain_id not in COURSES_BY_ID:
        raise HTTPException(status_code=404, detail="Terrain introuvable")

    terrain = COURSES_BY_ID[terrain_id]

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
            "tee_times": [],
            "message": f"La réservation ouvre dans {jours_avant - fenetre} jour(s)",
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
            else "Aucun départ dans cette plage"
        ),
    }


# ─── Routes : Alertes ──────────────────────────────────
@app.post("/api/alerts", status_code=201)
async def creer_alerte(alerte: AlerteCreate):
    terrain = COURSES_BY_ID[alerte.terrain_id]

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

    logger.info(f"📲 Alerte #{alert_id} créée — {terrain['nom']} le {alerte.date}")

    return {
        "id": alert_id,
        "message": (
            f"Alerte créée. Vous serez notifié au {alerte.telephone} "
            f"dès qu'un départ se libère au {terrain['nom']}."
        ),
        "terrain": terrain,
    }


@app.get("/api/alerts")
async def lister_alertes():
    return await get_all_alerts()


@app.delete("/api/alerts/{alert_id}")
async def supprimer_alerte(alert_id: int):
    await delete_alert(alert_id)
    return {"message": f"Alerte #{alert_id} supprimée"}


@app.get("/api/alerts/{alert_id}/logs")
async def logs_alerte(alert_id: int, limit: int = 20):
    return await get_poll_logs(alert_id, limit)


@app.post("/api/check/{alert_id}")
async def forcer_check(alert_id: int):
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


# ─── Servir le frontend ────────────────────────────────
# Cherche index.html dans BASE_DIR ou dans frontend/
FRONTEND_DIR = _find_dir("frontend")
INDEX_HTML = _find_file("index.html")
ALERTS_HTML = _find_file("alerts.html")

logger.info(f"Frontend : {FRONTEND_DIR}")

# Servir les fichiers statiques (CSS, images éventuelles)
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
async def serve_index():
    if INDEX_HTML.exists():
        return FileResponse(str(INDEX_HTML))
    return {"message": "Golf Alert API — voir /docs"}

@app.get("/alerts")
async def serve_alerts_page():
    if ALERTS_HTML.exists():
        return FileResponse(str(ALERTS_HTML))
    raise HTTPException(status_code=404, detail="Page alertes introuvable")


# ─── Démarrage direct ──────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True)
