// ---- radio della squadra gestite da lontano (dalla 1.10.0), attraverso il nodo di un ponte ----

const SEZIONI_IT = {device: "Dispositivo e ruolo", position: "Posizione e GPS", power: "Alimentazione e risparmio",
                    network: "Rete (Wi-Fi)", display: "Schermo", lora: "Radio LoRa (frequenze, preset)",
                    bluetooth: "Bluetooth", security: "Sicurezza (solo lettura)", mqtt: "MQTT",
                    telemetry: "Telemetria (batteria, sensori)", neighbor_info: "Radio vicine",
                    external_notification: "Avvisi esterni (cicalino, LED)", store_forward: "Store & Forward",
                    range_test: "Prova di portata", serial: "Porta seriale", canned_message: "Messaggi pronti",
                    detection_sensor: "Sensore di presenza", ambient_lighting: "Luci", paxcounter: "Contapersone",
                    remote_hardware: "Hardware remoto", audio: "Audio"};
const CAMPI_IT = {
  "device.role": "Ruolo della radio", "device.rebroadcast_mode": "Che cosa ripete",
  "device.node_info_broadcast_secs": "Manda il suo nome ogni (secondi)", "device.tzdef": "Fuso orario",
  "device.led_heartbeat_disabled": "LED lampeggiante spento", "device.button_gpio": "Pin del pulsante",
  "position.position_broadcast_secs": "Manda la posizione ogni (secondi)",
  "position.position_broadcast_smart_enabled": "Posizione intelligente (più spesso se si muove)",
  "position.fixed_position": "Usa una posizione fissa", "position.gps_mode": "GPS",
  "position.gps_update_interval": "Legge il GPS ogni (secondi)",
  "position.broadcast_smart_minimum_distance": "Posizione intelligente: distanza minima (metri)",
  "position.broadcast_smart_minimum_interval_secs": "Posizione intelligente: intervallo minimo (secondi)",
  "power.is_power_saving": "Risparmio di energia", "power.on_battery_shutdown_after_secs": "A batteria si spegne dopo (secondi)",
  "lora.region": "Regione (frequenze di legge)", "lora.use_preset": "Usa un preset", "lora.modem_preset": "Preset (velocità e portata)",
  "lora.hop_limit": "Salti al massimo", "lora.tx_enabled": "Trasmissione accesa",
  "lora.tx_power": "Potenza di trasmissione (dBm, 0 = la massima consentita)", "lora.channel_num": "Slot di frequenza (0 = automatico)",
  "lora.override_duty_cycle": "Ignora il limite di tempo radio (non in Europa)",
  "bluetooth.enabled": "Bluetooth acceso", "bluetooth.mode": "Abbinamento", "bluetooth.fixed_pin": "PIN fisso",
  "display.screen_on_secs": "Schermo acceso per (secondi)",
  "telemetry.device_telemetry_enabled": "Manda batteria e canale alla rete (Device Metrics)",
  "telemetry.device_update_interval": "Manda batteria e canale ogni (secondi)",
  "security.public_key": "Chiave pubblica della radio", "security.admin_key": "Chiavi amministratore",
  "security.is_managed": "Gestita solo da lontano", "security.serial_enabled": "Porta seriale attiva",
};
const RISCHI = {
  lora: "Se cambi regione, preset o frequenza questa radio non si sente più con le altre finché non le cambi tutte: da lontano non la recuperi.",
  bluetooth: "Se spegni il Bluetooth o cambi il PIN, da vicino ci si rientra solo con il cavo.",
  network: "Impostazioni di rete sbagliate possono rendere la radio irraggiungibile dal Wi-Fi.",
  power: "Con il risparmio di energia la radio dorme e da lontano risponde molto più lentamente, o per niente.",
  device: "Un ruolo sbagliato cambia come la radio ripete i messaggi degli altri (i ripetitori vanno messi solo in punti alti).",
};
let schemaRadio = null;             // sezioni e campi, come li conosce il ponte
let radioAperta = null;             // {v, ponte, ...}

function pontiCheGestiscono(v) {
  return v.ponti.filter(x => { const d = (stati.get(x.ponte) || {}).dati; return d && d.gestione && d.gestione.radio; });
}

