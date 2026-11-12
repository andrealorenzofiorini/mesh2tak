// ---- Gestisci il Raspberry (dalla 1.13.0): il server mette il comando nella registrazione del ponte e lo avvisa dal
// tunnel; il Raspberry lo fa da se' (da root) e scrive com'e' andata nel suo stato, che questa pagina legge
const NOMI_COMANDI = {"riavvia-programma": "Riavvio del programma", "riavvia-radio": "Riavvio della radio",
                      "riavvia-raspberry": "Riavvio del Raspberry", registro: "Registro", impostazioni: "Impostazioni",
                      certificato: "Certificato nuovo", "reimposta-radio": "Radio come all'installazione"};
const STATI_COMANDI = {in_corso: "in corso", fatto: "fatto", errore: "non riuscito", scaduto: "scaduto"};
const SQUADRE_IT = [["White", "Bianco"], ["Yellow", "Giallo"], ["Orange", "Arancione"], ["Magenta", "Magenta"],
                    ["Red", "Rosso"], ["Maroon", "Bordeaux"], ["Purple", "Viola"], ["Dark Blue", "Blu scuro"],
                    ["Blue", "Blu"], ["Cyan", "Azzurro"], ["Teal", "Verde acqua"], ["Green", "Verde"],
                    ["Dark Green", "Verde scuro"], ["Brown", "Marrone"]];
let raspberryAperto = null;          // {p, occupato}

function datiPonte(nome) {
  return (stati.get(nome) || {}).dati || null;
}

function statoRaspberry(testo, esito) {
  const s = document.getElementById("raspberry-stato");
  if (!s) return;
  s.textContent = testo;
  s.dataset.esito = esito || "";
}

function disegnaComandi() {
  const a = raspberryAperto;
  const z = document.getElementById("raspberry-comandi");
  if (!a || !z) return;
  const d = datiPonte(a.p.nome);
  if (!d) return;                    // ponte che riparte: resta quello che si sapeva
  const azioni = ((d.sistema && d.sistema.azioni) || []).slice().reverse();
  if (!azioni.length) { z.replaceChildren(el("p", "nota", "Nessun comando ancora.")); return; }
  const ul = el("ul", "comandi");
  azioni.forEach(x => {
    const li = el("li");
    li.dataset.stato = x.stato;
    li.append(el("b", null, (NOMI_COMANDI[x.azione] || x.azione) + ": " + (STATI_COMANDI[x.stato] || x.stato)),
              el("small", null, (x.messaggio ? maiuscola(frase(x.messaggio)) + " " : "") +
                                (x.quando ? maiuscola(giornoOra(x.quando)) + "." : "")));
    ul.append(li);
  });
  z.replaceChildren(ul);
}

async function seguiComando(a, id, azione, cosa, limiteS) {
  const inizio = Date.now();
  let notizia = "";
  for (;;) {
    await new Promise(ok => setTimeout(ok, 2500));
    if (raspberryAperto !== a) return null;          // finestra chiusa: il Raspberry il comando lo fa lo stesso
    await aggiornaGestione();
    const lv = gestione && (gestione.lavori || []).find(l => l.id === id);
    if (lv && lv.esito === "errore") throw new Error(lv.messaggio || "il server non ha passato il comando");
    if (lv && lv.esito === "ok" && lv.messaggio) notizia = lv.messaggio;
    await aggiornaPonte(a.p);
    const d = datiPonte(a.p.nome);
    const x = d && d.sistema && (d.sistema.azioni || []).find(z => z.id === id);
    disegnaComandi();
    if (x && x.stato !== "in_corso") {
      if (x.stato === "fatto") return x;
      throw new Error(x.messaggio || STATI_COMANDI[x.stato] || "non riuscito");
    }
    let dove;
    if (x) dove = x.messaggio || "in corso";
    else if (!d) dove = azione === "riavvia-raspberry" ? "il Raspberry si sta riavviando" :
                        (azione === "registro" ? "il gateway non risponde adesso" : "il gateway riparte");
    else dove = notizia ? notizia.replace(/^chiesto al ponte: /, "") : "lo passo al Raspberry";
    statoRaspberry(cosa + ": " + dove + " (" + durata((Date.now() - inizio) / 1000) + ")");
    if (Date.now() - inizio > limiteS * 1000) {
      throw new Error("nessuna notizia dal Raspberry in " + durata(limiteS) + ": guarda più tardi negli ultimi comandi");
    }
  }
}

