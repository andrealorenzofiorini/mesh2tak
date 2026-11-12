#!/usr/bin/env bash
# Prova completa dell'installatore su un "Raspberry appena scritto", simulato in un contenitore:
#  - config.txt senza SPI, meshtasticd con la cartella available.d vera del firmware
#  - meshtasticd finto appena installato (regione non impostata, solo canale pubblico)
#  - TAK Server finto con TLS e certificato client
#  - systemctl/journalctl/ss/apt-get/qrencode finti; librerie meshtastic 2.7.11 e meshtastic-tak gia' nel venv
# Da lanciare come root. Esegue: 1a esecuzione (fino al riavvio), 2a (fino alla fine), 3a (rilancio),
# l'aggiornamento chiesto dalla pagina della rete (installatore vero da un GitHub finto), poi la rimozione.
set -uo pipefail
QUI=$(cd "$(dirname "$0")" && pwd)
INST="$QUI/../dist/installa-ponte-meshtastic.sh"
PKI=${PKI:-/tmp/m2t-pki}
MT=${MT_SRC:-/home/claude/src/mt-2711}
SRC=/home/claude/src
FW=$SRC/fw
STUB=/tmp/stubs-pi
FINTO=/tmp/pi-finto
ERR=0
ORA=$(sed -n 's/^VERSIONE="\(.*\)"$/\1/p' "$INST" | head -n1)          # versione di questo installatore
controlla() { if eval "$2"; then echo "  ok   - $1"; else echo "  FAIL - $1"; ERR=1; fi; }

pkill -f 'fake_node[.]py --porta 440[34]'; pkill -f 'fake_tak[.]py --porta 8089'; pkill -f 'mesh2tak[.]py -c /etc'
mkdir -p /etc/avahi/services; rm -f /etc/avahi/services/mesh2tak-meshtastic.service
pkill -f 'http.server 18099'
rm -rf /opt/mesh2tak /etc/mesh2tak /usr/local/bin/mesh2tak-* /var/log/mesh2tak-installazione.log \
  /root/canale-meshtastic.txt "$STUB" "$FINTO" /tmp/pi-eventi.jsonl /var/lib/mesh2tak-aggiorna \
  /etc/systemd/system/mesh2tak-aggiorna.service /etc/systemd/system/mesh2tak-aggiorna.timer \
  /etc/systemd/system/mesh2tak-aggiorna.path /var/lib/mesh2tak \
  /var/log/mesh2tak-aggiornamento.log /tmp/pi-github /tmp/pi-registrazione.json \
  /etc/systemd/system/mesh2tak-installazione.service
id mesh2tak >/dev/null 2>&1 && userdel mesh2tak
[ -f "$PKI/meshtastic-cnsas-integration.zip" ] || bash "$QUI/pki.sh" "$PKI" >/dev/null

# --- Raspberry finto
mkdir -p "$FINTO/meshtasticd/available.d" "$FINTO/meshtasticd/config.d" "$STUB"
# protezioni (1.7.0): systemd e apt finti, watchdog presente; apt con le origini di serie di Raspberry Pi OS
export MESH2TAK_SYSTEMD_DIR="$FINTO/systemd" MESH2TAK_APT_CONF_DIR="$FINTO/apt.conf.d" MESH2TAK_WATCHDOG_DEV="$FINTO/watchdog0"
mkdir -p "$FINTO/apt.conf.d"; : > "$FINTO/watchdog0"
cat > "$FINTO/apt.conf.d/50unattended-upgrades" <<'EOF'
Unattended-Upgrade::Origins-Pattern {
        "origin=Debian,codename=${distro_codename},label=Debian";
        "origin=Debian,codename=${distro_codename}-security,label=Debian-Security";
        "origin=Raspberry Pi Foundation,codename=${distro_codename},label=Raspberry Pi Foundation";
};
EOF
cp "$FW"/bin/config.d/lora-*.yaml "$FINTO/meshtasticd/available.d/"
cat > "$FINTO/config.txt" <<'EOF'
# For more options and information see http://rptl.io/configtxt
#dtparam=i2c_arm=on
#dtparam=i2s=on
#dtparam=spi=on
dtparam=audio=on
camera_auto_detect=1
display_auto_detect=1
auto_initramfs=1
dtoverlay=vc4-kms-v3d
max_framebuffers=2
disable_fw_kms_setup=1
arm_64bit=1
disable_overscan=1
arm_boost=1

[cm4]
otg_mode=1

[all]
EOF
cp "$FINTO/config.txt" "$FINTO/config.txt.originale"

cat > "$STUB/systemctl" <<'EOF'
#!/bin/bash
echo "systemctl $*" >> /tmp/stubs-pi/chiamate.log
rest=(); for a in "$@"; do case "$a" in --quiet|--no-pager|-q|--now) ;; *) rest+=("$a");; esac; done
cmd=${rest[0]:-}; unit=${rest[1]:-}
pidf=/tmp/stubs-pi/mesh2tak.pid
vivo() { [ -f $pidf ] && kill -0 "$(cat $pidf)" 2>/dev/null; }
ferma() { if vivo; then kill "$(cat $pidf)"; sleep 1; fi; rm -f $pidf; }
case "$cmd" in
  reboot) echo RIAVVIO >> /tmp/stubs-pi/chiamate.log; exit 0 ;;
  is-active) [ "$unit" = meshtasticd ] && exit 0; [ "$unit" = mesh2tak ] && vivo && exit 0; exit 3 ;;
  stop) [ "$unit" = mesh2tak ] && ferma; exit 0 ;;
  restart|start)
    if [ "$unit" = mesh2tak ]; then
      ferma
      exe=$(sed -n 's/^ExecStart=//p' /etc/systemd/system/mesh2tak.service)
      u=$(sed -n 's/^User=//p' /etc/systemd/system/mesh2tak.service)
      install -d -o "$u" -g "$u" -m 755 /var/lib/mesh2tak        # come StateDirectory=mesh2tak
      nohup runuser -u "$u" -- $exe >> /tmp/stubs-pi/servizio.log 2>&1 &
      echo $! > $pidf
    fi; exit 0 ;;
  disable) [ "$unit" = mesh2tak ] && ferma; exit 0 ;;
  show) if [ -f /tmp/pi-finto/systemd/system.conf.d/mesh2tak-watchdog.conf ]; then echo 14s; else echo 0; fi; exit 0 ;;
  *) exit 0 ;;
esac
EOF
printf '#!/bin/bash\nexit 0\n' > "$STUB/rpi-connect"
printf '#!/bin/bash\necho "loginctl $*" >> /tmp/stubs-pi/chiamate.log\n' > "$STUB/loginctl"
chmod 755 "$STUB/rpi-connect" "$STUB/loginctl"
cat > "$STUB/journalctl" <<'EOF'
#!/bin/bash
case "$*" in *meshtasticd*) echo "INFO  | 18:01:02 4 sx1262 init success";; *) tail -n 20 /tmp/stubs-pi/servizio.log 2>/dev/null;; esac
EOF
cat > "$STUB/apt-get" <<'EOF'
#!/bin/bash
echo "apt-get $*" >> /tmp/stubs-pi/chiamate.log
case "$*" in *--only-upgrade*meshtasticd*) echo 2.7.27.prova > /tmp/stubs-pi/meshtasticd-versione ;; esac
exit 0
EOF
cat > "$STUB/dpkg-query" <<'EOF'
#!/bin/bash
pacchetto="${@: -1}"
[ "$pacchetto" = meshtasticd ] || exit 1
case "$*" in
  *Status*) echo "install ok installed" ;;
  *Version*) cat /tmp/stubs-pi/meshtasticd-versione 2>/dev/null || echo 2.7.26.54e0d8d ;;
esac
EOF
cat > "$STUB/apt-mark" <<'EOF'
#!/bin/bash
echo "apt-mark $*" >> /tmp/stubs-pi/chiamate.log
case "$1" in
  hold) touch "/tmp/stubs-pi/bloccato-$2" ;;
  unhold) rm -f "/tmp/stubs-pi/bloccato-$2" ;;
  showhold) for f in /tmp/stubs-pi/bloccato-*; do [ -e "$f" ] && echo "${f#/tmp/stubs-pi/bloccato-}"; done ;;
