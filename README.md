# btedge

Nodo Bluetooth "edge" per schede piccole (Orange Pi Zero, Raspberry Pi…): rileva i
dispositivi Bluetooth nelle vicinanze, li mostra in un pannello web e condivide quello
che vede con gli altri nodi, formando una **mesh** che copre un'area più ampia.

Scritto in Python 3 con la sola libreria standard più `python3-dbus` / `python3-gi`
(già presenti su Armbian/Debian). Gira in ~25 MB di RAM.

## Come funziona

```
  [dispositivi BT]        [dispositivi BT]              [dispositivi BT]
        │ BLE scan              │                             │
   ┌────┴─────┐  multicast ┌────┴─────┐   HTTP push-pull  ┌───┴──────┐
   │  nodo A  │◄──────────►│  nodo B  │◄─────────────────►│  nodo C  │
   │ (casa)   │    LAN     │ (casa)   │  mesh over IP     │ (ufficio)│
   └──────────┘            └──────────┘    (internet)     └──────────┘
        ▲      beacon BLE      ▲
        └──────────────────────┘  i nodi si "vedono" anche via radio
```

- **Scansione**: BlueZ via D-Bus, discovery continua (BLE + classic). Ogni dispositivo
  resta in lista `device_ttl` secondi dopo l'ultimo avvistamento; quelli connessi restano
  finché sono connessi.
- **Beacon BLE**: ogni nodo trasmette un advertisement con service data
  `62746564-6765-4e4f-8000-6d6573680001` contenente il suo `node_id`. Così un nodo sa quali
  altri nodi sono in portata radio e con che RSSI (colonna "Radio BT" nel pannello).