async function chiamaRadio(ponte, percorso, corpo) {
  const opzioni = {cache: "no-store", redirect: "manual", credentials: "same-origin"};
  if (corpo) Object.assign(opzioni, {method: "POST", headers: {"Content-Type": "application/json", "X-Mesh2tak": "1"},
                                     body: JSON.stringify(corpo)});
  const r = await fetch("radio/" + encodeURIComponent(ponte) + percorso, opzioni);
  if (r.type === "opaqueredirect") { document.getElementById("sessione").hidden = false; throw new Error("sessione scaduta: ricarica la pagina"); }
  let d = {};
  try { d = await r.json(); } catch (e) { d = {}; }
  if (!r.ok) throw new Error(d.errore || (r.status === 502 ? "il gateway " + ponte + " non risponde" : "il gateway risponde " + r.status));
  return d;
}

function statoRadio(testo, esito) {
  const s = document.getElementById("radio-stato");
  s.textContent = testo;
  s.dataset.esito = esito || "";
}

async function operazioneRadio(corpo, cosa) {
  // una richiesta alla radio passa dal ponte e torna via radio: si aspetta l'esito chiedendolo al ponte
  const a = radioAperta;
  if (a.occupata) { statoRadio("Aspetta che finisca l'operazione in corso.", "errore"); return null; }
  a.occupata = true;
  document.getElementById("radio-dentro").querySelectorAll("button").forEach(b => { b.disabled = true; });
  statoRadio(cosa + ": mando la richiesta al gateway " + a.ponte + "...");
  try {
    const {op} = await chiamaRadio(a.ponte, "/op", Object.assign({radio: a.v.id}, corpo));
    const inizio = Date.now();
    for (;;) {
      await new Promise(ok => setTimeout(ok, 2000));
      if (radioAperta !== a) return null;                 // finestra chiusa: l'operazione va avanti sul ponte
      const o = await chiamaRadio(a.ponte, "/op?id=" + encodeURIComponent(op));
      if (o.stato === "fatto") { statoRadio(cosa + ": " + ((o.risultato && o.risultato.messaggio) || "fatto."), "fatto"); return o; }
      if (o.stato === "errore") { a.parziale = o.risultato || {}; throw new Error(o.errore || "non riuscito"); }
      statoRadio(cosa + ": " + (o.passo || "in corso") + " (" + Math.round((Date.now() - inizio) / 1000) + " s)");
      if (Date.now() - inizio > 12 * 60 * 1000) throw new Error("il gateway non ha finito in 12 minuti");
    }
  } catch (e) {
    statoRadio(cosa + " non riuscito: " + frase(e.message), "errore");
    return null;
  } finally {
    a.occupata = false;
    if (radioAperta === a) document.getElementById("radio-dentro").querySelectorAll("button").forEach(b => { b.disabled = false; });
  }
}

function controlloCampo(c, valore, prefisso) {
  // un controllo per un campo dello schema; .leggi() rida' il valore nel formato del protocollo
  const id = "rc-" + prefisso.replace(/\W/g, "-");
  const etichetta = CAMPI_IT[prefisso] || c.nome;
  if (c.tipo === "gruppo" && !c.ripetuto) {
    const fs = el("fieldset");
    fs.append(el("legend", null, etichetta));
    const figli = c.campi.map(f => controlloCampo(f, (valore || {})[f.nome], prefisso + "." + f.nome));
    figli.forEach(f => fs.append(...f.nodi));
    return {nodi: [fs], leggi: () => Object.fromEntries(c.campi.map((f, i) => [f.nome, figli[i].leggi()]))};
  }
  let i;
  if (c.ripetuto || c.tipo === "gruppo") {
    i = el("textarea");
    const righe = Array.isArray(valore) ? valore.map(x => typeof x === "object" ? JSON.stringify(x) : String(x)) : [];
    i.value = righe.join("\n");
    i.rows = Math.max(2, righe.length + 1);
    i.spellcheck = false;
    i.id = id;
    const l = el("label", null, etichetta);
    l.append(el("small", null, "uno per riga" + (CAMPI_IT[prefisso] ? " (" + c.nome + ")" : "")));
    l.htmlFor = id;
    return {nodi: [l, i], leggi: () => i.value.split("\n").map(x => x.trim()).filter(Boolean).map(x => {
      if (c.tipo === "gruppo") return JSON.parse(x);
      if (c.tipo === "intero" || c.tipo === "decimale") return Number(x);
      return x;
    })};
  }
  if (c.tipo === "si_no") {
    i = el("input"); i.type = "checkbox"; i.checked = !!valore; i.id = id;
    const l = el("label", "si-no");
    l.append(i, document.createTextNode(etichetta));
    if (CAMPI_IT[prefisso]) l.append(el("small", null, c.nome));
    return {nodi: [l], leggi: () => i.checked};
  }
  if (c.tipo === "scelta") {
    i = el("select");
    c.opzioni.forEach(o => { const op = el("option", null, (c.nome === "role" && RUOLI[o]) ? o + " (" + RUOLI[o] + ")" : o); op.value = o; i.append(op); });
    i.value = valore === undefined || valore === null ? c.opzioni[0] : String(valore);
  } else {
    i = el("input");
    i.autocomplete = "off"; i.spellcheck = false;
    if ((c.tipo === "intero" || c.tipo === "decimale") && typeof valore !== "string") { i.type = "number"; i.step = c.tipo === "decimale" ? "any" : "1"; }
    i.value = valore === undefined || valore === null ? "" : String(valore);
  }
  i.id = id;
  const l = el("label", null, etichetta);
  if (CAMPI_IT[prefisso]) l.append(el("small", null, c.nome));
  l.htmlFor = id;
  return {nodi: [l, i], leggi: () => {
    if (c.tipo === "intero" || c.tipo === "decimale") return i.type === "number" ? Number(i.value || 0) : i.value.trim();
    return i.value;
  }};
}

