# Study Buddy

Application de chat pour accompagner les études. Le dépôt contient le frontend, le backend et les ressources graphiques.

## Prérequis

- Node.js et npm
- Python 3.14 ou plus récent
- uv
- Une clé API RodiumAI

## Démarrage local

### Backend

Depuis `backend/`, installer les dépendances, créer `.env` depuis `.env.example` et renseigner `RODIUMAI_API_KEY` :

```bash
uv sync
Copy-Item .env.example .env
uv run alembic upgrade head
uv run fastapi dev main.py
```

L’API démarre sur `http://localhost:8000`.

### Frontend

Dans un second terminal, depuis `frontend/` :

```bash
npm install
npm run dev
```

Ouvrir l’adresse indiquée par Vite, généralement `http://localhost:5173`. Le proxy Vite transmet les appels `/api/` au backend local.

## Structure

- `frontend/` : interface React, TypeScript et Vite
- `backend/` : API FastAPI, accès aux données et migrations Alembic
- `assets/` : ressources graphiques

## Tests et vérifications

Depuis `frontend/` :

```bash
npm run build
npm run lint
```

Depuis `backend/` :

```bash
uv run python -m unittest discover -s tests
```
