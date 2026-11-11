// Test du rendu de la page r630-etat sans navigateur : on simule le DOM et
// fetch, on charge app.js, puis on vérifie ce qui a été écrit dans la page.
// Lancer : node tests/rendu.test.mjs   (depuis la racine du dépôt)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const racine = dirname(dirname(fileURLToPath(import.meta.url)));
const donnees = JSON.parse(readFileSync(join(racine, 'mock.json'), 'utf8'));

// Le mock porte un horodatage figé : on le rajeunit pour que le test reste
// déterministe quelle que soit la date à laquelle il est lancé.
donnees.generated_at = Math.round(Date.now() / 1000);
donnees.age_seconds = 3;

const elements = new Map();

function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id, textContent: '', innerHTML: '', className: '', hidden: false,
      style: {}, querySelector: () => ({ style: {} }),
    });
  }
  return elements.get(id);
}

globalThis.document = { getElementById: element };
globalThis.location = { search: '?api=mock.json' };
globalThis.setInterval = () => 0;
globalThis.fetch = async () => ({ ok: true, json: async () => donnees });

await import(join(racine, 'app.js'));
await new Promise((r) => setTimeout(r, 50));

const texte = (id) => element(id).textContent;

// Le contrat du mock doit produire des valeurs lisibles, jamais « — ».
assert.equal(texte('hote'), 'r630');
assert.equal(texte('cpu-valeur'), '14');           // 100 - idle(85.6)
assert.match(texte('cpu-detail'), /attente disque 5,0 %/);
assert.equal(texte('ram-valeur'), '31');            // 10025 / 32032
assert.match(texte('ram-detail'), /9,8 Go utilisés sur 31,3 Go/);
assert.equal(texte('disque-valeur'), '90');
assert.match(texte('disque-detail'), /39,6 Go libres sur 433,0 Go/);
assert.equal(texte('charge-valeur'), '32,5');
assert.match(texte('charge-detail'), /80 cœurs/);
assert.equal(texte('pression-io'), '21,7 %');
assert.equal(texte('io-util'), '60,5 %');

// Décimal française partout, jamais de point.
for (const id of ['cpu-detail', 'ram-detail', 'disque-detail', 'charge-detail']) {
  assert.ok(!/\d\.\d/.test(texte(id)), `${id} contient un point décimal`);
}

// Niveaux de couleur : disque à 90 % = critique, CPU à 14 % = ok.
assert.equal(element('carte-disque').className, 'carte n-crit');
assert.equal(element('carte-cpu').className, 'carte n-ok');
assert.equal(element('carte-charge').className, 'carte n-ok');   // 32.5/80 = 41 %

// Alertes : deux badges, niveau warning, libellé français.
const badges = element('alertes').innerHTML;
assert.equal(element('alertes').hidden, false);
assert.match(badges, /Espace disque/);
assert.match(badges, /File d&#39;attente disque|File d'attente disque/);
assert.equal((badges.match(/class="alerte warning"/g) || []).length, 2);

// Sparklines : un SVG par carte, avec polyline et aire.
for (const cle of ['cpu', 'ram', 'disque', 'charge']) {
  const svg = element('spark-' + cle).innerHTML;
  assert.match(svg, /<svg viewBox="0 0 100 30"/, `sparkline ${cle} absente`);
  assert.match(svg, /<polyline class="trait"/);
  // 60 points dans la polyline + les mêmes 60 dans l'aire + ses 2 coins.
  assert.equal((svg.match(/-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?/g) || []).length, 122,
    `sparkline ${cle} : 60 points attendus (polyline + aire + 2 coins)`);
}

// État : données fraîches -> pastille verte et mention « direct ».
assert.equal(element('pastille').className, 'pastille vivant');
assert.match(texte('etat-texte'), /^direct · il y a \d+ s$/);

console.log('OK — rendu validé sur mock.json');
