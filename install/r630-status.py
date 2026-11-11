#!/usr/bin/env python3
"""r630-status — proxy filtré : état minimal du r630 pour la page publique.

Netdata tourne en local sur 127.0.0.1:19999 et expose bien plus que ce qui doit
être public : modèle du serveur, version d'OS, interfaces réseau, montages,
processus. Ce service ne lit que quelques charts, les réduit à un JSON au
contrat fixe et le sert en lecture seule avec CORS ouvert.

Il n'existe AUCUNE route de relais vers Netdata : les charts interrogés sont
codés en dur et aucun paramètre de requête n'entre dans un appel. Donc pas de
SSRF, pas de fuite d'un chart non prévu.

Le snapshot est rafraîchi en tâche de fond : une requête publique ne coûte
qu'une lecture en mémoire, ce qui protège Netdata sur un serveur déjà saturé
par la CI. Si Netdata ne répond plus, le dernier snapshot reste servi avec un
`age_seconds` qui augmente — la page affiche alors « données obsolètes ».

Deux particularités de l'API Netdata à connaître avant de modifier ce fichier :
  * `/api/v1/data` renvoie les points du plus récent au plus ancien → on inverse.
  * le chart `system.cpu` de cette installation n'a PAS de dimension `idle`
    (labels : user, system, iowait, nice, softirq, irq, steal, guest*).
    L'utilisation est donc calculée par somme des dimensions non oisives, ce qui
    reste juste si `idle` réapparaît un jour.

Installation : service systemd user `r630-status.service` sur le r630, exposé
publiquement via un Tailscale Funnel relayé (voir install/README.md).
Contrat consommé par la page GitHub Pages du dépôt `r630-etat`.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NETDATA = "http://127.0.0.1:19999"
PORT = 8814
REFRESH_SECONDS = 10
HISTORY_SECONDS = 3600
HISTORY_POINTS = 60
CONTRACT_VERSION = 1
HOST_LABEL = "r630"  # figé : on ne lit jamais le hostname réel dans Netdata

CHART_CPU = "system.cpu"
CHART_RAM = "system.ram"
CHART_DISK = "disk_space./"
CHART_LOAD = "system.load"
CHART_IO = "disk_util.sda"
CHART_PRESSURE_CPU = "system.cpu_some_pressure"
CHART_PRESSURE_IO = "system.io_full_pressure"
# Le noyau n'expose pas de ligne « full » pour la pression CPU (il n'y a que
# « some »), d'où deux fenêtres différentes. Vérifié sur le r630 le 2026-11-11 :
# `system.cpu_full_pressure` renvoie « No metrics where matched to query ».
PRESSION_CPU_FENETRE = "some 60"
PRESSION_IO_FENETRE = "full 60"

# Dimensions qui comptent comme de l'inactivité : exclues de l'utilisation CPU.
DIMENSIONS_OISIVES = frozenset({"idle"})

# Liste blanche des alertes publiées : nom Netdata -> (libellé, unité, diviseur).
# Toute alerte absente est ignorée : les noms internes de charts révéleraient
# l'agencement des disques et des services. Le diviseur convertit l'unité de
# Netdata vers celle du contrat (ex. backlog en ms -> s).
ALERTES = {
    "disk_space_usage": ("Espace disque", "%", 1.0),
    "disk_full": ("Disque plein", "%", 1.0),
    "10min_disk_backlog": ("File d'attente disque", "s", 0.001),
    "ram_in_use": ("Mémoire utilisée", "%", 1.0),
    "ram_available": ("Mémoire disponible", "%", 1.0),
    "1min_swapped_io": ("Échange mémoire (swap)", "%", 1.0),
    "10min_cpu_utilization": ("Utilisation CPU", "%", 1.0),
    "10min_iowait": ("Attente disque (iowait)", "%", 1.0),
    "load15": ("Charge moyenne 15 min", "", 1.0),
    "10min_cpu_full_pressure": ("Pression CPU", "%", 1.0),
    "10min_io_full_pressure": ("Pression disque", "%", 1.0),
}


def netdata(chemin: str, timeout: float = 6.0):
    """GET JSON sur Netdata local. Renvoie None en cas d'échec (jamais d'exception)."""
    try:
        with urllib.request.urlopen(NETDATA + chemin, timeout=timeout) as rep:
            return json.load(rep)
    except Exception:
        return None


def points(chemin: str) -> tuple[list, list]:
    """Lignes d'un appel /api/v1/data sous forme (labels, lignes)."""
    donnees = netdata(chemin)
    if not donnees:
        return [], []
    return donnees.get("labels") or [], donnees.get("data") or []


