'use strict';

/* Page publique de l'état du r630. Aucune dépendance externe : tout est
   vanilla et les graphes sont des SVG construits ici.
   Source des données : le proxy filtré `r630-status` installé sur le serveur,
   qui ne publie que ces mesures (voir README.md pour ce qui est exclu).
   Test local : ouvrir index.html?api=mock.json */

const API_URL = new URLSearchParams(location.search).get('api')
  || 'https://r630-debian.tail3a8bdf.ts.net/r630/status.json';

const REFRESH_MS = 10000;
const OBSOLETE_APRES_S = 90;

/* Seuils [attention, critique] en pourcentage. Pour la charge, le pourcentage
   est calculé par rapport au nombre de cœurs. */
const SEUILS = {
  cpu: [65, 85],
  ram: [75, 90],
  disque: [80, 90],
  charge: [70, 100],
};

let dernier = null;

const $ = (id) => document.getElementById(id);

function texte(valeur) {
  return String(valeur === null || valeur === undefined ? '' : valeur)
    .replace(/[&<>"]/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function nb(valeur, decimales = 1) {
  if (typeof valeur !== 'number' || !isFinite(valeur)) return '—';
  return valeur.toFixed(decimales).replace('.', ',');
}

function go(megaoctets) {
  return nb(megaoctets / 1024) + ' Go';
}

function niveau(cle, pourcentage) {
  const seuils = SEUILS[cle] || [75, 90];
  if (pourcentage >= seuils[1]) return 'crit';
  if (pourcentage >= seuils[0]) return 'warn';
  return 'ok';
}

function formaterDuree(secondes) {
  if (secondes < 60) return secondes + ' s';
  const minutes = Math.floor(secondes / 60);
  if (minutes < 60) return minutes + ' min';
  return Math.floor(minutes / 60) + ' h ' + String(minutes % 60).padStart(2, '0');
}

/* Sparkline SVG (polyline + aire). `plafond` borne l'échelle verticale :
   100 pour les pourcentages, le nombre de cœurs pour la charge. */
function sparkline(valeurs, plafond) {
  if (!Array.isArray(valeurs) || valeurs.length < 2) return '';
  const max = Math.max(plafond || 0, ...valeurs);
  const echelle = max > 0 ? max : 1;
  const pas = 100 / (valeurs.length - 1);
  const points = valeurs.map((v, i) => {
    const x = (i * pas).toFixed(2);
    const y = (29 - (v / echelle) * 27).toFixed(2);
    return x + ',' + y;
  }).join(' ');
  return '<svg viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">'
    + '<polygon class="aire" points="0,30 ' + points + ' 100,30"></polygon>'
    + '<polyline class="trait" points="' + points + '"></polyline>'
    + '</svg>';
}

/* Met à jour une carte : valeur, jauge, détail, sparkline et couleur de niveau.
   Les identifiants suivent la convention <cle>-valeur / <cle>-detail /
   spark-<cle> / carte-<cle>. */
function carte(cle, pourcentage, valeur, detail, serie, plafond) {
  const element = $('carte-' + cle);
  if (!element) return;
  element.className = 'carte n-' + niveau(cle, pourcentage);
  const champValeur = $(cle + '-valeur');
  if (champValeur) champValeur.textContent = valeur;
  const champDetail = $(cle + '-detail');
  if (champDetail) champDetail.textContent = detail;
  const barre = element.querySelector('.jauge > i');
  if (barre) barre.style.width = Math.max(0, Math.min(100, pourcentage)) + '%';
  const graphe = $('spark-' + cle);
  if (graphe) graphe.innerHTML = sparkline(serie, plafond);
}

function rendreAlertes(alertes) {
  const zone = $('alertes');
  const liste = Array.isArray(alertes) ? alertes : [];
  zone.hidden = liste.length === 0;
  zone.innerHTML = liste.map((a) => {
    const gravite = a.level === 'critical' ? 'critical' : 'warning';
    const valeur = typeof a.value === 'number'
      ? nb(a.value) + (a.unit ? '\u00A0' + a.unit : '') : '';
    return '<span class="alerte ' + gravite + '">' + texte(a.label || 'Alerte')
      + ' <b>' + texte(valeur) + '</b></span>';
  }).join('');
}

function rendreCartes(d) {
  const cpu = d.cpu || {};
  const cpuUtilise = Math.max(0, 100 - (cpu.idle || 0));
  carte('cpu', cpuUtilise, nb(cpuUtilise, 0),
    'user ' + nb(cpu.user) + ' % · système ' + nb(cpu.system)
      + ' % · attente disque ' + nb(cpu.iowait) + ' %',
    d.history.cpu_pct, 100);

  const ram = d.ram || {};
  const ramPct = ram.total_mb ? (ram.used_mb / ram.total_mb) * 100 : 0;
  carte('ram', ramPct, nb(ramPct, 0),
    go(ram.used_mb) + ' utilisés sur ' + go(ram.total_mb)
      + ' · ' + go(ram.cached_mb) + ' en cache',
    d.history.ram_used_pct, 100);

  const disque = d.disk || {};
  carte('disque', disque.used_pct || 0, nb(disque.used_pct, 0),
    nb(disque.avail_gb) + ' Go libres sur ' + nb(disque.total_gb) + ' Go',
    d.history.disk_used_pct, 100);

  const charge = d.load || {};
  const coeurs = charge.cores || 1;
  const serieCharge = d.history.load1 || [];
  carte('charge', (charge.l1 / coeurs) * 100, nb(charge.l1),
    '5 min ' + nb(charge.l5) + ' · 15 min ' + nb(charge.l15)
      + ' · ' + coeurs + ' cœurs',
    serieCharge, Math.max(coeurs, ...serieCharge));
}

function rendre(d) {
  dernier = d;
  const hote = $('hote');
  if (hote && d.host_label) hote.textContent = d.host_label;
  rendreCartes(d);
  rendreAlertes(d.alerts);
  const pression = d.pressure || {};
  $('pression-cpu').textContent = nb(pression.cpu_pct) + ' %';
  $('pression-io').textContent = nb(pression.io_pct) + ' %';
  $('io-util').textContent = nb((d.io || {}).util_pct) + ' %';
}

function ageSecondes(d) {
  const genere = d.generated_at || 0;
  const depuisGeneration = genere ? Math.round(Date.now() / 1000) - genere : 0;
  return Math.max(d.age_seconds || 0, depuisGeneration);
}

/* Horodatage rafraîchi en continu, y compris entre deux relevés réussis. */
function rendreEtat() {
  if (!dernier) return;
  const age = ageSecondes(dernier);
  const vivant = age < OBSOLETE_APRES_S;
  $('pastille').className = 'pastille ' + (vivant ? 'vivant' : 'mort');
  $('etat-texte').textContent = vivant
    ? 'direct · il y a ' + age + ' s'
    : 'obsolète · il y a ' + formaterDuree(age);
  $('pied-maj').textContent = vivant
    ? 'Dernier relevé il y a ' + age + ' s — rafraîchi toutes les '
      + (REFRESH_MS / 1000) + ' s.'
    : 'Données vieilles de ' + formaterDuree(age)
      + ' : la source ne répond plus, nouvelle tentative en cours.';
}

function signalerPanne(message) {
  $('pastille').className = 'pastille mort';
  $('etat-texte').textContent = dernier ? 'obsolète' : 'injoignable';
  $('pied-maj').textContent = message + ' — nouvelle tentative dans '
    + (REFRESH_MS / 1000) + ' s.';
}

async function charger() {
  try {
    const reponse = await fetch(API_URL, { cache: 'no-store' });
    if (!reponse.ok) throw new Error('HTTP ' + reponse.status);
    rendre(await reponse.json());
    rendreEtat();
  } catch (erreur) {
    signalerPanne('Source injoignable (' + erreur.message + ')');
  }
}

function demarrer() {
  charger();
  setInterval(charger, REFRESH_MS);
  setInterval(rendreEtat, 1000);
}

demarrer();