esac
exit 0
EOF
cat > "$STUB/ss" <<'EOF'
#!/bin/bash
echo "LISTEN 0      128          0.0.0.0:4404       0.0.0.0:*"
EOF
cat > "$STUB/qrencode" <<'EOF'
#!/bin/bash
read -r riga; echo "[QR di ${#riga} caratteri]"
EOF
chmod +x "$STUB"/*

python3 -m venv --system-site-packages /opt/mesh2tak/venv
SP=$(/opt/mesh2tak/venv/bin/python -c 'import site; print(site.getsitepackages()[0])')
cp -r "$MT/meshtastic" "$SRC/pypubsub/src/pubsub" "$SRC/pyserial/serial" "$SRC/takpacket-sdk/python/src/meshtastic_tak" \
  "$SRC/zstd-target/zstandard" "$SP/"
metadati_tak() {  # come la installerebbe pip: con la versione leggibile
  mkdir -p "$SP/meshtastic_tak-0.10.0.dist-info"
  printf 'Metadata-Version: 2.1\nName: meshtastic-tak\nVersion: 0.10.0\n' > "$SP/meshtastic_tak-0.10.0.dist-info/METADATA"
}
metadati_tak
chmod -R a+rX /opt/mesh2tak

export PYTHONPATH="$MT:$SRC/pypubsub/src:$SRC/pyserial"
python3 "$QUI/fake_node.py" --porta 4404 --nuovo --primo-scenario 99 --stato "$FINTO/nodo.json" > /tmp/pi-nodo.log 2>&1 &
python3 "$QUI/fake_tak.py" --porta 8089 --cert "$PKI/takserver-chain.pem" --chiave "$PKI/takserver.key" \
  --ca "$PKI/ca-chain.pem" --uscita /tmp/pi-eventi.jsonl > /tmp/pi-tak.log 2>&1 &
sleep 1.5
unset PYTHONPATH

esegui() {  # $1 = boot id, $2 = file di uscita, poi argomenti
  local boot=$1 out=$2; shift 2
  (cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID="$boot" \
    MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
    MESH2TAK_POSIZIONE="45.8205, 8.8257, 400" \
    bash "$INST" "$@" </dev/null > "$out" 2>&1)
}
nodo() { python3 -c "import json,sys; d=json.load(open('$FINTO/nodo.json')); print(eval(sys.argv[1]))" "$1"; }

echo "== 0. ramo di sistema (aggiornamento, repository Meshtastic) con apt/curl/gpg finti"
STUB0=/tmp/stubs-sistema; rm -rf "$STUB0"; mkdir -p "$STUB0"; cp "$STUB"/* "$STUB0/"
cat > "$STUB0/apt-get" <<'EOF'
#!/bin/bash
echo "apt-get $*" >> /tmp/stubs-sistema/apt.log; exit 0
EOF
cat > "$STUB0/curl" <<'EOF'
#!/bin/bash
echo "curl $*" >> /tmp/stubs-sistema/apt.log; echo "-----BEGIN PGP PUBLIC KEY BLOCK-----"
EOF
cat > "$STUB0/gpg" <<'EOF'
#!/bin/bash
echo "gpg $*" >> /tmp/stubs-sistema/apt.log; cat > /dev/null
for ((i=1;i<=$#;i++)); do [ "${!i}" = -o ] && { j=$((i+1)); echo chiave > "${!j}"; }; done; exit 0
EOF
cat > "$STUB0/getent" <<'EOF'
#!/bin/bash
[ "$1" = hosts ] && { echo "195.135.221.134 download.opensuse.org"; exit 0; }; exec /usr/bin/getent "$@"
EOF
cat > "$STUB0/meshtasticd-assente" <<'EOF'
EOF
chmod +x "$STUB0"/*
printf 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\nID=debian\n' > "$FINTO/os-release"
cp "$FINTO/config.txt" "$FINTO/config0.txt"
(cd /tmp && PATH="$STUB0:/usr/local/sbin:/usr/sbin:/sbin:/usr/local/bin:/usr/bin:/bin" MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=b0 \
  MESH2TAK_OS_RELEASE="$FINTO/os-release" MESH2TAK_CONFIG_TXT="$FINTO/config0.txt" \
  MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" MESH2TAK_POSIZIONE="45.8,8.8" \
  bash "$INST" --scheda waveshare "$PKI/meshtastic-cnsas-integration.zip" </dev/null > /tmp/pi-run0.log 2>&1); U0=$?
controlla "uscita 0" '[ $U0 = 0 ]'
controlla "aggiornamento completo con le opzioni giuste" 'grep -q "apt-get -o DPkg::Lock::Timeout=600 -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade" "$STUB0/apt.log"'
controlla "programmi di base" 'grep -q "install -y -q curl gnupg python3-venv openssl qrencode ca-certificates" "$STUB0/apt.log"'
controlla "repository Meshtastic Debian_13 come da documentazione" 'grep -qx "deb http://download.opensuse.org/repositories/network:/Meshtastic:/beta/Debian_13/ /" "/etc/apt/sources.list.d/network:Meshtastic:beta.list"'
controlla "chiave del repository" 'grep -q "curl -fsSL https://download.opensuse.org/repositories/network:Meshtastic:beta/Debian_13/Release.key" "$STUB0/apt.log" && [ -s /etc/apt/trusted.gpg.d/network_Meshtastic_beta.gpg ]'
controlla "meshtasticd installato" 'grep -q "install -y -q meshtasticd" "$STUB0/apt.log"'
controlla "riavvio dopo l'aggiornamento" 'grep -q RIAVVIO "$STUB/chiamate.log"'
rm -f "/etc/apt/sources.list.d/network:Meshtastic:beta.list" /etc/apt/trusted.gpg.d/network_Meshtastic_beta.gpg
rm -rf /etc/mesh2tak /root/canale-meshtastic.txt "$FINTO/meshtasticd/config.d"/*; : > "$STUB/chiamate.log"

echo "== 1a esecuzione (Raspberry appena scritto, certificato copiato sulla microSD dal Mac)"
cp "$PKI/meshtastic-cnsas-integration.zip" "$FINTO/"
printf 'AppleDouble del Mac' > "$FINTO/._meshtastic-cnsas-integration.zip"
(export SUDO_USER=nobody; esegui boot1 /tmp/pi-run1.log --scheda waveshare); U1=$?
controlla "uscita 0" '[ $U1 = 0 ]'
controlla "certificato trovato sulla microSD (disco bootfs), non nel file ._ del Mac" 'grep -q "uso: $FINTO/meshtastic-cnsas-integration.zip" /tmp/pi-run1.log'
controlla "poi tolto dalla microSD, con il file ._" '[ ! -e "$FINTO/meshtastic-cnsas-integration.zip" ] && [ ! -e "$FINTO/._meshtastic-cnsas-integration.zip" ] && grep -q "pacchetto del certificato tolto dalla microSD" /tmp/pi-run1.log'
controlla "Raspberry Pi Connect raggiungibile dopo i riavvii (linger)" 'grep -qx "loginctl enable-linger nobody" "$STUB/chiamate.log" && grep -q "Raspberry Pi Connect resta raggiungibile anche dopo i riavvii" /tmp/pi-run1.log'
controlla "dopo il riavvio riparte da sola: unita' e copia dell'installatore" 'grep -qx "ExecStart=/bin/bash /etc/mesh2tak/installatore-in-corso.sh --scheda waveshare" /etc/systemd/system/mesh2tak-installazione.service && grep -qx "Environment=MESH2TAK_DOPO_RIAVVIO=1 HOME=/root" /etc/systemd/system/mesh2tak-installazione.service && grep -qx "StandardOutput=append:/etc/mesh2tak/installazione-schermo.log" /etc/systemd/system/mesh2tak-installazione.service && grep -qx "WantedBy=multi-user.target" /etc/systemd/system/mesh2tak-installazione.service && cmp -s /etc/mesh2tak/installatore-in-corso.sh "$INST" && [ "$(stat -c %a:%U /etc/mesh2tak/installatore-in-corso.sh)" = 700:root ] && grep -q "systemctl enable mesh2tak-installazione.service" "$STUB/chiamate.log"'
controlla "lo dice prima di riavviare" 'grep -q "Poi l.installazione continua da sola: non serve rilanciarla" /tmp/pi-run1.log'
controlla "SPI e spi0-0cs aggiunti in [all]" 'tail -n 4 "$FINTO/config.txt" | tr "\n" " " | grep -q "\[all\] # Scheda LoRa.* dtparam=spi=on dtoverlay=spi0-0cs"'
controlla "copia di sicurezza di config.txt" 'cmp -s "$FINTO/config.txt.prima-di-meshtastic" "$FINTO/config.txt.originale"'
controlla "configurazione Waveshare in config.d" 'cmp -s "$FINTO/meshtasticd/config.d/lora-waveshare-sxxx.yaml" "$FW/bin/config.d/lora-waveshare-sxxx.yaml"'
controlla "meshtasticd su porte interne: pagina 9444, API 4404" 'grep -qx "  Port: 9444" "$FINTO/meshtasticd/config.d/mesh2tak.yaml" && grep -qx "  APIPort: 4404" "$FINTO/meshtasticd/config.d/mesh2tak.yaml" && [ ! -e "$FINTO/meshtasticd/config.d/cnsas-web.yaml" ]'
controlla "l'app Meshtastic trova il Raspberry da sola (mDNS)" 'grep -q "<type>_meshtastic._tcp</type>" /etc/avahi/services/mesh2tak-meshtastic.service && grep -q "<port>4403</port>" /etc/avahi/services/mesh2tak-meshtastic.service && grep -q "shortname=GTSA" /etc/avahi/services/mesh2tak-meshtastic.service'
controlla "meshtasticd risponde solo al Raspberry" 'grep -qx "IPAddressAllow=localhost" /etc/systemd/system/meshtasticd.service.d/mesh2tak-solo-locale.conf && grep -qx "IPAddressDeny=any" /etc/systemd/system/meshtasticd.service.d/mesh2tak-solo-locale.conf'
controlla "riavvio chiesto" 'grep -q RIAVVIO "$STUB/chiamate.log"'
controlla "esito per il primo avvio dei gateway nuovi: riavvio" 'grep -qx riavvio /etc/mesh2tak/esito-installazione'
controlla "si ferma prima della configurazione del nodo" '! grep -q "5/9" /tmp/pi-run1.log'
controlla "certificato gia' installato" '[ -s /etc/mesh2tak/client.key ] && grep -q "CERTIFICATO_OK=1" /etc/mesh2tak/installazione.conf'
controlla "posizione ricordata" '[ "$(. /etc/mesh2tak/installazione.conf; echo "$POSIZIONE")" = "45.8205,8.8257,400" ]'
controlla "nodo non ancora toccato" '[ "$(nodo "d[\"regione\"]")" = UNSET ]'

echo "== 2a esecuzione (dopo il riavvio: la lancia systemd, da sola)"
: > "$STUB/chiamate.log"
(cd / && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
  MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" MESH2TAK_DOPO_RIAVVIO=1 \
  bash /etc/mesh2tak/installatore-in-corso.sh --scheda waveshare </dev/null > /tmp/pi-run2.log 2>&1); U2=$?
sleep 8
controlla "uscita 0" '[ $U2 = 0 ]'
controlla "ripresa da sola, una volta sola" 'grep -q "dopo il riavvio l.installazione riprende da sola" /tmp/pi-run2.log && grep -q "systemctl disable mesh2tak-installazione.service" "$STUB/chiamate.log"'
controlla "esito: ok con la versione" 'grep -qE "^ok [0-9]+\.[0-9]+\.[0-9]+$" /etc/mesh2tak/esito-installazione'
controlla "alla fine niente ripresa rimasta in giro" '[ ! -e /etc/systemd/system/mesh2tak-installazione.service ] && [ ! -e /etc/mesh2tak/installatore-in-corso.sh ] && [ ! -e /etc/mesh2tak/.in-corso ]'
controlla "gateway nuovo: nome del nodo ricordato per i rilanci" 'grep -q "^NOME_NODO_AUTOMATICO=1" /etc/mesh2tak/installazione.conf'
controlla "nessun nuovo riavvio" '! grep -q RIAVVIO "$STUB/chiamate.log"'
controlla "nessuna domanda: certificato e risposte gia' dati prima del riavvio" '! grep -q -e "1/9" -e "Password del certificato" -e "Coordinate (Invio" /tmp/pi-run2.log && grep -q "2/9" /tmp/pi-run2.log'
controlla "radio vista nel registro" 'grep -q "la scheda risponde: sx1262 init success" /tmp/pi-run2.log'
controlla "nodo in EU_868" '[ "$(nodo "d[\"regione\"]")" = EU_868 ]'
controlla "ruolo CLIENT_MUTE (Waveshare)" '[ "$(nodo "d[\"ruolo\"]")" = CLIENT_MUTE ]'
controlla "nome del nodo e sigla (dalla 1.12.0)" '[ "$(nodo "d[\"nome\"], d[\"nome_breve\"]")" = "('"'"'Gateway CNSAS Varese'"'"', '"'"'GTSA'"'"')" ]'
controlla "canale primario CNSAS con chiave a 256 bit e posizione precisa" \
  '[ "$(nodo "\"%s %s %s %s\" % (d[\"canali\"][0][\"ruolo\"], d[\"canali\"][0][\"nome\"], d[\"canali\"][0][\"psk_len\"], d[\"canali\"][0][\"precisione\"])")" = "PRIMARY CNSAS 32 32" ]'
controlla "nessun canale secondario (dalla 1.12.0)" \
  '[ "$(nodo "\" \".join(c[\"ruolo\"] for c in d[\"canali\"][1:])")" = "DISABLED DISABLED DISABLED DISABLED DISABLED DISABLED DISABLED" ]'
controlla "posizione fissa del Raspberry" '[ "$(nodo "(d[\"posizione_fissa\"], d[\"lat\"], d[\"lon\"], d[\"alt\"])")" = "(True, 458205000, 88257000, 400)" ]'
controlla "link del canale salvato, solo root" '[ "$(stat -c %a /root/canale-meshtastic.txt)" = 600 ] && grep -q "^https://meshtastic.org/e/#" /root/canale-meshtastic.txt'
controlla "QR mostrato" 'grep -q "\[QR di" /tmp/pi-run2.log'
controlla "comando mesh2tak-canale" '[ -x /usr/local/bin/mesh2tak-canale ]'
controlla "prova del gateway: nodo e TAK ok" 'grep -q "collegamento riuscito" /tmp/pi-run2.log && grep -q "canale 0 .CNSAS.: privato -> inoltrato" /tmp/pi-run2.log'
controlla "servizio avviato" '[ -f "$STUB/mesh2tak.pid" ] && grep -q "Collegato al nodo Meshtastic" "$STUB/servizio.log"'
controlla "il Raspberry compare sul TAK Server" 'grep -q "\"callsign\": \"Gateway CNSAS Varese\"" /tmp/pi-eventi.jsonl'
controlla "con l'icona del traliccio" 'grep -qx "icona_ponte = 6d781afb-89a6-4c07-b2b9-a89748b6a38f/Landmarks/tower.png" /etc/mesh2tak/mesh2tak.conf && grep -q "Landmarks/tower.png" /tmp/pi-eventi.jsonl'
controlla "libreria TAK verificata" 'grep -q "librerie pronte (meshtastic-tak 0.10.0)" /tmp/pi-run2.log && grep -q "conversione di prova riuscita" /tmp/pi-run2.log'
controlla "niente piu' dizionari scaricati a parte" '[ ! -e /opt/mesh2tak/dizionari ]'
controlla "sezione [radio] nella configurazione" 'grep -qx "invio = si" /etc/mesh2tak/mesh2tak.conf && grep -qx "tempo_radio_percento = 3" /etc/mesh2tak/mesh2tak.conf'
controlla "ricorda il certificato con lettura e scrittura" 'grep -q "lettura e scrittura (BOTH)" /tmp/pi-run2.log'
controlla "posizioni ripetute ogni 2 minuti" 'grep -qx "aggiornamento_min = 2" /etc/mesh2tak/mesh2tak.conf && grep -q "ripetute al TAK Server ogni 2 minuti" "$STUB/servizio.log"'
controlla "il servizio legge la libreria TAK" 'grep -q "Libreria TAK meshtastic-tak 0.10.0" "$STUB/servizio.log"'
controlla "certificato della pagina, chiave leggibile solo dal gateway" '[ -s /etc/mesh2tak/pagina.pem ] && [ "$(stat -c %a:%G /etc/mesh2tak/pagina.key)" = 640:mesh2tak ]'
controlla "sezione [gestione] e gateway sulla porta interna 4404" 'grep -qx "attiva = si" /etc/mesh2tak/mesh2tak.conf && grep -qx "porta_pagina = 9443" /etc/mesh2tak/mesh2tak.conf && grep -qx "porta_app = 4403" /etc/mesh2tak/mesh2tak.conf && grep -qx "nodo_pagina = https://127.0.0.1:9444" /etc/mesh2tak/mesh2tak.conf && grep -qx "pausa_massima_min = 30" /etc/mesh2tak/mesh2tak.conf && grep -qx "collegamento = tcp:127.0.0.1:4404" /etc/mesh2tak/mesh2tak.conf'
controlla "gestione pronta: pagina 9443 e app 4403" 'grep -q "gestione del nodo pronta sulla porta 9443" /tmp/pi-run2.log && grep -q "gestione del nodo pronta sulla porta 4403" /tmp/pi-run2.log && grep -q "Gestione del nodo: pagina nel browser sulla porta 9443, app Meshtastic via Wi-Fi sulla porta 4403" "$STUB/servizio.log"'
controlla "istruzioni per l'app via Wi-Fi" "grep -q \"Dall'app Meshtastic via Wi-Fi: Rete\" /tmp/pi-run2.log"
controlla "la pagina risponde in HTTPS" 'python3 -c "import ssl,socket; ssl._create_unverified_context().wrap_socket(socket.create_connection((\"127.0.0.1\", 9443), 3)).close()"'
controlla "istruzioni finali sulla pausa" 'grep -q "mentre la usi il gateway va in pausa" /tmp/pi-run2.log'
controlla "nessun errore o traceback" '! grep -q -e "✘" -e Traceback /tmp/pi-run1.log /tmp/pi-run2.log'
controlla "passo 9: protezioni del Raspberry" 'grep -q "9/9  Protezioni del Raspberry" /tmp/pi-run2.log && ! grep -q "/8  " /tmp/pi-run2.log'
controlla "riavvio automatico: watchdog di 14 secondi" 'grep -qx "RuntimeWatchdogSec=14s" "$FINTO/systemd/system.conf.d/mesh2tak-watchdog.conf" && grep -q "systemctl daemon-reexec" "$STUB/chiamate.log" && grep -q "riavvio automatico se il Raspberry si blocca (watchdog di 14 secondi)" /tmp/pi-run2.log'
controlla "registri del sistema limitati" 'grep -qx "SystemMaxUse=200M" "$FINTO/systemd/journald.conf.d/mesh2tak.conf" && grep -q "systemctl restart systemd-journald" "$STUB/chiamate.log"'
printf 'Dir::Etc::Parts "%s";\n' "$FINTO/apt.conf.d" > "$FINTO/apt-prova.conf"
APT_FINTO=$(APT_CONFIG="$FINTO/apt-prova.conf" apt-config dump)   # niente "| grep -q": con pipefail fallirebbe a caso
controlla "unattended-upgrades installato" 'grep -q "install -y -q unattended-upgrades" "$STUB/chiamate.log" && grep -q "aggiornamenti di sicurezza automatici ogni notte" /tmp/pi-run2.log'
controlla "apt legge solo le origini Debian-Security (le altre di serie tolte)" '[ "$(grep -c "^Unattended-Upgrade::Origins-Pattern:: " <<<"$APT_FINTO")" = 2 ] && ! grep -q -e "Raspberry Pi Foundation" -e "label=Debian\";" <<<"$APT_FINTO"'
controlla "mai meshtasticd, mai riavvii, controllo ogni notte" 'grep -q "^Unattended-Upgrade::Package-Blacklist:: \"meshtasticd\"" <<<"$APT_FINTO" && grep -q "^Unattended-Upgrade::Automatic-Reboot \"false\"" <<<"$APT_FINTO" && grep -q "^APT::Periodic::Unattended-Upgrade \"1\"" <<<"$APT_FINTO"'
controlla "meshtasticd bloccato alla versione installata" 'grep -q "apt-mark hold meshtasticd" "$STUB/chiamate.log" && grep -q "meshtasticd fermo alla versione 2.7.26.54e0d8d" /tmp/pi-run2.log'
controlla "aggiornamenti dalla pagina: programma e timer ogni 2 minuti, da root" '[ -x /opt/mesh2tak/aggiorna.py ] && grep -q "^VERSIONE = \"$ORA\"" /opt/mesh2tak/aggiorna.py && grep -qx "ExecStart=/usr/bin/python3 -I /opt/mesh2tak/aggiorna.py" /etc/systemd/system/mesh2tak-aggiorna.service && grep -qx "Type=oneshot" /etc/systemd/system/mesh2tak-aggiorna.service && ! grep -q "^User=" /etc/systemd/system/mesh2tak-aggiorna.service && grep -qx "OnUnitInactiveSec=2min" /etc/systemd/system/mesh2tak-aggiorna.timer && grep -qx "WantedBy=timers.target" /etc/systemd/system/mesh2tak-aggiorna.timer && grep -q "systemctl enable --now mesh2tak-aggiorna.timer" "$STUB/chiamate.log"'
controlla "comandi dalla pagina subito: unita' .path su /var/lib/mesh2tak/controlla-adesso (1.13.0)" 'grep -qx "PathChanged=/var/lib/mesh2tak/controlla-adesso" /etc/systemd/system/mesh2tak-aggiorna.path && grep -qx "Unit=mesh2tak-aggiorna.service" /etc/systemd/system/mesh2tak-aggiorna.path && grep -q "systemctl enable --now mesh2tak-aggiorna.timer mesh2tak-aggiorna.path" "$STUB/chiamate.log"'
controlla "radio preparata tutta, ricordato per gli aggiornamenti (1.13.0)" '[ "$(. /etc/mesh2tak/installazione.conf; echo "$RADIO_PREPARATA:$RADIO_DA_FARE")" = "1:" ]'
controlla "installatore tenuto da parte, solo root (serve per tornare indietro)" 'cmp -s "$INST" /opt/mesh2tak/installatore.sh && [ "$(stat -c %a:%U /opt/mesh2tak/installatore.sh)" = 700:root ] && [ "$(stat -c %a /var/lib/mesh2tak-aggiorna)" = 755 ]'
controlla "senza accesso da internet il controllo non fa niente" '(cd /tmp && python3 -I /opt/mesh2tak/aggiorna.py) && [ ! -e /var/lib/mesh2tak-aggiorna/stato.json ]'
controlla "comando mesh2tak-scorta" '[ -x /usr/local/bin/mesh2tak-scorta ] && /usr/local/bin/mesh2tak-scorta --help | grep -q -- "--disco" && grep -q "^VERSIONE = \"$ORA\"" /opt/mesh2tak/scorta.py && grep -q "sudo mesh2tak-scorta" /tmp/pi-run2.log'

echo "== 3a esecuzione (rilancio)"
LINK1=$(cat /root/canale-meshtastic.txt)
: > "$STUB/chiamate.log"
sed -i 's/^tempo_radio_percento = 3$/tempo_radio_percento = 5/' /etc/mesh2tak/mesh2tak.conf
sed -i 's/^aggiornamento_min = 2$/aggiornamento_min = 1/' /etc/mesh2tak/mesh2tak.conf
sed -i 's/^pausa_massima_min = 30$/pausa_massima_min = 15/' /etc/mesh2tak/mesh2tak.conf
CERT_PAGINA=$(sha256sum < /etc/mesh2tak/pagina.pem)
printf '[Unit]\n' > /etc/systemd/system/mesh2tak-installazione.service; cp "$INST" /etc/mesh2tak/installatore-in-corso.sh
esegui boot2 /tmp/pi-run3.log; U3=$?
controlla "rilancio a mano: prende il posto della ripresa non ancora partita" 'grep -q "systemctl disable --now mesh2tak-installazione.service" "$STUB/chiamate.log" && grep -q "continuo io" /tmp/pi-run3.log && [ ! -e /etc/systemd/system/mesh2tak-installazione.service ] && [ ! -e /etc/mesh2tak/installatore-in-corso.sh ]'
controlla "impostazione radio cambiata a mano conservata" 'grep -qx "tempo_radio_percento = 5" /etc/mesh2tak/mesh2tak.conf' 
controlla "ripetizione cambiata a mano conservata" 'grep -qx "aggiornamento_min = 1" /etc/mesh2tak/mesh2tak.conf'
controlla "pausa massima cambiata a mano conservata" 'grep -qx "pausa_massima_min = 15" /etc/mesh2tak/mesh2tak.conf'
controlla "certificato della pagina non rifatto" '[ "$(sha256sum < /etc/mesh2tak/pagina.pem)" = "$CERT_PAGINA" ]'
controlla "uscita 0" '[ $U3 = 0 ]'
controlla "nessun riavvio" '! grep -q RIAVVIO "$STUB/chiamate.log"'
controlla "radio lasciata com'era (1.13.0: niente messaggi al nodo)" 'grep -q "7/9  Nodo Meshtastic: impostazioni della radio lasciate come sono" /tmp/pi-run3.log && grep -q "radio com.era: quello che si e. cambiato dall.app o dalla pagina resta" /tmp/pi-run3.log && grep -q "reimposta-radio" /tmp/pi-run3.log && ! grep -q "era gia. tutto a posto" /tmp/pi-run3.log'
controlla "stessa chiave del canale" '[ "$(cat /root/canale-meshtastic.txt)" = "$LINK1" ]'
controlla "config.txt non toccato di nuovo" '[ "$(grep -c "dtoverlay=spi0-0cs" "$FINTO/config.txt")" = 1 ]'
controlla "protezioni gia' a posto: systemd e registri non toccati" '! grep -q "daemon-reexec" "$STUB/chiamate.log" && ! grep -q "restart systemd-journald" "$STUB/chiamate.log" && grep -q "riavvio automatico se il Raspberry si blocca" /tmp/pi-run3.log'

echo "== 3a2 aggiornamento di meshtasticd (bloccato alla versione installata) e Raspberry senza watchdog"
: > "$STUB/chiamate.log"
esegui boot2 /tmp/pi-run-agg.log --aggiorna-meshtasticd; UAG=$?
controlla "meshtasticd aggiornato e bloccato di nuovo" '[ $UAG = 0 ] && grep -q "meshtasticd aggiornato: 2.7.26.54e0d8d -> 2.7.27.prova" /tmp/pi-run-agg.log && [ "$(grep -o -e "apt-mark unhold meshtasticd" -e "only-upgrade -o Dpkg::Options::=--force-confold meshtasticd" -e "apt-mark hold meshtasticd" -e "systemctl restart meshtasticd" "$STUB/chiamate.log" | tr "\n" "|")" = "apt-mark unhold meshtasticd|only-upgrade -o Dpkg::Options::=--force-confold meshtasticd|apt-mark hold meshtasticd|systemctl restart meshtasticd|" ] && [ -e "$STUB/bloccato-meshtasticd" ]'
controlla "l'aggiornamento di meshtasticd non tocca il resto" '! grep -q -e "9/9" -e "restart mesh2tak" /tmp/pi-run-agg.log "$STUB/chiamate.log"'
(export MESH2TAK_WATCHDOG_DEV=/nonesiste; esegui boot2 /tmp/pi-run-nowd.log); UNW=$?
controlla "Raspberry senza watchdog: lo dice e va avanti" '[ $UNW = 0 ] && grep -q "questo Raspberry non ha il watchdog" /tmp/pi-run-nowd.log && grep -q "meshtasticd fermo alla versione 2.7.27.prova" /tmp/pi-run-nowd.log'

echo "== 3b canale della squadra dato da fuori (come quello di ICARO)"
CHIAVE_PROVA=$(python3 -c "import base64; print(base64.b64encode(bytes(range(100, 132))).decode())")
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
  MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
  MESH2TAK_CANALE_SQUADRA=CNSAS-VA MESH2TAK_CHIAVE_CANALE="base64:$CHIAVE_PROVA" \
  bash "$INST" --canale-squadra </dev/null > /tmp/pi-run3b.log 2>&1); U3B=$?
controlla "uscita 0" '[ $U3B = 0 ]'
canali_nodo() {  # primario CNSAS-VA con la chiave di prova; dalla 1.12.0 nessun canale secondario
  python3 -c "import json,sys; c=json.load(open(sys.argv[1]))[\"canali\"]; sys.exit(0 if (c[0][\"ruolo\"], c[0][\"nome\"], c[0][\"psk\"], c[0][\"precisione\"]) == (\"PRIMARY\", \"CNSAS-VA\", sys.argv[2], 32) and all(x[\"ruolo\"] == \"DISABLED\" for x in c[1:]) else 1)" "$FINTO/nodo.json" "$CHIAVE_PROVA"
}
controlla "primario CNSAS-VA con la chiave data, nessun canale secondario" 'canali_nodo'
controlla "passo 7 con il canale CNSAS-VA, solo quello" 'grep -q "7/9  Nodo Meshtastic: solo canale della squadra; il resto della radio resta com.e." /tmp/pi-run3b.log && grep -q "canale privato: CNSAS-VA" /tmp/pi-run3b.log && grep -q "canale della squadra .CNSAS-VA. con la chiave data" /tmp/pi-run3b.log && grep -q "nessun canale secondario" /tmp/pi-run3b.log'
controlla "chiave non lasciata in giro" '[ ! -e /etc/mesh2tak/.chiave-canale ] && ! grep -rqs "$CHIAVE_PROVA" /tmp/pi-run3b.log /etc/mesh2tak /var/log/mesh2tak-installazione.log'
controlla "nome del canale ricordato, niente piu' secondario" 'grep -qx "CANALE_SQUADRA=CNSAS-VA" /etc/mesh2tak/installazione.conf && ! grep -q "^SECONDARIO" /etc/mesh2tak/installazione.conf && grep -q "lo imposto al passo 7 come canale principale del nodo; gli altri canali restano come sono" /tmp/pi-run3b.log'
controlla "QR del canale nuovo" '[ -s /root/canale-meshtastic.txt ] && [ "$(cat /root/canale-meshtastic.txt)" != "$LINK1" ] && grep -q "Canale .CNSAS-VA. per le radio della squadra" /tmp/pi-run3b.log'
LINK2=$(cat /root/canale-meshtastic.txt)
esegui boot2 /tmp/pi-run3c.log; U3C=$?
controlla "rilancio senza opzione: canale della squadra lasciato com'era" '[ $U3C = 0 ] && grep -q "impostazioni della radio lasciate come sono" /tmp/pi-run3c.log && [ "$(cat /root/canale-meshtastic.txt)" = "$LINK2" ]'

echo "== 3c2 canale secondario acceso dall'app: vale l'interfaccia (1.13.0); spento solo reimpostando la radio"
accendi_longfast() {  # come dall'app Meshtastic: LongFast pubblico come secondario
  PATH="$STUB:$PATH" systemctl stop mesh2tak
  PYTHONPATH="$MT:$SRC/pypubsub/src:$SRC/pyserial" python3 - <<'PY'
import time
import meshtastic.tcp_interface as t
from meshtastic.protobuf import channel_pb2
i = t.TCPInterface("127.0.0.1", portNumber=4404)
c = i.localNode.channels[1]
c.role, c.settings.psk, c.settings.name = channel_pb2.Channel.Role.SECONDARY, b"\x01", ""
i.localNode.writeChannel(1)
time.sleep(1.5)
i.close()
PY
}
longfast_acceso() { [ "$(nodo "d[\"canali\"][1][\"ruolo\"], d[\"canali\"][1][\"psk\"]")" = "('SECONDARY', 'AQ==')" ]; }
ruolo_e_nome() { [ "$(nodo "d[\"ruolo\"], d[\"nome\"]")" = "('CLIENT_MUTE', 'Gateway CNSAS Varese')" ]; }
accendi_longfast
controlla "LongFast secondario acceso dall'app" 'longfast_acceso'
esegui boot2 /tmp/pi-run3p.log --canale-pubblico=Public; U3P=$?
controlla "aggiornamento: LongFast lasciato acceso, --canale-pubblico ignorato" '[ $U3P = 0 ] && longfast_acceso && grep -q "canale-pubblico ignorato" /tmp/pi-run3p.log && grep -q "impostazioni della radio lasciate come sono" /tmp/pi-run3p.log && grep -q "canali secondari: LongFast" /tmp/pi-run3p.log && ! grep -q "spento" /tmp/pi-run3p.log'
sed -i '/^RADIO_PREPARATA=/d; /^RADIO_DA_FARE=/d' /etc/mesh2tak/installazione.conf          # come la 1.12.0 e prima
esegui boot2 /tmp/pi-run3s.log; U3S=$?
controlla "gateway di prima della 1.13.0: i secondari si spengono una volta sola" '[ $U3S = 0 ] && canali_nodo && grep -q "7/9  Nodo Meshtastic: solo canali secondari spenti" /tmp/pi-run3s.log && grep -q "canale secondario .LongFast. spento" /tmp/pi-run3s.log && grep -qx "RADIO_PREPARATA=1" /etc/mesh2tak/installazione.conf'
controlla "QR del canale come prima (solo la squadra)" '[ "$(cat /root/canale-meshtastic.txt)" = "$LINK2" ]'
accendi_longfast; esegui boot2 /tmp/pi-run3t.log; U3T=$?
controlla "dopo: l'interfaccia vale di nuovo" '[ $U3T = 0 ] && longfast_acceso && grep -q "impostazioni della radio lasciate come sono" /tmp/pi-run3t.log'
: > "$STUB/chiamate.log"
esegui boot2 /tmp/pi-run3r.log --reimposta-radio; U3R=$?
controlla "--reimposta-radio: solo la radio, come all'installazione" '[ $U3R = 0 ] && canali_nodo && grep -q "Radio del gateway come all.installazione" /tmp/pi-run3r.log && grep -q "canale secondario .LongFast. spento" /tmp/pi-run3r.log && ! grep -q -e "1/9" -e "6/9" -e "8/9" /tmp/pi-run3r.log && ruolo_e_nome'
controlla "--reimposta-radio: il gateway si ferma e riparte" 'grep -q "systemctl stop mesh2tak" "$STUB/chiamate.log" && grep -q "systemctl start mesh2tak" "$STUB/chiamate.log" && grep -qx "ok [0-9.]*" /etc/mesh2tak/esito-installazione'
accendi_longfast
(export MESH2TAK_REIMPOSTA_RADIO=1; esegui boot2 /tmp/pi-run3u.log); U3U=$?
controlla "aggiornamento con MESH2TAK_REIMPOSTA_RADIO=1: radio come all'installazione" '[ $U3U = 0 ] && canali_nodo && grep -q "7/9  Nodo Meshtastic: regione EU 868, nome, ruolo CLIENT_MUTE, solo il canale privato" /tmp/pi-run3u.log && grep -q "canale secondario .LongFast. spento" /tmp/pi-run3u.log'
controlla "chiave della squadra mai nei registri" '! grep -rqs "$CHIAVE_PROVA" /tmp/pi-run3p.log /tmp/pi-run3s.log /tmp/pi-run3t.log /tmp/pi-run3r.log /tmp/pi-run3u.log /etc/mesh2tak /var/log/mesh2tak-installazione.log'

echo "== 3d accesso da internet (pagina della rete Meshtastic sul server)"
: > "$STUB/chiamate.log"
# pagina della rete finta su https://meshtastic.prova.lan:8443, con un certificato della CA di prova
if [ ! -s "$PKI/rete.pem" ]; then   # CA con le estensioni che Python 3.13 pretende (come Let's Encrypt)
  openssl req -x509 -newkey rsa:2048 -nodes -keyout "$PKI/rete-ca.key" -out "$PKI/rete-ca.pem" -days 30 -subj "/CN=rete-ca" \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    -addext subjectKeyIdentifier=hash >/dev/null 2>&1
  printf '%s\n' basicConstraints=CA:FALSE keyUsage=critical,digitalSignature,keyEncipherment extendedKeyUsage=serverAuth \
    subjectAltName=DNS:meshtastic.prova.lan subjectKeyIdentifier=hash authorityKeyIdentifier=keyid,issuer > "$PKI/rete.cnf"
  openssl req -newkey rsa:2048 -nodes -keyout "$PKI/rete.key" -out "$PKI/rete.csr" -subj "/CN=meshtastic.prova.lan" >/dev/null 2>&1
  openssl x509 -req -in "$PKI/rete.csr" -CA "$PKI/rete-ca.pem" -CAkey "$PKI/rete-ca.key" -CAcreateserial -days 30 \
    -extfile "$PKI/rete.cnf" -out "$PKI/rete.pem" >/dev/null 2>&1
fi
grep -q " meshtastic.prova.lan$" /etc/hosts || echo "127.0.0.1 meshtastic.prova.lan" >> /etc/hosts
pkill -f 'hub_finto[.]py'; rm -f /tmp/pi-hub.jsonl
python3 "$QUI/hub_finto.py" --porta 8443 --cert "$PKI/rete.pem" --chiave "$PKI/rete.key" --uscita /tmp/pi-hub.jsonl \
  --registrazione /tmp/pi-registrazione.json \
  > /tmp/pi-hub.log 2>&1 &
sleep 1
sed -i '/^NOME_NODO=/d; /^NOME_BREVE=/d; /^NOME_NODO_AUTOMATICO=/d' /etc/mesh2tak/installazione.conf   # come la 1.8.0
printf 'NOME_NODO=%q\nNOME_BREVE=PONT\n' "Ponte CNSAS Varese" >> /etc/mesh2tak/installazione.conf          # e il suo nome di allora
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
  MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
  MESH2TAK_ACCESSO_RETE=https://meshtastic.prova.lan:8443 SSL_CERT_FILE="$PKI/rete-ca.pem" \
  bash "$INST" --accesso-esterno=casa </dev/null > /tmp/pi-run3d.log 2>&1); U3D=$?
controlla "gateway di casa (prima della 1.9.0): diventa Gateway CNSAS Varese, GTSA" '[ "$(nodo "d[\"nome\"], d[\"nome_breve\"]")" = "('"'"'Gateway CNSAS Varese'"'"', '"'"'GTSA'"'"')" ] && grep -q "^NOME_NODO_AUTOMATICO=0" /etc/mesh2tak/installazione.conf && grep -qx "NOME_BREVE=GTSA" /etc/mesh2tak/installazione.conf && grep -q "nome nuovo del nodo: .Gateway CNSAS Varese. (sigla GTSA)" /tmp/pi-run3d.log && ! grep -q "nome del nodo e del gateway sulla mappa" /tmp/pi-run3d.log'
CODICE_ATTESO=$(python3 "$QUI/../mesh2tak.py" --codice-controllo /etc/mesh2tak/accesso/chiave.pub 2>/dev/null)
controlla "uscita 0" '[ $U3D = 0 ]'
controlla "chiave SSH del gateway, leggibile solo dall'utente del gateway" '[ "$(stat -c %a:%U /etc/mesh2tak/accesso/chiave)" = 600:mesh2tak ] && [ "$(stat -c %a:%U /etc/mesh2tak/accesso)" = 700:mesh2tak ] && grep -q "^ssh-ed25519 .* mesh2tak-casa@" /etc/mesh2tak/accesso/chiave.pub'
controlla "sezione [accesso_esterno]" 'grep -qx "\[accesso_esterno\]" /etc/mesh2tak/mesh2tak.conf && grep -qx "nome = casa" /etc/mesh2tak/mesh2tak.conf && grep -qx "rete = https://meshtastic.prova.lan:8443" /etc/mesh2tak/mesh2tak.conf && grep -qx "chiave = /etc/mesh2tak/accesso/chiave" /etc/mesh2tak/mesh2tak.conf'
controlla "cartella di stato scrivibile dal servizio" 'grep -qx "StateDirectory=mesh2tak" /etc/systemd/system/mesh2tak.service'
controlla "il gateway ha chiesto da solo di entrare, con nome e chiave pubblica" 'python3 -c "
import json, sys
righe = [json.loads(x) for x in open(\"/tmp/pi-hub.jsonl\")]
post = [r for r in righe if r[\"metodo\"] == \"POST\"]
chiave = open(\"/etc/mesh2tak/accesso/chiave.pub\").read().strip()
assert post and post[0][\"percorso\"] == \"/accesso/richiesta\", righe
c = post[0][\"corpo\"]
assert (c[\"formato\"], c[\"nome\"], c[\"chiave\"]) == (1, \"casa\", chiave), c
assert set(c) == {\"formato\", \"nome\", \"chiave\", \"macchina\", \"versione\"}, c
assert righe[0][\"metodo\"] == \"GET\" and righe[0][\"percorso\"].startswith(\"/accesso/\"), righe
"'
controlla "alla fine: codice di controllo e approvazione sulla pagina della rete" '[ -n "$CODICE_ATTESO" ] && grep -q "ultimo passo, sulla pagina della rete" /tmp/pi-run3d.log && grep -q "Codice di controllo di questo Raspberry: .*$CODICE_ATTESO" /tmp/pi-run3d.log && grep -q "1. Apri https://meshtastic.prova.lan:8443" /tmp/pi-run3d.log && grep -q "Adesso il gateway dice: in attesa di approvazione sulla pagina della rete" /tmp/pi-run3d.log && ! grep -q "rete-meshtastic.sh aggiungi" /tmp/pi-run3d.log'
controlla "mesh2tak-accesso: codice, stato e chiave per aggiungerlo a mano" '/usr/local/bin/mesh2tak-accesso > /tmp/pi-accesso.txt 2>&1; grep -q "Codice di controllo di questo Raspberry:  $CODICE_ATTESO" /tmp/pi-accesso.txt && grep -q "in attesa di approvazione" /tmp/pi-accesso.txt && grep -qF "$(cat /etc/mesh2tak/accesso/chiave.pub)" /tmp/pi-accesso.txt'
controlla "il gateway non cerca server SSH prima dell'approvazione" '! grep -q "Authenticated to\|tunnel verso" "$STUB/servizio.log"'
controlla "alla fine dice come si aggiorna" 'grep -q "Aggiornamenti:.*dalla pagina della rete, .Aggiorna i gateway." /tmp/pi-run3d.log'
echo "== 3d2 le opzioni insieme, come nei comandi della guida (--accesso-esterno=casa --canale-pubblico, ignorata)"
CHIAVE_PRIMA=$(cat /etc/mesh2tak/accesso/chiave.pub)
esegui boot2 /tmp/pi-run3d2.log --accesso-esterno=casa --canale-pubblico; U3D2=$?
controlla "opzioni insieme: accesso tenuto (stessa chiave, stesso nome, nessuna domanda), solo la squadra" '[ $U3D2 = 0 ] && [ "$(cat /etc/mesh2tak/accesso/chiave.pub)" = "$CHIAVE_PRIMA" ] && grep -qx "nome = casa" /etc/mesh2tak/mesh2tak.conf && canali_nodo && grep -q "canale-pubblico ignorato" /tmp/pi-run3d2.log && ! grep -q "Nome (minuscole" /tmp/pi-run3d2.log && grep -q "Codice di controllo di questo Raspberry" /tmp/pi-run3d2.log'
controlla "la stessa chiave a ogni rilancio" 'CHIAVE1=$(cat /etc/mesh2tak/accesso/chiave.pub); esegui boot2 /tmp/pi-run3e.log && [ "$(cat /etc/mesh2tak/accesso/chiave.pub)" = "$CHIAVE1" ] && grep -qx "nome = casa" /etc/mesh2tak/mesh2tak.conf'

echo "== 3d3 aggiornamento chiesto dalla pagina della rete: installatore vero, scaricato da un GitHub finto"
NUOVA=$(echo "$ORA" | awk -F. '{print $1"."$2+1".0"}'); ROTTA=$(echo "$ORA" | awk -F. '{print $1"."$2+2".0"}')
GH=/tmp/pi-github
rilascio() {  # $1 = versione, $2 = "rotta": il gateway parte ma la sua pagina non risponde piu'
  local d="$GH/andrealorenzofiorini/mesh2tak/releases/download/v$1"; mkdir -p "$d"
  sed -e "s/^VERSIONE=\"$ORA\"\$/VERSIONE=\"$1\"/" -e "s/^VERSIONE = \"$ORA\"\$/VERSIONE = \"$1\"/" "$INST" \
    > "$d/installa-ponte-meshtastic.sh"
  [ "${2:-}" = rotta ] && sed -i 's|if percorso == "/mesh2tak/stato.json":|if percorso == "/mesh2tak/stato-rotto.json":|' \
    "$d/installa-ponte-meshtastic.sh"
  (cd "$d" && sha256sum installa-ponte-meshtastic.sh > SHA256SUMS)
}
chiedi_dalla_pagina() { printf '{"formato": 1, "nome": "casa", "aggiorna": {"versione": "%s", "chiesto": %s}}\n' "$1" "$2" \
  > /tmp/pi-registrazione.json; }
aggiorna_dalla_pagina() {  # come lo lancia il timer: da root, senza terminale
  (cd / && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
    MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
    SSL_CERT_FILE="$PKI/rete-ca.pem" MESH2TAK_PROVA_GITHUB=http://127.0.0.1:18099 MESH2TAK_AGGIORNA_ATTESA=25 \
    timeout 900 python3 -I /opt/mesh2tak/aggiorna.py </dev/null > "$1" 2>&1)
}
stato_aggiornamento() { python3 -c "import json,sys; d=json.load(open('/var/lib/mesh2tak-aggiorna/stato.json')); print(eval(sys.argv[1]))" "$1"; }
pagina_del_ponte() { python3 -c "
import json, ssl, sys, urllib.request
r = urllib.request.urlopen('https://127.0.0.1:9443/mesh2tak/stato.json', context=ssl._create_unverified_context(), timeout=10)
d = json.load(r); print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
rilascio "$NUOVA"; rilascio "$ROTTA" rotta
python3 -m http.server 18099 --bind 127.0.0.1 --directory "$GH" > /tmp/pi-github.log 2>&1 &
sleep 1
aggiorna_dalla_pagina /tmp/pi-agg0.log; UA0=$?
controlla "nessuna richiesta: niente da fare" '[ $UA0 = 0 ] && [ ! -e /var/lib/mesh2tak-aggiorna/stato.json ] && ! grep -q "GET /andrealorenzofiorini" /tmp/pi-github.log'
chiedi_dalla_pagina "$NUOVA" 1000; : > "$STUB/chiamate.log"
aggiorna_dalla_pagina /tmp/pi-agg1.log; UA1=$?
controlla "aggiornato alla versione chiesta dalla pagina" '[ $UA1 = 0 ] && [ "$(stato_aggiornamento "d[\"stato\"], d[\"versione\"], d[\"da\"]")" = "('"'fatto', '$NUOVA', '$ORA'"')" ] && grep -q "^VERSIONE = \"$NUOVA\"" /opt/mesh2tak/mesh2tak.py && grep -q "^VERSIONE = \"$NUOVA\"" /opt/mesh2tak/aggiorna.py'
controlla "scaricato da GitHub (la pagina della rete chiede soltanto)" 'grep -q "GET /andrealorenzofiorini/mesh2tak/releases/download/v$NUOVA/SHA256SUMS" /tmp/pi-github.log && grep -q "GET /andrealorenzofiorini/mesh2tak/releases/download/v$NUOVA/installa-ponte-meshtastic.sh" /tmp/pi-github.log'
controlla "installatore vero, senza domande: configurazione, certificato e accesso tenuti" 'grep -q "systemctl restart mesh2tak" "$STUB/chiamate.log" && grep -qx "nome = casa" /etc/mesh2tak/mesh2tak.conf && [ "$(cat /etc/mesh2tak/accesso/chiave.pub)" = "$CHIAVE1" ] && grep -q "gia. installato nella prima esecuzione" /var/log/mesh2tak-aggiornamento.log && grep -q "esito: fatto: aggiornato dalla $ORA alla $NUOVA" /var/log/mesh2tak-aggiornamento.log'
controlla "installatore nuovo tenuto, quello di prima da parte" 'cmp -s /opt/mesh2tak/installatore.sh "$GH/andrealorenzofiorini/mesh2tak/releases/download/v$NUOVA/installa-ponte-meshtastic.sh" && cmp -s /var/lib/mesh2tak-aggiorna/installatore-precedente.sh "$INST" && [ ! -e "/var/lib/mesh2tak-aggiorna/installatore-$NUOVA.sh" ]'
controlla "la pagina del gateway lo dice alla pagina della rete" '[ "$(pagina_del_ponte "d[\"versione\"], d[\"sistema\"][\"aggiornabile\"], d[\"sistema\"][\"aggiornamento\"][\"stato\"]")" = "('"'$NUOVA', True, 'fatto'"')" ]'
QUANDO=$(stato_aggiornamento 'd["quando"]'); aggiorna_dalla_pagina /tmp/pi-agg2.log; UA2=$?
controlla "dopo: la stessa richiesta non rifa' niente" '[ $UA2 = 0 ] && [ "$(stato_aggiornamento "d[\"quando\"]")" = "$QUANDO" ]'
chiedi_dalla_pagina "$ORA" 2000; aggiorna_dalla_pagina /tmp/pi-agg3.log; UA3=$?
controlla "mai indietro, anche se la pagina lo chiede" '[ $UA3 = 0 ] && grep -q "^VERSIONE = \"$NUOVA\"" /opt/mesh2tak/mesh2tak.py && [ "$(stato_aggiornamento "d[\"quando\"]")" = "$QUANDO" ]'
chiedi_dalla_pagina "$ROTTA" 3000
aggiorna_dalla_pagina /tmp/pi-agg4.log; UA4=$?
controlla "versione che non riparte: rimessa quella di prima, da sola" '[ $UA4 = 1 ] && [ "$(stato_aggiornamento "d[\"stato\"], d[\"messaggio\"]")" = "('"'ripristinato', 'con la $ROTTA il gateway non riparte; rimessa la $NUOVA'"')" ] && grep -q "^VERSIONE = \"$NUOVA\"" /opt/mesh2tak/mesh2tak.py'
controlla "dopo il ritorno il gateway risponde di nuovo" '[ "$(pagina_del_ponte "d[\"versione\"], d[\"sistema\"][\"aggiornamento\"][\"stato\"]")" = "('"'$NUOVA', 'ripristinato'"')" ]'
controlla "nessun errore o traceback nei controlli" '! grep -q -e Traceback /tmp/pi-agg0.log /tmp/pi-agg1.log /tmp/pi-agg2.log /tmp/pi-agg3.log /tmp/pi-agg4.log /var/log/mesh2tak-aggiornamento.log'

echo "== 3d4 comandi dalla pagina della rete (Gestisci il Raspberry, 1.13.0): li fa mesh2tak-aggiorna, da root"
ADESSO=$(date +%s)
cat > /tmp/pi-registrazione.json <<FINE_REGISTRAZIONE
{"formato": 1, "nome": "casa", "azioni": [
 {"id": "00000000000000a1", "azione": "impostazioni", "chiesto": $ADESSO, "valori": {"squadra": "Red", "validita_min": 120, "invio": false}},
 {"id": "00000000000000a2", "azione": "registro", "chiesto": $ADESSO},
 {"id": "00000000000000a3", "azione": "reimposta-radio", "chiesto": $ADESSO},
 {"id": "00000000000000a4", "azione": "riavvia-programma", "chiesto": $ADESSO},
 {"id": "00000000000000a5", "azione": "riavvia-programma", "chiesto": $((ADESSO - 7200))}]}
FINE_REGISTRAZIONE
accendi_longfast                     # dall'app; ferma anche il gateway, che i comandi rimettono in moto
: > "$STUB/chiamate.log"
aggiorna_dalla_pagina /tmp/pi-agg5.log; UA5=$?
esito_comando() { python3 -c "import json,sys; d={a['id']: a for a in json.load(open('/var/lib/mesh2tak-aggiorna/azioni.json'))['azioni']}; print(d[sys.argv[1]]['stato'])" "$1"; }
controlla "impostazioni dalla pagina: salvate, commenti tenuti, gateway ripartito" '[ $UA5 = 0 ] && [ "$(esito_comando 00000000000000a1)" = fatto ] && grep -qx "squadra = Red" /etc/mesh2tak/mesh2tak.conf && grep -qx "validita_min = 120" /etc/mesh2tak/mesh2tak.conf && grep -qx "invio = no" /etc/mesh2tak/mesh2tak.conf && grep -q "^# colore squadra TAK" /etc/mesh2tak/mesh2tak.conf && [ -s /etc/mesh2tak/mesh2tak.conf.prima-della-pagina ]'
controlla "registro per la pagina: lo leggono solo root e il gateway, senza la chiave del canale" '[ "$(esito_comando 00000000000000a2)" = fatto ] && [ "$(stat -c %a:%U:%G /var/lib/mesh2tak-aggiorna/registro.txt)" = 640:root:mesh2tak ] && grep -q "===== Programma del gateway (mesh2tak)" /var/lib/mesh2tak-aggiorna/registro.txt && grep -q "sx1262 init success" /var/lib/mesh2tak-aggiorna/registro.txt && ! grep -q "$CHIAVE_PROVA" /var/lib/mesh2tak-aggiorna/registro.txt'
controlla "radio reimpostata dalla pagina: LongFast spento" '[ "$(esito_comando 00000000000000a3)" = fatto ] && canali_nodo && grep -q "lancio /opt/mesh2tak/installatore.sh --reimposta-radio" /var/log/mesh2tak-aggiornamento.log'
controlla "programma riavviato dalla pagina; il comando vecchio scade" '[ "$(esito_comando 00000000000000a4)" = fatto ] && [ "$(esito_comando 00000000000000a5)" = scaduto ]'
controlla "la pagina del gateway mostra comandi, impostazioni e registro" '[ "$(pagina_del_ponte "d[\"sistema\"][\"comandi\"] and sorted(a[\"stato\"] for a in d[\"sistema\"][\"azioni\"]) == [\"fatto\"] * 4 + [\"scaduto\"] and d[\"impostazioni\"][\"squadra\"] == \"Red\" and d[\"impostazioni\"][\"invio\"] is False and d[\"sistema\"][\"registro\"][\"byte\"] > 0")" = True ]'
controlla "dal tunnel: avviso dei comandi e registro solo per gli amministratori" 'python3 -c "
import os, ssl, urllib.error, urllib.request
c = ssl._create_unverified_context()
def chiedi(percorso, dati=None, **testate):
    r = urllib.request.Request(\"https://127.0.0.1:9443\" + percorso, data=dati, headers=testate,
                               method=\"POST\" if dati is not None else \"GET\")
    try:
        with urllib.request.urlopen(r, context=c, timeout=10) as x:
            return x.status, x.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
assert chiedi(\"/mesh2tak/controlla\", b\"{}\")[0] == 403
assert chiedi(\"/mesh2tak/controlla\", b\"{}\", **{\"X-Mesh2tak\": \"1\"})[0] == 200
assert os.path.exists(\"/var/lib/mesh2tak/controlla-adesso\")
assert chiedi(\"/mesh2tak/registro\")[0] == 403
s, t = chiedi(\"/mesh2tak/registro\", **{\"X-Authentik-Username\": \"andrea\"})
assert s == 200 and b\"Registro del gateway\" in t
"'
controlla "mappa della copertura (1.14.0; formato 2 con i ripetitori dalla 1.15.0): solo dal tunnel, per gli amministratori" 'python3 -c "
import json, ssl, urllib.error, urllib.request
c = ssl._create_unverified_context()
def chiedi(**testate):
    r = urllib.request.Request(\"https://127.0.0.1:9443/mesh2tak/copertura.json\", headers=testate)
    try:
        with urllib.request.urlopen(r, context=c, timeout=10) as x:
            return x.status, json.loads(x.read())
    except urllib.error.HTTPError as e:
        return e.code, None
assert chiedi()[0] == 403
s, d = chiedi(**{\"X-Authentik-Username\": \"andrea\"})
assert s == 200 and d[\"formato\"] == 2 and d[\"zoom\"] == 17 and isinstance(d[\"celle\"], list), d
assert isinstance(d[\"ripetitori\"], list), d
assert d[\"preset\"] == \"LONG_FAST\" and d[\"limite_snr\"] == -17.5, d
"'
: > "$STUB/chiamate.log"
aggiorna_dalla_pagina /tmp/pi-agg6.log
controlla "comandi gia' fatti: non si rifanno" '! grep -q "systemctl restart mesh2tak" "$STUB/chiamate.log" && [ "$(grep -c "=== comando dalla pagina" /var/log/mesh2tak-aggiornamento.log)" = 4 ]'
controlla "nessun errore o traceback nei comandi" '! grep -q -e Traceback /tmp/pi-agg5.log /tmp/pi-agg6.log /var/log/mesh2tak-aggiornamento.log'
pkill -f 'http.server 18099'; rm -f /tmp/pi-registrazione.json

(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
  MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
  bash "$INST" --accesso-esterno=no </dev/null > /tmp/pi-run3f.log 2>&1); U3F=$?
controlla "--accesso-esterno=no lo spegne e toglie la chiave" '[ $U3F = 0 ] && [ ! -e /etc/mesh2tak/accesso ] && [ ! -e /usr/local/bin/mesh2tak-accesso ] && ! grep -q "accesso_esterno" /etc/mesh2tak/mesh2tak.conf && grep -q "Togli dalla rete. sotto casa" /tmp/pi-run3f.log'
controlla "le impostazioni cambiate dalla pagina restano dopo un'installazione" 'grep -qx "squadra = Red" /etc/mesh2tak/mesh2tak.conf && grep -qx "validita_min = 120" /etc/mesh2tak/mesh2tak.conf'
pkill -f 'hub_finto[.]py'
controlla "nessun errore o traceback" '! grep -q -e "✘" -e Traceback /tmp/pi-run3d.log /tmp/pi-run3e.log /tmp/pi-run3f.log'

echo "== 4a esecuzione da terminale, con la connessione che cade a meta'"
: > "$STUB/chiamate.log"; rm -f /tmp/pi-run4.log /tmp/pi-run5.log
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=boot2 \
  MESH2TAK_CONFIG_TXT="$FINTO/config.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" \
  timeout 8 script -qec "bash $INST" /tmp/pi-run4.log </dev/null >/dev/null 2>&1)   # "ssh" chiuso dopo 8 s
controlla "lavoro ancora in corso dopo la caduta" '[ -f /etc/mesh2tak/.in-corso ] && kill -0 "$(cat /etc/mesh2tak/.in-corso)"'
(cd /tmp && PATH="$STUB:$PATH" timeout 120 script -qec "bash $INST" /tmp/pi-run5.log </dev/null >/dev/null 2>&1)  # ricollegato
controlla "rilanciando si segue il lavoro gia' in corso" 'grep -q "sta gia. lavorando" /tmp/pi-run5.log'
controlla "il lavoro staccato arriva fino in fondo" 'grep -q "Fatto." /etc/mesh2tak/installazione-schermo.log'
controlla "il rilancio mostra la fine" 'grep -q "Fatto." /tmp/pi-run5.log'
controlla "nessuna installazione doppia" '[ "$(grep -c "configurazione: lora-waveshare" /etc/mesh2tak/installazione-schermo.log)" = 1 ]'
controlla "segnaposto rimosso a fine lavoro" '[ ! -f /etc/mesh2tak/.in-corso ]'
controlla "servizio riavviato dal lavoro staccato" 'grep -q "systemctl restart mesh2tak" "$STUB/chiamate.log"'

echo "== rimozione"
: > "$STUB/chiamate.log"
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd" bash "$INST" --rimuovi </dev/null > /tmp/pi-rim.log 2>&1)
controlla "protezioni tolte, meshtasticd sbloccato, mesh2tak-scorta tolto" '[ ! -e "$FINTO/systemd/system.conf.d/mesh2tak-watchdog.conf" ] && [ ! -e "$FINTO/systemd/journald.conf.d/mesh2tak.conf" ] && [ ! -e "$FINTO/apt.conf.d/52mesh2tak-aggiornamenti" ] && [ -e "$FINTO/apt.conf.d/50unattended-upgrades" ] && grep -q "apt-mark unhold meshtasticd" "$STUB/chiamate.log" && grep -q "systemctl daemon-reexec" "$STUB/chiamate.log" && [ ! -e /usr/local/bin/mesh2tak-scorta ] && grep -q "tolte anche le protezioni del Raspberry" /tmp/pi-rim.log'
controlla "meshtasticd come prima: porte normali, niente filtro, niente annuncio" '[ ! -e /etc/systemd/system/meshtasticd.service.d/mesh2tak-solo-locale.conf ] && [ ! -e "$FINTO/meshtasticd/config.d/mesh2tak.yaml" ] && grep -qx "  Port: 9443" "$FINTO/meshtasticd/config.d/cnsas-web.yaml" && [ ! -e /etc/avahi/services/mesh2tak-meshtastic.service ]'
controlla "file del gateway rimossi" '[ ! -e /opt/mesh2tak ] && [ ! -e /etc/mesh2tak ] && [ ! -e /usr/local/bin/mesh2tak-canale ] && [ ! -e /etc/systemd/system/mesh2tak-installazione.service ] && grep -q "systemctl disable --now mesh2tak-installazione.service" "$STUB/chiamate.log"'
controlla "aggiornamenti e comandi dalla pagina tolti" '[ ! -e /etc/systemd/system/mesh2tak-aggiorna.timer ] && [ ! -e /etc/systemd/system/mesh2tak-aggiorna.service ] && [ ! -e /etc/systemd/system/mesh2tak-aggiorna.path ] && [ ! -e /var/lib/mesh2tak-aggiorna ] && grep -q "systemctl disable --now mesh2tak-aggiorna.timer mesh2tak-aggiorna.path" "$STUB/chiamate.log"'
controlla "configurazione meshtasticd lasciata" '[ -f "$FINTO/meshtasticd/config.d/lora-waveshare-sxxx.yaml" ]'

echo "== radio Meshtastic via USB (Raspberry appena scritto, nessuna scheda innestata)"
pkill -f 'fake_node[.]py --porta 4404'; sleep 0.5
rm -f "$STUB/servizio.log" "$STUB/mesh2tak.pid" /root/canale-meshtastic.txt; : > "$STUB/chiamate.log"
cp "$FINTO/config.txt.originale" "$FINTO/config-usb.txt"; rm -rf "$FINTO/meshtasticd-usb"; mkdir -p "$FINTO/meshtasticd-usb"
python3 -m venv --system-site-packages /opt/mesh2tak/venv   # la rimozione ha tolto anche il venv di prova
cp -r "$MT/meshtastic" "$SRC/pypubsub/src/pubsub" "$SRC/pyserial/serial" "$SRC/takpacket-sdk/python/src/meshtastic_tak" \
  "$SRC/zstd-target/zstandard" "$SP/"; metadati_tak; chmod -R a+rX /opt/mesh2tak
RADIO=/tmp/pi-radio-usb
PYTHONPATH="$MT:$SRC/pypubsub/src:$SRC/pyserial" python3 "$QUI/fake_node.py" --porta 4403 --nuovo --primo-scenario 99 \
  --stato "$FINTO/radio.json" --usb "$RADIO" > /tmp/pi-radio.log 2>&1 &
for _ in $(seq 1 50); do [ -e "$RADIO" ] && break; sleep 0.1; done
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=usb1 \
  MESH2TAK_CONFIG_TXT="$FINTO/config-usb.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd-usb" \
  MESH2TAK_POSIZIONE="45.8205, 8.8257, 400" \
  MESH2TAK_COLLEGAMENTO="serial:$RADIO" bash "$INST" "$PKI/meshtastic-cnsas-integration.zip" </dev/null > /tmp/pi-run-usb.log 2>&1); UU=$?
sleep 8
radio() { python3 -c "import json,sys; d=json.load(open('$FINTO/radio.json')); print(eval(sys.argv[1]))" "$1"; }
controlla "uscita 0" '[ $UU = 0 ]'
controlla "riconosciuta la radio USB senza indicare la scheda" 'grep -q "3/9  Meshtastic per la scheda LoRa (usb)" /tmp/pi-run-usb.log'
controlla "nessun meshtasticd ne' SPI" 'cmp -s "$FINTO/config-usb.txt" "$FINTO/config.txt.originale" && [ -z "$(ls -A "$FINTO/meshtasticd-usb")" ]'
controlla "radio configurata via USB: EU_868, CLIENT, canale CNSAS" '[ "$(radio "d[\"regione\"], d[\"ruolo\"], d[\"canali\"][0][\"nome\"], d[\"canali\"][0][\"psk_len\"]")" = "('"'"'EU_868'"'"', '"'"'CLIENT'"'"', '"'"'CNSAS'"'"', 32)" ]'
controlla "posizione fissa sulla radio" '[ "$(radio "d[\"posizione_fissa\"]")" = True ]'
controlla "radio USB: niente gestione attraverso il gateway" 'grep -qx "attiva = no" /etc/mesh2tak/mesh2tak.conf && [ ! -e /etc/mesh2tak/pagina.pem ] && ! grep -q -e "Gestione del nodo dal browser" -e "via Wi-Fi: Rete" /tmp/pi-run-usb.log'
controlla "servizio con il gruppo dialout" 'grep -q "^SupplementaryGroups=dialout" /etc/systemd/system/mesh2tak.service && id -nG mesh2tak | grep -qw dialout'
controlla "gateway collegato alla radio USB" 'grep -q "Collegato al nodo Meshtastic .* radio USB" "$STUB/servizio.log"'
controlla "QR del canale" 'grep -q "\[QR di" /tmp/pi-run-usb.log && [ -s /root/canale-meshtastic.txt ]'
controlla "nessun errore o traceback" '! grep -q -e "✘" -e Traceback /tmp/pi-run-usb.log'
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=usb1 \
  MESH2TAK_CONFIG_TXT="$FINTO/config-usb.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd-usb" \
  MESH2TAK_COLLEGAMENTO="serial:$RADIO" MESH2TAK_ACCESSO_RETE=https://meshtastic.prova.lan \
  bash "$INST" --accesso-esterno=deposito </dev/null > /tmp/pi-run-usb-accesso.log 2>&1); UUA=$?
sleep 8
controlla "gateway nuovo nella rete: il nodo prende il nome del gateway" '[ "$(radio "d[\"nome\"], d[\"nome_breve\"]")" = "('"'"'Gateway CNSAS Deposito'"'"', '"'"'GDEP'"'"')" ] && grep -q "nome del nodo e del gateway sulla mappa: .Gateway CNSAS Deposito. (sigla GDEP)" /tmp/pi-run-usb-accesso.log'
controlla "radio USB con accesso da internet: pagina del gateway solo per lo stato" '[ $UUA = 0 ] && grep -qx "attiva = si" /etc/mesh2tak/mesh2tak.conf && grep -qx "porta_pagina = 9443" /etc/mesh2tak/mesh2tak.conf && grep -qx "porta_app = 0" /etc/mesh2tak/mesh2tak.conf && grep -qx "nodo_pagina = " /etc/mesh2tak/mesh2tak.conf && grep -qx "nome = deposito" /etc/mesh2tak/mesh2tak.conf'
controlla "il servizio da' lo stato sulla 9443 e cerca la registrazione" 'grep -q "Stato del gateway per la pagina della rete sulla porta 9443" "$STUB/servizio.log" && grep -q "Accesso da internet: gateway .deposito." "$STUB/servizio.log" && grep -q "stato del gateway pronto sulla porta 9443" /tmp/pi-run-usb-accesso.log'
controlla "pagina della rete irraggiungibile: alla fine lo dice, con il codice" 'grep -q "Codice di controllo di questo Raspberry:" /tmp/pi-run-usb-accesso.log && grep -q "Adesso il gateway dice: la pagina della rete https://meshtastic.prova.lan non risponde" /tmp/pi-run-usb-accesso.log'
controlla "nessun errore o traceback" '! grep -q -e "✘" -e Traceback /tmp/pi-run-usb-accesso.log'
# un gateway nuovo installato dalla 1.9.0 alla 1.11.0 si chiamava "Gateway CNSAS Deposito" (PDEP): l'aggiornamento lo cambia
sed -i 's/^NOME_NODO=.*/NOME_NODO=Ponte\\ CNSAS\\ Deposito/; s/^NOME_BREVE=.*/NOME_BREVE=PDEP/' /etc/mesh2tak/installazione.conf
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=usb1 \
  MESH2TAK_CONFIG_TXT="$FINTO/config-usb.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd-usb" \
  MESH2TAK_COLLEGAMENTO="serial:$RADIO" MESH2TAK_ACCESSO_RETE=https://meshtastic.prova.lan \
  bash "$INST" </dev/null > /tmp/pi-run-usb-nomi.log 2>&1); UUN=$?
