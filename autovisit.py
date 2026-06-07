#!/usr/bin/env python3

import json
import logging
import os
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
import requests

try:
    import pyotp
    PYOTP_AVAILABLE = True
except ImportError:
    PYOTP_AVAILABLE = False

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

BASE_DIR = Path(os.environ.get("AUTOVISIT_DIR", Path(__file__).resolve().parent))
CONFIG   = BASE_DIR / "sites.json"
LOG_FILE = BASE_DIR / "logs" / f"visit_{datetime.now().strftime('%Y-%m')}.log"

LOG_FILE.parent.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

def parse_args():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--list",    action="store_true", help="Afficher la config des sites et quitter")
    parser.add_argument("--silent",  action="store_true", help="Aucune notification")
    parser.add_argument("--mp",      action="store_true", help="Notifier les alertes MP")
    parser.add_argument("--error",   action="store_true", help="Notifier les erreurs")
    parser.add_argument("--verbose", action="store_true", help="Sortie complete + toutes les notifications")
    parser.add_argument("--site",    type=str, nargs="+", default=None, help="Visiter uniquement ces sites")
    parser.add_argument("--stats",   action="store_true", help="Afficher et mettre a jour les stats")
    parser.add_argument("--json-output", action="store_true", help="Ecrire status.json apres le run")
    args = parser.parse_args()
    # --json-output toujours actif
    args.json_output = True
    # Par defaut : --mp --error --stats
    if not any([args.silent, args.mp, args.error, args.verbose]):
        args.mp    = True
        args.error = True
    if not args.stats:
        args.stats = True
    return args

def load_config():
    if not CONFIG.exists():
        log.error("Fichier de config introuvable : " + str(CONFIG))
        sys.exit(1)
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)

def send_apprise(cfg, subject, body):
    notif  = cfg.get("notifications", {})
    server = notif.get("apprise_url", "").rstrip("/")
    urls   = notif.get("urls", [])
    if not server or not urls:
        log.warning("Apprise non configure -- notification ignoree.")
        return
    try:
        r = requests.post(server + "/notify/", json={
            "title": subject,
            "body":  body,
            "urls":  "\n".join(urls),
        }, timeout=15)
        if r.status_code == 200:
            log.info("Notification envoyee via Apprise : " + subject)
        else:
            log.error("Echec Apprise HTTP " + str(r.status_code))
    except Exception as e:
        log.error("Echec Apprise : " + str(e))

# Alias pour compatibilite
def send_pushover(cfg, subject, body):
    send_apprise(cfg, subject, body)


