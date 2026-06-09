#!/usr/bin/env python3
import base64
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
from meshcoredecoder import MeshCoreDecoder
from meshcoredecoder.crypto import MeshCoreKeyStore
from meshcoredecoder.types.crypto import DecryptionOptions
from meshcoredecoder.types.enums import PayloadType

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# --- Config ---
INPUT_BROKER      = os.environ.get('INPUT_BROKER',      'mqtt://localhost:1884')
OUTPUT_BROKER     = os.environ.get('OUTPUT_BROKER',     'mqtt://192.168.11.10:1883')
INPUT_USER        = os.environ.get('INPUT_MQTT_USER')
INPUT_PASS        = os.environ.get('INPUT_MQTT_PASS')
OUTPUT_USER       = os.environ.get('OUTPUT_MQTT_USER',  'mqtt')
OUTPUT_PASS       = os.environ.get('OUTPUT_MQTT_PASS',  'mqtt')
PUBLIC_SECRET_B64 = os.environ.get('PUBLIC_SECRET',     'izOH6cXN6mrJ5e26oRXNcg==')
HASHTAGS          = [t.strip() for t in os.environ.get('HASHTAGS', 'test,northwest').split(',') if t.strip()]

# --- Key store ---
public_secret_hex = base64.b64decode(PUBLIC_SECRET_B64).hex()
channel_secrets = [
    public_secret_hex,
    *[hashlib.sha256('#{}'.format(tag).encode()).hexdigest()[:32] for tag in HASHTAGS],
]
key_store = MeshCoreKeyStore({'channel_secrets': channel_secrets})
decode_options = DecryptionOptions(key_store=key_store)


PAYLOAD_TYPE_NAMES = {
    PayloadType.Request:     'request',
    PayloadType.Response:    'response',
    PayloadType.TextMessage: 'text_message',
    PayloadType.Ack:         'ack',
    PayloadType.Advert:      'advert',
    PayloadType.GroupText:   'group_text',
    PayloadType.GroupData:   'group_data',
    PayloadType.AnonRequest: 'anon_request',
    PayloadType.Path:        'path',
    PayloadType.Trace:       'trace',
    PayloadType.Multipart:   'multipart',
    PayloadType.Control:     'control',
    PayloadType.RawCustom:   'raw_custom',
}


def channel_hash_from_secret(secret_hex):
    return hashlib.sha256(bytes.fromhex(secret_hex)).hexdigest()[:2].upper()


channel_names = {
    channel_hash_from_secret(public_secret_hex): 'Public',
    **{
        channel_hash_from_secret(
            hashlib.sha256('#{}'.format(tag).encode()).hexdigest()[:32]
        ): '#{}'.format(tag)
        for tag in HASHTAGS
    },
}

# --- Dedup cache ---
EXPIRY_S = 10 * 60
_seen = {}
_seen_lock = threading.Lock()


def has_seen(h):
    with _seen_lock:
        ts = _seen.get(h)
        if ts is None:
            return False
        if time.monotonic() - ts > EXPIRY_S:
            del _seen[h]
            return False
        return True


def add_seen(h):
    with _seen_lock:
        _seen[h] = time.monotonic()


def _cleanup_seen():
    while True:
        time.sleep(EXPIRY_S)
        cutoff = time.monotonic() - EXPIRY_S
        with _seen_lock:
            expired = [k for k, v in _seen.items() if v < cutoff]
            for k in expired:
                del _seen[k]


threading.Thread(target=_cleanup_seen, daemon=True).start()


# --- Helpers ---
def parse_broker(url):
    p = urlparse(url)
    return p.hostname, p.port or 1883


def to_unix(timestamp_str):
    if not timestamp_str:
        return None
    try:
        return int(datetime.fromisoformat(timestamp_str.replace('Z', '+00:00')).timestamp())
    except Exception:
        return None



# --- Output MQTT ---
out_host, out_port = parse_broker(OUTPUT_BROKER)
output_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='meshcore-decoder-out')
if OUTPUT_USER:
    output_client.username_pw_set(OUTPUT_USER, OUTPUT_PASS)


def on_output_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code.is_failure:
        log.error('Output connection failed: %s', reason_code)
    else:
        log.info('Output connected: %s', OUTPUT_BROKER)


def on_output_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code.is_failure:
        log.warning('Output disconnected unexpectedly: %s', reason_code)


output_client.on_connect = on_output_connect
output_client.on_disconnect = on_output_disconnect
output_client.connect(out_host, out_port)
output_client.loop_start()


def publish(type_name, payload):
    output_client.publish('meshcore/{}'.format(type_name), json.dumps(payload, default=str), qos=0)


