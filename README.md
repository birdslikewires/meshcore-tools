# meshcore-tools

Tools for working with MeshCore mesh radio networks.

## decoder

An MQTT bridge that receives raw MeshCore packets from a local broker, decodes them, and publishes structured JSON to a main broker.

### How it works

- Subscribes to `meshcore/#` on a **local** MQTT broker (e.g. Mosquitto running on the same Pi as your MeshCore gateway)
- Decodes each packet using the [`meshcoredecoder`](https://pypi.org/project/meshcoredecoder/) library
- Publishes decoded JSON to a **main** broker on typed topics: `meshcore/advert`, `meshcore/message`, `meshcore/ack`, etc.
- Deduplicates packets within a 10-minute window

### Requirements

- Python 3.8+
- A local MQTT broker (Mosquitto recommended)

### Install

Clone the repo, create a virtual environment, and install dependencies:

```bash
sudo git clone https://github.com/yourusername/meshcore-tools /opt/meshcore-tools
sudo chown -R $USER:$USER /opt/meshcore-tools
python3 -m venv /opt/meshcore-tools/venv
/opt/meshcore-tools/venv/bin/pip install -r /opt/meshcore-tools/decoder/requirements.txt
```

### Configure

Copy the example env file and edit it:

```bash
cp /opt/meshcore-tools/decoder/decoder.env.example /opt/meshcore-tools/decoder/decoder.env
nano /opt/meshcore-tools/decoder/decoder.env
```

| Variable | Default | Description |
|---|---|---|
| `INPUT_BROKER` | `mqtt://localhost:1884` | Local broker receiving raw packets |
| `INPUT_MQTT_USER` | _(none)_ | Username for local broker (if required) |
| `INPUT_MQTT_PASS` | _(none)_ | Password for local broker (if required) |
| `OUTPUT_BROKER` | `mqtt://192.168.11.10:1883` | Main broker to publish decoded messages to |
| `OUTPUT_MQTT_USER` | `mqtt` | Username for main broker |
| `OUTPUT_MQTT_PASS` | `mqtt` | Password for main broker |
| `PUBLIC_SECRET` | _(built-in default)_ | Base64-encoded public channel secret |
| `HASHTAGS` | `test,northwest` | Comma-separated hashtag channels to monitor |

### Mosquitto configuration

To accept raw packet input from localhost only, while allowing external clients to subscribe read-only, use two listeners:

**`/etc/mosquitto/conf.d/meshcore.conf`**
```
per_listener_settings true

# Localhost only — full publish + subscribe
listener 1884 127.0.0.1
allow_anonymous true

# All interfaces — subscribe only
listener 1883
allow_anonymous true
acl_file /etc/mosquitto/acl_readonly.conf
```

**`/etc/mosquitto/acl_readonly.conf`**
```
topic read #
```

Then restart Mosquitto:

```bash
sudo systemctl restart mosquitto
```

### Run manually (for testing)

```bash
set -a && source /opt/meshcore-tools/decoder/decoder.env && set +a
/opt/meshcore-tools/venv/bin/python /opt/meshcore-tools/decoder/decode.py
```

### Install as a system service

```bash
sudo cp /opt/meshcore-tools/decoder/meshcore-decoder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshcore-decoder
```

Check logs:

```bash
journalctl -u meshcore-decoder -f
```

### Output format

All messages share a common set of fields:

| Field | Description |
|---|---|
| `type` | Packet type name (e.g. `advert`, `message`, `ack`) |
| `receivedTimestamp` | ISO timestamp from the gateway |
| `receivedUnix` | Unix timestamp (seconds) |
| `date`, `time` | Date and time strings from the gateway |
| `packetHash` | Unique packet hash (used for deduplication) |
| `path` | Array of path hop hashes |
| `hops` | Number of hops |
| `pathHashBytes` | Byte length of path hashes |
| `snr` | Signal-to-noise ratio (dB) |
| `rssi` | Received signal strength (dBm) |
| `score` | Link score |
| `decoded` | Full raw decoded payload from the library |

Additional fields by type:

**`meshcore/advert`** — `publicKey`, `signatureValid`, `name`, `lat`, `lon`, `deviceRole`, `deviceRoleName`

**`meshcore/message`** — `channel`, `channelHash`, `sentUnix`, `propagationSecs`, `sender`, `message`
