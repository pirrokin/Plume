# Plume — éditeur de PDF

Plume permet de **modifier le texte d'un PDF directement**, comme l'outil « Modifier le PDF »
d'Acrobat Pro. On clique sur un paragraphe, on tape, on valide : le texte est réécrit dans la
police d'origine, avec la même taille, le même interligne et le même alignement, sans toucher
au reste de la page.

## Fonctionnalités

- **Modification du texte par paragraphe** : le texte se recompose automatiquement (retour à la
  ligne, justification, retrait de première ligne). Les lignes non modifiées restent intactes.
- **Mise en forme** : police, taille, gras, italique, souligné, barré, couleur, alignement.
- **Blocs** : déplacer, élargir, supprimer un bloc de texte ; ajouter du texte n'importe où.
- **Polices** :
  - réutilisation des polices intégrées au PDF, y compris les sous-ensembles réparés
    (PDF créés sur Mac, Word, Canva…) ;
  - police installée correspondante sinon (Times New Roman, Calibri, Arial…) ;
  - téléchargement automatique depuis Google Fonts quand la police est libre ;
  - à défaut, police installée la plus ressemblante (largeurs des lettres, hauteur d'x).
- **Documents numérisés (OCR)** : reconnaissance du texte (français et anglais). La page
  devient consultable et ses paragraphes modifiables : la zone d'origine est repeinte à la
  couleur du papier, puis le nouveau texte est écrit à la couleur de l'encre.
- **Pages** : pivoter, dupliquer, insérer une page blanche, supprimer, réordonner par
  glisser-déposer, ajouter les pages d'un autre PDF.
- **Annuler / rétablir** sur tout l'historique, et téléchargement du PDF final.
  Les polices y sont allégées et ne gardent que les lettres utilisées.

## Lancer Plume sur son ordinateur

Prérequis : Python 3.11 ou plus récent, et Node.js 18 ou plus récent.

```bash
./plume.sh
```

Puis ouvrir <http://127.0.0.1:8000>. Au premier lancement, le script installe les dépendances
et construit l'interface.

En mode développement, avec rechargement automatique du serveur et de l'interface sur
<http://127.0.0.1:5173> :

```bash
./plume.sh dev
```

Les documents sont stockés dans `data/documents/`. Chaque modification en crée une nouvelle
version (jusqu'à 60), ce qui permet d'annuler. Les documents inactifs depuis 72 h sont supprimés.

## Mettre Plume en ligne sur son domaine

La configuration fournie utilise Docker et [Caddy](https://caddyserver.com), qui obtient et
renouvelle tout seul le certificat HTTPS.

1. Sur un serveur (VPS) avec Docker, cloner le projet.
2. Faire pointer le domaine (enregistrement DNS `A`) vers l'adresse IP du serveur.
3. Créer le fichier de configuration :
   ```bash
   cp .env.example .env
   # puis renseigner PLUME_DOMAIN et un mot de passe solide dans PLUME_PASSWORD
   ```
4. Démarrer :
   ```bash
   docker compose up -d --build
   ```

Plume est alors accessible sur `https://ton-domaine`, protégé par le mot de passe. L'accès est
limité à 8 essais par quart d'heure, et la session dure 30 jours.

Le serveur n'ayant pas les polices Microsoft, l'image embarque leurs équivalents libres à
dimensions identiques : Liberation (Arial, Times, Courier), Carlito (Calibri) et Caladea
(Cambria). Les modèles OCR et les polices Google sont téléchargés au premier besoin, dans le
volume `/data`.

## Réglages (variables d'environnement)

| Variable | Défaut | Rôle |
|---|---|---|
| `PLUME_PASSWORD` | *(vide)* | Mot de passe d'accès ; vide = pas de connexion requise (usage local) |
| `PLUME_DATA_DIR` | `data/` | Documents, cache, modèles OCR, polices téléchargées |
| `PLUME_RETENTION_HOURS` | `72` | Suppression des documents inactifs |
| `PLUME_MAX_UPLOAD_MB` | `200` | Taille maximale d'un PDF envoyé |
| `PLUME_MAX_VERSIONS` | `60` | Versions conservées pour annuler |
| `PLUME_GOOGLE_FONTS` | `1` | `0` pour ne jamais contacter Google Fonts |
| `PLUME_OCR_LANGUAGES` | `fra+eng` | Langues de l'OCR (codes Tesseract) |
| `PLUME_FONT_DIRS` | — | Dossiers de polices supplémentaires (séparés par `:`) |
| `PLUME_SECRET` | *(généré)* | Clé de signature des sessions |
| `PLUME_COOKIE_SECURE` | `0` | `1` pour forcer les cookies HTTPS derrière un proxy |

## Tests

```bash
cd backend
../.venv/bin/python -m pytest
```

Outils de mise au point (dossier `backend/tools`) :

- `python -m tools.inspect_blocks fichier.pdf 0 sortie.png` dessine les blocs détectés ;
- `python -m tools.try_edit fichier.pdf 0 "début du bloc" "ancien" "nouveau" sortie` fait une
  modification et produit une image avant/après ;
- `python -m tools.sweep ~/Documents` fait des modifications au hasard sur tous les PDF d'un
  dossier, sur des copies en mémoire, et vérifie que le reste des pages ne change pas.

## Architecture

- `backend/plume/` : serveur Python ([FastAPI](https://fastapi.tiangolo.com) et
  [PyMuPDF](https://pymupdf.readthedocs.io)) :
  - `extract.py` regroupe le texte en paragraphes et détecte l'alignement et l'interligne ;
  - `layout.py` recompose le texte (coupure des lignes, justification) ;
  - `editor.py` et `writer.py` effacent et réécrivent le texte ;
  - `docfonts.py`, `pdffonts.py`, `fontindex.py` et `googlefonts.py` gèrent les polices ;
  - `ocr.py` reconnaît le texte des scans ; `store.py` gère les versions ;
    `app.py` expose l'API HTTP.
- `frontend/` : interface React + TypeScript (Vite). `src/editor/richtext.ts` est l'éditeur de
  texte riche, qui reproduit la mise en page de la page pendant la saisie.

## Limites connues

- Le texte tourné (vertical, en biais) n'est pas encore modifiable.
- Un texte qui s'allonge peut chevaucher le contenu situé dessous, comme dans Acrobat :
  il faut alors élargir ou déplacer le bloc.
- Les polices Type 3 (dessins de lettres, fréquentes dans les PDF exportés par Chrome) ne
  peuvent pas être réutilisées : le texte modifié prend une police proche.
- Quand une police intégrée ne contient pas une lettre et qu'elle n'est disponible nulle part,
  cette lettre est écrite avec une police ressemblante.

## Licence

Plume utilise PyMuPDF, distribué sous licence AGPL. Pour un usage personnel, rien à faire. Si
tu proposes Plume à d'autres personnes via Internet, le code source doit leur être accessible
(AGPL), ou bien il faut une licence commerciale d'Artifex.
