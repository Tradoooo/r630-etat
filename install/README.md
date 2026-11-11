# Installation côté serveur

Deux services systemd **user** (aucun droit root nécessaire pour les installer).
Testé sur Debian 13, 80 cœurs, 32 Go de RAM.

Remplacer ci-dessous `100.124.18.48` par l'IP Tailscale du serveur. L'exposition
publique passe par le Tailscale Funnel d'une machine du tailnet (section 3) :
aucune commande `sudo` n'est nécessaire si ce Funnel est déjà établi et le compte
opérateur Tailscale.

## 1. Netdata, sans root

Le static build officiel est compilé pour `/opt/netdata` et **vérifie son
existence au démarrage** (`verify_required_directory()` dans
`daemon/environment.c`) : sans ce répertoire, il s'arrête net. Or `/opt`
appartient à root.

```sh
mkdir -p ~/.cache/netdata-install && cd ~/.cache/netdata-install
curl -sLO https://github.com/netdata/netdata/releases/download/v2.12.3/netdata-x86_64-v2.12.3.gz.run
```

L'installeur refuse de s'exécuter sans root (`Administrative privileges
required for this archive`), y compris avec `--noexec`. On extrait donc le
payload à la main — l'offset est dans l'en-tête (`skip="830"`, à lire avec
`head -c 4000 netdata.run | strings | grep skip`) :

```sh
mkdir -p ~/opt/netdata
tail -n +831 netdata.run | gzip -dc \
  | tar xf - -C ~/opt/netdata --no-same-owner --no-same-permissions
chmod -R u+w ~/opt/netdata          # l'archive est en lecture seule
```

