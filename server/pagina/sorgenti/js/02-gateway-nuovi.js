// ---- ponti nuovi (dalla 1.10.0): certificato e canale una volta, poi un invito per ogni Raspberry ----

const SCHEDE_RADIO = [["waveshare", "Scheda Waveshare SX1262 (sui pin del Raspberry)"],
                      ["usb", "Radio Meshtastic collegata via USB"],
                      ["meshadv", "Scheda MeshAdv (sui pin)"], ["meshadv-mini", "Scheda MeshAdv Mini (sui pin)"],
                      ["rak", "Scheda RAK6421 con RAK13300 (sui pin)"]];
let zonaStatoNuovi = null, dettagliImpostazioni = null, zonaInviti = null, moduloInvito = null;

function nomeScheda(s) {
  const v = SCHEDE_RADIO.find(x => x[0] === s);
  return v ? v[1] : (s || "radio non scelta");
}

function sceltaScheda(id, predefinita) {
  const s = el("select");
  s.id = id;
  SCHEDE_RADIO.forEach(([valore, testo]) => { const o = el("option", null, testo); o.value = valore; s.append(o); });
  s.value = predefinita || "waveshare";
  return s;
}

function campo(etichetta, controllo, nota) {
  const l = el("label", null, etichetta);
  if (nota) l.append(el("small", null, nota));
  l.htmlFor = controllo.id;
  return [l, controllo];
}

async function copiaTesto(bottone, testo, dopo) {
  const prima = bottone.textContent;
  try { await navigator.clipboard.writeText(testo); bottone.textContent = dopo || "Copiato"; }
  catch (e) { bottone.textContent = "Non riesco a copiare: selezionalo a mano"; }
  setTimeout(() => { bottone.textContent = prima; }, 4000);
}

function fileInBase64(file) {
  return new Promise((ok, no) => {
    const lettore = new FileReader();
    lettore.onload = () => ok(String(lettore.result).split(",")[1] || "");
    lettore.onerror = () => no(new Error("non riesco a leggere il file"));
    lettore.readAsDataURL(file);
  });
}

function passiMicroSD(inv) {
  const elencoImager = (gestione && gestione.imager) || "";
  return [
    "Sul computer installa Raspberry Pi Imager (versione 2 o successiva) da https://www.raspberrypi.com/software/ e aprilo.",
    "Solo la prima volta: premi \"Opzioni app\" (in basso a sinistra), alla voce \"Repository dei contenuti\" premi \"Modifica\", scegli \"Usa URL personalizzato\", incolla questo indirizzo e premi \"Applica & Riavvia\":\n" + elencoImager,
    "Scegli il modello del Raspberry, poi il sistema \"Gateway Meshtastic (mesh2tak)\", poi la microSD.",
    "Personalizzazione. Hostname: ponte-" + inv.nome + ". Localizzazione: Europe/Rome, tastiera it. Utente: un nome e una password a scelta (servono solo per la manutenzione). Wi-Fi: nome e password della rete del posto dove starà il gateway (con il cavo di rete si salta).",
    "Accesso remoto: attiva \"Abilita SSH\", scegli \"Usa autenticazione con chiave pubblica\", incolla la chiave d'invito qui sotto e premi \"Add\". Senza questa chiave il Raspberry non sa a quale rete presentarsi.\n" + (inv.riga || ""),
    "Raspberry Pi Connect e Interfacce: vai avanti senza toccare nulla, poi scrivi la microSD.",
    "Metti la microSD nel Raspberry, collega la radio e l'antenna (mai accenderlo senza antenna) e l'alimentatore. Non serve altro: in 5-10 minuti compare sulla pagina della rete e si installa da solo in 30-60 minuti.",
  ];
}