sleep 8
controlla "gateway nuovo della 1.9.0-1.11.0: all'aggiornamento diventa Gateway CNSAS Deposito (GDEP)" '[ $UUN = 0 ] && [ "$(radio "d[\"nome\"], d[\"nome_breve\"]")" = "('"'"'Gateway CNSAS Deposito'"'"', '"'"'GDEP'"'"')" ] && grep -q "nome nuovo del nodo: .Gateway CNSAS Deposito. (sigla GDEP)" /tmp/pi-run-usb-nomi.log && grep -qx "NOME_BREVE=GDEP" /etc/mesh2tak/installazione.conf'
(cd /tmp && PATH="$STUB:$PATH" bash "$INST" --rimuovi </dev/null > /tmp/pi-rim-usb.log 2>&1)
controlla "rimozione: ricorda di togliere il gateway dalla pagina della rete" 'grep -q "su https://meshtastic.prova.lan, .Togli dalla rete. sotto deposito" /tmp/pi-rim-usb.log && [ ! -e /usr/local/bin/mesh2tak-accesso ]'
pkill -f 'fake_node[.]py --porta 4403 --nuovo --primo-scenario 99 --stato'

echo "== scheda RAK6421 sui pin (in config.txt e' rimasta una riga spi0-0cs di un'altra scheda)"
rm -f "$STUB/servizio.log" "$STUB/mesh2tak.pid"; : > "$STUB/chiamate.log"
cp "$FINTO/config.txt.originale" "$FINTO/config-rak.txt"
printf '%s\n' "# vecchia scheda" "dtoverlay=spi0-0cs" >> "$FINTO/config-rak.txt"
cp "$FINTO/config-rak.txt" "$FINTO/config-rak.txt.originale"
rm -rf "$FINTO/meshtasticd-rak"; mkdir -p "$FINTO/meshtasticd-rak/available.d" "$FINTO/meshtasticd-rak/config.d"
cp "$FW"/bin/config.d/lora-*.yaml "$FINTO/meshtasticd-rak/available.d/"
cp "$FW/bin/config.d/lora-waveshare-sxxx.yaml" "$FINTO/meshtasticd-rak/config.d/"
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 MESH2TAK_SALTA_PIP=1 MESH2TAK_BOOT_ID=rak1 \
  MESH2TAK_CONFIG_TXT="$FINTO/config-rak.txt" MESH2TAK_MESHTASTICD_DIR="$FINTO/meshtasticd-rak" \
  MESH2TAK_POSIZIONE="45.8205, 8.8257, 400" \
  bash "$INST" --scheda rak "$PKI/meshtastic-cnsas-integration.zip" </dev/null > /tmp/pi-run-rak.log 2>&1); UR=$?