async function comandoRaspberry(azione, cosa, chiamata, limiteS) {
  const a = raspberryAperto;
  if (!a) return null;
  if (a.occupato) { statoRaspberry("Aspetta che finisca il comando in corso.", "errore"); return null; }
  a.occupato = true;
  const dentro = document.getElementById("raspberry-dentro");
  const blocca = si => dentro.querySelectorAll("button, input, select").forEach(b => { if (!b.classList.contains("resta")) b.disabled = si; });
  blocca(true);
  statoRaspberry(cosa + ": lo mando al server...");
  try {
    const r = await chiamata();
    statoRaspberry(cosa + ": " + (r.messaggio || "chiesto"));
    const x = await seguiComando(a, r.lavoro, azione, cosa, limiteS);
    // fatto: il messaggio del Raspberry dice gia' cosa (es. "impostazioni salvate: squadra = Red")
    if (x) statoRaspberry(x.messaggio ? maiuscola(frase(x.messaggio)) : cosa + ": fatto.", "fatto");
    return x;
  } catch (e) {
    if (raspberryAperto === a) statoRaspberry(cosa + " non riuscito: " + frase(e.message), "errore");
    return null;
  } finally {
    a.occupato = false;
    if (raspberryAperto === a) blocca(false);
    disegnaComandi();
    disegna();
  }
}

function comandoSemplice(azione, cosa, domanda, limiteS) {
  const b = el("button", "bottone secondario piccolo", cosa);
  b.type = "button";
  b.addEventListener("click", () => {
    const a = raspberryAperto;
    if (!a || (domanda && !window.confirm(domanda))) return;
    comandoRaspberry(azione, NOMI_COMANDI[azione], () => manda("gestisci-ponte", {nome: a.p.nome, azione}), limiteS);
  });
  return b;
}

async function leggiRegistroPonte(nome) {
  const r = await fetch("registro/" + encodeURIComponent(nome) + ".txt", {cache: "no-store", redirect: "manual",
                                                                          credentials: "same-origin"});
  if (r.type === "opaqueredirect") { document.getElementById("sessione").hidden = false; throw new Error("sessione scaduta: ricarica la pagina"); }
  if (!r.ok) {
    await r.text().catch(() => "");
    throw new Error(r.status === 502 ? "il gateway " + nome + " non risponde" : "il gateway risponde " + r.status);
  }
  return r.text();
}

function moduloImpostazioniPonte(a, d) {
  const imp = d.impostazioni || {};
  const f = el("form");
  const squadra = el("select");
  squadra.id = "rp-squadra";
  SQUADRE_IT.forEach(([v, t]) => { const o = el("option", null, t + (t !== v ? " (" + v + ")" : "")); o.value = v; squadra.append(o); });
  squadra.value = imp.squadra || "Cyan";
  const numero = (id, valore, minimo, massimo) => {
    const i = el("input");
    Object.assign(i, {id, type: "number", min: minimo, max: massimo, step: "any", required: true});
    i.value = valore === undefined || valore === null ? "" : String(valore);
    return i;
  };
  const casella = (id, testo, valore, nota) => {
    const i = el("input");
    Object.assign(i, {id, type: "checkbox", checked: !!valore});
    const l = el("label", "si-no");
    l.append(i, document.createTextNode(testo));
    if (nota) l.append(el("small", null, nota));
    return {l, i};
  };
  const validita = numero("rp-validita", imp.validita_min, 5, 1440);
  const ripeti = numero("rp-ripeti", imp.aggiornamento_min, 0.5, 60);
  const errore = numero("rp-errore", imp.errore_massimo_m, 10, 100000);
  const tempo = numero("rp-tempo", imp.tempo_radio_percento, 0.5, 10);
  const pausa = numero("rp-pausa", imp.pausa_massima_min, 1, 240);
  const ponte = casella("rp-ponte", "Mostra il Raspberry sulla mappa", imp.includi_ponte);
  const invio = casella("rp-invio", "Manda alle radio chat, segnaposti e allarmi che arrivano dal TAK Server", imp.invio,
                        "Serve un certificato con lettura e scrittura sul canale della squadra.");
  const salva = el("button", "bottone piccolo", "Salva le impostazioni");
  salva.type = "submit";
  f.append(...campo("Colore delle radio sulla mappa", squadra, "il colore della squadra su ATAK e TAK Aware"),
           ...campo("Minuti sulla mappa dopo l'ultimo segnale", validita, "da 5 a 1440"),
           ...campo("Ripeti al TAK Server le posizioni ancora valide ogni (minuti)", ripeti,
                    "per chi apre l'app dopo: non usa la radio"),
           ...campo("Scarta le posizioni approssimate oltre (metri)", errore),
           ponte.l, invio.l,
           ...campo("Tempo radio massimo per gli invii alle radio (%)", tempo,
                    "la legge permette al massimo il 10%: il resto serve alla squadra"),
           ...campo("Pausa massima del gateway quando si gestisce la radio dal browser (minuti)", pausa),
           el("p"), salva);
  f.addEventListener("submit", ev => {
    ev.preventDefault();
    const nuovi = {squadra: squadra.value, validita_min: Number(validita.value), aggiornamento_min: Number(ripeti.value),
                   errore_massimo_m: Number(errore.value), includi_ponte: ponte.i.checked, invio: invio.i.checked,
                   tempo_radio_percento: Number(tempo.value), pausa_massima_min: Number(pausa.value)};
    const cambiati = Object.fromEntries(Object.entries(nuovi).filter(([k, v]) => v !== imp[k]));
    if (!Object.keys(cambiati).length) { statoRaspberry("Niente da salvare: le impostazioni sono già queste.", "fatto"); return; }
    if (!window.confirm("Il programma del gateway riparte con le impostazioni nuove: circa mezzo minuto. Se non riparte, " +
                        "il Raspberry rimette da solo quelle di prima.\n\nSalvo?")) return;
    comandoRaspberry("impostazioni", "Impostazioni",
                     () => manda("gestisci-ponte", {nome: a.p.nome, azione: "impostazioni", valori: cambiati}), 7 * 60);
  });
  return f;
}

