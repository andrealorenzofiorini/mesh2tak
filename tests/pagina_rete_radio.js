// Scheda "Radio della rete" dietro Caddy e Authentik: elenco, ordine (prima i problemi), nessun codice eseguito
// dai nomi delle radio, ricerca che resta dopo l'aggiornamento, ritorno alla scheda dei ponti.
const { chromium } = require('playwright');
const { sfondoFinto, scheda, IMMAGINI } = require('./pagina_comune');
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  const p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url + '#radio', {waitUntil: 'networkidle'});    // indirizzo della scheda di prima della 1.15.0
  await p.waitForSelector('li.voce .nome b', {timeout: 15000});
  await p.waitForTimeout(300);
  if (foto) await p.screenshot({path: foto, fullPage: true});
  const r = {errori, finestre,
             riassunto: await p.innerText('.radio-rete .riassunto'),
             ordine: await p.$$eval('li.voce .nome b', x => x.map(e => e.textContent)),
             voci: await p.$$eval('li.voce', x => x.map(e => e.innerText)),
             immagini: await p.evaluate(IMMAGINI)};
  r.visibili = [await p.isVisible('#squadra'), await p.isVisible('#mappa-copertura')];
  await p.fill('#cerca-radio', 'bianchi');
  await p.evaluate(() => giro());
  await p.waitForTimeout(800);
  r.cerca = await p.$$eval('li.voce .nome b', x => x.map(e => e.textContent));
  // dalla scheda Mappa, l'elenco accanto alla mappa porta alle radio
  await scheda(p, 'mappa');
  r.mappa = [await p.isVisible('#mappa-copertura'), await p.isVisible('#squadra'), p.url().endsWith('#mappa')];
  await p.click('.voce-radio a');
  await p.waitForSelector('#scheda-squadra[aria-selected="true"]');
  r.ponti = [await p.isVisible('#squadra'), p.url().endsWith('#squadra')];
  await scheda(p, 'ripetitori');
  r.ripetitori = await p.innerText('#ripetitori');
  r.schede = await p.$$eval('.schede button', x => x.map(b => b.textContent));
  console.log(JSON.stringify(r));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
