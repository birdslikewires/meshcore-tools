const { MeshCoreDecoder } = require('@michaelhart/meshcore-decoder');
const crypto = require('crypto');
const mqtt = require('mqtt');

// --- Config from environment ---
const INPUT_BROKER  = process.env.INPUT_BROKER  || 'mqtt://localhost:1883';
const OUTPUT_BROKER = process.env.OUTPUT_BROKER || 'mqtt://192.168.11.10:1883';
const INPUT_USER    = process.env.INPUT_MQTT_USER  || undefined;
const INPUT_PASS    = process.env.INPUT_MQTT_PASS  || undefined;
const OUTPUT_USER   = process.env.OUTPUT_MQTT_USER || 'mqtt';
const OUTPUT_PASS   = process.env.OUTPUT_MQTT_PASS || 'mqtt';
const PUBLIC_SECRET = process.env.PUBLIC_SECRET   || 'izOH6cXN6mrJ5e26oRXNcg==';
const HASHTAGS      = (process.env.HASHTAGS || 'test,northwest').split(',').map(s => s.trim()).filter(Boolean);

// --- Key store ---
const publicSecretHex = Buffer.from(PUBLIC_SECRET, 'base64').toString('hex');
const channelSecrets = [
	publicSecretHex,
	...HASHTAGS.map(tag => crypto.createHash('sha256').update('#' + tag).digest('hex').slice(0, 32))
];
const keyStore = MeshCoreDecoder.createKeyStore({ channelSecrets });

const PAYLOAD_TYPES = {
	0x00: 'request',
	0x01: 'response',
	0x02: 'text_message',
	0x03: 'ack',
	0x04: 'advert',
	0x05: 'group_text',
	0x06: 'group_data',
	0x07: 'anon_request',
	0x08: 'returned_path',
	0x09: 'trace',
	0x0A: 'multipart',
	0x0B: 'control',
	0x0F: 'raw_custom'
};

const DEVICE_ROLES = {
	0: 'client',
	1: 'client_mute',
	2: 'repeater',
	3: 'room_server',
	4: 'sensor',
	5: 'kiss_modem'
};

function channelHashFromSecret(secretHex) {
	return crypto.createHash('sha256').update(Buffer.from(secretHex, 'hex')).digest('hex').slice(0, 2).toUpperCase();
}

const channelNames = {
	[channelHashFromSecret(publicSecretHex)]: 'Public',
	...Object.fromEntries(
		HASHTAGS.map(tag => {
			const secret = crypto.createHash('sha256').update('#' + tag).digest('hex').slice(0, 32);
			return [channelHashFromSecret(secret), '#' + tag];
		})
	)
};

// --- Dedup cache ---
const EXPIRY_MS = 10 * 60 * 1000;
const seen = new Map();

function addSeen(hash) { seen.set(hash, Date.now()); }
function hasSeen(hash) {
	const ts = seen.get(hash);
	if (!ts) return false;
	if (Date.now() - ts > EXPIRY_MS) { seen.delete(hash); return false; }
	return true;
}
setInterval(() => {
	const now = Date.now();
	for (const [hash, ts] of seen) if (now - ts > EXPIRY_MS) seen.delete(hash);
}, EXPIRY_MS);

// --- MQTT clients ---
const inputClient = mqtt.connect(INPUT_BROKER, {
	...(INPUT_USER && { username: INPUT_USER, password: INPUT_PASS }),
	clientId: 'meshcore-decoder-in'
});

const outputClient = mqtt.connect(OUTPUT_BROKER, {
	username: OUTPUT_USER,
	password: OUTPUT_PASS,
	clientId: 'meshcore-decoder-out'
});

function publish(type, payload) {
	const topic = `meshcore/${type}`;
	outputClient.publish(topic, JSON.stringify(payload), { qos: 0 }, (err) => {
		if (err) process.stderr.write(`Publish error [${topic}]: ${err.message}\n`);
	});
}

inputClient.on('connect', () => {
	process.stderr.write(`Input connected: ${INPUT_BROKER}\n`);
	inputClient.subscribe('meshcore/#', (err) => {
		if (err) process.stderr.write(`Subscribe error: ${err}\n`);
		else process.stderr.write('Subscribed to meshcore/#\n');
	});
});

outputClient.on('connect', () => {
	process.stderr.write(`Output connected: ${OUTPUT_BROKER}\n`);
});

inputClient.on('message', (topic, message) => {
	if (!topic.endsWith('/packets')) return;
	try {
		const json = JSON.parse(message.toString());
		if (!json.raw || !json.hash) return;
		if (hasSeen(json.hash)) return;
		addSeen(json.hash);

		const packet = MeshCoreDecoder.decode(json.raw, { keyStore });
		if (!packet?.payload?.decoded) return;
		const { type } = packet.payload.decoded;

		const path = packet.path || [];
		const hops = path.length;
		const pathHashBytes = path.length > 0 ? path[0].length / 2 : null;

		const common = {
			receivedTimestamp: json.timestamp,
			receivedUnix: Math.floor(new Date(json.timestamp).getTime() / 1000),
			date: json.date,
			time: json.time,
			packetHash: json.hash,
			path,
			hops,
			pathHashBytes,
			snr: json.SNR,
			rssi: json.RSSI,
			score: json.score
		};

		const { type: _type, ...decodedRest } = packet.payload.decoded;
		const typeName = PAYLOAD_TYPES[type] || `unknown_${type}`;

		// Advert — enrich with structured fields
		if (type === 4) {
			const { name, location, deviceRole } = packet.payload.decoded.appData || {};
			const { publicKey, signatureValid } = packet.payload.decoded;
			publish('advert', {
				...common,
				...(publicKey !== undefined && { publicKey }),
				...(signatureValid !== undefined && { signatureValid }),
				...(name && { name }),
				...(location && { lat: location.latitude, lon: location.longitude }),
				...(deviceRole !== undefined && {
					deviceRole,
					deviceRoleName: DEVICE_ROLES[deviceRole] || `unknown(${deviceRole})`
				}),
				decoded: decodedRest
			});
			return;
		}

		// Group text — enrich with channel and message fields
		if (type === 5) {
			const channelHash = packet.payload.decoded.channelHash?.toUpperCase();
			const channel = channelNames[channelHash] || `unknown(${channelHash})`;
			const { sender, message: msg, timestamp: sentUnix } = packet.payload.decoded.decrypted || {};
			const receivedUnix = common.receivedUnix;
			publish('message', {
				...common,
				channel,
				channelHash,
				sentUnix: sentUnix || null,
				propagationSecs: sentUnix ? receivedUnix - sentUnix : null,
				...(sender !== undefined && { sender }),
				...(msg !== undefined && { message: msg }),
				decoded: decodedRest
			});
			return;
		}

		// Everything else — publish raw decoded payload under its type name
		publish(typeName, { ...common, decoded: decodedRest });
	} catch (e) {
		process.stderr.write(`Decode error: ${e.message}\n`);
	}
});

inputClient.on('error', (err) => process.stderr.write(`Input MQTT error: ${err.message}\n`));
outputClient.on('error', (err) => process.stderr.write(`Output MQTT error: ${err.message}\n`));
