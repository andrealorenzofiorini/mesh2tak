// Dove mettere un ripetitore, nel browser (1.18.0): con la copertura prevista accesa, «Trova i punti migliori» propone
// il colle di prova (con il nome e il traliccio che dice OpenStreetMap, qui finto), «Copri un punto» dice da dove si
// raggiunge un punto dietro al colle, «Profilo» disegna il terreno tra due capi, e per ogni impianto c'e' scritto che
// cosa si perde se si spegne. Stampa in JSON.
const { chromium } = require('playwright');
const { sfondoFinto } = require('./pagina_comune');
let p = null;
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 1000}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx, null);
  p = await ctx.newPage();
  const errori = [], luoghi = [];
  let osmSpento = false;
  p.on('console', m => { if (m.type() === 'error' && !(osmSpento && /502/.test(m.text()))) errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('response', x => { if (x.url().includes('/api/luoghi/')) luoghi.push(x.status()); });
  await p.goto(url, {waitUntil: 'networkidle'});
  const r = {errori};
  const mappa = '#mappa-copertura', spunta = '.comandi-copertura label.spunta input';
  await p.waitForSelector(mappa + '[data-quadretti="6"][data-punti="2"]', {timeout: 30000});
  r.strumentiPrima = await p.isVisible('.strumenti-prevista');
  await p.check(spunta);
  await p.waitForFunction(() => /Copertura prevista entro/.test(document.querySelector('.stato-prevista').textContent), null, {timeout: 120000});
  r.strumenti = await p.$$eval('.strumenti-prevista button', x => x.map(e => e.textContent));
  const elenco = sel => p.$$eval(sel, x => x.map(e => e.innerText.replace(/\s+/g, ' ').trim()));
  r.elenco = await p.$$eval('.esito-siti ~ .elenco-previste > li', x => x.map(e => [e.dataset.impianto, e.innerText.replace(/\s+/g, ' ').trim()]));
  const profilo = async () => {
    await p.waitForSelector('#profilo[open] svg path.terreno', {timeout: 60000});
    return [await p.innerText('#profilo-titolo'), await p.innerText('#profilo-esito')];
  };
  // il profilo tra il ripetitore e il gateway
  await p.click('.elenco-previste li[data-impianto="r:!3333cccc"] p button:has-text("Profilo")');
  [r.profiloTitolo, r.profiloEsito] = await profilo();
  r.profiloDisegno = await p.$eval('#profilo-figura', f => ({terreno: f.querySelectorAll('path.terreno').length,
    linea: f.querySelectorAll('path.linea').length, libero: f.querySelectorAll('path.libero').length,
    capi: [...f.querySelectorAll('text.capo')].map(t => t.textContent), legenda: [...f.querySelectorAll('.legenda-profilo li')].map(l => l.textContent)}));
  const disegno = await p.$eval('#profilo-figura svg', e => { const q = e.getBoundingClientRect(); return {x: q.left, y: q.top, w: q.width, h: q.height}; });
  await p.mouse.move(disegno.x + disegno.w * 0.5, disegno.y + disegno.h * 0.4);
  await p.waitForFunction(() => /^A /.test(document.querySelector('#profilo-figura .lettura').textContent));
  r.profiloLettura = await p.innerText('#profilo-figura .lettura');
  await p.click('#profilo-chiudi');
  // trova i punti migliori nella zona della mappa
  await p.click('button:has-text("Trova i punti migliori")');
  await p.waitForSelector('.proposte[data-proposte="trova"] ol li', {timeout: 240000});
  r.trovaTesta = await p.innerText('.proposte[data-proposte="trova"] > p');
  r.trova = await elenco('.proposte[data-proposte="trova"] ol li');
  r.segniTrova = Number(await p.getAttribute(mappa, 'data-segni'));
  r.luoghi = luoghi.slice();
  const passa = async (lat, lon, attesa) => {
    await p.evaluate(([a, o]) => mappaCopertura.centra(a, o), [lat, lon]);
    await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
    const q = await p.evaluate(([a, o]) => document.getElementById('mappa-copertura').puntoSchermo(a, o), [lat, lon]);
    await p.mouse.move(q.x + 2, q.y + 2);
    await p.mouse.move(q.x, q.y);
    await p.waitForFunction(t => { const s = document.querySelector('#mappa-copertura .suggerimento');
                                   return !s.hidden && new RegExp(t).test(s.innerText); }, attesa);
    return p.innerText(mappa + ' .suggerimento');
  };
  const uno = await p.evaluate(() => siti.ricerca.proposte[0]);
  r.segnoUno = await passa(uno.lat, uno.lon, '^Punto 1');
  if (foto) await p.screenshot({path: foto, fullPage: true});
  await p.click('.proposte[data-proposte="trova"] li[data-sito="1"] button:has-text("Profilo")');
  [r.trovaProfilo, r.trovaProfiloEsito] = await profilo();
  await p.click('#profilo-chiudi');
  await p.click('.proposte[data-proposte="trova"] li[data-sito="1"] button:has-text("Prova qui")');
  await p.waitForFunction(() => { const li = document.querySelector('.elenco-previste li[data-impianto="p:prova"]');
                                  return li && /aggiunge/.test(li.textContent); }, null, {timeout: 120000});
  r.provaQui = [await p.inputValue('.comandi-copertura label:has-text("Mostra") select'),
                await p.$eval('.elenco-previste li[data-impianto="p:prova"]', e => e.innerText.replace(/\s+/g, ' '))];
  await p.click('.elenco-previste button:has-text("Togli il punto")');
  // copri un punto: 2,3 km a est del colle
  await p.click('button:has-text("Copri un punto")');
  r.copriInvito = await p.innerText('.stato-prevista');
  await p.evaluate(() => mappaCopertura.centra(45.80, 8.90));
  await p.$eval(mappa, e => e.scrollIntoView({block: 'center'}));
  const q = await p.evaluate(() => document.getElementById('mappa-copertura').puntoSchermo(45.80, 8.90));
  await p.mouse.click(q.x, q.y);
  await p.waitForFunction(() => { const e = document.querySelector('.proposte[data-proposte="copri"]'); return e && /Oggi/.test(e.textContent); },
                          null, {timeout: 240000});
  r.copriTesta = await p.innerText('.proposte[data-proposte="copri"] .testa-prevista b');
  r.copriOggi = await p.$eval('.proposte[data-proposte="copri"] > p', e => e.innerText.replace(/\s+/g, ' '));
  r.copri = await elenco('.proposte[data-proposte="copri"] ol li');
  r.segniCopri = Number(await p.getAttribute(mappa, 'data-segni'));
  r.segnoBersaglio = await passa(45.80, 8.90, '^Punto da coprire');
  if (foto) await p.screenshot({path: foto.replace(/\.png$/, '-copri.png'), fullPage: true});
  await p.click('.proposte[data-proposte="copri"] li[data-sito="A"] button:has-text("Profilo fino al punto")');
  [r.copriProfilo, r.copriProfiloEsito] = await profilo();
  if (foto) await p.screenshot({path: foto.replace(/\.png$/, '-profilo.png')});
  await p.click('#profilo-chiudi');
  // tolte le proposte e spenta la prevista
  await p.click('button:has-text("Togli le proposte")');
  await p.click('.proposte[data-proposte="copri"] button:has-text("Togli il punto")');
  await p.uncheck(spunta);
  r.dopo = {proposte: await p.$$eval('.proposte', x => x.length), segni: await p.getAttribute(mappa, 'data-segni'),
            strumenti: await p.isVisible('.strumenti-prevista')};
  // OpenStreetMap che non risponde (qui: il nostro server risponde 502): la ricerca va avanti senza nomi
  osmSpento = true;
  await p.route('**/api/luoghi/**', x => x.fulfill({status: 502, contentType: 'application/json', body: '{"errore":"OpenStreetMap non risponde: riprova"}'}));
  await p.evaluate(() => luoghiTenuti.clear());
  await p.check(spunta);
  await p.waitForFunction(() => /Copertura prevista entro/.test(document.querySelector('.stato-prevista').textContent), null, {timeout: 120000});
  await p.click('button:has-text("Trova i punti migliori")');
  await p.waitForSelector('.proposte[data-proposte="trova"] ol li', {timeout: 240000});
  r.senzaOsmTesta = await p.innerText('.proposte[data-proposte="trova"] > p');
  r.senzaOsm = (await elenco('.proposte[data-proposte="trova"] ol li'))[0];
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  let stato = null;
  try { if (p) stato = await p.innerText('#mappa', {timeout: 2000}); } catch (x) { /* pagina chiusa */ }
  console.log(JSON.stringify({errore: String(e).slice(0, 600), stato: stato && stato.slice(0, 1500)}));
  process.exit(1);
});