function disegnaInvito(inv) {
  const li = el("li", "invito");
  li.append(el("h4", null, inv.nome));
  li.append(el("p", "da", nomeScheda(inv.scheda) + (inv.posizione ? ", posizione " + inv.posizione : ", senza posizione fissa") +
                          ". Preparato da " + (inv.da || "?") + " " + giornoOra(inv.creato) + "."));
  if (inv.stato !== "attesa") {
    const ins = gestione.installazioni && gestione.installazioni[inv.nome];
    li.append(el("p", "stato-invito", ins ? "Il Raspberry è arrivato: lo vedi tra i gateway qui sopra." :
                                            "Il Raspberry è arrivato " + giornoOra(inv.usato || inv.creato) + ": lo sto aggiungendo alla rete."));
    return li;
  }
  li.append(el("p", "stato-invito", "In attesa che il Raspberry si accenda. La chiave d'invito vale una volta sola, fino al " +
                                    new Date(inv.scade * 1000).toLocaleDateString("it-IT", {day: "numeric", month: "long"}) + "."));
  const passi = passiMicroSD(inv);
  const ol = el("ol");
  passi.forEach(t => {
    const [testo, daCopiare] = t.split("\n");
    const voce = el("li", null, testo);
    if (daCopiare) {
      const riga = el("div", "comando chiave-invito");
      const copia = el("button", "bottone secondario piccolo", "Copia");
      copia.type = "button";
      copia.addEventListener("click", () => copiaTesto(copia, daCopiare));
      riga.append(el("code", null, daCopiare), copia);
      voce.append(riga);
    }
    ol.append(voce);
  });
  li.append(dettagli("invito:" + inv.id, "Istruzioni per chi prepara la microSD", ol));
  const riga = el("div", "riga-pulsanti");
  const tutte = el("button", "bottone piccolo", "Copia le istruzioni da mandare");
  tutte.type = "button";
  tutte.addEventListener("click", () => copiaTesto(tutte, "Gateway Meshtastic \"" + inv.nome + "\": come preparare la microSD\n\n" +
    passi.map((t, i) => (i + 1) + ". " + t).join("\n\n") + "\n", "Copiate: incollale in un messaggio"));
  const annulla = el("button", "togli", "Annulla l'invito");
  annulla.type = "button";
  annulla.addEventListener("click", () => azioneRichiesta(annulla, "annulla-invito", {id: inv.id}));
  riga.append(tutte, annulla);
  li.append(riga);
  return li;
}