function moduloSezione(sez, valori) {
  const f = el("form");
  const controlli = sez.campi.map(c => controlloCampo(c, valori[c.nome], sez.nome + "." + c.nome));
  controlli.forEach(c => f.append(...c.nodi));
  if (sez.sola_lettura) {
    f.querySelectorAll("input, select, textarea").forEach(x => { x.disabled = true; });
    f.append(el("p", "nota", "Da lontano la sicurezza si può solo leggere: si cambia da vicino, con l'app Meshtastic."));
    return f;
  }
  const salva = el("button", "bottone piccolo", "Salva sulla radio");
  salva.type = "submit";
  f.append(el("p"), salva);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    let nuovi;
    try { nuovi = Object.fromEntries(sez.campi.map((c, i) => [c.nome, controlli[i].leggi()])); }
    catch (e) { statoRadio("Un valore non è scritto bene: " + e.message, "errore"); return; }
    const rischio = RISCHI[sez.nome];
    if (!window.confirm((rischio ? rischio + "\n\n" : "") + "Salvo \"" + (SEZIONI_IT[sez.nome] || sez.nome) + "\" sulla radio " +
                        (radioAperta.v.nome || radioAperta.v.id) + "? Dopo si riavvia da sola.")) return;
    await operazioneRadio({azione: "scrivi", sezione: sez.nome, valori: nuovi}, "Salvataggio");
  });
  return f;
}

