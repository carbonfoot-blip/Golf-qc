"""
main.py — Application FastAPI pour golf-alert.
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
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

load_dotenv(Path(__file__).parent.parent / ".env")

from database import (
    init_db, create_alert, get_all_alerts, get_active_alerts,
    delete_alert, get_poll_logs,
)
from scheduler import start_scheduler, stop_scheduler
from scraper import get_available_tee_times
from booker import reserver_depart

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

# ─── Stockage session Chronogolf ───────────────────────
_chrono_sessions: dict = {}

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
    email: str
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


class ReservationRequest(BaseModel):
    terrain_id: str
    confirm_url: str
    username: str
    password: str
    date: str
    heure: str
    nb_joueurs: int


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
        fenetre = course["fenetreReservation"]
        c["date_ouverture_max"] = (today + timedelta(days=fenetre)).isoformat()
        enriched.append(c)
    return enriched


@app.get("/api/courses/regions")
async def get_regions():
    regions = sorted(set(c["region"] for c in COURSES))
    return regions


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
        raise HTTPException(status_code=400, detail="Format de date invalide")
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
        terrain=terrain, date=date,
        heure_debut=heure_debut, heure_fin=heure_fin, nb_joueurs=nb_joueurs,
    )
    return {
        "terrain": terrain,
        "disponible": len(tee_times) > 0,
        "jours_avant_ouverture": 0,
        "tee_times": tee_times,
        "message": f"{len(tee_times)} départ(s) disponible(s)" if tee_times else "Aucun départ dans cette plage",
    }


# ─── Routes : Réservation directe ──────────────────────
@app.post("/api/reserver")
async def reserver(req: ReservationRequest):
    if req.terrain_id not in COURSES_BY_ID:
        raise HTTPException(status_code=404, detail="Terrain introuvable")
    terrain = COURSES_BY_ID[req.terrain_id]
    logger.info(f"[Réservation] {terrain['nom']} — {req.date} {req.heure} — {req.nb_joueurs}j")
    result = await reserver_depart(
        terrain=terrain,
        confirm_url=req.confirm_url,
        username=req.username,
        password=req.password,
        date=req.date,
        heure=req.heure,
        nb_joueurs=req.nb_joueurs,
    )
    return result


# ─── Routes : Session Chronogolf ───────────────────────
@app.post("/api/chrono-capture")
async def chrono_capture(request: Request):
    """Reçoit la session Chronogolf depuis le bookmarklet."""
    try:
        data = await request.json()
        session = data.get("session", "")
        cf_clearance = data.get("cf_clearance", "")
        if not session:
            return {"ok": False, "message": "Session vide"}

        import httpx as _httpx
        email = ""
        try:
            async with _httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
                resp = await client.get(
                    "https://www.chronogolf.ca/marketplace/sessions",
                    cookies={"_chronogolf_session": session, "cf_clearance": cf_clearance},
                    headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
                )
                if resp.status_code == 200:
                    email = resp.json().get("email", "")
                    logger.info(f"[ChronoCapture] Session valide: {email}")
        except Exception as e:
            logger.warning(f"[ChronoCapture] Verification: {e}")

        client_ip = request.client.host
        _chrono_sessions[client_ip] = {
            "session": session,
            "cf_clearance": cf_clearance,
            "email": email,
            "valide": True,
        }
        logger.info(f"[ChronoCapture] Capturée pour {client_ip}: {email}")
        return {"ok": True, "email": email}
    except Exception as e:
        logger.error(f"[ChronoCapture] Erreur: {e}")
        return {"ok": False, "message": str(e)}


@app.get("/api/chrono-session-get")
async def chrono_session_get(request: Request):
    """Retourne la session Chronogolf capturée."""
    client_ip = request.client.host
    session_data = _chrono_sessions.get(client_ip)
    if not session_data:
        return {"valide": False, "message": "Aucune session capturée"}
    return {
        "valide": True,
        "session": session_data["session"],
        "cf_clearance": session_data["cf_clearance"],
        "email": session_data["email"],
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
        email=alerte.email,
        intervalle=alerte.intervalle,
    )
    logger.info(f"📧 Alerte #{alert_id} — {terrain['nom']} le {alerte.date} — {alerte.email}")
    return {
        "id": alert_id,
        "message": f"Alerte créée. Vous serez notifié à {alerte.email} dès qu'un départ se libère au {terrain['nom']}.",
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


# ─── Servir le frontend ────────────────────────────────
BASE = Path(__file__).parent
FRONTEND_DIR = BASE / "frontend" if (BASE / "frontend").exists() else BASE

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
async def serve_index():
    f = FRONTEND_DIR / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return {"message": "Golf Alert API"}

@app.get("/alerts")
async def serve_alerts():
    f = FRONTEND_DIR / "alerts.html"
    if f.exists():
        return FileResponse(str(f))
    raise HTTPException(status_code=404, detail="alerts.html introuvable")

@app.get("/chrono-auth")
async def serve_chrono_auth():
    f = FRONTEND_DIR / "chrono-auth.html"
    if f.exists():
        return FileResponse(str(f))
    raise HTTPException(status_code=404, detail="chrono-auth.html introuvable")


# ─── Démarrage direct ──────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, reload=True)
