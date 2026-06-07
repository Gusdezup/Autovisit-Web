# Autovisit Web

Interface web Docker pour [Autovisit](https://github.com/Gusdezup/Autovisit) — visite automatique de trackers BitTorrent privés pour éviter la désactivation des comptes inactifs.

> Basé sur `autovisit.py` — inclus directement dans l'image Docker.

## Fonctionnalités

- **Tableau de bord** — état de chaque site (dernier passage, stats, alertes MP)
- **Gestion des sites** — ajout / édition / suppression via interface graphique
- **Exécution manuelle** — lancer un run sur un ou plusieurs sites, avec log en temps réel
- **Planification** — toutes les X heures, chaque jour à heure fixe, ou certains jours de la semaine
- **Notifications** — via Apprise (Pushover, Telegram, Discord, etc.)

## Prérequis

- Docker + Docker Compose

## Installation

```bash
git clone https://github.com/Gusdezup/Autovisit-web.git
cd Autovisit-web
cp sites.example.json data/sites.json
# Éditer data/sites.json avec vos sites et credentials
docker compose up -d --build
```

L'interface est accessible sur `http://localhost:4567`.

## Structure

```
Autovisit-web/
├── autovisit.py          # Script de visite (intégré dans l'image)
├── app.py                # Backend Flask
├── templates/
│   └── index.html        # Frontend single-page
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── sites.example.json    # Modèle de configuration
└── data/                 # Volume monté (non commité)
    ├── sites.json        # Votre configuration
    ├── status.json       # État généré automatiquement
    ├── schedule.json     # Planning sauvegardé
    └── logs/             # Logs mensuels
```

## Configuration

Copiez `sites.example.json` vers `data/sites.json` et adaptez-le. Le fichier contient des exemples pour :

- Site simple (formulaire login)
- Site avec TOTP (2FA)
- Site avec stats HTML scraping
- Site Cloudflare (curl_cffi) avec stats JSON

`data/sites.json` est dans `.gitignore` — vos credentials ne seront jamais commités.

## Mise à jour

```bash
git pull
docker compose down && docker compose up -d --build
```

## Variables d'environnement

| Variable            | Défaut              | Description                        |
|---------------------|---------------------|------------------------------------|
| `AUTOVISIT_DIR`     | `/data`             | Dossier des données utilisateur    |
| `AUTOVISIT_SCRIPT`  | `/app/autovisit.py` | Chemin du script dans l'image      |

## Ports

| Port | Usage            |
|------|------------------|
| 4567 | Interface web    |
