# ⛳ Golf Alert QC

Application complète de recherche, surveillance et réservation automatisée de départs de golf au Québec.

## 🔗 Accès rapide aux URLs de l'application

- **Page principale (Recherche & Réservation)** : [http://127.0.0.1:8000/](http://127.0.0.1:8000/)
- **Mes Alertes** : [http://127.0.0.1:8000/alerts](http://127.0.0.1:8000/alerts)
- **Documentation API (Swagger)** : [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Dépôt GitHub** : [https://github.com/carbonfoot-blip/Golf-qc](https://github.com/carbonfoot-blip/Golf-qc)

*(Si déployé sur le cloud tel que Railway, Render ou Fly.io, remplacez `http://127.0.0.1:8000` par le nom de domaine HTTPS fourni par votre hébergeur)*

---

## ✨ Fonctionnalités

- **Recherche en temps réel** de départs disponibles par région, date, plage horaire et nombre de joueurs (1 à 4)
- **Filtre Apex Golf** pour repérer rapidement les terrains partenaires
- **Calcul dynamique de fenêtre de réservation** : indique immédiatement si un terrain est réservable aujourd'hui, dans N jours ou trop tôt
- **Alertes automatiques par e-mail** : polling toutes les minutes en arrière-plan avec APScheduler — dès qu'un départ se libère, un e-mail HTML est envoyé via SendGrid
- **Réservation automatisée** :
  - **GGG Golf** (`secure.gggolf.ca`) : pré-recherche rapide HTTP + réservation Playwright
  - **Chronogolf** (`chronogolf.ca` / `.com`) : intégration API Marketplace v1/v2 + injection de session
- **25 terrains québécois** configurés avec coordonnées, slugs et paramètres de réservation

---

## 🚀 Démarrage local

```bash
# 1. Cloner le projet
git clone https://github.com/carbonfoot-blip/Golf-qc.git
cd Golf-qc

# 2. Créer l'environnement virtuel & installer les dépendances
python -m venv .venv
source .venv/bin/activate   # Sur Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

# 3. Lancer l'application
python main.py
```

Ouvrir l'application dans votre navigateur : **http://127.0.0.1:8000**
- 📋 Documentation API Swagger : **http://127.0.0.1:8000/docs**
- 🔔 Tableau de bord des alertes : **http://127.0.0.1:8000/alerts**

---

## ⚙️ Configuration (`.env`)

| Variable | Défaut | Description |
|---|---|---|
| `SENDGRID_API_KEY` | `stub` | Clé API SendGrid (`stub` = affiche l'email dans les logs sans envoyer) |
| `SENDGRID_FROM_EMAIL` | `noreply@golfalert.ca` | Adresse expéditeur configurée sur SendGrid |
| `MOCK_SCRAPER` | `false` | Si `true`, génère des créneaux fictifs si un site ne répond pas |
| `DB_PATH` | `./alerts.db` | Chemin du fichier de base de données SQLite |
| `API_HOST` | `0.0.0.0` | Hôte d'écoute FastAPI |
| `API_PORT` | `8000` | Port d'écoute |

### Activer les vrais e-mails (Gratuit)
1. Créez un compte gratuit sur [SendGrid](https://sendgrid.com) (100 emails/jour 100% gratuit à vie).
2. Générez une clé API et validez une adresse d'expéditeur (Single Sender Verification).
3. Renseignez dans votre `.env` :
   ```env
   SENDGRID_API_KEY=SG.votre_cle_api_ici
   SENDGRID_FROM_EMAIL=votre_email@domaine.com
   ```

---

## 🌐 Déploiement en ligne gratuit

Voici les options recommandées pour mettre l'application en ligne gratuitement :

1. **Render (Web Service gratuit)** :
   - Connectez votre dépôt GitHub sur [render.com](https://render.com).
   - Choisissez l'environnement **Docker**.
   - Ajoutez les variables d'environnement dans le dashboard Render.
   - *Astuce mise en veille* : Utilisez un moniteur gratuit comme [cron-job.org](https://cron-job.org) ou UptimeRobot pour pinger `/api/courses` toutes les 10 minutes afin de garder le polling actif.

2. **Koyeb (Docker gratuit)** :
   - Déploiement Docker direct depuis GitHub avec 512 Mo de RAM offerts sans mise en veille brutale.

3. **Fly.io (Allocation gratuite)** :
   - Déploiement via le CLI `fly launch` avec support de volume persistant pour SQLite (`/data/alerts.db`).

4. **Self-Hosting / PC Local + Cloudflare Tunnel (100% gratuit & sans limites)** :
   - Exécutez l'app sur votre machine ou un mini PC / Raspberry Pi.
   - Lancez un tunnel gratuit `cloudflared tunnel` pour obtenir une URL HTTPS publique sécurisée sans ouvrir de ports sur votre box.

---

## 📡 Endpoints API

| Méthode | Route | Description |
|---|---|---|
| `GET` | `/api/courses` | Liste des terrains filtrable par région, système et statut Apex |
| `GET` | `/api/courses/regions` | Liste des régions québécoises disponibles |
| `GET` | `/api/search` | Recherche en direct de départs pour un terrain et une date |
| `POST` | `/api/alerts` | Créer une alerte de surveillance automatique |
| `GET` | `/api/alerts` | Lister toutes les alertes et leur statut |
| `DELETE` | `/api/alerts/{id}` | Désactiver / supprimer une alerte |
| `GET` | `/api/alerts/{id}/logs`| Historique des vérifications effectuées |
| `POST` | `/api/check/{id}` | Forcer une vérification immédiate sans attendre le scheduler |
| `POST` | `/api/reserver` | Déclencher une réservation automatisée |
