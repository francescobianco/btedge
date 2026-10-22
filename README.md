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

## Installazione (Armbian / Debian)

```sh
git clone https://github.com/francescobianco/btedge /opt/btedge
sudo /opt/btedge/deploy/install.sh
```

Il pannello è su `http://<ip-del-nodo>:4646`. La configurazione è in
`/etc/btedge/config.json` (vedi `deploy/config.example.json`); per far parte della
stessa mesh i nodi devono avere la stessa `mesh.key`. Per aggiornare:
`cd /opt/btedge && sudo git pull && sudo systemctl restart btedge`.

## Sviluppo

```sh
python3 -m btedge --fake --port 4646   # dispositivi simulati, nessun Bluetooth richiesto
python3 -m unittest discover -s tests
```

Due istanze `--fake` sulla stessa LAN (porte diverse) si trovano via multicast.

## API

| Metodo | Percorso | Descrizione |
|---|---|---|
| GET | `/` | pannello web |
| GET | `/api/state` | nodi, dispositivi aggregati, stato scanner e mesh |
| GET | `/api/health` | `{"ok": true}` |
| POST | `/api/mesh/sync` | scambio di stato tra nodi (pacchetto binario firmato) |

## Roadmap

- Trasporto mesh **via Bluetooth** (GATT o advertising esteso) per nodi senza rete IP.
- **Zone/aree**: assegnare i nodi ad aree e calcolare in quale area si trova un
  dispositivo (RSSI migliore, trilaterazione).
- Eventi di ingresso/uscita da un'area, webhook e MQTT.
- Gestione degli indirizzi BLE casuali (rotazione) tramite fingerprint dei payload.