def extract_csrf(html, field_name=None):
    # Si le nom du champ est connu, cherche directement
    if field_name:
        match = re.search(
            r'<input[^>]+name="' + re.escape(field_name) + r'"[^>]+value="([^"]+)"', html
        )
        if match:
            return match.group(1)
        match = re.search(
            r'<input[^>]+value="([^"]+)"[^>]+name="' + re.escape(field_name) + r'"', html
        )
        if match:
            return match.group(1)
        return None
    # Détection automatique — ordre de priorité
    patterns = [
        r'<meta name="csrf-token"\s+content="([^"]+)"',           # Laravel/Vue meta
        r'<input[^>]+name="_token"[^>]+value="([^"]+)"',           # Laravel form
        r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"',       # Flask/Werkzeug
        r'<input[^>]+name="_csrf_token"[^>]+value="([^"]+)"',      # Symfony
        r'<input[^>]+name="__RequestVerificationToken"[^>]+value="([^"]+)"',  # ASP.NET
        # ordre inversé attributs (value avant name)
        r'<input[^>]+value="([^"]+)"[^>]+name="_token"',
        r'<input[^>]+value="([^"]+)"[^>]+name="csrf_token"',
        r'<input[^>]+value="([^"]+)"[^>]+name="_csrf_token"',
        r'<input[^>]+value="([^"]+)"[^>]+name="__RequestVerificationToken"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return None

def extract_hidden_fields(html, exclude=None):
    exclude = exclude or []
    fields = {}
    for match in re.finditer(
        r'<input[^>]+type=["\']hidden["\'][^>]*>', html, re.IGNORECASE
    ):
        tag = match.group(0)
        name_m  = re.search(r'name=["\']([^"\']+)["\']', tag)
        value_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            name = name_m.group(1)
            if name not in exclude:
                fields[name] = value_m.group(1) if value_m else ""
    return fields

def fmt_bytes(val, base=1024):
    try:
        n = float(val)
    except (ValueError, TypeError):
        return str(val)
    if base == 1000:
        units = ["B", "KB", "MB", "GB", "TB", "PB"]
    else:
        units = ["o", "Ko", "Mo", "Go", "To", "Po"]
    for unit in units:
        if abs(n) < float(base):
            return f"{n:.2f} {unit}"
        n /= float(base)
    return f"{n:.2f} {'EB' if base == 1000 else 'Eo'}"

def extract_stats(html, patterns):
    stats = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if match:
            val = match.group(1).strip()
            val = val.replace('&nbsp;', ' ').replace('&#160;', ' ').strip()
            stats[key] = val
        else:
            stats[key] = "N/A"
    return stats

def extract_stats_json(data, fields, base=1024):
    stats = {}
    for key, path in fields.items():
        try:
            val = data
            for part in path.split("."):
                val = val[part] if isinstance(val, dict) else val[int(part)]
            # Conversion automatique si la clé suggère des octets
            if any(w in key.lower() for w in ["upload", "download", "bytes", "size"]):
                val = fmt_bytes(val, base=base)
            elif key.lower() == "ratio":
                try:
                    val = f"{float(val):.2f}"
                except (ValueError, TypeError):
                    val = str(val)
            else:
                val = str(val)
            stats[key] = val
        except (KeyError, IndexError, TypeError):
            stats[key] = "N/A"
    return stats

def visit_site_playwright(site):
    name = site["name"]
    timeout = site.get("timeout", 20) * 1000

    if not PLAYWRIGHT_AVAILABLE:
        msg = "ECHEC [" + name + "] playwright non installe"
        log.error(msg)
        return False, msg

    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            page = browser.new_page()

            # Interception des reponses API (playwright_intercept)
            intercepted = {}
            intercept_urls = site.get("playwright_intercept", [])
            def _on_response(response):
                for iurl in intercept_urls:
                    if response.url == iurl or response.url.startswith(iurl):
                        try:
                            intercepted[response.url] = response.json()
                        except Exception:
                            pass
            if intercept_urls:
                page.on("response", _on_response)

            log.info("[" + name + "] Chargement de la page de login (Playwright) : " + site["url"])
            page.goto(site["url"], timeout=timeout)

            username_field = site.get("username_field", "username")
            password_field = site.get("password_field", "password")
            submit_selector = site.get("playwright_submit", "button[type=submit]")
            pwd_selector = site.get("playwright_password_selector")

            # Attendre que le champ password soit pret (utile pour les hooks JS / LiveView)
            if pwd_selector:
                try:
                    page.wait_for_selector(pwd_selector, timeout=15000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
            if username_field and site.get("username"):
                page.fill("input[name='" + username_field + "']", site["username"])
            if pwd_selector:
                page.fill(pwd_selector, site["password"])
            else:
                page.fill("input[name='" + password_field + "']", site["password"])
            page.click(submit_selector)
            login_url = page.url
            wait_url_change = site.get("playwright_wait_url_change")
            post_login_wait = site.get("playwright_post_login_wait")
            if wait_url_change:
                # Attendre que l'URL change apres le submit
                try:
                    page.wait_for_function(
                        "url => window.location.href !== url",
                        arg=login_url,
                        timeout=int(wait_url_change) * 1000
                    )
                except Exception:
                    pass
                page.wait_for_timeout(int(post_login_wait or 3) * 1000)
            elif post_login_wait:
                page.wait_for_timeout(int(post_login_wait) * 1000)
            else:
                page.wait_for_load_state("networkidle", timeout=timeout)

            log.info("[" + name + "] URL apres login : " + page.url)

            # Gestion 2FA TOTP
            if "two-factor" in page.url or "2fa" in page.url:
                totp_secret = site.get("totp_secret")
                if not totp_secret:
                    browser.close()
                    msg = "ECHEC [" + name + "] 2FA detecte mais totp_secret absent"
                    log.error(msg)
                    return False, msg
                if not PYOTP_AVAILABLE:
                    browser.close()
                    msg = "ECHEC [" + name + "] pyotp non installe -- 2FA impossible"
                    log.error(msg)
                    return False, msg
                import pyotp as _pyotp
                totp_code = _pyotp.TOTP(totp_secret).now()
                totp_field = site.get("totp_field", "code")
                log.info("[" + name + "] Code TOTP genere pour 2FA Playwright : " + totp_code)
                page.fill("input[name='" + totp_field + "']", totp_code)
                try:
                    page.click("button.auth-form__primary-button", timeout=timeout)
                except Exception:
                    pass  # Navigation deja en cours
                page.wait_for_load_state("networkidle", timeout=timeout)
                log.info("[" + name + "] URL apres 2FA : " + page.url)

            # Mode fetch verify_url directement via Playwright (Nostradamus / sites a fingerprint strict)
            if site.get("playwright_fetch_verify"):
                verify_url = site.get("verify_url")
                if not verify_url:
                    browser.close()
                    msg = "ECHEC [" + name + "] verify_url manquant (playwright_fetch_verify)"
                    log.error(msg)
                    return False, msg
                page.goto(verify_url, timeout=timeout)
                page.wait_for_timeout(int(site.get("playwright_post_verify_wait", 3)) * 1000)
                html = page.content()
                browser.close()
                body_lower = html.lower()

                # Stats
                site_stats = site.get("stats", {})
                if site_stats:
                    stats = extract_stats(html, site_stats)
                    stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                    log.info("[" + name + "] Stats -- " + stats_str)

                # Alertes MP
                alert_keywords = site.get("alert_keywords", [])
                for kw in alert_keywords:
                    if kw.lower() in body_lower:
                        alert_label = site.get("alert_label", kw)
                        log.info("[" + name + "] ALERTE : " + alert_label)
                        return True, ("ALERTE", name, kw, True)

                # Verification succes
                custom_keywords = site.get("success_keywords", [])
                if custom_keywords:
                    matched = next((kw for kw in custom_keywords if kw.lower() in body_lower), None)
                    if matched:
                        msg = "OK [" + name + "] Connexion reussie (mot-cle : " + matched + ")"
                        log.info(msg)
                        return True, msg
                    else:
                        msg = "ECHEC [" + name + "] Mot-cle introuvable apres playwright_fetch_verify"
                        log.warning(msg)
                        return False, msg
                msg = "OK [" + name + "] Connexion reussie (playwright_fetch_verify)"
                log.info(msg)
                return True, msg

            cookies = page.context.cookies()
            browser.close()

        # Traitement des donnees interceptees (playwright_intercept)
        if intercepted:
            # Stats JSON
            stats_json = site.get("stats_json", {})
            stats_url = site.get("playwright_stats_url", "")
            if stats_json and stats_url:
                jdata = intercepted.get(stats_url)
                if jdata:
                    stats = extract_stats_json(jdata, stats_json, base=site.get("stats_base", 1024))
                    stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                    log.info("[" + name + "] Stats -- " + stats_str)
            # MP via mp_url intercepte
            mp_url = site.get("mp_url", "")
            mp_json_field = site.get("mp_json_field", "total")
            if mp_url:
                mp_data = intercepted.get(mp_url)
                if mp_data:
                    mp_count = mp_data.get(mp_json_field, 0)
                    if mp_count and int(mp_count) > 0:
                        log.info("[" + name + "] ALERTE : " + str(mp_count) + " MP non lu(s)")
                        return True, ("ALERTE", name, "mp_url", True)
            # Si pas de verify_url, retourner OK directement
            if not site.get("verify_url") and intercepted:
                success_keywords = site.get("success_keywords", [])
                if success_keywords:
                    msg = "OK [" + name + "] Connexion reussie (Playwright intercept, mot-cle : " + success_keywords[0] + ")"
                else:
                    msg = "OK [" + name + "] Connexion reussie (Playwright intercept)"
                log.info(msg)
                return True, msg

        # Fallback si intercepted vide et pas de verify_url
        if not intercepted and not site.get("verify_url") and intercept_urls:
            msg = "ECHEC [" + name + "] Interception API vide (Cloudflare ?)"
            log.warning(msg)
            return False, msg

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        })
        for c in cookies:
            session.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))

        verify_url = site.get("verify_url")
        if verify_url:
            rv = session.get(verify_url, timeout=site.get("timeout", 20))
        else:
            msg = "ECHEC [" + name + "] verify_url manquant (Playwright)"
            log.error(msg)
            return False, msg

        body_lower = rv.text.lower()

        site_stats = site.get("stats", {})
        if site_stats:
            stats = extract_stats(rv.text, site_stats)
            stats_str = " | ".join(k + ": " + v for k, v in stats.items())
            log.info("[" + name + "] Stats -- " + stats_str)

        alert_keywords = site.get("alert_keywords", [])
        for kw in alert_keywords:
            if kw.lower() in body_lower:
                alert_label = site.get("alert_label", kw)
                log.info("[" + name + "] ALERTE : " + alert_label)
                return True, ("ALERTE", name, kw, True)

        custom_keywords = site.get("success_keywords", [])
        if custom_keywords:
            matched = next((kw for kw in custom_keywords if kw.lower() in body_lower), None)
            if matched:
                msg = "OK [" + name + "] Connexion reussie (mot-cle : " + matched + ")"
                log.info(msg)
                return True, msg
            else:
                msg = "ECHEC [" + name + "] Mot-cle introuvable apres Playwright -- URL : " + rv.url
                log.warning(msg)
                return False, msg

        if any(w in body_lower for w in ["logout", "deconnexion", "mon compte", "my account", "bienvenue", "welcome", "sign out"]):
            msg = "OK [" + name + "] Connexion reussie (Playwright)"
            log.info(msg)
            return True, msg

        msg = "ECHEC [" + name + "] Connexion douteuse apres Playwright -- URL : " + rv.url
        log.warning(msg)
        return False, msg

    except Exception as e:
        msg = "ECHEC [" + name + "] Erreur Playwright : " + str(e)
        log.error(msg)
        return False, msg

