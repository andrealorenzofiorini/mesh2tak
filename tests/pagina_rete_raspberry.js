// "Gestisci il Raspberry" (1.13.0) nel browser: dalla scheda del ponte casa si apre la finestra, si legge il registro
// (il Raspberry lo raccoglie e la pagina lo mostra) e si salvano le impostazioni con la conferma. Stampa in JSON.
const { chromium } = require('playwright');
const { sfondoFinto } = require('./pagina_comune');
let p = null;
(async () => {
  const [url, foto] = process.argv.slice(2);
  const b = await chromium.launch({args: ['--host-resolver-rules=MAP *.prova.lan 127.0.0.1', '--no-proxy-server']});
  const ctx = await b.newContext({ignoreHTTPSErrors: true, viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
  await ctx.addCookies([{name: 'ak_finto', value: 'andrea', domain: 'meshtastic.prova.lan', path: '/'}]);
  await sfondoFinto(ctx);
  p = await ctx.newPage();
  const errori = [], finestre = [];
  p.on('console', m => { if (m.type() === 'error') errori.push(m.text()); });
  p.on('pageerror', e => errori.push(String(e)));
  p.on('dialog', d => { finestre.push(d.message()); d.accept(); });
  await p.goto(url + '#gateway', {waitUntil: 'networkidle'});
  const bottone = 'section.ponte:has(h3:text-is("casa")) button:has-text("Gestisci il Raspberry")';
  await p.waitForSelector(bottone, {timeout: 20000});
  const r = {errori, finestre};
  await p.click(bottone);
  await p.waitForSelector('#raspberry[open] #rp-squadra');
  if (foto) await p.screenshot({path: foto.replace(/\.png$/, '-inizio.png')});
  r.titolo = await p.innerText('#raspberry-titolo');
  r.sezioni = await p.$$eval('#raspberry-dentro h3', x => x.map(e => e.textContent));
  r.squadra = await p.$eval('#rp-squadra', e => e.value);
  r.validita = await p.$eval('#rp-validita', e => e.value);
  r.invio = await p.$eval('#rp-invio', e => e.checked);
  r.certificato = await p.innerText('#raspberry-dentro form:has(#rp-pacchetto) .spiegazione');
  r.comandiPrima = await p.$$eval('#raspberry-comandi li', x => x.map(e => e.innerText));
  // il registro: il comando va al Raspberry, che lo raccoglie; poi la pagina lo legge dal tunnel
  await p.click('#raspberry-dentro button:has-text("Leggi il registro")');
  await p.waitForSelector('#raspberry-dentro pre.registro', {timeout: 90000});
  r.registro = (await p.innerText('#raspberry-dentro pre.registro')).slice(0, 400);
  r.scarica = await p.$eval('#raspberry-dentro a[download]', e => e.download);
  r.statoRegistro = await p.innerText('#raspberry-stato');
  if (foto) {
    await p.$eval('#raspberry-dentro pre.registro', e => e.scrollIntoView({block: 'center'}));
    await p.screenshot({path: foto.replace(/\.png$/, '-registro.png')});
  }
  // le impostazioni: si cambia il colore della squadra e si salva, con la conferma
  await p.selectOption('#rp-squadra', 'Red');
  await p.click('#raspberry-dentro button:has-text("Salva le impostazioni")');
  await p.waitForFunction(() => {
    const s = document.getElementById('raspberry-stato');
    return s && /^Impostazioni/.test(s.textContent) && ['fatto', 'errore'].includes(s.dataset.esito);
  }, null, {timeout: 90000});
  r.statoImpostazioni = await p.innerText('#raspberry-stato');
  r.esitoImpostazioni = await p.$eval('#raspberry-stato', e => e.dataset.esito);
  r.comandi = await p.$$eval('#raspberry-comandi li', x => x.map(e => e.innerText));
  if (foto) await p.screenshot({path: foto, fullPage: false});
  await p.click('#raspberry-dentro button:has-text("Chiudi")');
  r.chiusa = await p.$eval('#raspberry', e => !e.open);
  r.ultimoComando = await p.innerText('section.ponte:has(h3:text-is("casa")) .fatti');
  console.log(JSON.stringify(r));
  await b.close();
})().catch(async e => {
  // per capire dove si e' fermata: la riga di stato della finestra
  let stato = null;
  try { if (p) stato = await p.innerText('#raspberry-stato', {timeout: 2000}); } catch (x) { /* finestra chiusa */ }
  console.log(JSON.stringify({errore: String(e), stato}));
  process.exit(1);
});