def publish_advert(payload):
    msg = json.dumps(payload, default=str)
    output_client.publish('meshcore/advert', msg, qos=0)
    output_client.publish('meshcore/advert/{}'.format(payload['publicKey']), msg, qos=0, retain=True)


# --- Message handler ---
def on_message(client, userdata, msg):
    if not msg.topic.endswith('/packets'):
        return
    try:
        data = json.loads(msg.payload.decode())
        if not data.get('raw') or not data.get('hash'):
            return
        if has_seen(data['hash']):
            return
        add_seen(data['hash'])

        packet = MeshCoreDecoder.decode(data['raw'], decode_options)
        if packet is None:
            return

        path = list(getattr(packet, 'path', None) or [])
        hops = len(path)
        path_hash_bytes = len(path[0]) // 2 if path else None
        received_unix = to_unix(data.get('timestamp'))

        payload_type = getattr(packet, 'payload_type', None)
        type_name = PAYLOAD_TYPE_NAMES.get(payload_type, 'unknown_{}'.format(payload_type))

        def to_float(v):
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        common = {
            'type':              type_name,
            'receivedTimestamp': data.get('timestamp'),
            'receivedUnix':      received_unix,
            'packetHash':        data['hash'],
            'path':              path,
            'hops':              hops,
            'pathHashBytes':     path_hash_bytes,
            'snr':               to_float(data.get('SNR')),
            'rssi':              to_float(data.get('RSSI')),
            'score':             to_float(data.get('score')),
        }

        if isinstance(packet.payload, dict):
            decoded = packet.payload.get('decoded')
        else:
            decoded = getattr(packet.payload, 'decoded', None)

        # Advert
        if payload_type == PayloadType.Advert:
            app_data = getattr(decoded, 'app_data', {}) or {}
            location = app_data.get('location')
            device_role = app_data.get('device_role')
            public_key = getattr(decoded, 'public_key', None)
            signature_valid = getattr(decoded, 'signature_valid', None)
            out = dict(common)
            if public_key is not None:
                out['publicKey'] = public_key
            if signature_valid is not None:
                out['signatureValid'] = signature_valid
            name = app_data.get('name')
            if name:
                out['name'] = name
            if location:
                out['lat'] = location.get('latitude')
                out['lon'] = location.get('longitude')
            if device_role is not None:
                role_value = device_role.value if hasattr(device_role, 'value') else device_role
                role_name = device_role.name if hasattr(device_role, 'name') else str(device_role)
                out['deviceRole'] = role_value
                out['deviceRoleName'] = role_name
            if out.get('publicKey'):
                publish_advert(out)
            else:
                publish('advert', out)
            return

        # Group text
        if payload_type == PayloadType.GroupText:
            channel_hash = getattr(decoded, 'channel_hash', None)
            if channel_hash:
                channel_hash = channel_hash.upper()
            channel = channel_names.get(channel_hash, 'unknown({})'.format(channel_hash))
            decrypted = getattr(decoded, 'decrypted', None)
            decrypted_data = decrypted or {}
            sender    = decrypted_data.get('sender')
            message   = decrypted_data.get('message')
            sent_unix = decrypted_data.get('timestamp')
            propagation = (received_unix - sent_unix) if (received_unix and sent_unix) else None
            out = dict(common)
            out.update({
                'channel':         channel,
                'channelHash':     channel_hash,
                'decrypted':       decrypted is not None,
                'sentUnix':        sent_unix,
                'propagationSecs': propagation,
            })
            if sender is not None:
                out['sender'] = sender
            if message is not None:
                out['message'] = message
            publish('group_text', out)
            return

        # Encrypted direct message types we can't decrypt
        ENCRYPTED_TYPES = {
            PayloadType.TextMessage, PayloadType.Request,
            PayloadType.Response, PayloadType.AnonRequest,
        }
        if payload_type in ENCRYPTED_TYPES:
            publish(type_name, dict(common, decrypted=False))
            return

        # Everything else
        publish(type_name, common)

    except Exception as e:
        log.error('Decode error: %s', e)


# --- Input MQTT ---
in_host, in_port = parse_broker(INPUT_BROKER)
input_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='meshcore-decoder-in')
if INPUT_USER:
    input_client.username_pw_set(INPUT_USER, INPUT_PASS)


def on_input_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code.is_failure:
        log.error('Input connection failed: %s', reason_code)
    else:
        log.info('Input connected: %s', INPUT_BROKER)
        client.subscribe('meshcore/#')
        log.info('Subscribed to meshcore/#')


def on_input_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    if reason_code.is_failure:
        log.warning('Input disconnected unexpectedly: %s', reason_code)


input_client.on_connect = on_input_connect
input_client.on_disconnect = on_input_disconnect
input_client.on_message = on_message
input_client.connect(in_host, in_port)
input_client.loop_forever()
