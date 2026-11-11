# r630 — état du serveur

Une page web publique qui montre l'état d'un serveur en temps réel : CPU, mémoire,
disque, charge, pression IO et alertes. Relevés toutes les 10 secondes, historique
des 60 dernières minutes.

**Page** : <https://baptistefaisy.github.io/r630-etat/>
**Source des données** : <https://r630-debian.tail3a8bdf.ts.net/r630/status.json>

Aucune dépendance externe : pas de framework, pas de CDN, pas de police tierce,
pas d'analytics. HTML, CSS et JavaScript vanilla, graphes en SVG dessinés à la
main. La page fonctionne hors ligne une fois chargée (les données en moins).

## Architecture

```
Netdata (r630, 127.0.0.1:19999, privé)
        │  lecture locale seule
        ▼
r630-status.py (r630, 127.0.0.1:8814, privé)   ← proxy filtré, liste blanche
        │  cache rafraîchi toutes les 10 s
        ▼
Tailscale Serve/Funnel  →  https://r630-debian.tail3a8bdf.ts.net/r630/status.json
        │  HTTPS, public
        ▼
Cette page (GitHub Pages)  →  fetch toutes les 10 s
```

Netdata reste **strictement privé** : il n'écoute que sur loopback et sur l'IP
Tailscale. Seul le proxy est exposé, et il ne sert qu'un JSON au contrat fixe.

## Ce qui est publié — et ce qui ne l'est pas

Le proxy n'a **aucune route de relais** vers Netdata : les charts interrogés sont
codés en dur et aucun paramètre de requête n'entre dans un appel. Il n'y a donc
ni SSRF ni moyen de demander un chart non prévu.

| Publié | Jamais publié |
|---|---|
| CPU (user, système, attente disque) | modèle du serveur (Dell R630) |
| Mémoire (utilisée, cache, totale) | version et nom du système |
| Disque racine (utilisé, libre, %) | adresses IP et interfaces réseau |
| Charge (1/5/15 min) et nb de cœurs | noms des processus et des services |
| Pression CPU/IO (PSI) et activité disque | points de montage, disques, cgroups |
| Alertes CPU/RAM/disque, **libellés en clair** | noms internes des alertes Netdata |

Les alertes passent par une liste blanche (`ALERTES` dans `r630-status.py`) qui
remplace les noms techniques (`disk_space./.disk_space_usage`) par des libellés
français : toute alerte non prévue est ignorée.

## Le contrat JSON

`GET /status.json` → voir [`mock.json`](mock.json) pour un exemple complet et
exact. Points à connaître :

- unités : `ram` en Mo, `disk` en Go, `cpu` et `pressure` en %, `load` sans unité ;
- `history` : 60 points espacés de 60 s, **en ordre chronologique croissant**
  (Netdata renvoie l'inverse, le proxy retourne les séries) ;
- `history` ne contient que des nombres : les trous sont comblés par la première
  ou la dernière valeur connue, jamais par `0` ;
- `age_seconds` : âge du relevé côté serveur. Si Netdata ne répond plus, le proxy
  continue de servir le dernier snapshot avec un `age_seconds` qui augmente, et
  la page passe en « obsolète » au-delà de 90 s sans effacer l'affichage.

## Tester en local

```sh
python3 -m http.server 8080        # puis ouvrir http://localhost:8080/?api=mock.json
node tests/rendu.test.mjs          # valide le rendu sans navigateur (DOM simulé)
```

Le test simule `document` et `fetch`, charge `app.js` et vérifie les valeurs
affichées, les décimales françaises, les seuils de couleur, les badges d'alerte
et le nombre de points de chaque sparkline.

L'URL de l'API est surchargeable par le paramètre `?api=`, ce qui permet de viser
un proxy local : `?api=http://127.0.0.1:8814/status.json`.

## Installation côté serveur

Tout est dans [`install/`](install) et se pose **sans root**. Détails et pièges
dans [`install/README.md`](install/README.md) : Netdata en static build dans un
namespace utilisateur, le proxy filtré, les deux services systemd user, et
l'exposition par Tailscale Funnel.

## Licences

Le code de ce dépôt est sous licence MIT (voir [LICENSE](LICENSE)).
Netdata est un logiciel tiers sous GPL v3, téléchargé séparément : rien de
Netdata n'est redistribué ici.
