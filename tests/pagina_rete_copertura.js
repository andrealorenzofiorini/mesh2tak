// Mappa della rete nel browser: copertura (1.14.0) e, dalla 1.15.0, in cima alla pagina con gateway e ripetitori. Sei
// quadretti del gateway casa (uno per colore, uno tramite altre radio, uno tramite il ripetitore fisso), legenda,
// elenco accanto alla mappa, dettagli al passaggio del mouse, zoom, file KML per ATAK, scelta di cosa mostrare e
// "Azzera". Le immagini della mappa di sfondo le da' la prova stessa (niente internet): cosi' si controlla anche che la
// pagina le possa caricare (CSP) e con che intestazioni. Stampa in JSON.
const { chromium } = require('playwright');
const { sfondoFinto, scheda } = require('./pagina_comune');
const fs = require('fs');
let p = null;
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 1000}, deviceScaleFactor: 1,
                                  acceptDownloads: true});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  const sfondo = [];
  await sfondoFinto(ctx, sfondo);
  p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.accept(); });
  await p.goto(url, {waitUntil: 'networkidle'});
  const r = {errori, finestre};
  await p.waitForSelector('#mappa-copertura[data-quadretti="6"][data-punti="2"]', {timeout: 30000});
  await p.waitForFunction(() => Number(document.getElementById('mappa-copertura').dataset.disegnati) > 0);
  await p.waitForSelector('#ripetitori section.ripetitore', {state: 'attached', timeout: 30000});
  r.prima = [await p.isVisible('#mappa-copertura'), await p.isVisible('#gateway-casa')];     // si apre sulla mappa
  r.schede = await p.$$eval('.schede button', x => x.map(b => b.textContent));
  r.prevista = await p.getAttribute('#ripetitori section.ripetitore a:has-text("Site Planner")', 'href');
  r.riassunto = await p.innerText('.piede-mappa .riassunto');
  r.legenda = await p.$$eval('.legenda li', x => x.map(e => e.innerText.trim()));
  r.rete = await p.$$eval('.elenco-rete li', x => x.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
  r.coperturaGateway = await p.innerText('section.ponte .fatti div:has(dt:text-is("Copertura")) dd');
  r.ripetitore = await p.innerText('#ripetitori section.ripetitore');
  r.mostra = await p.$$eval('.comandi-copertura label:has-text("Mostra") option', x => x.map(o => [o.value, o.textContent]));
  r.zoom = Number(await p.getAttribute('#mappa-copertura', 'data-zoom'));
  await p.waitForFunction(() => [...document.querySelectorAll('#mappa-copertura .tessere img')].some(i => i.complete && i.naturalWidth > 0));
  r.sfondo = sfondo.slice(0, 2);
  r.sfondoCaricato = await p.$$eval('#mappa-copertura .tessere img', x => x.filter(i => i.naturalWidth > 0).length);
  r.crediti = await p.innerText('#mappa-copertura .crediti');
  // dettagli passandoci sopra con il mouse: vicino al gateway buono, a nord tramite altre radio, piu' a nord tramite il
  // ripetitore; sul ripetitore stesso, il suo stato
  await p.$eval('#mappa-copertura', e => e.scrollIntoView({block: 'center'}));
  const punto = async (lat, lon) => p.evaluate(([a, o]) => document.getElementById('mappa-copertura').puntoSchermo(a, o), [lat, lon]);
  const suggerimento = async (lat, lon, attesa) => {
    const q = await punto(lat, lon);
    await p.mouse.move(q.x, q.y);
    await p.waitForFunction(t => { const s = document.querySelector('#mappa-copertura .suggerimento');
                                   return !s.hidden && new RegExp(t).test(s.innerText); }, attesa);
    return p.innerText('#mappa-copertura .suggerimento');
  };
  r.buono = await suggerimento(45.8206, 8.8281, '^Buono');
  r.ripetuta = await suggerimento(45.8406, 8.8251, '^Tramite altre radio');
  r.viaRipetitore = await suggerimento(45.8606, 8.8051, '^Tramite un ripetitore');
  r.segnoRipetitore = await suggerimento(45.875, 8.805, 'Velate');
  if (foto) {
    await p.screenshot({path: foto, fullPage: false});
    await p.setViewportSize({width: 390, height: 844});           // com'e' sul telefono (solo per guardarla)
    await p.evaluate(() => window.scrollTo(0, 0));
    await p.screenshot({path: foto.replace(/\.png$/, '-telefono.png'), fullPage: true});
    await p.setViewportSize({width: 1280, height: 1000});
  }
  // dall'elenco accanto alla mappa: il ripetitore al centro della mappa
  await p.evaluate(() => window.scrollTo(0, 0));
  await p.click('.elenco-rete li[data-tipo="ripetitore"] button.centra');
  await p.waitForTimeout(300);
  r.centrato = await p.evaluate(() => {
    const m = document.getElementById('mappa-copertura'), q = m.puntoSchermo(45.875, 8.805), b = m.getBoundingClientRect();
    return {zoom: Number(m.dataset.zoom), dx: Math.round(q.x - (b.left + b.width / 2)), dy: Math.round(q.y - (b.top + b.height / 2))};
  });
  // zoom: da lontano i quadretti si uniscono
  await p.selectOption('.comandi-copertura label:has-text("Mostra") select', '');       // torna a tutta la rete, adattata
  await p.selectOption('.comandi-copertura label:has-text("Mostra") select', 'g:casa');
  await p.selectOption('.comandi-copertura label:has-text("Mostra") select', '');
  r.zoomAdattata = Number(await p.getAttribute('#mappa-copertura', 'data-zoom'));
  await p.$eval('#mappa-copertura', e => e.scrollIntoView({block: 'center'}));
  for (let i = 0; i < 3; i++) await p.click('#mappa-copertura button[aria-label="Allontana"]');
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
  await p.click('#mappa-copertura .sfondi button:has-text("Stradale")');
  await p.waitForFunction(() => [...document.querySelectorAll('#mappa-copertura .tessere img')].some(i => i.src.includes('tile.openstreetmap.org')));
  r.creditiStrade = await p.innerText('#mappa-copertura .crediti');
  r.sfondoScelto = await p.$$eval('#mappa-copertura .sfondi button', x => x.map(e => e.getAttribute('aria-pressed')));
  // solo il ripetitore: il suo quadretto
  await p.selectOption('.comandi-copertura label:has-text("Mostra") select', 'r:!3333cccc');
  r.soloRipetitore = [await p.getAttribute('#mappa-copertura', 'data-quadretti'), await p.innerText('.piede-mappa .riassunto'),
                      await p.isVisible('button:has-text("Azzera")')];
  // lo storico: i grafici del gateway e del ripetitore
  await scheda(p, 'storico');
  await p.waitForSelector('.blocco-storico svg path.linea', {timeout: 20000});
  r.storico = await p.$$eval('.blocco-storico', x => x.map(b => [b.querySelector('h3').textContent,
    [...b.querySelectorAll('figcaption')].map(f => f.innerText.replace(/\s+/g, ' ').trim())]));
  r.linee = await p.$$eval('.blocco-storico svg path.linea', x => x.length);
  await scheda(p, 'rete');
  // azzera: solo scegliendo un gateway, con la conferma
  r.azzeraPrima = await p.isVisible('button:has-text("Azzera")');
  await p.selectOption('.comandi-copertura label:has-text("Mostra") select', 'g:casa');
  r.azzeraBottone = await p.innerText('button:has-text("Azzera")');
  await p.click('button:has-text("Azzera")');
  await p.waitForSelector('#mappa-copertura[data-quadretti="0"]', {timeout: 20000});
  r.statoAzzera = await p.innerText('.stato-copertura');
  r.riassuntoDopo = await p.innerText('.piede-mappa .riassunto');
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  let stato = null;
  try { if (p) stato = await p.innerText('#mappa', {timeout: 2000}); } catch (x) { /* pagina chiusa */ }
  console.log(JSON.stringify({errore: String(e).slice(0, 600), stato: stato && stato.slice(0, 900)}));
  process.exit(1);
});
