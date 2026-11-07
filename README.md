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
- **Sicurezza: la mesh appartiene a una chiave SSH.** `mesh.owner` è la chiave
  pubblica SSH del proprietario; `mesh.credential` è la sua firma SSH (`ssh-keygen -Y
  sign`, namespace `btedge-mesh`) su un messaggio fisso, prodotta con
  `python3 -m btedge credential` sulla macchina che ha la chiave (passa da ssh-agent: la
  chiave privata non esce mai). Le firme RSA/Ed25519 sono deterministiche, quindi chi sa
  firmare allo stesso modo ottiene la stessa credenziale. All'avvio ogni nodo verifica la
  credenziale contro `mesh.owner` e, se non è del proprietario, non parte; la chiave
  HMAC-SHA256 che firma ogni pacchetto è derivata dalla credenziale, quindi senza di
  essa non si entra (i pacchetti estranei vengono rifiutati). La credenziale va tenuta
  segreta come una password. I dati sono firmati ma **non cifrati**: via internet usa
  HTTPS o una VPN.

## Pannello

Elenco dei dispositivi di tutta la mesh, a pagina intera: filtri (tutti / con nome /
connessi), ricerca, filtro per nodo nella barra laterale. Un clic su un dispositivo apre
il dettaglio: produttore, servizi, nodi che lo vedono con RSSI, e il **nome** da dare al
dispositivo (viaggia nella mesh, lo vedono tutti; salvato in `<state_dir>/labels.json`).

Per tenere connesso un dispositivo associato (es. uno smartwatch) mettilo in
`scanner.keep_connected`: il nodo lo riconnette ogni volta che cade.

## Installazione (Armbian / Debian)

```sh
git clone https://github.com/francescobianco/btedge /opt/btedge
sudo /opt/btedge/deploy/install.sh
```

Il pannello è su `http://<ip-del-nodo>:4646`. La configurazione è in
`/etc/btedge/config.json` (vedi `deploy/config.example.json`); per far parte della
stessa mesh i nodi devono avere gli stessi `mesh.owner` e `mesh.credential`. Per aggiornare:
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
| `yoho` | bande Yoho Sports / uThink, es. ID115 Plus (`MSF0`), servizio `cc00` ([id115](https://github.com/francescobianco/id115)) | Fai vibrare (`08 01` su `cc06`) |
| `immediate_alert` | qualsiasi dispositivo con il profilo standard Find Me (servizio `1802`) | Fai vibrare (Alert Level `2A06` = 2) |

Un'azione cliccata su qualsiasi pannello diventa un comando nello stato firmato del
nodo, indirizzato al nodo che sente meglio il dispositivo (prima chi è già connesso):
quel nodo lo esegue via GATT e pubblica l'esito, che torna al pannello attraverso la
mesh. Funziona anche dal nodo di controllo su internet verso un nodo dietro NAT
(qualche secondo, al ritmo del gossip). Solo i membri della mesh possono dare comandi.

Per aggiungere una famiglia di dispositivi: un modulo in `btedge/adapters/` (classe
con `name`, `actions` e `run(gatt, action)`), registrato in `ADAPTERS`, e una regola
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

Username, password del pannello e credenziale della mesh vengono chiesti al primo deploy e conservati sul
server (`--ask all` per cambiarli). In container la config arriva da variabili
d'ambiente: `BTEDGE_ROLE`, `BTEDGE_NODE_NAME`, `BTEDGE_PUBLIC_URL`, `BTEDGE_MESH_OWNER`, `BTEDGE_MESH_CREDENTIAL`,
`BTEDGE_PEERS` (separati da virgola), `BTEDGE_LAN`, `BTEDGE_ADMIN_USER`,
`BTEDGE_ADMIN_PASSWORD`, `BTEDGE_PORT`, `BTEDGE_STATE_DIR`.

## Sviluppo

```sh
python3 -m btedge --fake --port 4646   # dispositivi simulati, nessun Bluetooth richiesto
python3 -m unittest discover -s tests
```

Anche in sviluppo servono `mesh.owner` e `mesh.credential` (`BTEDGE_MESH_OWNER`,
`BTEDGE_MESH_CREDENTIAL`): per non toccare la mesh reale usa una chiave SSH di prova e
una `mesh.lan.port` diversa. Due istanze `--fake` sulla stessa LAN si trovano via multicast.

## API

| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/` | pannello web |
| GET | `/api/state` | nodi, dispositivi aggregati, stato scanner e mesh |
| POST | `/api/label` | `{"address", "label"}`: nome condiviso nella mesh |
| POST | `/api/action` | `{"address", "action"}`: esegue un'azione dell'adapter; risponde `{"id", "node"}` |
| GET | `/api/action?id=…` | esito: `pending`, `running`, `ok` o `error` |
| GET | `/api/health` | `{"ok": true}` |
| POST | `/api/mesh/sync` | scambio di stato tra nodi (pacchetto binario firmato) |

## Roadmap

- Trasporto mesh **via Bluetooth** (GATT o advertising esteso) per nodi senza rete IP.
- **Zone/aree** composte da più nodi e posizione più fine (trilaterazione).
- Eventi di ingresso/uscita da un'area, webhook e MQTT.
- Gestione degli indirizzi BLE casuali (rotazione) tramite fingerprint dei payload.
