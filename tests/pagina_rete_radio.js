// Scheda "Radio della rete" dietro Caddy e Authentik: elenco, ordine (prima i problemi), nessun codice eseguito
// dai nomi delle radio, ricerca che resta dopo l'aggiornamento, ritorno alla scheda dei ponti.
const { chromium } = require('playwright');
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  const p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url + '#radio', {waitUntil: 'networkidle'});
  await p.waitForSelector('li.voce .nome b', {timeout: 15000});
  await p.waitForTimeout(300);
  if (foto) await p.screenshot({path: foto, fullPage: true});
  const r = {errori, finestre,
             riassunto: await p.innerText('.radio-rete .riassunto'),
             ordine: await p.$$eval('li.voce .nome b', x => x.map(e => e.textContent)),
             voci: await p.$$eval('li.voce', x => x.map(e => e.innerText)),
             visibili: [await p.isVisible('#vista-radio'), await p.isVisible('#vista-ponti')],
             immagini: await p.$$eval('main img:not(.figura-icona)', x => x.length)};
  await p.fill('#cerca-radio', 'bianchi');
  await p.evaluate(() => giro());
  await p.waitForTimeout(800);
  r.cerca = await p.$$eval('li.voce .nome b', x => x.map(e => e.textContent));
  await p.click('#scheda-ponti');
  r.ponti = [await p.isVisible('section.ponte'), p.url().endsWith('#radio')];
  console.log(JSON.stringify(r));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