function costruisciNuovi() {
  const zona = el("div", "nuovi");
  zona.append(el("h3", null, "Raspberry nuovo, preparato con Raspberry Pi Imager"));
  zona.append(el("p", null, "Chi ha il Raspberry scrive la microSD con Raspberry Pi Imager incollando una chiave d'invito preparata qui, " +
    "collega la radio e lo accende. Il Raspberry si presenta da solo a questa pagina, riceve certificato e canale, si installa e " +
    "compare tra i gateway: nessun terminale, nessun file da copiare."));
  zonaStatoNuovi = el("p", "spiega");
  zona.append(zonaStatoNuovi);

  // certificato e canale: una volta
  const f = el("form");
  const file = el("input"); file.type = "file"; file.id = "nuovi-file"; file.accept = ".zip,.p12,.pfx"; file.required = true;
  const password = el("input"); password.type = "password"; password.id = "nuovi-password"; password.autocomplete = "off";
  password.placeholder = "atakatak, se non l'hai cambiata";
  const host = el("input"); host.id = "nuovi-host"; host.autocomplete = "off"; host.placeholder = "di solito non serve";
  const canale = el("input"); canale.id = "nuovi-canale"; canale.required = true; canale.maxLength = 11; canale.autocomplete = "off";
  canale.placeholder = "es. CNSAS-VA";
  const chiave = el("input"); chiave.type = "password"; chiave.id = "nuovi-chiave"; chiave.required = true; chiave.autocomplete = "off";
  chiave.placeholder = "44 caratteri, finisce con =";
  const invia = el("button", "bottone", "Controlla e salva");
  invia.type = "submit";
  const esito = el("p", "esito");
  esito.setAttribute("aria-live", "polite");
  f.append(...campo("Pacchetto del certificato", file, "il file .zip che la console infra-TAK fa scaricare per il certificato del gateway"),
           ...campo("Password del certificato", password),
           ...campo("Indirizzo del TAK Server", host, "solo se il pacchetto non lo contiene"),
           ...campo("Nome del canale della squadra", canale),
           ...campo("Chiave del canale della squadra", chiave, "dall'app Meshtastic: Canali, il canale della squadra, chiave"),
           el("p", "nota", "Restano sul server, leggibili solo da root. Da qui non si possono rileggere: si possono solo sostituire. " +
                           "A ogni Raspberry approvato arrivano cifrati, e solo a lui."),
           invia, esito);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    invia.disabled = true;
    esito.textContent = "Mando...";
    try {
      if (!file.files.length) throw new Error("scegli il pacchetto del certificato");
      if (file.files[0].size > 256 * 1024) throw new Error("il file è troppo grande: serve il pacchetto .zip della console infra-TAK");
      const d = await manda("impostazioni-nuovi", {
        nome_pacchetto: file.files[0].name, pacchetto: await fileInBase64(file.files[0]), password: password.value,
        host: host.value.trim(), canale: canale.value.trim(), chiave: chiave.value.trim()});
      esito.textContent = (d.messaggio || "Mandato.") + " L'esito compare in cima alla pagina tra qualche secondo.";
      f.reset();
      await giro();
    } catch (e) {
      esito.textContent = "Non accettato: " + e.message;
    } finally {
      invia.disabled = false;
    }
  });
  dettagliImpostazioni = el("details");
  dettagliImpostazioni.append(el("summary", null, "Certificato e canale per i gateway nuovi"), f);
  zona.append(dettagliImpostazioni);

  // un invito per ogni Raspberry
  zonaInviti = el("ul", "inviti");
  zona.append(zonaInviti);
  moduloInvito = el("form");
  const nome = el("input");
  nome.id = "invito-nome"; nome.required = true; nome.maxLength = 20; nome.pattern = "[a-z0-9]([a-z0-9\\-]{0,18}[a-z0-9])?";
  nome.placeholder = "es. velate"; nome.autocomplete = "off";
  nome.title = "minuscole, numeri e trattini (non all'inizio o alla fine), al massimo 20 caratteri";
  const scheda = sceltaScheda("invito-scheda");
  const posizione = el("input");
  posizione.id = "invito-posizione"; posizione.autocomplete = "off"; posizione.placeholder = "es. 45.8205, 8.8257";
  const prepara = el("button", "bottone", "Prepara l'invito");
  prepara.type = "submit";
  const esitoInvito = el("p", "esito");
  esitoInvito.setAttribute("aria-live", "polite");
  moduloInvito.append(el("h4", null, "Prepara un gateway nuovo"),
                      ...campo("Nome del gateway", nome, "diventa anche il nome sulla mappa: Gateway CNSAS Velate"),
                      ...campo("Radio del gateway", scheda),
                      ...campo("Dove sta", posizione, "coordinate copiate da Google Maps; vuoto se la radio ha il suo GPS"),
                      el("p"), prepara, esitoInvito);
  moduloInvito.addEventListener("submit", async ev => {
    ev.preventDefault();
    prepara.disabled = true;
    esitoInvito.textContent = "Preparo...";
    try {
      const d = await manda("invita", {nome: nome.value.trim(), scheda: scheda.value, posizione: posizione.value.trim()});
      dettagliAperti.add("invito:" + d.invito.id);
      esitoInvito.textContent = "Invito pronto: qui sopra ci sono le istruzioni da mandare a chi prepara la microSD.";
      moduloInvito.reset();
      await giro();
    } catch (e) {
      esitoInvito.textContent = "Non riuscito: " + e.message;
    } finally {
      prepara.disabled = false;
    }
  });
  zona.append(moduloInvito);
  return zona;
}

function aggiornaNuovi() {
  if (!zonaStatoNuovi) return;
  const n = gestione && gestione.ponti_nuovi;
  zonaStatoNuovi.parentNode.hidden = !gestione || !n;      // server piu' vecchio della 1.10.0: niente
  if (!gestione || !n) return;
  if (n.pronto) {
    zonaStatoNuovi.textContent = "Per i gateway nuovi: certificato " + (n.certificato ? "\"" + n.certificato + "\"" : "caricato") +
      (n.scade ? " (scade il " + new Date(n.scade * 1000).toLocaleDateString("it-IT") + ")" : "") + ", TAK Server " + (n.server || "?") +
      ", canale " + n.canale + " (l'unico del nodo dei gateway). Caricati da " + (n.da || "?") + " " +
      giornoOra(n.aggiornato) + ".";
    dettagliImpostazioni.firstChild.textContent = "Cambia certificato o canale";
  } else {
    zonaStatoNuovi.textContent = "Prima, una volta sola: carica qui sotto il certificato e la chiave del canale che i gateway nuovi devono usare.";
    dettagliImpostazioni.firstChild.textContent = "Certificato e canale per i gateway nuovi";
    if (!dettagliImpostazioni.dataset.aperto) { dettagliImpostazioni.open = true; dettagliImpostazioni.dataset.aperto = "1"; }
  }
  moduloInvito.hidden = !n.pronto;
  zonaInviti.replaceChildren(...(gestione.inviti || []).map(disegnaInvito));
}