function moduloCanale(c, canaliPonte) {
  const f = el("form", "canale");
  const p = "rk-" + c.indice;
  const nome = el("input"); nome.id = p + "-nome"; nome.maxLength = 11; nome.value = c.nome || ""; nome.autocomplete = "off";
  const ruolo = el("select"); ruolo.id = p + "-ruolo";
  (c.indice === 0 ? ["PRIMARY"] : ["SECONDARY", "DISABLED"]).forEach(r => {
    const o = el("option", null, {PRIMARY: "primario", SECONDARY: "secondario", DISABLED: "spento"}[r]); o.value = r; ruolo.append(o);
  });
  ruolo.value = c.ruolo === "DISABLED" && c.indice ? "DISABLED" : (c.indice ? "SECONDARY" : "PRIMARY");
  const chiave = el("select"); chiave.id = p + "-chiave";
  const descr = c.chiave.tipo === "privata" ? "privata" + (c.chiave.come_ponte ? ", uguale a quella del canale " + c.chiave.come_ponte + " del gateway" : ", diversa da quelle del gateway")
              : (c.chiave.tipo === "predefinita" ? "quella pubblica nota a tutti" : "nessuna (in chiaro)");
  const scelte = [];
  if (c.ruolo !== "DISABLED" || c.indice === 0) scelte.push(["tieni", "Lascia la chiave com'è (" + descr + ")"]);
  canaliPonte.filter(x => x.privato).forEach(x => scelte.push(["ponte:" + x.indice, "Stessa chiave del canale " + x.nome + " del gateway"]));
  scelte.push(["predefinita", "Chiave pubblica nota a tutti (come LongFast)"], ["nuova", "Un'altra chiave, la scrivo io"]);
  scelte.forEach(([v, t]) => { const o = el("option", null, t); o.value = v; chiave.append(o); });
  const nuova = el("input"); nuova.id = p + "-nuova"; nuova.type = "password"; nuova.autocomplete = "off"; nuova.hidden = true;
  nuova.placeholder = "chiave in base64 (16 o 32 byte)";
  chiave.addEventListener("change", () => { nuova.hidden = chiave.value !== "nuova"; });
  const precisione = el("input"); precisione.id = p + "-prec"; precisione.type = "number"; precisione.min = 0; precisione.max = 32;
  precisione.value = c.precisione || 0;
  const salva = el("button", "bottone piccolo", "Salva il canale " + c.indice);
  salva.type = "submit";
  f.append(el("h4", null, "Canale " + c.indice + (c.nome ? ": " + c.nome : (c.ruolo === "DISABLED" ? " (spento)" : ""))),
           ...campo("Nome", nome), ...campo("Stato", ruolo), ...campo("Chiave", chiave), nuova,
           ...campo("Precisione della posizione su questo canale", precisione, "0 = non la manda, 32 = esatta, 13 = circa 3 km"),
           el("p"), salva);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    const modo = chiave.value.startsWith("ponte:") ? {modo: "ponte", canale_ponte: Number(chiave.value.slice(6))}
               : (chiave.value === "nuova" ? {modo: "nuova", valore: nuova.value.trim()} : {modo: chiave.value});
    if (!window.confirm("Se sbagli nome o chiave del canale della squadra questa radio esce dalla rete e da lontano non la recuperi.\n\n" +
                        "Salvo il canale " + c.indice + " sulla radio " + (radioAperta.v.nome || radioAperta.v.id) + "?")) return;
    const o = await operazioneRadio({azione: "canale", indice: c.indice, ruolo: ruolo.value, nome: nome.value.trim(), chiave: modo,
                                     precisione: Number(precisione.value || 0), uplink: !!c.uplink, downlink: !!c.downlink},
                                    "Canale " + c.indice);
    nuova.value = "";
    return o;
  });
  return f;
}