def visit_site_session(site):
    """Visite un site en utilisant des cookies de session pre-existants (skip login)."""
    name = site["name"]
    timeout = site.get("timeout", 20)
    cookies_file = site.get("session_cookies_file")

    if not cookies_file:
        msg = "ECHEC [" + name + "] session_cookies_file manquant"
        log.error(msg)
        return False, msg

    user_agent = site.get("user_agent")
    if not user_agent and "User-Agent" not in site.get("extra_headers", {}):
        msg = "ECHEC [" + name + "] user_agent requis en mode session (les cookies cf_clearance y sont lies)"
        log.error(msg)
        return False, msg

    cookies_path = Path(cookies_file)
    if not cookies_path.exists():
        msg = "ECHEC [" + name + "] fichier cookies introuvable : " + cookies_file
        log.error(msg)
        return False, msg

    try:
        with open(cookies_path, encoding="utf-8") as f:
            cookies_data = json.load(f)
    except Exception as e:
        msg = "ECHEC [" + name + "] erreur lecture cookies : " + str(e)
        log.error(msg)
        return False, msg

    session = requests.Session()
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    if user_agent:
        session.headers["User-Agent"] = user_agent
    extra_headers = site.get("extra_headers", {})
    if extra_headers:
        session.headers.update(extra_headers)

    # Chargement des cookies (format Cookie-Editor : liste d objets)
    for c in cookies_data:
        session.cookies.set(
            c["name"],
            c["value"],
            domain=c.get("domain", "").lstrip("."),
            path=c.get("path", "/"),
        )

    log.info("[" + name + "] " + str(len(cookies_data)) + " cookie(s) charge(s) depuis " + cookies_file)

    # Mode hybride : si post_url + username/password definis, faire le login derriere les cookies
    post_url = site.get("post_url")
    if post_url and site.get("username") and site.get("password"):
        login_url = site.get("url", post_url)
        try:
            r_login = session.get(login_url, timeout=timeout)
            if r_login.status_code != 200:
                msg = "ECHEC [" + name + "] GET login HTTP " + str(r_login.status_code)
                log.error(msg)
                return False, msg
            payload = {
                site["username_field"]: site["username"],
                site["password_field"]: site["password"],
            }
            # CSRF si configure
            csrf_field = site.get("csrf_field")
            csrf_token = extract_csrf(r_login.text, csrf_field)
            if csrf_token:
                field_key = csrf_field if csrf_field else "_token"
                payload[field_key] = csrf_token
                log.info("[" + name + "] Token CSRF detecte et inclus (" + field_key + ")")
            payload.update(site.get("extra_fields", {}))
            time.sleep(random.uniform(0.5, 1.5))
            r_post = session.post(post_url, data=payload, timeout=timeout, allow_redirects=True)
            log.info("[" + name + "] Login effectue (HTTP " + str(r_post.status_code) + ")")
        except Exception as e:
            msg = "ECHEC [" + name + "] erreur login hybride : " + str(e)
            log.error(msg)
            return False, msg

    verify_url = site.get("verify_url")
    if not verify_url:
        msg = "ECHEC [" + name + "] verify_url manquant (mode session)"
        log.error(msg)
        return False, msg

    try:
        rv = session.get(verify_url, timeout=timeout)
    except Exception as e:
        msg = "ECHEC [" + name + "] erreur GET verify_url : " + str(e)
        log.error(msg)
        return False, msg

    body_lower = rv.text.lower()

    # Stats
    site_stats = site.get("stats", {})
    if site_stats:
        stats = extract_stats(rv.text, site_stats)
        stats_str = " | ".join(k + ": " + v for k, v in stats.items())
        log.info("[" + name + "] Stats -- " + stats_str)

    # Alertes MP
    alert_keywords = site.get("alert_keywords", [])
    for kw in alert_keywords:
        if kw.lower() in body_lower:
            alert_label = site.get("alert_label", kw)
            log.info("[" + name + "] ALERTE : " + alert_label)
            return True, ("ALERTE", name, kw, True)

    # Verification succes
    custom_keywords = site.get("success_keywords", [])
    if custom_keywords:
        matched = next((kw for kw in custom_keywords if kw.lower() in body_lower), None)
        if matched:
            msg = "OK [" + name + "] Connexion reussie via session (mot-cle : " + matched + ")"
            log.info(msg)
            return True, msg
        else:
            msg = "ECHEC [" + name + "] Mot-cle introuvable -- cookies expires ? URL : " + rv.url
            log.warning(msg)
            return False, msg

    msg = "OK [" + name + "] Connexion reussie via session"
    log.info(msg)
    return True, msg