function moduloCertificatoPonte(a, d) {
  const f = el("form");
  const c = d.certificato;
  const adesso = c && c.scade ? "Adesso: " + (c.nome ? "certificato " + c.nome + ", " : "") +
    (c.scade <= d.ora ? "scaduto il " : "valido fino al ") + giorno(c.scade) + "." : "Adesso: certificato non letto.";
  const file = el("input");
  Object.assign(file, {id: "rp-pacchetto", type: "file", accept: ".zip,.p12,.pfx", required: true});
  const password = el("input");
  Object.assign(password, {id: "rp-password", type: "password", autocomplete: "off", placeholder: "vuota = atakatak"});
  const salva = el("button", "bottone piccolo", "Sostituisci il certificato");
  salva.type = "submit";
  f.append(el("p", "spiegazione", adesso),
           ...campo("Pacchetto del gateway", file, "il .zip scaricato dalla console infra-TAK per questo gateway, o il suo .p12"),
           ...campo("Password del certificato", password, "quella mostrata nella console infra-TAK"), el("p"), salva);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    const scelto = file.files && file.files[0];
    if (!scelto) return;
    if (scelto.size > 256 * 1024) { statoRaspberry("Il file è troppo grande: serve il pacchetto della console infra-TAK.", "errore"); return; }
    if (!window.confirm("Il Raspberry installa il certificato nuovo come fa l'installatore: il gateway si ferma per qualche " +
                        "minuto. Se il pacchetto o la password non vanno, resta il certificato di prima.\n\nProcedo?")) return;
    let pacchetto;
    try { pacchetto = await fileInBase64(scelto); } catch (e) { statoRaspberry(maiuscola(frase(e.message)), "errore"); return; }
    const x = await comandoRaspberry("certificato", "Certificato nuovo", () => manda("certificato-ponte", {
      nome: a.p.nome, nome_pacchetto: scelto.name, pacchetto, password: password.value}), 25 * 60);
    if (x) { file.value = ""; password.value = ""; }
  });
  return f;
}

