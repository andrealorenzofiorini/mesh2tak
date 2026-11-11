// Scheda "Copertura" (1.14.0) nel browser: mappa con i quadretti del ponte casa, legenda, dettagli al passaggio del
// mouse, zoom, file KML per ATAK e "Azzera". Le immagini della mappa di sfondo le da' la prova stessa (niente internet):
// cosi' si controlla anche che la pagina le possa caricare (CSP) e con che intestazioni. Stampa in JSON.
const { chromium } = require('playwright');
const fs = require('fs');
const SFONDO = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGN4/PIOAAVbAqlheK8AAAAAAElFTkSuQmCC', 'base64');
let p = null;
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 1000}, deviceScaleFactor: 1,
                                  acceptDownloads: true});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  const sfondo = [];
  await ctx.route(/^https:\/\/([abc]\.)?tile\.(opentopomap|openstreetmap)\.org\//, r => {
    sfondo.push({url: r.request().url(), referer: r.request().headers()['referer'] || ''});
    r.fulfill({status: 200, contentType: 'image/png', body: SFONDO});
  });
  p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.accept(); });
  await p.goto(url, {waitUntil: 'networkidle'});
  const r = {errori, finestre};
  await p.click('#scheda-copertura');
  r.indirizzo = p.url().split('#')[1] || '';
  await p.waitForSelector('#mappa-copertura[data-quadretti="5"]', {timeout: 20000});
  await p.waitForFunction(() => Number(document.getElementById('mappa-copertura').dataset.disegnati) > 0);
  r.riassunto = await p.innerText('.copertura .riassunto');
  r.legenda = await p.$$eval('.legenda li', x => x.map(e => e.innerText.trim()));
  r.ponti = await p.$$eval('.ponti-copertura li', x => x.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
  r.zoom = Number(await p.getAttribute('#mappa-copertura', 'data-zoom'));
  await p.waitForFunction(() => [...document.querySelectorAll('#mappa-copertura .tessere img')].some(i => i.complete && i.naturalWidth > 0));
  r.sfondo = sfondo.slice(0, 2);
  r.sfondoCaricato = await p.$$eval('#mappa-copertura .tessere img', x => x.filter(i => i.naturalWidth > 0).length);
  r.crediti = await p.innerText('#mappa-copertura .crediti');
  // dettagli di un quadretto passandoci sopra con il mouse (vicino al ponte: buono; a nord: solo tramite altre radio)
  await p.$eval('#mappa-copertura', e => e.scrollIntoView({block: 'center'}));
  const punto = async (lat, lon) => p.evaluate(([a, o]) => document.getElementById('mappa-copertura').puntoSchermo(a, o), [lat, lon]);
  let q = await punto(45.8206, 8.8281);
  await p.mouse.move(q.x, q.y);
  await p.waitForSelector('#mappa-copertura .suggerimento:not([hidden])');
  r.buono = await p.innerText('#mappa-copertura .suggerimento');
  q = await punto(45.8406, 8.8251);
  await p.mouse.move(q.x, q.y);
  await p.waitForFunction(() => /tramite/.test(document.querySelector('#mappa-copertura .suggerimento').innerText));
  r.ripetuta = await p.innerText('#mappa-copertura .suggerimento');
  if (foto) {
    await p.screenshot({path: foto, fullPage: false});
    await p.setViewportSize({width: 390, height: 844});           // com'e' sul telefono (solo per guardarla)
    await p.$eval('#scheda-copertura', e => e.scrollIntoView({block: 'start'}));
    await p.screenshot({path: foto.replace(/\.png$/, '-telefono.png'), fullPage: true});
    await p.setViewportSize({width: 1280, height: 1000});
    await p.$eval('#mappa-copertura', e => e.scrollIntoView({block: 'center'}));
  }
  // zoom: da lontano i quadretti si uniscono
  await p.click('#mappa-copertura button[aria-label="Allontana"]');
  await p.click('#mappa-copertura button[aria-label="Allontana"]');
  await p.click('#mappa-copertura button[aria-label="Allontana"]');
  r.zoomLontano = Number(await p.getAttribute('#mappa-copertura', 'data-zoom'));
  r.disegnatiLontano = Number(await p.getAttribute('#mappa-copertura', 'data-disegnati'));
  await p.focus('#mappa-copertura');
  await p.keyboard.press('+');
  r.zoomTastiera = Number(await p.getAttribute('#mappa-copertura', 'data-zoom'));
  // il file per ATAK
  const [scaricato] = await Promise.all([p.waitForEvent('download'), p.click('button:has-text("Scarica per ATAK")')]);
  r.kmlNome = scaricato.suggestedFilename();
  r.kml = fs.readFileSync(await scaricato.path(), 'utf8');
  r.statoKml = await p.innerText('.stato-copertura');
  // solo oggi: gli stessi quadretti; mappa stradale
  await p.selectOption('.comandi-copertura label:has-text("Periodo") select', '1');
  r.oggi = await p.getAttribute('#mappa-copertura', 'data-quadretti');
  await p.selectOption('.comandi-copertura label:has-text("Mappa") select', 'strade');
  await p.waitForFunction(() => [...document.querySelectorAll('#mappa-copertura .tessere img')].some(i => i.src.includes('tile.openstreetmap.org')));
  r.creditiStrade = await p.innerText('#mappa-copertura .crediti');
  // azzera: solo scegliendo un ponte, con la conferma
  r.azzeraPrima = await p.isVisible('button:has-text("Azzera")');
  await p.selectOption('.comandi-copertura label:has-text("Ponte") select', 'casa');
  r.azzeraBottone = await p.innerText('button:has-text("Azzera")');
  await p.click('button:has-text("Azzera")');
  await p.waitForSelector('#mappa-copertura[data-quadretti="0"]', {timeout: 20000});
  r.statoAzzera = await p.innerText('.stato-copertura');
  r.riassuntoDopo = await p.innerText('.copertura .riassunto');
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  let stato = null;
  try { if (p) stato = await p.innerText('.copertura', {timeout: 2000}); } catch (x) { /* pagina chiusa */ }
  console.log(JSON.stringify({errore: String(e).slice(0, 600), stato: stato && stato.slice(0, 900)}));
  process.exit(1);
});