def visit_site(site):
    name = site["name"]
    timeout = site.get("timeout", 20)
    use_curl = site.get("use_curl_cffi", False)

    if use_curl:
        if not CURL_CFFI_AVAILABLE:
            msg = "ECHEC [" + name + "] curl_cffi non installe"
            log.error(msg)
            return False, msg
        session = curl_requests.Session(impersonate="firefox")
    else:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        })

    try:
        log.info("[" + name + "] Chargement de la page de login : " + site["url"])
        get_headers = {"Accept-Encoding": "identity"} if use_curl else {}
        r = session.get(site["url"], timeout=timeout, headers=get_headers)
        if r.status_code != 200:
            msg = "ECHEC [" + name + "] Page de login inaccessible (HTTP " + str(r.status_code) + ")"
            log.error(msg)
            return False, msg

        # GET préliminaires optionnels
        for pre_url in site.get("pre_visit_urls", []):
            try:
                session.get(pre_url, timeout=timeout, headers=get_headers)
                log.info("[" + name + "] GET preliminaire : " + pre_url)
                time.sleep(random.uniform(0.3, 0.8))
            except:
                pass

        time.sleep(random.uniform(1.5, 3.0))


        csrf_field = site.get("csrf_field")
        csrf_token = extract_csrf(r.text, csrf_field)

        payload = {
            site["username_field"]: site["username"],
            site["password_field"]: site["password"],
        }

        if site.get("extract_hidden_fields"):
            hidden = extract_hidden_fields(r.text, exclude=["_token", site.get("csrf_field", "")])
            payload.update(hidden)
            log.info("[" + name + "] Champs hidden extraits : " + ", ".join(hidden.keys()))


        if csrf_token and not site.get("api_json"):
            field_key = csrf_field if csrf_field else "_token"
            payload[field_key] = csrf_token
            log.info("[" + name + "] Token CSRF detecte et inclus (" + field_key + ")")

        totp_secret = site.get("totp_secret")
        totp_url = site.get("totp_url")

        if totp_secret and not totp_url:
            if not PYOTP_AVAILABLE:
                log.error("[" + name + "] pyotp non installe -- 2FA impossible")
                return False, "ECHEC [" + name + "] pyotp manquant"
            totp_code = pyotp.TOTP(totp_secret).now()
            totp_field = site.get("totp_field", "mfa")
            payload[totp_field] = totp_code
            log.info("[" + name + "] Code TOTP genere : " + totp_code)

        extra_fields = site.get("extra_fields", {})
        payload.update(extra_fields)

        post_url = site.get("post_url", site["url"])
        log.info("[" + name + "] Envoi des identifiants")
        time.sleep(random.uniform(0.5, 1.5))

        extra_headers = site.get("extra_headers", {})
        if extra_headers:
            session.headers.update(extra_headers)

        if site.get("api_json"):
            post_headers = {"Content-Type": "application/json"}
            if use_curl:
                post_headers["Accept-Encoding"] = "identity"
            if csrf_token:
                post_headers["csrf-token"] = csrf_token
                log.info("[" + name + "] Header csrf-token inclus")
            if not use_curl:
                origin = "https://" + site["url"].split("/")[2]
                post_headers["Origin"] = origin
                post_headers["Referer"] = site["url"]
            r2 = session.post(post_url, json=payload, headers=post_headers, timeout=timeout, allow_redirects=False)
        else:
            r2 = session.post(post_url, data=payload, timeout=timeout, allow_redirects=True)

        if site.get("api_json") and r2.status_code in [200, 201]:
            try:
                data = r2.json()

                # Flux MFA JSON (ex: C411)
                if data.get("mfaRequired"):
                    if not totp_url or not totp_secret:
                        msg = "ECHEC [" + name + "] MFA requis mais totp_url/totp_secret absent dans la config"
                        log.error(msg)
                        return False, msg
                    if not PYOTP_AVAILABLE:
                        log.error("[" + name + "] pyotp non installe -- 2FA impossible")
                        return False, "ECHEC [" + name + "] pyotp manquant"
                    totp_code = pyotp.TOTP(totp_secret).now()
                    log.info("[" + name + "] MFA requis (API JSON), envoi du code TOTP : " + totp_code)
                    time.sleep(random.uniform(0.5, 1.0))
                    totp_headers = {"Content-Type": "application/json", "csrf-token": csrf_token}
                    if use_curl:
                        totp_headers["Accept-Encoding"] = "identity"
                        totp_headers["Origin"] = "https://" + site["url"].split("/")[2]
                        totp_headers["Referer"] = site["url"]
                    r3 = session.post(totp_url, json={"code": totp_code}, headers=totp_headers, timeout=timeout, allow_redirects=False)
                    try:
                        data3 = r3.json()
                        if data3.get("success"):
                            custom_keywords = site.get("success_keywords", [])
                            verify_url = site.get("verify_url")
                            if verify_url:
                                rv = session.get(verify_url, timeout=timeout, headers={"Accept-Encoding": "identity"} if use_curl else {})
                                # Alertes MP
                                alert_keywords = site.get("alert_keywords", [])
                                if alert_keywords:
                                    for kw in alert_keywords:
                                        if kw.lower() in rv.text.lower():
                                            alert_label = site.get("alert_label", kw)
                                            log.info("[" + name + "] ALERTE : " + alert_label)
                                            return True, ("ALERTE", name, kw, True)
                                # Stats HTML
                                site_stats = site.get("stats", {})
                                if site_stats:
                                    stats = extract_stats(rv.text, site_stats)
                                    stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                                    log.info("[" + name + "] Stats -- " + stats_str)
                                # Stats JSON
                                stats_json = site.get("stats_json", {})
                                rv_json = None
                                if stats_json:
                                    try:
                                        rv_json = rv.json()
                                        stats = extract_stats_json(rv_json, stats_json, base=site.get("stats_base", 1024))
                                        stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                                        log.info("[" + name + "] Stats -- " + stats_str)
                                    except Exception as e:
                                        log.warning("[" + name + "] Erreur parsing stats JSON apres MFA : " + str(e))
                                # Validation : success_json_field en priorité, sinon success_keywords
                                success_json_field = site.get("success_json_field")
                                if success_json_field:
                                    try:
                                        if rv_json is None:
                                            rv_json = rv.json()
                                        if rv_json.get(success_json_field):
                                            msg = "OK [" + name + "] Connexion reussie apres MFA JSON (champ : " + success_json_field + ")"
                                            log.info(msg)
                                            return True, msg
                                        else:
                                            msg = "ECHEC [" + name + "] Champ JSON manquant apres MFA : " + success_json_field
                                            log.warning(msg)
                                            return False, msg
                                    except Exception:
                                        pass
                                if custom_keywords:
                                    matched = next((kw for kw in custom_keywords if kw.lower() in rv.text.lower()), None)
                                    if matched:
                                        msg = "OK [" + name + "] Connexion reussie apres MFA JSON (mot-cle : " + matched + ")"
                                        log.info(msg)
                                        return True, msg
                                    else:
                                        msg = "ECHEC [" + name + "] MFA JSON ok mais mot-cle introuvable sur " + verify_url
                                        log.warning(msg)
                                        return False, msg
                            msg = "OK [" + name + "] Connexion reussie apres MFA JSON"
                            log.info(msg)
                            return True, msg
                    except:
                        pass
                    msg = "ECHEC [" + name + "] Echec apres MFA JSON -- HTTP " + str(r3.status_code)
                    log.warning(msg)
                    return False, msg

                success_json_field = site.get("success_json_field")
                if success_json_field:
                    if data.get(success_json_field):
                        # GET verify_url avec Bearer token si présent
                        verify_url = site.get("verify_url")
                        jwt_token = data.get("token") or data.get("access_token")
                        if verify_url:
                            if jwt_token:
                                auth_headers = {"Authorization": "Bearer " + jwt_token}
                            else:
                                auth_headers = {}
                            auth_headers["Accept"] = "application/json"
                            auth_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"
                            if use_curl and site.get("stats_json"):
                                import requests as _req
                                _s = _req.Session()
                                _s.cookies.update(dict(session.cookies))
                                _s.headers.update({
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
                                    "Accept": "application/json",
                                })
                                if jwt_token:
                                    _s.headers["Authorization"] = "Bearer " + jwt_token
                                rv = _s.get(verify_url, timeout=timeout)
                            else:
                                rv = session.get(verify_url, headers=auth_headers, timeout=timeout)
                            stats_json = site.get("stats_json", {})
                            if stats_json:
                                try:
                                    jdata = rv.json()
                                    stats = extract_stats_json(jdata, stats_json, base=site.get("stats_base", 1024))
                                    stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                                    log.info("[" + name + "] Stats -- " + stats_str)
                                except Exception as e:
                                    log.warning("[" + name + "] Erreur parsing stats JSON : " + str(e))
                            # Alertes MP via mp_url
                            mp_url = site.get("mp_url")
                            mp_json_field = site.get("mp_json_field", "total")
                            if mp_url:
                                try:
                                    rmp = session.get(mp_url, headers=auth_headers, timeout=timeout)
                                    mp_data = rmp.json()
                                    mp_count = mp_data.get(mp_json_field, 0)
                                    if mp_count and int(mp_count) > 0:
                                        log.info("[" + name + "] ALERTE : " + str(mp_count) + " MP non lu(s)")
                                        return True, ("ALERTE", name, "mp_url", True)
                                except Exception as e:
                                    log.warning("[" + name + "] Erreur mp_url : " + str(e))
                            # Alertes MP
                            alert_keywords = site.get("alert_keywords", [])
                            for kw in alert_keywords:
                                if kw.lower() in rv.text.lower():
                                    alert_label = site.get("alert_label", kw)
                                    log.info("[" + name + "] ALERTE : " + alert_label)
                                    return True, ("ALERTE", name, kw, True)
                            # Stats
                            site_stats = site.get("stats", {})
                            if site_stats:
                                stats = extract_stats(rv.text, site_stats)
                                stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                                log.info("[" + name + "] Stats -- " + stats_str)
                        msg = "OK [" + name + "] Connexion reussie (champ JSON : " + success_json_field + ")"
                        log.info(msg)
                        return True, msg
                    else:
                        msg = "ECHEC [" + name + "] Champ JSON manquant : " + success_json_field
                        log.warning(msg)
                        return False, msg

                if data.get("token") or data.get("access_token") or data.get("user") or data.get("success"):
                    msg = "OK [" + name + "] Connexion reussie (API JSON)"
                    log.info(msg)
                    return True, msg

                msg = "ECHEC [" + name + "] Reponse API JSON inattendue : " + r2.text[:200]
                log.warning(msg)
                return False, msg

            except Exception as e:
                msg = "ECHEC [" + name + "] Erreur parsing JSON : " + str(e)
                log.error(msg)
                return False, msg

        if totp_secret and totp_url and totp_url.split("?")[0] in r2.url:
            if not PYOTP_AVAILABLE:
                log.error("[" + name + "] pyotp non installe -- 2FA impossible")
                return False, "ECHEC [" + name + "] pyotp manquant"
            log.info("[" + name + "] Page 2FA detectee, envoi du code TOTP")
            # GET de la page 2FA pour recuperer le bon token CSRF
            r2fa = session.get(r2.url, timeout=timeout)
            totp_code = pyotp.TOTP(totp_secret).now()
            totp_field = site.get("totp_field", "code")
            totp_payload = {totp_field: totp_code}
            csrf_field2 = site.get("csrf_field")
            csrf_token2 = extract_csrf(r2fa.text, csrf_field2)
            if csrf_token2:
                field_key2 = csrf_field2 if csrf_field2 else "_token"
                totp_payload[field_key2] = csrf_token2
                log.info("[" + name + "] Token CSRF 2FA inclus (" + field_key2 + ")")
            # Champs hidden supplementaires sur la page 2FA (toujours extrait)
            hidden2 = extract_hidden_fields(r2fa.text, exclude=[csrf_field2 or "_token"])
            totp_payload.update(hidden2)
            if hidden2:
                log.info("[" + name + "] Champs hidden 2FA extraits : " + ", ".join(hidden2.keys()))
            time.sleep(random.uniform(0.5, 1.0))
            r3 = session.post(r2.url, data=totp_payload, timeout=timeout, allow_redirects=True)
            body_lower = r3.text.lower()

            custom_keywords = site.get("success_keywords", [])
            if custom_keywords:
                matched = next((kw for kw in custom_keywords if kw.lower() in body_lower), None)
                if matched:
                    # Alertes MP avant de retourner OK
                    alert_keywords = site.get("alert_keywords", [])
                    for kw in alert_keywords:
                        if kw.lower() in body_lower:
                            alert_label = site.get("alert_label", kw)
                            log.info("[" + name + "] ALERTE : " + alert_label)
                            return True, ("ALERTE", name, kw, True)
                    # Stats
                    site_stats = site.get("stats", {})
                    if site_stats:
                        stats = extract_stats(r3.text, site_stats)
                        stats_str = " | ".join(k + ": " + v for k, v in stats.items())
                        log.info("[" + name + "] Stats -- " + stats_str)
                    msg = "OK [" + name + "] Connexion reussie apres 2FA (mot-cle : " + matched + ")"
                    log.info(msg)
                    return True, msg
                else:
                    msg = "ECHEC [" + name + "] Mot-cle introuvable apres 2FA -- URL : " + r3.url
                    log.warning(msg)
                    return False, msg

            if any(w in body_lower for w in ["logout", "deconnexion", "mon compte", "my account", "bienvenue", "welcome", "sign out"]):
                msg = "OK [" + name + "] Connexion reussie apres 2FA"
                log.info(msg)
                return True, msg
            success_kw = site.get("success_url_contains", "")
            if success_kw and success_kw.lower() in r3.url.lower():
                msg = "OK [" + name + "] Connexion reussie apres 2FA -> " + r3.url
                log.info(msg)
                return True, msg
            msg = "ECHEC [" + name + "] Echec apres 2FA -- URL : " + r3.url
            log.warning(msg)
            return False, msg

        # GET de vérification optionnel (ex: login AJAX qui retourne body vide)
        verify_url = site.get("verify_url")
        if verify_url:
            r2 = session.get(verify_url, timeout=timeout)

        body_lower = r2.text.lower()

        # Stats
        site_stats = site.get("stats", {})
        if site_stats:
            stats = extract_stats(r2.text, site_stats)
            stats_str = " | ".join(k + ": " + v for k, v in stats.items())
            log.info("[" + name + "] Stats -- " + stats_str)

        # Alertes MP
        alert_keywords = site.get("alert_keywords", [])
        if alert_keywords:
            for kw in alert_keywords:
                if kw.lower() in body_lower:
                    alert_label = site.get("alert_label", kw)
                    log.info("[" + name + "] ALERTE : " + alert_label)
                    return True, ("ALERTE", name, kw, True)

        custom_keywords = site.get("success_keywords", [])
        if custom_keywords:
            matched = next((kw for kw in custom_keywords if kw.lower() in body_lower), None)
            if matched:
                msg = "OK [" + name + "] Connexion reussie (mot-cle : " + matched + ")"
                log.info(msg)
                return True, msg
            else:
                msg = "ECHEC [" + name + "] Mot-cle introuvable dans la page -- URL : " + r2.url
                log.warning(msg)
                return False, msg

        success_kw = site.get("success_url_contains", "")
        if success_kw and success_kw.lower() in r2.url.lower():
            msg = "OK [" + name + "] Connexion reussie -> " + r2.url
            log.info(msg)
            return True, msg

        if any(w in body_lower for w in ["logout", "deconnexion", "mon compte", "my account", "bienvenue", "welcome", "sign out"]):
            msg = "OK [" + name + "] Connexion reussie (detectee dans la page)"
            log.info(msg)
            return True, msg

        if any(w in body_lower for w in ["invalid", "incorrect", "wrong", "error", "invalide"]):
            msg = "ECHEC [" + name + "] Identifiants refuses"
            log.warning(msg)
            return False, msg

        msg = "ECHEC [" + name + "] Connexion douteuse -- URL : " + r2.url + " HTTP " + str(r2.status_code)
        log.warning(msg)
        return False, msg

    except requests.exceptions.Timeout:
        msg = "ECHEC [" + name + "] Timeout"
        log.error(msg)
        return False, msg
    except Exception as e:
        msg = "ECHEC [" + name + "] Erreur : " + str(e)
        log.error(msg)
        return False, msg