function apriGestioneRadio(v) {
  const adatti = pontiCheGestiscono(v);
  if (!adatti.length) return;
  const dentro = document.getElementById("radio-dentro");
  const a = radioAperta = {v, ponte: adatti[0].ponte, occupata: false};
  const titolo = el("h2", null, v.nome || "Radio " + v.id);
  titolo.id = "radio-titolo";
  const chi = el("p", "da", [v.nome_breve, v.id, nomeModello(v.modello)].filter(Boolean).join(", ") + ". Sentita " + fa(v.eta) + ".");

  // da quale ponte: ogni comando parte dal nodo di quel ponte e arriva alla radio via radio
  const scelta = el("select"); scelta.id = "radio-ponte";
  adatti.forEach(x => {
    const o = el("option", null, x.ponte + (x.salti === 0 ? " (la sente direttamente)" : (x.salti > 0 ? " (passando da " + x.salti + " radio)" : "")));
    o.value = x.ponte; scelta.append(o);
  });
  const chiaveAdmin = el("div", "comando chiave-invito");
  const spiegaChiave = el("div");
  function mostraChiave() {
    a.ponte = scelta.value;
    const d = (stati.get(a.ponte) || {}).dati || {};
    const copia = el("button", "bottone secondario piccolo", "Copia");
    copia.type = "button";
    copia.addEventListener("click", () => copiaTesto(copia, d.chiave_pubblica || ""));
    chiaveAdmin.replaceChildren(el("code", null, d.chiave_pubblica || "il gateway non l'ha ancora letta dal suo nodo: riprova tra un minuto"), copia);
  }
  scelta.addEventListener("change", mostraChiave);
  mostraChiave();
  spiegaChiave.append(el("p", null, "La radio accetta comandi solo da chi ha messo tra le sue \"chiavi amministratore\". Va fatto una volta, da vicino, " +
    "con il telefono: app Meshtastic collegata a quella radio, Impostazioni, Sicurezza, Chiave amministratore: incolla la chiave qui sotto " +
    "(è la chiave pubblica del nodo del gateway) e salva. Serve il firmware 2.5 o successivo. Ogni gateway ha la sua: una radio ne tiene fino a tre."), chiaveAdmin);

  const stato = el("p", "stato-op", "Ogni operazione è un messaggio radio che va e torna: può volerci da qualche secondo a più di un minuto.");
  stato.id = "radio-stato";
  stato.setAttribute("aria-live", "polite");

  // nome, posizione fissa, riavvio
  const zonaNome = el("div");
  const leggiBase = el("button", "bottone piccolo", "Leggi dalla radio");
  leggiBase.type = "button";
  const fNome = el("form");
  const lungo = el("input"); lungo.id = "radio-nome"; lungo.maxLength = 39; lungo.required = true; lungo.value = v.nome || ""; lungo.autocomplete = "off";
  const corto = el("input"); corto.id = "radio-sigla"; corto.maxLength = 4; corto.required = true; corto.value = v.nome_breve || ""; corto.autocomplete = "off";
  const salvaNome = el("button", "bottone piccolo", "Salva il nome");
  salvaNome.type = "submit";
  fNome.append(...campo("Nome", lungo), ...campo("Sigla", corto, "al massimo 4 caratteri"), el("p"), salvaNome);
  fNome.addEventListener("submit", async ev => {
    ev.preventDefault();
    await operazioneRadio({azione: "proprietario", nome: lungo.value.trim(), nome_breve: corto.value.trim()}, "Nome");
  });
  const info = el("p", "da");
  leggiBase.addEventListener("click", async () => {
    const o = await operazioneRadio({azione: "leggi", sezioni: ["proprietario", "info"]}, "Lettura");
    const r = (o && o.risultato) || a.parziale || {};
    if (r.proprietario) { lungo.value = r.proprietario.nome; corto.value = r.proprietario.nome_breve; }
    if (r.info) info.textContent = "Firmware " + r.info.firmware + ", " + nomeModello(r.info.modello) + ", ruolo " + (RUOLI[r.info.ruolo] || r.info.ruolo) + ".";
  });
  zonaNome.append(el("h3", null, "Nome"), leggiBase, info, fNome);

  const fPos = el("form");
  const coord = el("input"); coord.id = "radio-coord"; coord.autocomplete = "off"; coord.placeholder = "es. 45.8700, 8.7700";
  const quota = el("input"); quota.id = "radio-quota"; quota.type = "number"; quota.placeholder = "metri";
  const metti = el("button", "bottone piccolo", "Imposta la posizione fissa");
  metti.type = "submit";
  const togliPos = el("button", "bottone secondario piccolo", "Torna al GPS");
  togliPos.type = "button";
  const rigaPos = el("div", "riga-pulsanti");
  rigaPos.append(metti, togliPos);
  fPos.append(...campo("Coordinate", coord, "copiate da Google Maps"), ...campo("Quota", quota), rigaPos);
  fPos.addEventListener("submit", async ev => {
    ev.preventDefault();
    const m = coord.value.trim().split(/[\s,;]+/).filter(Boolean).map(Number);
    if (m.length < 2 || m.some(isNaN)) { statoRadio("Coordinate: latitudine e longitudine separate da una virgola.", "errore"); return; }
    await operazioneRadio({azione: "posizione_fissa", lat: m[0], lon: m[1], quota: Number(quota.value || 0)}, "Posizione fissa");
  });
  togliPos.addEventListener("click", () => operazioneRadio({azione: "togli_posizione_fissa"}, "Posizione fissa"));
  const zonaPos = el("div");
  zonaPos.append(el("h3", null, "Posizione fissa"), el("p", "da", "Per ripetitori e radio senza GPS. Gli intervalli di invio stanno in \"Posizione e GPS\", più sotto."), fPos);

  // tutte le impostazioni, una sezione alla volta
  const zonaSez = el("div");
  const sceltaSez = el("select"); sceltaSez.id = "radio-sezione";
  const leggiSez = el("button", "bottone piccolo", "Leggi dalla radio");
  leggiSez.type = "button";
  const rigaSez = el("div", "riga-pulsanti");
  rigaSez.append(sceltaSez, leggiSez);
  const zonaModuloSez = el("div");
  zonaSez.append(el("h3", null, "Impostazioni"), el("p", "da", "Le stesse dell'app Meshtastic, con i nomi originali accanto. Prima si leggono dalla radio, poi si cambiano."), rigaSez, zonaModuloSez);
  leggiSez.addEventListener("click", async () => {
    const sez = schemaRadio.sezioni.find(s => s.nome === sceltaSez.value);
    const o = await operazioneRadio({azione: "leggi", sezioni: [sez.nome]}, "Lettura");
    if (o && o.risultato && o.risultato[sez.nome]) zonaModuloSez.replaceChildren(moduloSezione(sez, o.risultato[sez.nome]));
  });

  // canali
  const zonaCanali = el("div");
  const leggiCanali = el("button", "bottone piccolo", "Leggi i canali dalla radio");
  leggiCanali.type = "button";
  const elencoCanali = el("div");
  zonaCanali.append(el("h3", null, "Canali"), el("p", "da", "Le chiavi non escono mai dal gateway: qui vedi solo se sono uguali a quelle del gateway."), leggiCanali, elencoCanali);
  leggiCanali.addEventListener("click", async () => {
    const o = await operazioneRadio({azione: "leggi", sezioni: ["canali"]}, "Lettura dei canali");
    if (!o || !o.risultato || !o.risultato.canali) return;
    const canaliPonte = (((stati.get(a.ponte) || {}).dati || {}).nodo || {}).canali || [];
    const letti = o.risultato.canali;
    const moduli = letti.map(c => moduloCanale(c, canaliPonte));
    if (letti.length < 8) moduli.push(moduloCanale({indice: letti.length, ruolo: "DISABLED", nome: "", chiave: {tipo: "nessuna"}, precisione: 0}, canaliPonte));
    elencoCanali.replaceChildren(...moduli);
  });

  const zonaRiavvia = el("div");
  const riavvia = el("button", "bottone secondario piccolo", "Riavvia la radio");
  riavvia.type = "button";
  riavvia.addEventListener("click", () => { if (window.confirm("Riavvio la radio " + (v.nome || v.id) + "?")) operazioneRadio({azione: "riavvia"}, "Riavvio"); });
  zonaRiavvia.append(el("h3", null, "Riavvio"), riavvia);

  const chiudi = el("button", "bottone secondario", "Chiudi");
  chiudi.type = "button";
  chiudi.addEventListener("click", () => document.getElementById("radio").close());
  const fondo = el("div", "pulsanti");
  fondo.append(chiudi);
  dentro.replaceChildren(titolo, chi, ...campo("Attraverso il gateway", scelta),
                         dettagli("radio-chiave", "La prima volta: la chiave di amministrazione da mettere sulla radio", spiegaChiave),
                         zonaNome, zonaPos, zonaSez, zonaCanali, zonaRiavvia, stato, fondo);
  document.getElementById("radio").showModal();
  (async () => {
    try {
      if (!schemaRadio) schemaRadio = await chiamaRadio(a.ponte, "/schema");
      schemaRadio.sezioni.forEach(s => { const o = el("option", null, SEZIONI_IT[s.nome] || s.nome); o.value = s.nome; sceltaSez.append(o); });
    } catch (e) {
      statoRadio("Non riesco a parlare con il gateway " + a.ponte + ": " + frase(e.message), "errore");
    }
  })();
}