def dernier_point(chart: str) -> dict:
    """{dimension: valeur} du dernier point disponible (30 dernières secondes)."""
    labels, lignes = points(f"/api/v1/data?chart={chart}&after=-30&points=1")
    if not labels:
        return {}
    for ligne in reversed(lignes):
        valeurs = {k: v for k, v in zip(labels, ligne) if k != "time"}
        if any(v is not None for v in valeurs.values()):
            return valeurs
    return {}


def combler(valeurs: list) -> list[float]:
    """Remplace les None pour ne publier que des nombres (contrat du client).

    Netdata renvoie null avant son propre démarrage : ces trous de tête prennent
    la première valeur connue (backward-fill) plutôt que 0.0, qui afficherait un
    faux « 0 % » pendant les premières minutes. Après la première valeur connue,
    forward-fill.
    """
    premiere = next((v for v in valeurs if isinstance(v, (int, float))), None)
    if premiere is None:
        return [0.0] * len(valeurs)
    out, precedente = [], float(premiere)
    for v in valeurs:
        if isinstance(v, (int, float)):
            precedente = float(v)
        out.append(round(precedente, 2))
    return out


def historique(chart: str) -> tuple[list[int], dict[str, list[float]]]:
    """Historique d'un chart sur 1 h : horodatages croissants + toutes les séries.

    Netdata renvoie les points du plus récent au plus ancien : on inverse pour
    que le contrat public soit en ordre chronologique.
    """
    url = (f"/api/v1/data?chart={chart}&after=-{HISTORY_SECONDS}"
           f"&points={HISTORY_POINTS}")
    labels, lignes = points(url)
    if not labels or "time" not in labels:
        return [], {}
    index = {nom: i for i, nom in enumerate(labels)}
    lignes = list(reversed(lignes))
    ts = [int(l[index["time"]]) for l in lignes if l]
    series = {}
    for nom, i in index.items():
        if nom == "time":
            continue
        series[nom] = combler([l[i] if len(l) > i else None for l in lignes])
    return ts, series


def pourcentage(nom: float, denom: float) -> float:
    """Pourcentage arrondi, 0.0 si le dénominateur est nul."""
    return round(nom * 100.0 / denom, 1) if denom else 0.0


def borne(valeur: float, mini: float = 0.0, maxi: float = 100.0) -> float:
    return round(min(maxi, max(mini, valeur)), 1)


def utilisation_cpu(valeurs: dict) -> float:
    """Utilisation CPU en % : somme des dimensions non oisives, bornée à 0-100."""
    total = sum(v for k, v in valeurs.items()
                if k not in DIMENSIONS_OISIVES and isinstance(v, (int, float)))
    return borne(total)


def bloc_cpu() -> tuple[dict, dict]:
    """État CPU + historique cpu_pct."""
    instant = dernier_point(CHART_CPU)
    occupe = utilisation_cpu(instant)
    etat = {"user": round(float(instant.get("user") or 0.0), 1),
            "system": round(float(instant.get("system") or 0.0), 1),
            "iowait": round(float(instant.get("iowait") or 0.0), 1),
            "idle": round(100.0 - occupe, 1)}
    ts, series = historique(CHART_CPU)
    return etat, {"ts": ts, "cpu_pct": [
        utilisation_cpu({k: s[i] for k, s in series.items()})
        for i in range(len(ts))]}