def list_sites(cfg):
    sites = cfg.get("sites", [])
    if not sites:
        print("Aucun site configure.")
        return

    def trunc(url, n=28):
        domain = url.split("/")[2] if "//" in url else url
        return domain[:n] + "…" if len(domain) > n else domain

    def get_2fa_type(s):
        if s.get("api_json") and s.get("totp_url"):
            return "api_json"
        if s.get("totp_url"):
            return "page"
        if s.get("totp_secret"):
            return "inline"
        return "-"

    COL = [
        ("Nom",    20),
        ("Actif",   5),
        ("URL",    30),
        ("TOTP",    4),
        ("2FA",     8),
        ("Stats",   5),
        ("MP",      4),
        ("Curl",    4),
    ]

    sep = "─" * (sum(w for _, w in COL) + len(COL) * 2)
    header = "  ".join(name.ljust(w) for name, w in COL)
    print(header)
    print(sep)

    for s in sites:
        actif  = "✓" if s.get("enabled", True) else "✗"
        totp   = "✓" if s.get("totp_secret") else "-"
        two_fa = get_2fa_type(s)
        stats  = "✓" if s.get("stats") or s.get("stats_json") else "-"
        mp     = "✓" if s.get("alert_keywords") else "-"
        curl   = "✓" if s.get("use_curl_cffi") else "-"

        row = [
            s["name"][:COL[0][1]],
            actif,
            trunc(s.get("url", ""), COL[2][1]),
            totp,
            two_fa,
            stats,
            mp,
            curl,
        ]
        print("  ".join(str(v).ljust(w) for v, (_, w) in zip(row, COL)))