document.getElementById("radio").addEventListener("close", () => { radioAperta = null; });

let riassuntoRadio = null, avvisiRadio = null, cercaRadio = null, testaVoci = null, elencoRadio = null;
let cacheRete = null;              // radioDellaRete() fino al prossimo disegno: gli stati non cambiano nel frattempo
let elencoRete = null, costruita = false;
const gestioneDettagli = {};
const LIVELLI = {ok: 0, attenzione: 1, problema: 2};
const NOMI_LIVELLI = ["ok", "attenzione", "problema"];
// Un ripetitore con ruolo Router manda il nome ogni 3 ore, posizione e batteria ogni 12 (le impostazioni di fabbrica):
// se nessuno trasmette, non ha niente da ripetere e lo si sente solo cosi'
const RIPETITORE_ATTENZIONE_S = 6 * 3600;
const RIPETITORE_SPENTO_S = 12 * 3600;

function radioDellaRete() {
  // le radio sentite da tutti i gateway, unite: i dati piu' recenti vincono. Le eta' si calcolano con
  // l'orologio di ogni gateway (d.ora), cosi' un orologio sbagliato non confonde nulla.
  if (cacheRete) return cacheRete;
  const tutte = new Map(), ponteDi = new Map(), vecchi = [], canali = [];
  let raggiunti = 0;
  for (const p of elenco) {
    const st = stati.get(p.nome);
    const d = st && st.dati;
    if (!d) continue;
    raggiunti++;
    if (d.id_nodo) ponteDi.set(d.id_nodo, p.nome);
    if (d.canale_radio && d.canale_radio.occupato !== null && d.canale_radio.occupato !== undefined) {
      canali.push({ponte: p.nome, occupato: d.canale_radio.occupato, trasmesso: d.canale_radio.trasmesso,
                   eta: d.canale_radio.quando ? Math.max(0, d.ora - d.canale_radio.quando) : null});
    }
    if (!Array.isArray(d.radio_rete)) { vecchi.push(p.nome); continue; }
    for (const r of d.radio_rete) {
      if (!r || typeof r.id !== "string") continue;
      const eta = Math.max(0, (d.ora || 0) - (r.ultimo || 0));
      let v = tutte.get(r.id);
      if (!v) {
        v = {id: r.id, eta: Infinity, etaMetriche: Infinity, etaRipetuto: Infinity, ripetuti: 0, fisso: false,
             posizione_fissa: false, ponti: []};
        tutte.set(r.id, v);
      }
      const piuRecente = eta < v.eta;
      if (piuRecente) v.eta = eta;
      for (const k of ["nome", "nome_breve", "modello", "ruolo"]) if (r[k] && (piuRecente || !v[k])) v[k] = r[k];
      if (r.metriche) {
        const em = Math.max(0, (d.ora || 0) - r.metriche);
        if (em < v.etaMetriche) {
          v.etaMetriche = em;
          v.batteria = r.batteria; v.tensione = r.tensione; v.occupato = r.occupato; v.trasmesso = r.trasmesso;
          v.acceso = (r.acceso_s === null || r.acceso_s === undefined) ? null : r.acceso_s + em;
        }
      }
      // dalla 1.15.0: nodi fissi (ripetitori) e messaggi degli altri che hanno ripetuto verso il gateway
      if (r.fisso) v.fisso = true;
      if (r.posizione_fissa) v.posizione_fissa = true;
      if (Number.isFinite(r.ripetuti)) v.ripetuti += r.ripetuti;
      if (r.ultimo_ripetuto) v.etaRipetuto = Math.min(v.etaRipetuto, Math.max(0, (d.ora || 0) - r.ultimo_ripetuto));
      v.ponti.push({ponte: p.nome, eta, salti: r.salti, snr: r.snr, rssi: r.rssi});
    }
  }
  for (const v of tutte.values()) {
    v.ponte = ponteDi.get(v.id) || "";
    v.ponti.sort((a, b) => a.eta - b.eta);
  }
  return (cacheRete = {radio: [...tutte.values()], vecchi, canali, raggiunti});
}