def bloc_ram(total_memoire: float) -> tuple[dict, dict]:
    """État RAM + historique ram_used_pct. `total_memoire` sert de repli."""
    instant = dernier_point(CHART_RAM)
    utilise = float(instant.get("used") or 0.0)
    cache = float(instant.get("cached") or 0.0)
    libre = float(instant.get("free") or 0.0)
    tampons = float(instant.get("buffers") or 0.0)
    total = utilise + cache + libre + tampons or total_memoire
    etat = {"total_mb": round(total, 1), "used_mb": round(utilise, 1),
            "cached_mb": round(cache, 1), "free_mb": round(libre, 1)}
    ts, series = historique(CHART_RAM)
    util = series.get("used", [])
    return etat, {"ts": ts,
                  "ram_used_pct": [pourcentage(v, total) for v in util]}


def bloc_disque() -> tuple[dict, dict]:
    """État du disque racine + historique disk_used_pct."""
    instant = dernier_point(CHART_DISK)
    utilise = float(instant.get("used") or 0.0)
    dispo = float(instant.get("avail") or 0.0)
    reserve = float(instant.get("reserved for root") or 0.0)
    etat = {"total_gb": round(utilise + dispo + reserve, 1),
            "used_gb": round(utilise, 1), "avail_gb": round(dispo, 1),
            "used_pct": pourcentage(utilise, utilise + dispo)}
    ts, series = historique(CHART_DISK)
    util, dispo_h = series.get("used", []), series.get("avail", [])
    return etat, {"ts": ts, "disk_used_pct": [
        pourcentage(u, u + d) for u, d in zip(util, dispo_h)]}


def bloc_charge(coeurs: int) -> tuple[dict, dict]:
    """État de la charge + historique load1."""
    instant = dernier_point(CHART_LOAD)
    etat = {"l1": round(float(instant.get("load1") or 0.0), 1),
            "l5": round(float(instant.get("load5") or 0.0), 1),
            "l15": round(float(instant.get("load15") or 0.0), 1),
            "cores": coeurs}
    ts, series = historique(CHART_LOAD)
    return etat, {"ts": ts, "load1": series.get("load1", [])}


def bloc_io_pression() -> tuple[dict, dict]:
    """Utilisation du disque et pression (PSI) CPU/IO sur 60 s."""
    io = dernier_point(CHART_IO)
    cpu = dernier_point(CHART_PRESSURE_CPU).get(PRESSION_CPU_FENETRE)
    disque = dernier_point(CHART_PRESSURE_IO).get(PRESSION_IO_FENETRE)
    return ({"util_pct": round(float(io.get("utilization") or 0.0), 1)},
            {"cpu_pct": round(float(cpu or 0.0), 1),
             "io_pct": round(float(disque or 0.0), 1)})


def bloc_alertes() -> list[dict]:
    """Alertes actives filtrées par la liste blanche ALERTES."""
    donnees = netdata("/api/v1/alarms") or {}
    out = []
    for alarme in (donnees.get("alarms") or {}).values():
        regle = ALERTES.get(str(alarme.get("name") or ""))
        if not regle or alarme.get("status") not in ("WARNING", "CRITICAL"):
            continue
        libelle, unite, diviseur = regle
        try:
            valeur = round(float(alarme.get("value") or 0.0) * diviseur, 1)
        except (TypeError, ValueError):
            continue
        out.append({"label": libelle, "level": str(alarme["status"]).lower(),
                    "value": valeur, "unit": unite})
    return out


def infos_hote() -> tuple[int, float]:
    """(cœurs, RAM totale en Mo) — seuls champs lus dans /api/v1/info."""
    info = netdata("/api/v1/info") or {}
    return int(info.get("cores_total") or 0), float(info.get("ram_total") or 0.0)