def main():
    args  = parse_args()
    cfg   = load_config()

    # En mode non-verbose : filtre les logs intermediaires sur stdout
    if not args.verbose:
        class SimpleFilter(logging.Filter):
            def filter(self, record):
                msg = record.getMessage()
                # Garder : resultats finaux, stats, alertes, erreurs, resume
                keep = any(x in msg for x in [
                    "=== ", "OK [", "ECHEC [", "Stats --", "ALERTE", "echec(s)", "succes",
                    "status.json"
                ])
                return keep
        for handler in logging.root.handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream == sys.stdout:
                handler.addFilter(SimpleFilter())

    if args.list:
        list_sites(cfg)
        sys.exit(0)

    sites = [s for s in cfg.get("sites", []) if s.get("enabled", True)]

    if args.site:
        def matches(s):
            return any(
                s["name"].lower() == needle or any(a.lower() == needle for a in s.get("aliases", []))
                for needle in [n.lower() for n in args.site]
            )
        sites = [s for s in sites if matches(s)]
        if not sites:
            log.error("Sites introuvables : " + ", ".join(args.site))
            sys.exit(1)

    if not sites:
        log.warning("Aucun site actif trouve dans la config.")
        return

    log.info("=== Demarrage -- " + str(len(sites)) + " site(s) a visiter ===")
    results_ok  = []
    results_err = []
    status_sites = []

    # Handler de capture des logs pour extraire stats/alertes par site
    captured_logs = []
    class CaptureHandler(logging.Handler):
        def emit(self, record):
            captured_logs.append(record.getMessage())
    capture_handler = CaptureHandler()
    log.addHandler(capture_handler)

    for site in sites:
        captured_logs.clear()
        site_url = site.get("url") or site.get("verify_url", "")
        site_domain = site_url.split("/")[2] if "//" in site_url else site_url

        if site.get("session_cookies_file"):
            ok, msg = visit_site_session(site)
        elif site.get("use_playwright"):
            ok, msg = visit_site_playwright(site)
        else:
            ok, msg = visit_site(site)

        # Extraire stats et alerte depuis les logs captures
        site_stats_str = None
        site_alert     = None
        for line in captured_logs:
            if "Stats --" in line:
                site_stats_str = line.split("Stats -- ", 1)[1] if "Stats -- " in line else None
            if "ALERTE :" in line:
                site_alert = line.split("ALERTE : ", 1)[1] if "ALERTE : " in line else "MP non lu"

        # Si --stats desactive, ne pas conserver les stats dans le status
        if not args.stats:
            site_stats_str = None

        if ok and isinstance(msg, tuple) and msg[0] == "ALERTE":
            _, site_name, kw, _ = msg
            alerte_msg = "MP non lu sur " + site_name
            log.info("[" + site_name + "] ALERTE : " + alerte_msg)
            if args.verbose or args.mp:
                send_pushover(cfg, "Autovisit - MP", alerte_msg)
            results_ok.append("[" + site_name + "] OK")
            site_alert = "MP non lu"
            status_sites.append({"name": site["name"], "url": site_domain, "ok": True, "stats": site_stats_str, "alert": site_alert, "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M")})
        else:
            name = site["name"]
            if ok:
                simple_msg = "[" + name + "] OK"
                if site_stats_str and args.stats:
                    simple_msg += "\n[" + name + "] Stats -- " + site_stats_str
                results_ok.append(simple_msg)
            else:
                results_err.append(msg)
            status_sites.append({"name": name, "url": site_domain, "ok": ok, "stats": site_stats_str, "alert": site_alert, "last_seen": datetime.now().strftime("%Y-%m-%d %H:%M")})

    log.removeHandler(capture_handler)
    log.info("=== Resume ===")
    for m in results_ok + results_err:
        log.info(m)
    notif_prefs = cfg.get("notifications", {})
    notify_error   = notif_prefs.get("notify_error", True)
    notify_success = notif_prefs.get("notify_success", False)
    notify_saf     = notif_prefs.get("notify_success_after_failure", True)
    notify_stats   = notif_prefs.get("notify_stats", False)
    notify_mp      = notif_prefs.get("notify_mp", True)

    # Charger l'etat precedent pour detecter succès après echec
    prev_status = {}
    STATUS_JSON = BASE_DIR / "status.json"
    if STATUS_JSON.exists():
        try:
            import json as _json2
            with open(STATUS_JSON, encoding="utf-8") as f:
                prev = _json2.load(f)
                prev_status = {s["name"]: s.get("ok") for s in prev.get("sites", [])}
        except Exception:
            pass

    if results_err:
        body    = "Echecs :\n" + "\n".join(str(e) for e in results_err)
        subject = "Autovisit : " + str(len(results_err)) + " echec(s)"
        log.info(subject)
        if not args.silent and notify_error:
            send_apprise(cfg, subject, body)
    else:
        log.info("Tous les sites ont ete visites avec succes")
        # Succès après échec
        recovered = [s["name"] for s in status_sites if s.get("ok") and prev_status.get(s["name"]) == False]
        if not args.silent and notify_saf and recovered:
            body = "Sites rétablis : " + ", ".join(recovered)
            send_apprise(cfg, "Autovisit — Rétablissement", body)
        elif not args.silent and notify_success:
            lines = ["Tous les sites OK"]
            if notify_stats and args.stats:
                for s in status_sites:
                    if s.get("stats"):
                        lines.append(s["name"] + " : " + s["stats"])
            send_apprise(cfg, "Autovisit — Succès", "\n".join(lines))

    # Ecriture status.json (toujours)
    import json as _json
    # Inclure les sites desactives
    for s in cfg.get("sites", []):
        if not s.get("enabled", True):
            site_url = s.get("url") or s.get("verify_url", "")
            site_domain = site_url.split("/")[2] if "//" in site_url else site_url
            status_sites.append({
                "name": s["name"], "url": site_domain,
                "ok": None, "stats": None, "alert": None, "disabled": True
            })
    # Fusionner avec status.json existant
    status_path = BASE_DIR / "status.json"
    existing_status = {}
    if status_path.exists():
        try:
            with open(status_path, encoding="utf-8") as f:
                prev = _json.load(f)
                existing_status = {s["name"]: s for s in prev.get("sites", [])}
        except Exception:
            pass
    visited_names = {s["name"] for s in status_sites}
    for name, prev_site in existing_status.items():
        if name not in visited_names:
            # Preserver les stats existantes si --stats desactive
            if not args.stats and prev_site.get("stats"):
                pass  # garder les stats precedentes
            status_sites.append(prev_site)
    status = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sites":   status_sites
    }
    with open(status_path, "w", encoding="utf-8") as f:
        _json.dump(status, f, ensure_ascii=False, indent=2)
    log.info("status.json ecrit : " + str(status_path))

if __name__ == "__main__":
    main()