// un Raspberry nuovo arrivato senza una chiave d'invito valida: si puo' installare lo stesso, ma lo decide chi approva
const moduliRichiesta = new Map();     // i moduli restano gli stessi tra un aggiornamento e l'altro della pagina

function moduloInstalla(r) {
  if (moduliRichiesta.has(r.id)) return moduliRichiesta.get(r.id);
  const f = el("form", "installa-richiesta");
  const nome = el("input");
  nome.id = "inst-nome-" + r.id.slice(0, 8); nome.required = true; nome.maxLength = 20; nome.value = r.nome === "nuovo" ? "" : r.nome;
  nome.pattern = "[a-z0-9]([a-z0-9\\-]{0,18}[a-z0-9])?"; nome.autocomplete = "off";
  const scheda = sceltaScheda("inst-scheda-" + r.id.slice(0, 8), r.nuovo && r.nuovo.radio_usb ? "usb" : "waveshare");
  const posizione = el("input");
  posizione.id = "inst-pos-" + r.id.slice(0, 8); posizione.autocomplete = "off"; posizione.placeholder = "es. 45.8205, 8.8257";
  const no = el("button", "bottone secondario piccolo", "Rifiuta");
  no.type = "button";
  no.addEventListener("click", () => azioneRichiesta(no, "rifiuta", {id: r.id}));
  const si = el("button", "bottone piccolo", "Approva e installa");
  si.type = "submit";
  const riga = el("div", "riga-pulsanti");
  riga.append(no, si);
  f.append(...campo("Nome del gateway", nome), ...campo("Radio del gateway", scheda), ...campo("Dove sta", posizione, "vuoto se la radio ha il suo GPS"), riga);
  f.addEventListener("submit", ev => {
    ev.preventDefault();
    if (!window.confirm("Questo Raspberry non ha una chiave d'invito valida. Approvandolo riceve il certificato del TAK Server e la " +
                        "chiave del canale: fallo solo se sei sicuro che è il tuo (stesso nome, acceso adesso). Continuo?")) return;
    azioneRichiesta(si, "approva", {id: r.id, sostituisci: !!r.sostituisce,
                                    installa: {nome: nome.value.trim(), scheda: scheda.value, posizione: posizione.value.trim()}});
  });
  moduliRichiesta.set(r.id, f);
  return f;
}

// installazione di un ponte nuovo: a che punto e', e "Riprova"
function installazioneDi(nome) {
  return (gestione && gestione.installazioni && gestione.installazioni[nome]) || null;
}

let daRiprovare = null;

function chiediRiprova(p, ins) {
  daRiprovare = p;
  document.getElementById("riprova-testo").textContent = "Il Raspberry del gateway " + p.nome + " riscarica la configurazione (con il " +
    "certificato e il canale salvati adesso) e rifà l'installazione da dove serve. Parte entro un paio di minuti.";
  const scelta = document.getElementById("riprova-scheda");
  if (!scelta.options.length) SCHEDE_RADIO.forEach(([v, t]) => { const o = el("option", null, t); o.value = v; scelta.append(o); });
  scelta.value = ins.scheda || "waveshare";
  document.getElementById("riprova-posizione").value = ins.posizione || "";
  document.getElementById("riprova").showModal();
}

document.getElementById("riprova-annulla").addEventListener("click", () => document.getElementById("riprova").close());
document.getElementById("riprova-conferma").addEventListener("click", async ev => {
  const p = daRiprovare;
  document.getElementById("riprova").close();
  if (p) await azioneRichiesta(ev.target, "riprova-installazione", {nome: p.nome, scheda: document.getElementById("riprova-scheda").value,
                                                                   posizione: document.getElementById("riprova-posizione").value.trim()});
  ev.target.disabled = false;
});

let sezAggiungi = null, zonaServizio = null, zonaModulo = null, zonaRifiutate = null;