def construire_snapshot() -> dict:
    """Assemble le JSON public. Lève une exception si Netdata est muet."""
    coeurs, memoire = infos_hote()
    cpu, hist_cpu = bloc_cpu()
    ram, hist_ram = bloc_ram(memoire)
    disque, hist_disque = bloc_disque()
    charge, hist_charge = bloc_charge(coeurs)
    io, pression = bloc_io_pression()
    ts = hist_cpu.get("ts") or hist_charge.get("ts") or []
    return {
        "version": CONTRACT_VERSION,
        "generated_at": int(time.time()),
        "age_seconds": 0,
        "host_label": HOST_LABEL,
        "cpu": cpu,
        "ram": ram,
        "disk": disque,
        "load": charge,
        "io": io,
        "pressure": pression,
        "alerts": bloc_alertes(),
        "history": {
            "step_seconds": HISTORY_SECONDS // HISTORY_POINTS,
            "points": len(ts),
            "ts": ts,
            "cpu_pct": hist_cpu.get("cpu_pct") or [],
            "ram_used_pct": hist_ram.get("ram_used_pct") or [],
            "disk_used_pct": hist_disque.get("disk_used_pct") or [],
            "load1": hist_charge.get("load1") or [],
        },
    }


class Collecteur(threading.Thread):
    """Rafraîchit le snapshot en tâche de fond et le garde en cas de panne."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="collecteur")
        self._verrou = threading.Lock()
        self._snapshot: dict | None = None
        self._maj = 0.0

    def lire(self) -> dict | None:
        """Snapshot courant avec `age_seconds` à jour, ou None si jamais obtenu."""
        with self._verrou:
            if self._snapshot is None:
                return None
            copie = dict(self._snapshot)
        copie["age_seconds"] = int(time.time() - self._maj)
        return copie

    def run(self) -> None:
        while True:
            debut = time.time()
            try:
                snapshot = construire_snapshot()
            except Exception:
                snapshot = None
            if snapshot is not None:
                with self._verrou:
                    self._snapshot = snapshot
                    self._maj = time.time()
            time.sleep(max(1.0, REFRESH_SECONDS - (time.time() - debut)))


class Gestionnaire(BaseHTTPRequestHandler):
    """Trois routes fixes, aucune autre : pas de relais vers Netdata."""

    collecteur: Collecteur

    def version_string(self) -> str:
        return "r630-status"

    def log_message(self, fmt, *args):  # pas de spam dans journald
        return

    def _repondre(self, code: int, corps: bytes):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "public, max-age=5")
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(corps)

    def _json(self, code: int, objet) -> None:
        self._repondre(code, json.dumps(objet).encode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802 (nom imposé par BaseHTTPRequestHandler)
        self._repondre(204, b"")

    def do_GET(self):  # noqa: N802
        chemin = self.path.split("?", 1)[0].rstrip("/") or "/"
        if chemin in ("/status.json", "/status"):
            snapshot = self.collecteur.lire()
            if snapshot is None:
                self._json(503, {"erreur": "supervision indisponible"})
            else:
                self._json(200, snapshot)
        elif chemin == "/healthz":
            self._json(200, {"ok": self.collecteur.lire() is not None})
        else:
            self._json(404, {"erreur": "route inconnue"})

    do_HEAD = do_GET


def adresse_ecoute() -> str:
    """IP Tailscale du serveur, pour que le relais Funnel du PC puisse joindre
    ce proxy sans l'exposer sur le LAN physique.

    Repli sur loopback si Tailscale ne répond pas : le service reste sain et
    joignable en local, le relais public est alors simplement hors service.
    """
    try:
        sortie = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True,
            text=True, timeout=10, check=False)
        ips = [l.strip() for l in sortie.stdout.splitlines() if l.strip()]
        if ips:
            return ips[0]
    except Exception:
        pass
    return "127.0.0.1"


def main() -> None:
    collecteur = Collecteur()
    collecteur.start()
    Gestionnaire.collecteur = collecteur
    adresse = adresse_ecoute()
    serveur = ThreadingHTTPServer((adresse, PORT), Gestionnaire)
    serveur.daemon_threads = True
    print(f"r630-status en écoute sur {adresse}:{PORT}", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        serveur.server_close()


if __name__ == "__main__":
    main()