function eRipetitore(v) {
  // un nodo fisso che ripete gli altri (ruolo da ripetitore o posizione impostata a mano), che non e' un gateway
  return !!v.fisso && !v.ponte && v.ruolo !== "CLIENT_MUTE" && v.ruolo !== "CLIENT_HIDDEN";
}

function ripetitoriDellaRete() {
  return radioDellaRete().radio.filter(eRipetitore)
    .sort((a, b) => nomeRipetitore(a).localeCompare(nomeRipetitore(b), "it"));
}

function radioDellaSquadra() {
  return radioDellaRete().radio.filter(v => !eRipetitore(v) && !v.ponte);
}

function nomeRipetitore(v) {
  return v.nome || "Ripetitore " + v.id;
}

function idRipetitore(id) {
  return "ripetitore-" + String(id).replace(/^!/, "");
}

function problemi(v) {
  const p = [];
  if (v.eta >= SILENZIO_S) return p;          // spenta: la batteria di ore fa non e' un allarme
  if (v.batteria !== null && v.batteria !== undefined && v.batteria <= 20 && v.etaMetriche < 6 * 3600) {
    p.push(["batteria quasi scarica", ""]);
  }
  if (v.trasmesso !== null && v.trasmesso !== undefined && v.trasmesso >= 5 && v.etaMetriche < 3 * 3600) {
    p.push(["trasmette troppo: " + numIt(v.trasmesso) + "% del tempo", "lieve"]);
  }
  return p;
}

