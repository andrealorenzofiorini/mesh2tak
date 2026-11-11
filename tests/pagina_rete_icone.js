// Icone sulla mappa (1.11.0) nel browser: dalla scheda "Radio" si sceglie l'icona di una radio, poi si apre la
// scelta per il ponte dalla sua scheda. Stampa quello che vede, in JSON.
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
  await p.waitForSelector('li.voce:has-text("07-BIANCHI") button.icona-mappa', {timeout: 15000});
  const r = {errori, finestre};
  await p.click('li.voce:has-text("07-BIANCHI") button.icona-mappa');
  await p.waitForSelector('#icone[open] .tessera-icona');
  r.titolo = await p.innerText('#icone-titolo');
  r.gruppi = await p.$$eval('#icone-scelte h3', x => x.map(e => e.textContent));
  r.tessere = await p.$$eval('#icone-scelte .tessera-icona', x => x.map(e => e.textContent));
  r.sceltaPrima = await p.$eval('#icone-scelte [aria-pressed="true"]', e => e.textContent);
  await p.click('#icone-scelte .tessera-icona:has-text("Unità cinofila")');
  await p.waitForFunction(() => /^Icona salvata|^Non salvata/.test(document.getElementById('icone-esito').textContent),
                          null, {timeout: 15000});
  r.esito = await p.innerText('#icone-esito');
  r.sceltaDopo = await p.$eval('#icone-scelte [aria-pressed="true"]', e => e.textContent);
  r.ponti = await p.innerText('#icone-ponti');
  if (foto) await p.screenshot({path: foto, fullPage: false});
  await p.click('#icone-chiudi');
  r.bottoneDopo = await p.innerText('li.voce:has-text("07-BIANCHI") button.icona-mappa');
  r.figuraNelBottone = await p.$eval('li.voce:has-text("07-BIANCHI") button.icona-mappa img', e => e.src);
  // la scheda del ponte: la sua icona predefinita e' il traliccio
  await p.click('[role="tab"]:has-text("Ponti")');
  await p.click('section.ponte:has(h2:text-is("casa")) button.icona-mappa');
  await p.waitForSelector('#icone[open] .tessera-icona');
  r.titoloPonte = await p.innerText('#icone-titolo');
  r.predefinitaPonte = await p.$eval('#icone-scelte .tessera-icona', e => e.textContent);
  if (foto) await p.screenshot({path: foto.replace('.png', '-ponte.png'), fullPage: false});
  await p.click('#icone-chiudi');
  console.log(JSON.stringify(r));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