function costruisciAggiungi() {
  // una volta sola: chi sta scrivendo nel modulo non perde nulla quando la pagina si aggiorna
  const sez = el("section", "aggiungi");
  sez.append(costruisciNuovi());
  sez.append(el("h3", null, "Raspberry installato a mano, dal terminale"));
  const ol = el("ol");
  const passo1 = el("li", null, "Sul Raspberry del nuovo gateway lancia:");
  const riga = el("div", "comando");
  const codice = el("code", null, COMANDO_PI);
  const copia = el("button", "bottone secondario piccolo", "Copia");
  copia.type = "button";
  copia.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(COMANDO_PI); copia.textContent = "Copiato"; }
    catch (e) { window.getSelection().selectAllChildren(codice); copia.textContent = "Selezionato: copialo"; }
    setTimeout(() => { copia.textContent = "Copia"; }, 4000);
  });
  riga.append(codice, copia);
  passo1.append(riga);
  ol.append(passo1);
  ol.append(el("li", null, "Ti chiede il nome del gateway e alla fine mostra un codice di controllo (la procedura completa del Raspberry è nella guida)."));
  ol.append(el("li", null, "Entro un minuto la richiesta compare in cima a questa pagina: se il codice è lo stesso, approvala. Il resto lo fa il server."));
  sez.append(ol);
  zonaServizio = el("div");
  zonaModulo = el("div");
  zonaRifiutate = el("div");
  const det = el("details");
  det.append(el("summary", null, "Il Raspberry non riesce a mandare la richiesta? Aggiungilo a mano"));
  const f = el("form");
  const l1 = el("label", null, "Nome del gateway");
  const nome = el("input");
  nome.name = "nome"; nome.required = true; nome.maxLength = 20; nome.pattern = "[a-z0-9]([a-z0-9\\-]{0,18}[a-z0-9])?";
  nome.placeholder = "es. sede";
  nome.autocomplete = "off";
  nome.title = "minuscole, numeri e trattini (non all'inizio o alla fine), al massimo 20 caratteri";
  l1.htmlFor = nome.id = "nuovo-nome";
  const l2 = el("label", null, "Chiave del Raspberry (la mostra  sudo mesh2tak-accesso)");
  const chiave = el("textarea");
  chiave.name = "chiave"; chiave.required = true; chiave.placeholder = "ssh-ed25519 AAAA...";
  chiave.spellcheck = false;
  l2.htmlFor = chiave.id = "nuova-chiave";
  const invia = el("button", "bottone", "Aggiungi il gateway");
  invia.type = "submit";
  const esito = el("p", "esito");
  esito.setAttribute("aria-live", "polite");
  f.append(l1, nome, l2, chiave, el("p"), invia, esito);
  f.addEventListener("submit", async ev => {
    ev.preventDefault();
    invia.disabled = true;
    esito.textContent = "Mando...";
    try {
      await manda("aggiungi", {nome: nome.value.trim(), chiave: chiave.value.trim()});
      esito.textContent = "Fatto: il server sta aggiungendo il gateway " + nome.value.trim() + " (lo vedi in cima alla pagina).";
      f.reset();
      await giro();
    } catch (e) {
      esito.textContent = "Non accettato: " + e.message;
    } finally {
      invia.disabled = false;
    }
  });
  det.append(f);
  zonaModulo.append(det);
  sez.append(zonaServizio, zonaModulo, zonaRifiutate);
  return sez;
}

function aggiornaAggiungi() {
  if (!sezAggiungi) {
    sezAggiungi = costruisciAggiungi();
    gestioneDettagli.aggiungi.append(sezAggiungi);
  }
  zonaServizio.replaceChildren();
  if (!gestione) {
    zonaServizio.append(el("p", "spiega", "Il servizio della pagina non risponde" + (erroreGestione ? " (" + erroreGestione + ")" : "") +
                                          ": senza, le richieste non arrivano. Sul server, una volta: sudo bash rete-meshtastic.sh installa"));
  }
  zonaModulo.hidden = !gestione;
  aggiornaNuovi();
  const rif = gestione ? (gestione.rifiutate || []) : [];
  zonaRifiutate.replaceChildren(...(rif.length ? [disegnaRifiutate(rif)] : []));
}

