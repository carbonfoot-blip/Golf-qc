#!/bin/bash
# ─────────────────────────────────────────
# golf-alert — Script de démarrage
# ─────────────────────────────────────────
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo ""
echo "⛳  Golf Alert QC — Démarrage"
echo "────────────────────────────────"

# 1. Vérifier Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 requis. Installer via https://python.org"
  exit 1
fi

# 2. Créer venv si absent
if [ ! -d ".venv" ]; then
  echo "📦 Création de l'environnement virtuel…"
  python3 -m venv .venv
fi

source .venv/bin/activate

# 3. Installer les dépendances
echo "📦 Installation des dépendances…"
pip install -q -r requirements.txt

# 4. Installer Playwright (navigateurs)
echo "🎭 Installation de Playwright Chromium…"
playwright install chromium --with-deps 2>/dev/null || playwright install chromium

# 5. Copier .env si absent
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "📄 Fichier .env créé depuis .env.example"
fi

# 6. Démarrer le serveur
echo ""
echo "✅ Démarrage du serveur…"
echo "   → Interface : http://127.0.0.1:8000"
echo "   → API docs  : http://127.0.0.1:8000/docs"
echo "   → Alertes   : http://127.0.0.1:8000/alerts"
echo ""
echo "   Ctrl+C pour arrêter"
echo "────────────────────────────────"
echo ""

python main.py

