# Autovisit Web

Interface web Docker pour [Autovisit](https://github.com/Gusdezup/Autovisit) — gestion et planification des visites automatiques de trackers BitTorrent privés pour éviter la désactivation des comptes inactifs.

> `autovisit.py` est intégré directement dans l'image Docker — pas besoin de l'installer séparément.

---

## Fonctionnalités

| Page | Fonctionnalités |
|---|---|
| **Dashboard** | Statut de chaque site (OK / ÉCHEC / alerte MP), stats ratio/upload/download, date du dernier passage |
| **Sites** | Ajouter / modifier / supprimer / activer-désactiver chaque site via interface graphique |
| **Run** | Lancer un run manuel sur un ou plusieurs sites, logs en temps réel, bouton stop |
| **Logs** | Consulter les logs mensuels générés par le script |
| **Planification** | Cron interne : toutes les X heures, chaque jour à heure fixe, ou certains jours de la semaine |
| **Notifications** | Configuration Apprise (Pushover, Telegram, Discord, etc.) avec test intégré |

---

## Prérequis

- Docker + Docker Compose

---

## Installation

```bash
git clone https://github.com/Gusdezup/Autovisit-Web.git
cd Autovisit-Web
cp sites.example.json data/sites.json
# Éditer data/sites.json avec vos sites et credentials
docker compose up -d --build
```

L'interface est accessible sur `http://<IP-NAS>:4567`.

---

## Configuration

Copiez `sites.example.json` vers `data/sites.json` et adaptez-le avec vos sites et identifiants.

`data/sites.json` est dans `.gitignore` — vos credentials ne seront jamais commités.

### Structure générale de sites.json

```json
{
  "pushover": {
    "api_token": "TON_APP_TOKEN",
    "user_key": "TON_USER_KEY"
  },
  "notifications": {
    "apprise_url": "http://ton-serveur-apprise:8000",
    "urls": ["pover://USER_KEY@API_TOKEN"],
    "notify_error": true,
    "notify_success": false,
    "notify_success_after_failure": true,
    "notify_mp": true
  },
  "sites": [
    { ... }
  ]
}
```

### Exemples de sites

Le fichier `sites.example.json` contient des exemples pour les cas les plus courants. Pour la référence complète de tous les champs disponibles (`totp_secret`, `use_curl_cffi`, `use_playwright`, `stats`, `stats_json`, `session_cookies_file`, etc.), consulte la documentation du script : [Autovisit README](https://github.com/Gusdezup/Autovisit#readme).

---

## Mise à jour

```bash
git pull
docker compose down && docker compose up -d --build
```

> `docker compose restart` ne suffit pas — un rebuild est nécessaire pour prendre en compte les modifications du script ou de l'image.

---

## Structure du repo

```
Autovisit-Web/
├── autovisit.py          # Script de visite (intégré dans l'image au build)
├── app.py                # Backend Flask
├── templates/
│   └── index.html        # Frontend single-page
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── sites.example.json    # Modèle de configuration
└── data/                 # Volume monté (non commité)
    ├── sites.json        # Votre configuration
    ├── status.json       # État généré automatiquement après chaque run
    ├── schedule.json     # Planification sauvegardée
    └── logs/             # Logs mensuels (visit_YYYY-MM.log)
```

---

## Planification

L'interface intègre un planificateur (APScheduler) configurable directement depuis la page Planification. Trois modes disponibles :

- **Toutes les X heures** — ex: toutes les 6h
- **Chaque jour à heure fixe** — ex: tous les jours à 03h00
- **Certains jours de la semaine** — ex: lundi, mercredi, vendredi à 08h00

Si tu utilisais le Task Scheduler DSM pour lancer le script, tu peux le désactiver une fois la planification activée dans l'interface : Panneau de configuration → Planificateur de tâches.

Les deux peuvent coexister sans problème.

---

## Notifications

Les notifications passent par un serveur [Apprise](https://github.com/caronc/apprise). Configure l'URL du serveur et tes URLs de notification dans la page Notifications de l'interface.

Format URL Pushover : `pover://USER_KEY@API_TOKEN`

---

## Sécurité

- `data/sites.json` contient tes mots de passe et secrets TOTP en clair — protège-le
- Ne partage jamais ton `sites.json`
- `data/` est dans `.gitignore` — rien de ce dossier ne sera commité

---

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `AUTOVISIT_DIR` | `/data` | Dossier des données utilisateur (sites.json, logs, etc.) |
| `AUTOVISIT_SCRIPT` | `/app/autovisit.py` | Chemin du script dans l'image |

---

## Port

| Port | Usage |
|---|---|
| 4567 | Interface web |