controlla "uscita 0" '[ $UR = 0 ]'
controlla "configurazione RAK6421 + RAK13300 slot 1 in config.d" 'cmp -s "$FINTO/meshtasticd-rak/config.d/lora-RAK6421-13300-slot1.yaml" "$FW/bin/config.d/lora-RAK6421-13300-slot1.yaml"'
controlla "nessuna tabella dell'amplificatore della RAK13302" '! grep -q TX_GAIN_LORA "$FINTO"/meshtasticd-rak/config.d/lora-*.yaml'
controlla "configurazione Waveshare messa da parte" '[ -f "$FINTO/meshtasticd-rak/messi-da-parte/lora-waveshare-sxxx.yaml" ] && [ ! -e "$FINTO/meshtasticd-rak/config.d/lora-waveshare-sxxx.yaml" ]'
controlla "SPI attivato" 'grep -qE "^dtparam=spi=on" "$FINTO/config-rak.txt"'
controlla "spi0-0cs tolto, lasciato come commento" '! grep -qE "^[[:space:]]*dtoverlay=spi0-0cs" "$FINTO/config-rak.txt" && grep -q "^# dtoverlay=spi0-0cs (tolto" "$FINTO/config-rak.txt"'
controlla "copia di sicurezza di config.txt" 'cmp -s "$FINTO/config-rak.txt.prima-di-meshtastic" "$FINTO/config-rak.txt.originale"'
controlla "ricorda lo slot 1" 'grep -q "slot 1 della scheda RAK6421" /tmp/pi-run-rak.log'
controlla "riavvio chiesto" 'grep -q RIAVVIO "$STUB/chiamate.log"'
controlla "nessun errore o traceback" '! grep -q -e "✘" -e Traceback /tmp/pi-run-rak.log'
(cd /tmp && PATH="$STUB:$PATH" bash "$INST" --rimuovi </dev/null > /tmp/pi-rim-rak.log 2>&1)

echo "== nessuna radio collegata e nessuna scheda indicata"
(cd /tmp && PATH="$STUB:$PATH" MESH2TAK_SALTA_SISTEMA=1 bash "$INST" "$PKI/meshtastic-cnsas-integration.zip" </dev/null > /tmp/pi-run-niente.log 2>&1); UN=$?
controlla "si ferma subito con una spiegazione" '[ $UN = 1 ] && grep -q "Non vedo nessuna radio Meshtastic collegata via USB" /tmp/pi-run-niente.log'
controlla "senza toccare niente" '[ ! -e /etc/mesh2tak/client.pem ] && ! id mesh2tak >/dev/null 2>&1'

pkill -f 'fake_node[.]py --porta 440[34]'; pkill -f 'fake_tak[.]py --porta 8089'
if [ $ERR = 0 ]; then echo "TUTTO OK"; else
  for f in /tmp/pi-run1.log /tmp/pi-run2.log /tmp/pi-run3.log /tmp/pi-run-usb.log "$STUB/servizio.log"; do echo "--- $f"; cat "$f"; done; fi
exit $ERR
