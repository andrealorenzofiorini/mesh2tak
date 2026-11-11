// Radio della squadra gestite da lontano (1.10.0) nel browser: dalla scheda "Radio" alla radio finta dietro il
// ponte (Caddy, Authentik, tunnel, nodo). Legge nome e sezione, ne cambia una, legge i canali. Stampa in JSON.
const { chromium } = require('playwright');
const { sfondoFinto } = require('./pagina_comune');
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
  p.on('dialog', d => { finestre.push(d.message()); d.accept(); });
  const fatto = async () => {
    await p.waitForFunction(() => ['fatto', 'errore'].includes(document.getElementById('radio-stato').dataset.esito),
                            null, {timeout: 60000});
    const s = await p.$eval('#radio-stato', e => [e.dataset.esito, e.textContent]);
    await p.evaluate(() => { document.getElementById('radio-stato').dataset.esito = ''; });
    return s;
  };
  await p.goto(url + '#radio', {waitUntil: 'networkidle'});
  await p.waitForSelector('li.voce', {timeout: 15000});
  await p.waitForTimeout(300);
  const r = {errori, finestre, bottoni: await p.$$eval('li.voce', x => x.map(e => [e.querySelector('.nome b').textContent,
                                                                                    !!e.querySelector('button.gestisci')]))};
  await p.click('li.voce:has-text("07-BIANCHI") button.gestisci');
  await p.waitForSelector('#radio[open] #radio-titolo');
  await p.waitForFunction(() => document.querySelectorAll('#radio-sezione option').length > 5, null, {timeout: 15000});
  r.titolo = await p.innerText('#radio-titolo');
  await p.click('#radio summary');
  r.chiaveAdmin = await p.innerText('#radio .chiave-invito code');
  // nome e info
  await p.click('#radio-dentro button:has-text("Leggi dalla radio") >> nth=0');
  r.lettura = await fatto();
  r.nome = [await p.inputValue('#radio-nome'), await p.inputValue('#radio-sigla')];
  // una sezione: si legge, si cambia un valore, si salva (con la domanda di conferma)
  await p.selectOption('#radio-sezione', 'position');
  await p.click('#radio-sezione ~ button');
  r.letturaSezione = await fatto();
  await p.fill('#rc-position-position_broadcast_secs', '300');
  await p.click('#rc-position-position_broadcast_secs >> xpath=ancestor::form//button[@type="submit"]');
  r.salvataggio = await fatto();
  // canali: le chiavi non arrivano alla pagina
  await p.click('#radio-dentro button:has-text("Leggi i canali")');
  r.canali = await fatto();
  r.moduliCanali = await p.$$eval('#radio form.canale h4', x => x.map(e => e.textContent));
  r.sceltaChiave0 = await p.$eval('#rk-0-chiave', s => s.options[s.selectedIndex].textContent);
  r.html = (await p.content()).length;
  r.testoFinestra = await p.innerText('#radio-dentro');
  if (foto) await p.screenshot({path: foto, fullPage: true});
  await p.click('#radio-dentro .pulsanti button:has-text("Chiudi")');
  r.chiusa = !(await p.isVisible('#radio[open]'));
  console.log(JSON.stringify(r));
  await b.close();
})().catch(e => { console.log(JSON.stringify({errore: String(e)})); process.exit(1); });
