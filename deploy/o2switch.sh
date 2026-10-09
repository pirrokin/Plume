#!/usr/bin/env bash
# Met en ligne la version locale de Plume sur o2switch (application Python cPanel).
#   ./deploy/o2switch.sh
# Réglages possibles : PLUME_SSH, PLUME_SSH_KEY, PLUME_APP_ROOT, PLUME_DOMAIN, PLUME_PYTHON
set -euo pipefail
cd "$(dirname "$0")/.."

SSH_TARGET="${PLUME_SSH:-utilisateur@serveur.o2switch.net}"
SSH_KEY="${PLUME_SSH_KEY:-$HOME/.ssh/o2switch_claude}"
APP_ROOT="${PLUME_APP_ROOT:-plume-app}"
DOMAIN="${PLUME_DOMAIN:-plume.kinam.fr}"
PY="${PLUME_PYTHON:-3.12}"
SSH_OPTS=(-o BatchMode=yes -o IdentitiesOnly=yes -i "$SSH_KEY")
remote() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@"; }

echo "→ Construction de l'interface"
(cd frontend && npm run build --silent)

echo "→ Application Python sur $DOMAIN"
if ! remote "test -x ~/virtualenv/$APP_ROOT/$PY/bin/python"; then
  remote "cloudlinux-selector create --json --interpreter python --version $PY --app-root $APP_ROOT \
    --domain $DOMAIN --app-uri / --startup-file passenger_wsgi.py --entry-point application"
fi

echo "→ Envoi des fichiers"
rsync -azR --delete --exclude '__pycache__' -e "ssh ${SSH_OPTS[*]}" \
  passenger_wsgi.py backend/plume backend/requirements.txt frontend/dist \
  "$SSH_TARGET:$APP_ROOT/"

echo "→ Dépendances Python"
remote "~/virtualenv/$APP_ROOT/$PY/bin/pip install -q --disable-pip-version-check -r ~/$APP_ROOT/backend/requirements.txt"

echo "→ Redémarrage"
remote "cloudlinux-selector restart --json --interpreter python --app-root $APP_ROOT >/dev/null && mkdir -p ~/$APP_ROOT/tmp && touch ~/$APP_ROOT/tmp/restart.txt"

echo "→ Vérification"
for i in $(seq 1 30); do
  if curl -fsS -m 20 "https://$DOMAIN/api/health" 2>/dev/null; then echo; echo "✓ Plume est en ligne : https://$DOMAIN"; exit 0; fi
  if curl -fsS -m 20 "http://$DOMAIN/api/health" 2>/dev/null; then echo; echo "✓ En ligne en HTTP (certificat HTTPS en cours d'émission) : http://$DOMAIN"; exit 0; fi
  sleep 4
done
echo "✗ Le serveur ne répond pas encore ; voir ~/$APP_ROOT/stderr.log sur o2switch" >&2
exit 1
