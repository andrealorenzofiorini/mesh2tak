// Ponti nuovi (1.10.0) nel browser: certificato e canale caricati dal modulo (file vero, attraverso Caddy), poi
// l'invito con le istruzioni da mandare e la chiave d'invito. Stampa quello che vede, in JSON.
const { chromium } = require('playwright');
const { sfondoFinto, apri } = require('./pagina_comune');
(async () => {
  const [url, pacchetto, chiave, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: url.replace(/\/$/, '')});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  const p = await ctx.newPage();
  globalThis.paginaProva = p;
  const errori = [], finestre = [];
  globalThis.erroriProva = errori;
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.dismiss(); });
  await p.goto(url, {waitUntil: 'networkidle'});
  await apri(p, 'aggiungi');
  await p.waitForSelector('.nuovi > .spiega', {timeout: 15000});
  const r = {errori, finestre, prima: await p.innerText('.nuovi > .spiega'),
             moduloInvitoVisibile: await p.isVisible('#invito-nome'), impostazioniAperte: await p.$eval('.nuovi details', d => d.open),
             ponti: await p.$$eval('section.ponte h3', x => x.map(e => e.textContent))};
  // certificato e canale dal modulo
  await p.setInputFiles('#nuovi-file', pacchetto);
  await p.fill('#nuovi-password', 'atakatak');
  await p.fill('#nuovi-canale', 'CNSAS-VA');
  await p.fill('#nuovi-chiave', chiave);
  await p.click('.nuovi form button[type=submit]');
  await p.waitForFunction(() => document.querySelector('.nuovi > .spiega').textContent.startsWith('Per i gateway nuovi'),
                          null, {timeout: 60000});
  r.dopo = await p.innerText('.nuovi > .spiega');
  r.campoChiaveVuoto = await p.inputValue('#nuovi-chiave');
  // un invito
  await p.fill('#invito-nome', 'velate');
  await p.selectOption('#invito-scheda', 'usb');
  await p.fill('#invito-posizione', '45.8205, 8.8257');
  await p.click('#invito-nome >> xpath=ancestor::form//button[@type="submit"]');
  await p.waitForSelector('li.invito h4', {timeout: 15000});
  await p.waitForTimeout(300);
  r.invito = await p.innerText('li.invito');
  r.chiaviInvito = await p.$$eval('li.invito .chiave-invito code', x => x.map(e => e.textContent));
  r.istruzioniAperte = await p.$eval('li.invito details', d => d.open);
  await p.click('li.invito button:has-text("Copia le istruzioni")');
  await p.waitForTimeout(300);
  r.copiate = await p.evaluate(() => navigator.clipboard.readText());
  if (foto) await p.screenshot({path: foto, fullPage: true});
  // ricaricata la pagina l'invito c'e' ancora, con la sua chiave
  await p.reload({waitUntil: 'networkidle'});
  await apri(p, 'aggiungi');
  await p.waitForSelector('li.invito h4', {timeout: 15000});
  r.dopoRicarica = await p.$$eval('li.invito .chiave-invito code', x => x.map(e => e.textContent));
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  // per capire cosa non va: lo stato del servizio visto dalla pagina, il testo della pagina e una foto
  const r = {errore: String(e), errori: globalThis.erroriProva};
  try {
    const p = globalThis.paginaProva;
    r.stato = await p.evaluate(async () => {
      const x = await fetch('api/stato', {headers: {'X-Mesh2tak': '1'}, redirect: 'manual'});
      return x.status + ' ' + (await x.text()).slice(0, 400);
    });
    r.testo = (await p.innerText('body')).slice(0, 4000);
    const foto = process.argv[5];
    if (foto) await p.screenshot({path: foto.replace('.png', '-errore.png'), fullPage: true});
    if (foto) require('fs').writeFileSync(foto.replace('.png', '-errore.json'), JSON.stringify(r, null, 1));
  } catch (e2) { r.diagnosi = String(e2); }
  console.log(JSON.stringify(r));
  process.exit(1);
});
