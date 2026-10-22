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
- **Sicurezza**: ogni pacchetto è firmato HMAC-SHA256 con `mesh.key`; nodi con chiavi
  diverse si ignorano. I dati sono firmati ma **non cifrati**: via internet usa HTTPS
  (es. dietro un reverse proxy) o una VPN (WireGuard, Tailscale).

## Pannello

- **Mappa** (vista principale): ogni nodo è una *stanza*, trascinabile per disporla come
  nella realtà; dentro ci sono i dispositivi presenti ora. La stanza tratteggiata
  **Fuori portata** raccoglie chi è uscito dalla mesh, così entrate e uscite sono frecce
  come gli spostamenti. Le frecce tra stanze mostrano i flussi del periodo scelto.
- **Percorso**: clic su un dispositivo → frecce numerate con orari sulla mappa, elenco
  delle permanenze e un **time-lapse** a corsie (una per stanza) sull'asse del tempo.
- **Nomi**: si può dare un nome a un dispositivo; viaggia nella mesh e lo vedono tutti.
- **Dispositivi** (vista secondaria): tabella di nodi e dispositivi con RSSI per nodo.

### Stanze e storia

Ogni nodo calcola, dalla vista della mesh, in quale stanza si trova ogni dispositivo
(il nodo che lo sente meglio, con un margine di `history.room_margin` dB per non farlo
rimbalzare) e salva permanenze e spostamenti in SQLite (`<state_dir>/btedge.db`,
conservati `history.retention_days` giorni). Scrive solo ai cambi di stanza più un flush
ogni `history.flush_interval` secondi, per non consumare la SD. Poiché tutti i nodi
ricevono gli stessi dati, ogni pannello racconta la stessa storia (da quando quel nodo è
attivo).

## Installazione (Armbian / Debian)

```sh
git clone https://github.com/francescobianco/btedge /opt/btedge
sudo /opt/btedge/deploy/install.sh
```

Il pannello è su `http://<ip-del-nodo>:4646`. La configurazione è in
`/etc/btedge/config.json` (vedi `deploy/config.example.json`); per far parte della
stessa mesh i nodi devono avere la stessa `mesh.key`. Per aggiornare:
`cd /opt/btedge && sudo git pull && sudo systemctl restart btedge`.

Su macchine con VPN o molti bridge (Docker) imposta `mesh.lan.interface` (es. `"eth0"`),
altrimenti il multicast segue la rotta di default e può finire nel tunnel. Per usare
una configurazione già pronta: `sudo deploy/install.sh /percorso/config.json`.

## Sviluppo

```sh
python3 -m btedge --fake --port 4646   # dispositivi simulati, nessun Bluetooth richiesto
python3 -m unittest discover -s tests
```

Due istanze `--fake` sulla stessa LAN (porte diverse) si trovano via multicast. Per
simulare dispositivi che si spostano tra stanze, avvia N nodi fake con
`"scanner": {"backend": "fake", "fake_room": i, "fake_rooms": N}` (i = 0…N-1), usando
una `mesh.key` e una `mesh.lan.port` diverse da quelle della mesh reale.

## API

| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/` | pannello web |
| GET | `/api/state` | nodi, dispositivi aggregati, stato scanner e mesh |
| GET | `/api/rooms?window=s` | stanze, dispositivi presenti, flussi e movimenti del periodo |
| GET | `/api/journey?address=…&window=s` | permanenze e spostamenti di un dispositivo |
| POST | `/api/label` | `{"address", "label"}`: nome condiviso nella mesh |
| POST | `/api/layout` | `{"node_id", "x", "y"}`: posizione di una stanza sulla mappa |
| GET | `/api/health` | `{"ok": true}` |
| POST | `/api/mesh/sync` | scambio di stato tra nodi (pacchetto binario firmato) |

## Roadmap

- Trasporto mesh **via Bluetooth** (GATT o advertising esteso) per nodi senza rete IP.
- **Zone/aree** composte da più nodi e posizione più fine (trilaterazione).
- Condividere la storia tra nodi, così un nodo nuovo vede anche il passato.
- Eventi di ingresso/uscita da un'area, webhook e MQTT.
- Gestione degli indirizzi BLE casuali (rotazione) tramite fingerprint dei payload.