function apriRaspberry(p) {
  const d = datiPonte(p.nome);
  if (!d) return;
  const a = raspberryAperto = {p, occupato: false};
  const dentro = document.getElementById("raspberry-dentro");
  const titolo = el("h2", null, "Raspberry del gateway " + p.nome);
  titolo.id = "raspberry-titolo";
  const chi = el("p", "da", [d.nome, "mesh2tak " + (d.versione || "?"),
                             d.sistema && d.sistema.acceso_dal ? "acceso da " + durata(d.ora - d.sistema.acceso_dal) : ""]
                            .filter(Boolean).join(", ") + ".");
  const spiega = el("p", "spiegazione", "I comandi li fa il Raspberry stesso: partono entro pochi secondi. Qui sotto vedi " +
                                        "a che punto sono e com'è andata.");

  const riavvii = el("div", "riga-pulsanti");
  riavvii.append(comandoSemplice("riavvia-programma", "Riavvia il programma",
                                 "Il gateway si ferma per circa mezzo minuto e riparte. Riavvio il programma?", 5 * 60),
                 comandoSemplice("riavvia-radio", "Riavvia la radio",
                                 "La radio del gateway riparte; il gateway si ricollega da solo entro un minuto. Riavvio la radio?", 5 * 60),
                 comandoSemplice("riavvia-raspberry", "Riavvia il Raspberry",
                                 "Il Raspberry si spegne e si riaccende: il gateway torna in linea in 2-3 minuti. Riavvio il Raspberry?", 10 * 60));
  const zonaRiavvii = el("div");
  zonaRiavvii.append(el("h3", null, "Riavvia"), el("p", "spiegazione", "Se qualcosa non va prova prima il programma, " +
                     "poi la radio, poi tutto il Raspberry."), riavvii);

  const zonaRegistro = el("div");
  const leggi = el("button", "bottone secondario piccolo", "Leggi il registro");
  leggi.type = "button";
  const testoRegistro = el("div");
  leggi.addEventListener("click", async () => {
    const x = await comandoRaspberry("registro", "Registro", () => manda("gestisci-ponte", {nome: p.nome, azione: "registro"}), 3 * 60);
    if (!x || raspberryAperto !== a) return;
    try {
      const testo = await leggiRegistroPonte(p.nome);
      if (a.indirizzoRegistro) URL.revokeObjectURL(a.indirizzoRegistro);
      a.indirizzoRegistro = URL.createObjectURL(new Blob([testo], {type: "text/plain;charset=utf-8"}));
      const scarica = el("a", null, "Scarica il registro");
      scarica.href = a.indirizzoRegistro;
      scarica.download = "registro-" + p.nome + "-" + new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "") + ".txt";
      const riga = el("p");
      riga.append(scarica);
      testoRegistro.replaceChildren(el("pre", "registro", testo), riga);
    } catch (e) {
      statoRaspberry("Registro non letto: " + frase(e.message), "errore");
    }
  });
  zonaRegistro.append(el("h3", null, "Registro"), el("p", "spiegazione", "Le ultime righe del programma, della radio, " +
                      "degli aggiornamenti e dell'installazione. Chiavi e password sono tolte."), leggi, testoRegistro);

  const zonaImpostazioni = el("div");
  zonaImpostazioni.append(el("h3", null, "Impostazioni del gateway"), moduloImpostazioniPonte(a, d));

  const zonaCertificato = el("div");
  zonaCertificato.append(el("h3", null, "Certificato del TAK Server"), moduloCertificatoPonte(a, d));

  const zonaRadio = el("div");
  zonaRadio.append(el("h3", null, "Radio del gateway"),
                   el("p", "spiegazione", "Gli aggiornamenti lasciano la radio com'è. Questo la rimette come la prepara " +
                      "l'installatore: regione EU 868, nome e sigla dell'installatore, ruolo, solo il canale della squadra " +
                      "(la sua chiave resta) e posizione fissa. Il resto scelto dall'app non cambia."),
                   comandoSemplice("reimposta-radio", "Reimposta la radio",
                                   "Rimetto la radio del gateway come all'installazione? Il gateway si ferma per un paio di minuti; " +
                                   "eventuali canali secondari accesi dall'app si spengono.", 20 * 60));

  const zonaComandi = el("div");
  const elenco = el("div");
  elenco.id = "raspberry-comandi";
  zonaComandi.append(el("h3", null, "Ultimi comandi"), elenco);

  const stato = el("p", "stato-op", "Pronto.");
  stato.id = "raspberry-stato";
  stato.setAttribute("aria-live", "polite");
  const chiudi = el("button", "bottone secondario resta", "Chiudi");
  chiudi.type = "button";
  chiudi.addEventListener("click", () => document.getElementById("raspberry").close());
  const fondo = el("div", "pulsanti");
  fondo.append(chiudi);
  dentro.replaceChildren(titolo, chi, spiega, zonaRiavvii, zonaRegistro, zonaImpostazioni, zonaCertificato, zonaRadio,
                         zonaComandi, stato, fondo);
  disegnaComandi();
  document.getElementById("raspberry").showModal();
}

document.getElementById("raspberry").addEventListener("close", () => {
  if (raspberryAperto && raspberryAperto.indirizzoRegistro) URL.revokeObjectURL(raspberryAperto.indirizzoRegistro);
  raspberryAperto = null;
});