- **Mesh (gossip)**: ogni nodo è l'unica fonte autorevole dei propri avvistamenti. Lo stato
  di un nodo ha un numero di sequenza che solo lui incrementa; gli altri tengono la copia
  più recente e la inoltrano (fino a `max_hops`). Le età sono relative ("visto N secondi
  fa"), quindi non servono orologi sincronizzati.
- **Trasporti**:
  - **LAN**: UDP multicast `239.255.66.77:47474`, zero configurazione.
  - **IP / internet**: `POST /api/mesh/sync` verso i `peers` configurati; la risposta
    contiene lo stato del peer (push-pull). Un nodo dietro NAT si collega a un nodo
    pubblico, che inoltra il suo stato al resto della mesh.
- **Sicurezza: solo il proprietario crea nodi.** La mesh è identificata dalla chiave
  pubblica SSH del proprietario (`mesh.owner`). Ogni nodo genera al primo avvio una sua
  chiave Ed25519 (`<state_dir>/node.key`, non esce mai dalla macchina; l'id del nodo
  deriva dalla chiave) e resta fuori dalla mesh finché il proprietario non lo accredita:

  ```sh
  python3 -m btedge enroll root@orangepi          # o la cartella di stato di un nodo locale
  ```

  `enroll` legge la richiesta del nodo (id, chiave pubblica, impronta della macchina da
  `/etc/machine-id`), la firma con `ssh-keygen -Y sign` (via ssh-agent: la chiave privata
  del proprietario non esce mai) e scrive `<state_dir>/node.cert`; il nodo entra entro
  pochi secondi, senza riavvio. Il certificato **non è portabile**: senza la chiave
  privata del nodo non serve a niente, e copiando la cartella di stato su un'altra
  macchina il nodo lo rifiuta. Ogni nodo firma il proprio stato e ogni pacchetto; gli
  altri li accettano solo con un certificato valido del proprietario. Non c'è nessun
  segreto condiviso. I dati sono firmati ma **non cifrati**: via internet usa HTTPS o
  una VPN.

## Pannello

Elenco dei dispositivi di tutta la mesh, a pagina intera: filtri (tutti / con nome /
connessi), ricerca, filtro per nodo nella barra laterale. Un clic su un dispositivo apre
il dettaglio: produttore, servizi, nodi che lo vedono con RSSI, e il **nome** da dare al
dispositivo (viaggia nella mesh, lo vedono tutti; salvato in `<state_dir>/labels.json`).

Il tema chiaro/scuro si cambia dalla barra laterale (all'inizio segue quello del sistema).

### Connessione automatica

Nella scheda di un dispositivo, **Connetti automaticamente** lo segna per tutta la mesh:
l'informazione viene distribuita a tutti i nodi e il nodo che lo rileva si connette da
solo, attraverso la radio che lo sente meglio, e lo riconnette quando cade. Se un altro
nodo lo tiene già connesso gli altri non ci provano. L'elenco dei dispositivi segnati
(anche di quelli fuori portata) è in **Automazione**, da dove si possono togliere.

`scanner.keep_connected` nella config resta, per i dispositivi che un nodo preciso deve
tenere connessi a prescindere dalla mesh.

### Sveglie e ora dei braccialetti

Per i dispositivi che le hanno (oggi le bande Yoho / ID115 Plus) la scheda mostra la
griglia **Sveglie**, come `alarms.txt` di [id115](https://github.com/francescobianco/id115):
una riga per giorno (lun–dom) e una colonna per orario. Le colonne crescono una alla
volta fino al numero di orari che quel dispositivo può tenere: 3 sull'ID115 Plus, di più
(fino a 16) per gli adapter che dichiarano un `alarms` più alto. Ogni modifica viene salvata subito nella mesh.

Il braccialetto non permette di leggere le sveglie che ha, quindi la verità è lo **stato
desiderato** tenuto dalla mesh. Il bottone **Applica ora e sveglie** (accanto a "Fai
vibrare") lo scrive sul dispositivo: corregge l'ora e programma tutte le sveglie. Sotto
la griglia si vede se il dispositivo è allineato ("Applicate…") o se ci sono modifiche
"Da applicare". Finché le sveglie non sono mai state impostate dalla mesh il bottone
corregge solo l'ora e lascia quelle che il braccialetto ha già.

L'ora viene scritta nel fuso orario del browser da cui si sono impostate le sveglie (o,
senza sveglie, in quello del nodo che esegue). Per riapplicare da soli a ogni
connessione basta uno snippet: `host.onConnect(d => d.action("sync"))`.

### Snippet di automazione

In **Automazione** si scrivono gli snippet: piccoli script in un JavaScript minimale,
distribuiti a tutti i nodi ed eseguiti da ogni nodo con Bluetooth sugli eventi che vede.

```js
host.onReady(() => {
  log("pronto su " + host.name)
})

host.onDevice(device => {
  if (device.class == "wristband" && device.rssi > -70) {
    device.action("vibrate")
  }
})

host.onDisconnect = function (device) { log("perso", device.label || device.name) }
```

| Evento | Quando |
|---|---|
| `host.onReady(fn)` | lo snippet è stato caricato (o modificato) sul nodo |
| `host.onDevice(fn)` | un dispositivo è entrato in portata del nodo |
| `host.onDeviceLost(fn)` | non lo si sente più |
| `host.onConnect(fn)` / `host.onDisconnect(fn)` | si è connesso al nodo / disconnesso |
| `host.every(secondi, fn)` | periodicamente (minimo 5 s) |

- `host`: `name`, `id`, `role`, `radios`. Per limitare uno snippet a un nodo:
  `if (host.name == "orangepi") { ... }`.
- `device`: `address`, `name`, `label` (il nome dato nel pannello), `rssi`, `connected`,
  `paired`, `radio`, `manufacturer`, `uuids`, `class` e `model` (dal profilo),
  `autoconnect`; azioni `device.connect()`, `device.disconnect()`,
  `device.action("vibrate")` (le azioni dell'adapter). Le azioni vengono accodate ed
  eseguite in ordine dopo l'handler, che quindi non si blocca mai.
- `log(...)`: le ultime righe e l'ultimo errore di ogni nodo si vedono accanto allo
  snippet nel pannello (e nel log del nodo).
- Linguaggio: `let`/`const`, `if`/`else`, `return`, funzioni freccia e `function`,
  numeri, stringhe, `true`/`false`/`null`, liste, `+ - * / %`, confronti, `&& || !`,
  `.length`, `.includes()`, `.startsWith()`, `.endsWith()`, `.toLowerCase()`,
  `.toUpperCase()`, `.trim()`. **Niente cicli, import o accesso al sistema**: uno
  snippet può fare solo ciò che `host` e `device` offrono, e ogni esecuzione ha un
  tetto di passi. Gli errori di sintassi vengono segnalati al salvataggio, con la riga.

Snippet e connessioni automatiche sono *impostazioni condivise*: ogni nodo adotta la
versione più recente che sente, la salva in `<state_dir>/shared.json` e la ripubblica,
quindi arrivano a tutti e restano anche se il nodo che le ha scritte sparisce. Come per
le azioni, può scriverle chiunque abbia accesso a un pannello della mesh.

### Più radio sullo stesso nodo

Un nodo usa **tutti gli adattatori Bluetooth** dell'host (quello integrato, più i
dongle): scansiona e trasmette il beacon su ognuno, anche se vengono inseriti o tolti a
nodo acceso. Un dispositivo sentito da più radio resta un solo dispositivo, riportato
con la radio che lo tiene connesso o che lo sente meglio (visibile in "Visto da"); le
connessioni e le azioni passano da quella. Per usarne solo alcune:
`"scanner": {"adapter": "hci0"}` oppure `["hci0", "hci1"]`. La modalità Bluetooth
(attivo / rilasciato / spento) vale per tutte le radio del nodo.

### Bluetooth di un nodo: attivo, rilasciato, spento

Selezionando un nodo nella barra laterale compare il selettore **Bluetooth**:

| Modalità | Cosa fa btedge | Radio dell'host |
|---|---|---|
| **Attivo** | scansione, beacon, riconnessione dei `keep_connected`, azioni | accesa, usata da btedge |
| **Rilasciato** | niente: ferma la scansione, toglie il beacon, non riconnette, rifiuta le azioni | accesa, **a disposizione dell'host** (sviluppo, altri applicativi) |
| **Spento** | come sopra | spenta (`Powered = false` su BlueZ) |

In "Rilasciato" btedge toglie solo ciò che ha avviato lui: l'adattatore resta acceso e
i dispositivi già connessi restano connessi. Il nodo resta nella mesh, senza dispositivi,
con la dicitura "BT rilasciato" / "BT spento". Il comando viaggia nella mesh come le
azioni, quindi funziona da qualsiasi pannello, anche dal nodo di controllo verso un nodo
dietro NAT.

Le stesse cose si fanno **dall'host**, con il comando `btedge` messo dall'installer:

```sh
sudo btedge bluetooth release   # lascia la radio all'host
sudo btedge bluetooth on        # btedge se la riprende
sudo btedge bluetooth off       # spegne la radio
sudo btedge bluetooth           # stato
```

La modalità è il file `<state_dir>/bluetooth_mode`, che il nodo tiene d'occhio: il comando
ha effetto entro un secondo, senza login né riavvio, e la scelta sopravvive ai riavvii
(a nodo fermo decide come ripartirà).

Su macOS "Spento" non c'è (nessuna API pubblica per spegnere la radio) e in "Rilasciato"
il nodo ignora ciò che sente mentre l'helper continua la scansione, che lì non toglie
nulla alle altre applicazioni. Il comando è `~/.btedge/bin/btedge`.

## Installazione (Armbian / Debian)

```sh
git clone https://github.com/francescobianco/btedge /opt/btedge
sudo /opt/btedge/deploy/install.sh
```

Il pannello è su `http://<ip-del-nodo>:4646`. La configurazione è in
`/etc/btedge/config.json` (vedi `deploy/config.example.json`); per far parte della
stessa mesh i nodi devono avere lo stesso `mesh.owner` ed essere accreditati con
`btedge enroll` (lo ricorda anche l'installer). L'installer mette anche il comando
`btedge` in `/usr/local/bin` (equivale a `python3 -m btedge` dal checkout). Per aggiornare:
`cd /opt/btedge && sudo git pull && sudo systemctl restart btedge`.

Su macchine con VPN o molti bridge (Docker) imposta `mesh.lan.interface` (es. `"eth0"`),
altrimenti il multicast segue la rotta di default e può finire nel tunnel. Per usare
una configurazione già pronta: `sudo deploy/install.sh /percorso/config.json`.

## Profili, adapter e azioni

Ogni dispositivo viene **profilato** con la rubrica `btedge/profiles.json` (più le
regole locali in `<state_dir>/profiles.json`, che hanno la precedenza): regole ordinate
che riconoscono il dispositivo da nome (`name_prefix`, `name_regex`), servizi GATT
(`services`), produttore (`manufacturer`) e icona BlueZ (`icon`), e gli assegnano un
modello, una **classe** (smartwatch, braccialetto, localizzatore, telefono…) e un
**adapter**. La rubrica si fa crescere aggiungendo regole.

Gli adapter (`btedge/adapters/`) contengono il protocollo di ogni famiglia di
dispositivi e offrono le **azioni** che compaiono come bottoni nel pannello laterale:

| Adapter | Dispositivi | Azioni |
|---|---|---|
| `yoho` | bande Yoho Sports / uThink, es. ID115 Plus (`MSF0`), servizio `cc00` ([id115](https://github.com/francescobianco/id115)) | Fai vibrare (`08 01` su `cc06`); Applica ora e sveglie (`04 00 …` e `0C …`), 3 orari |
| `immediate_alert` | qualsiasi dispositivo con il profilo standard Find Me (servizio `1802`) | Fai vibrare (Alert Level `2A06` = 2) |

Un'azione cliccata su qualsiasi pannello diventa un comando nello stato firmato del
nodo, indirizzato al nodo che sente meglio il dispositivo (prima chi è già connesso):
quel nodo lo esegue via GATT e pubblica l'esito, che torna al pannello attraverso la
mesh. Funziona anche dal nodo di controllo su internet verso un nodo dietro NAT
(qualche secondo, al ritmo del gossip). Solo i membri della mesh possono dare comandi.

Per aggiungere una famiglia di dispositivi: un modulo in `btedge/adapters/` (classe
con `name`, `actions` e `run(gatt, action, settings)`; `alarms = N` se ha N sveglie), registrato in `ADAPTERS`, e una regola
in `profiles.json` che punta all'adapter.

## Nodi di controllo

Un nodo con `"role": "control"` non ha Bluetooth: non scansiona, non vede dispositivi, fa da **hub IP** della mesh (gli edge dietro NAT lo mettono nei `peers`) e ospita
il pannello di amministrazione dei dispositivi. Il pannello richiede sempre il login
(HTTP Basic, `admin.user` / `admin.password`); `/api/health` e `/api/mesh/sync` (già
firmato HMAC) restano aperti. Il pannello ("Collega un nodo edge") mostra la
config da dare ai nuovi nodi.

Il nodo di controllo pubblico è `btedge.yafb.net`, deployato con
[vps-supervisor](../vps-supervisor):

```sh
../vps-supervisor/vps-supervisor deploy deploy/btedge.yafb.net.yml
```

L'immagine Docker non contiene nessuna identità: la chiave del nodo nasce al primo avvio
nel volume `/var/lib/btedge-control` del server, quindi un altro container dalla stessa
immagine è un nodo nuovo, non accreditato. Il certificato è legato anche al
`/etc/machine-id` dell'host (montato in sola lettura): copiare il volume su un altro
server non basta. Dopo il primo deploy, una volta sola:

```sh
python3 -m btedge enroll root@82.165.241.56 --state-dir /var/lib/btedge-control
```

Username e password del pannello vengono chiesti al primo deploy e conservati sul
server (`--ask all` per cambiarli). La password si può cambiare anche dalle impostazioni
del pannello (⚙): viene salvata con hash in `<state_dir>/admin_password.hash` e da quel
momento ha la precedenza su config e variabili d'ambiente, anche dopo un nuovo deploy.
Se la si dimentica, cancellando quel file torna valida la password della config. In container la config arriva da variabili
d'ambiente: `BTEDGE_ROLE`, `BTEDGE_NODE_NAME`, `BTEDGE_PUBLIC_URL`, `BTEDGE_MESH_OWNER`,
`BTEDGE_PEERS` (separati da virgola), `BTEDGE_LAN`, `BTEDGE_ADMIN_USER`,
`BTEDGE_ADMIN_PASSWORD`, `BTEDGE_PORT`, `BTEDGE_STATE_DIR`.

## macOS

Con `deploy/install-macos.sh` btedge gira come LaunchAgent dell'utente (niente sudo, serve
Xcode Command Line Tools), con `"scanner": {"backend": "corebluetooth"}` nella config
`~/.btedge/config.json`. macOS chiede il permesso Bluetooth al primo avvio, una sola volta.
Se la richiesta è stata rifiutata o persa (il nodo non vede nessun dispositivo e il log dice
"Bluetooth not allowed"), `deploy/macos/allow-bluetooth.sh` la fa ricomparire: azzera la
risposta, riavvia il nodo, apre la pagina giusta delle impostazioni e aspetta finché il
nodo scansiona. Il clic su "Consenti" resta tuo: macOS non lo lascia fare a uno script.
Da un'altra macchina: `ssh utente@mac 'sh -s' < deploy/macos/allow-bluetooth.sh`.
La scansione la fa `BtedgeBluetooth.app` (`deploy/macos/`), un piccolo helper firmato che
avvia btedge: il Python di sistema non può dichiarare il permesso.

Limiti rispetto a BlueZ: CoreBluetooth non espone i MAC, quindi un dispositivo ha un UUID
valido solo su quel Mac e, se lo sente anche un nodo Linux, nella mesh compare due volte.
Le azioni sui dispositivi, le connessioni (anche automatiche) e il beacon BLE non sono supportati.

## Sviluppo

```sh
python3 -m btedge --fake --port 4646   # dispositivi simulati, nessun Bluetooth richiesto
python3 -m unittest discover -s tests
```

Anche in sviluppo serve `mesh.owner` (`BTEDGE_MESH_OWNER`) e i nodi vanno accreditati
(`python3 -m btedge enroll <state_dir>`): per non toccare la mesh reale usa una chiave
SSH di prova e una `mesh.lan.port` diversa. Due istanze `--fake` sulla stessa LAN si trovano via multicast.

## API

| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/` | pannello web |
| GET | `/api/state` | nodi, dispositivi aggregati, stato scanner e mesh |
| POST | `/api/label` | `{"address", "label"}`: nome condiviso nella mesh |
| POST | `/api/alarms` | `{"address", "slots": [{"time": "07:00", "days": [0..6]}], "tz"}` (0 = lunedì): sveglie volute su un dispositivo; le applica l'azione `sync` |
| POST | `/api/autoconnect` | `{"address", "enabled"}`: connessione automatica, per tutta la mesh |
| POST | `/api/snippet` | `{"id"?, "name", "code", "enabled"}` crea o modifica uno snippet (400 con l'errore di sintassi); `{"id", "delete": true}` lo elimina |
| POST | `/api/password` | `{"current", "new"}`: cambia la password del pannello |
| POST | `/api/action` | `{"address", "action"}`: esegue un'azione dell'adapter; risponde `{"id", "node"}` |
| POST | `/api/bluetooth` | `{"node", "mode"}` con `mode` = `on`, `released` o `off`: modalità Bluetooth di un nodo; risponde `{"id", "node"}`, esito da `/api/action?id=…` |
| GET | `/api/action?id=…` | esito: `pending`, `running`, `ok` o `error` |
| GET | `/api/health` | `{"ok": true}` |
| POST | `/api/mesh/sync` | scambio di stato tra nodi (pacchetto binario firmato) |

## Roadmap

- Trasporto mesh **via Bluetooth** (GATT o advertising esteso) per nodi senza rete IP.
- **Zone/aree** composte da più nodi e posizione più fine (trilaterazione).
- Eventi di ingresso/uscita da un'area, webhook e MQTT (anche dagli snippet).
- Modalità Bluetooth per singola radio (lasciare un dongle allo sviluppo, tenere l'altro in scansione).
- Gestione degli indirizzi BLE casuali (rotazione) tramite fingerprint dei payload.
