#!/usr/bin/env bash
# Lance Plume en local : http://127.0.0.1:8000
#   ./plume.sh          construit l'interface si besoin puis démarre le serveur
#   ./plume.sh dev      mode développement (rechargement auto, interface sur :5173)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "→ Création de l'environnement Python…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r backend/requirements-dev.txt
fi

if [ ! -d frontend/node_modules ]; then
  echo "→ Installation des dépendances de l'interface…"
  (cd frontend && npm install --no-audit --no-fund)
fi

if [ "${1:-}" = "dev" ]; then
  trap 'kill 0' EXIT
  (cd backend && ../.venv/bin/uvicorn plume.app:app --reload --host 127.0.0.1 --port 8000) &
  (cd frontend && npx vite --host 127.0.0.1) &
  wait
else
  if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src -newer frontend/dist/index.html -print -quit)" ]; then
    echo "→ Construction de l'interface…"
    (cd frontend && npm run build --silent)
  fi
  echo "→ Plume est disponible sur http://127.0.0.1:${PORT:-8000}"
  cd backend && exec ../.venv/bin/uvicorn plume.app:app --host 127.0.0.1 --port "${PORT:-8000}"
fi