Les variables `NETDATA_*_DIR` **ne sont pas lues** par le binaire pour ses
chemins de base (piège : elles ne servent qu'aux plugins). Ce qui fonctionne, ce
sont les clés de config `[global] * directory`, posées dans `netdata.conf` et
chargées avec `-c` : c'est le rôle de `netdata-user`.

Reste le répertoire de config utilisateur, non redirigeable. D'où le namespace :
`netdata-ns` monte un tmpfs sur `/opt`, y bind l'installation réelle, puis lance
Netdata. Rien n'est créé ni visible côté hôte.

> **`--propagation private` est indispensable.** Sans lui, le tmpfs monté sur
> `/opt` se propage à tout le système et masque le vrai `/opt`.

## 2. Fichiers à poser

| fichier | destination |
|---|---|
| `netdata.conf`, `go.d.conf` | `~/opt/netdata/etc/netdata/` |
| `netdata-user`, `netdata-ns` | `~/opt/netdata/bin/` (exécutables) |
| `netdata.service` | `~/.config/systemd/user/` |
| `r630-status.py` | `~/opt/r630-status/` (exécutable) |
| `r630-status.service` | `~/.config/systemd/user/` |
| `r630-relais.service` | `~/.config/systemd/user/` — sur la **machine du Funnel**, pas sur le serveur |

Les deux fichiers `.service` contiennent des chemins absolus vers le home :
les adapter si le compte n'est pas `/home/baptiste`.

```sh
systemctl --user daemon-reload
systemctl --user enable --now netdata.service r630-status.service
loginctl enable-linger "$USER"    # déjà actif ici : les services survivent au logout
```

## 3. Exposition publique

Trajet : Internet → Tailscale Funnel (machine du tailnet) → relais TCP local →
proxy `r630-status` sur le serveur. `sudo` n'est nécessaire que si le Funnel n'est
pas déjà établi ni le compte opérateur Tailscale (`sudo tailscale set
--operator=$USER`, une fois).

1. Sur le serveur, vérifier que le proxy écoute sur son IP Tailscale
   (`ss -tln | grep 8814` doit montrer `100.x.x.x:8814`, pas `127.0.0.1`) —
   c'est `adresse_ecoute()` dans `r630-status.py` qui s'en charge au démarrage.
2. Sur la machine qui porte le Funnel, poser le relais (pas de sudo) :
   `cp r630-relais.service ~/.config/systemd/user/` puis
   `systemctl --user daemon-reload && systemctl --user enable --now r630-relais`.
   Ce `socat` écoute sur `127.0.0.1:18814` et transmet à `100.124.18.48:8814`.
3. Sur cette même machine, ajouter le chemin **puis restaurer le Funnel** —
   `tailscale serve` le désactive à chaque modification, il faut donc
   impérativement terminer par `tailscale funnel` :

   ```sh
   tailscale serve --bg --https=8443 --set-path=/r630 http://127.0.0.1:18814
   tailscale funnel --bg --https=8443 --yes http://127.0.0.1:8000
   ```

   La seconde commande réactive le Funnel pour le service déjà exposé sur `/`
   (remplacer `8000` par le port racine existant) et le propage au chemin `/r630`.
   Vérifier : `tailscale serve status` doit montrer `Funnel on` avec `/` et `/r630`.
4. Vérifier le **contenu** (jamais le seul code HTTP : un chemin catch-all
   répond 200 avec du HTML) :

   ```sh
   curl -s https://<machine>.tail3a8bdf.ts.net:8443/r630/status.json | head -c 60
   ```

> `tailscale serve` ne relaie que vers la machine locale : proxyser directement
> une IP du tailnet se solde par un timeout sans aucune réponse — d'où le relais.

## 4. Vérification

```sh
systemctl --user is-active netdata.service r630-status.service
curl -s http://$(tailscale ip -4):8814/healthz                # {"ok": true}
curl -s http://127.0.0.1:8814/api/v1/charts                 # {"erreur": "route inconnue"}
curl -s https://<nom-tailscale>/r630/status.json | head -c 200
ls /opt ; grep -c " /opt " /proc/mounts                      # hôte intact : 0 montage
journalctl --user -u netdata --since "1 min ago" | grep -c "level=error"   # 0 attendu
```

La deuxième ligne doit renvoyer une 404 : c'est la preuve qu'aucune route ne
relaie vers Netdata.

## 5. Réglages choisis

Netdata (dans `netdata.conf` et `go.d.conf`) :

- `[ml] enabled = no`, `[cloud] enabled = no` : serveur saturé par la CI, pas
  d'entraînement ML ni de sortie vers Netdata Cloud ;
- `[health] script to execute on alarm = /bin/true` : `alarm-notify.sh` sortait en
  erreur à chaque alerte et tenterait un envoi vers l'extérieur. Les alertes
  restent visibles dans le dashboard et l'API ;
- `[plugins] apps = no`, `systemd-journal = no`, `scripts.d = no`, `cgroups = no`,
  `tc = no`, `ebpf = no` : sans root ces plugins échouent et remplissaient
  journald (jusqu'à 150 lignes d'erreur par minute). Bilan après réglage :
  **0 erreur par minute** ;
- `go.d` : `logind: no` (timeout D-Bus en boucle depuis le namespace) ;
- `[db]` : 512 Mo + 256 Mo de rétention, le disque racine étant à ~90 % ;
- service : `Nice=10`, `CPUWeight=60`, IO best-effort, `TasksMax=4096` (Netdata
  ouvre ~590 threads sur 80 cœurs).

Coût mesuré : Netdata ~5-8 % d'un cœur et 65 Mo de RSS ; le proxy ~0,3 % et 16 Mo.

Deux particularités de l'API Netdata à connaître avant de modifier
`r630-status.py` : `/api/v1/data` renvoie les points **du plus récent au plus
ancien**, et le chart `system.cpu` de cette installation n'a **pas** de
dimension `idle` (d'où un calcul par somme des dimensions non oisives).

## 6. Pièges rencontrés

- `pkill -f "srv/netdata"` depuis SSH **tue la session SSH elle-même** : le motif
  matche la ligne `bash -c …` en cours. Utiliser `pkill -x netdata` ou un motif
  entre crochets (`pkill -f "netdat[a]"`).
- `[web] allow connections from` faisait fermer les connexions locales
  (`curl 127.0.0.1` → code 000 en 0,7 ms) alors que le socket écoutait bien.
  `bind to` suffit à restreindre l'accès.
- `system.cpu_full_pressure` n'existe pas : le noyau n'expose que la ligne
  `some` pour la pression CPU. D'où deux fenêtres différentes dans le proxy.
- Un one-liner SSH avec `python3 -c` et guillemets imbriqués casse facilement :
  écrire le script dans un fichier et le pousser avec `scp`.

## 7. Désinstallation

Sur le serveur :

```sh
systemctl --user disable --now r630-status.service netdata.service
rm -rf ~/opt/netdata ~/opt/r630-status \
       ~/.config/systemd/user/{netdata,r630-status}.service
```

Sur la machine du Funnel :

```sh
systemctl --user disable --now r630-relais.service
rm -f ~/.config/systemd/user/r630-relais.service
tailscale serve --https=8443 --set-path=/r630 off
tailscale funnel --bg --https=8443 --yes http://127.0.0.1:8000  # garde la racine publique
```
