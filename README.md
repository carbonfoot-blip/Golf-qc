# ⛳ Golf Alert QC

Application de réservation et d'alerte SMS pour les départs de golf au Québec.

## Ce que ça fait

- **Recherche** de départs disponibles par région, date, plage horaire et nombre de joueurs
- **Filtre Apex Golf** pour prioriser les terrains membres
- **Logique de fenêtre de réservation** : affiche si un terrain est réservable maintenant, dans N jours, ou trop tôt
- **Alertes SMS** : polling automatique toutes les N minutes — dès qu'un départ se libère, SMS envoyé
- **25 terrains québécois** couverts (GGG Golf, Chronogolf, sites propres)

## Démarrage rapide

```bash
# Cloner / dézipper le projet, puis :
chmod +x start.sh
./start.sh
```

Ouvrir http://127.0.0.1:8000

## Structure

```
golf-alert/
├── backend/
│   ├── main.py          # FastAPI — routes API
│   ├── scraper.py       # Playwright — extraction des départs
│   ├── scheduler.py     # APScheduler — polling périodique
│   ├── database.py      # SQLite — alertes et logs
│   ├── notifier.py      # SMS — stub ou Twilio
│   └── courses.json     # 25 terrains québécois
├── frontend/
│   ├── index.html       # Page de recherche
│   └── alerts.html      # Gestion des alertes
├── .env.example
├── requirements.txt
└── start.sh
```

## Variables d'environnement (.env)

| Variable | Défaut | Description |
|---|---|---|
| `SMS_STUB` | `true` | Si true, SMS dans les logs (dev) |
| `DEFAULT_POLL_INTERVAL` | `15` | Intervalle par défaut (minutes) |
| `DB_PATH` | `./backend/alerts.db` | Chemin SQLite |
| `API_HOST` | `127.0.0.1` | Hôte FastAPI |
| `API_PORT` | `8000` | Port FastAPI |
| `MOCK_SCRAPER` | `true` | Génère des créneaux fictifs si scraping échoue |

## Activer Twilio (SMS réels)

1. Créer un compte sur [twilio.com](https://twilio.com)
2. Obtenir un numéro SMS
3. Remplir dans `.env` :

```env
SMS_STUB=false
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
```

4. Installer le SDK : `pip install twilio`

## API

Documentation interactive : http://127.0.0.1:8000/docs

| Route | Description |
|---|---|
| `GET /api/courses` | Liste des terrains (filtre: region, systeme, apex) |
| `GET /api/courses/regions` | Liste des régions |
| `GET /api/search` | Recherche de départs disponibles |
| `POST /api/alerts` | Créer une alerte SMS |
| `GET /api/alerts` | Lister toutes les alertes |
| `DELETE /api/alerts/{id}` | Supprimer une alerte |
| `GET /api/alerts/{id}/logs` | Logs de polling |
| `POST /api/check/{id}` | Forcer un check immédiat |

## Notes sur le scraping

Le scraping est en mode `MOCK_SCRAPER=true` par défaut — il génère des créneaux fictifs quand les vrais sites ne répondent pas (dev local sans accès aux sites). En production :

- **GGG Golf** (`secure.gggolf.ca`) : parsing HTML via Playwright
- **Chronogolf** : interception des appels API JSON (plus fiable)
- **Sites propres** : extraction générique par regex d'heures

Les terrains peuvent mettre à jour leur interface — le scraper peut nécessiter des ajustements par terrain.

## V2 — idées futures

- Intégration API Chronogolf officielle
- Géolocalisation — terrains les plus proches
- Notifications push (PWA)
- Alerte multi-terrains (même critère, plusieurs terrains)
- Dashboard historique des disponibilités
