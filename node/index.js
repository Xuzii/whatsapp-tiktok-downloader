const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const http = require('http');
const fs = require('fs');
const path = require('path');

// ============================================================
// CONFIGURATION
// ============================================================
const TARGET_GROUP_NAME = 'Fam1';
const PYTHON_BASE_URL = 'http://localhost:5001';
const MAX_TIKTOKS_TO_FETCH = 100;
const STATE_FILE = path.join(__dirname, 'state.json');
// ============================================================

const TIKTOK_URL_REGEX = /https?:\/\/(www\.)?(vm\.tiktok\.com|vt\.tiktok\.com|tiktok\.com\/(t|@[\w.-]+\/video))\/[\w.-]+/gi;

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        headless: true,
        protocolTimeout: 600000, // 10 min — getChats() is slow on e2-micro
        executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || undefined,
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-gpu',
            '--no-first-run',
            '--disable-extensions',
            '--disable-background-networking',
            '--disable-default-apps',
            '--disable-sync',
            '--disable-translate',
            '--metrics-recording-only',
            '--no-default-browser-check',
            '--js-flags=--max-old-space-size=384',
        ],
    },
});

// ============================================================
// UTILITIES
// ============================================================

function log(msg) {
    const timestamp = new Date().toISOString();
    console.log(`[${timestamp}] ${msg}`);
}

function postJSON(endpoint, data) {
    return new Promise((resolve, reject) => {
        const body = JSON.stringify(data);
        const url = new URL(`${PYTHON_BASE_URL}${endpoint}`);

        const options = {
            hostname: url.hostname,
            port: url.port,
            path: url.pathname,
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
            timeout: 300000, // 5 min timeout (Gemini analysis can be slow)
        };

        const req = http.request(options, (res) => {
            let responseData = '';
            res.on('data', (chunk) => { responseData += chunk; });
            res.on('end', () => {
                try {
                    resolve(JSON.parse(responseData));
                } catch {
                    resolve({ raw: responseData });
                }
            });
        });

        req.on('error', (err) => reject(err));
        req.on('timeout', () => {
            req.destroy();
            reject(new Error('Request timed out'));
        });

        req.write(body);
        req.end();
    });
}

function loadState() {
    try {
        if (fs.existsSync(STATE_FILE)) {
            return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
        }
    } catch {}
    return null;
}

function saveState(state) {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function extractTiktokUrls(messageBody) {
    if (!messageBody) return [];
    // Reset regex lastIndex since it's global
    TIKTOK_URL_REGEX.lastIndex = 0;
    return messageBody.match(TIKTOK_URL_REGEX) || [];
}

// ============================================================
// PHASE A: Batch Scan (first run — scan history for up to 1000 TikToks)
// ============================================================

async function batchScan(group) {
    log(`[BATCH] Starting full scan of "${group.name}" for up to ${MAX_TIKTOKS_TO_FETCH} TikTok links...`);

    const messages = await group.fetchMessages({ limit: 50000 });
    log(`[BATCH] Fetched ${messages.length} messages. Scanning for TikTok links...`);

    // Collect all TikTok URLs (scan newest to oldest)
    const tiktokEntries = [];
    const seenUrls = new Set();

    for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        const urls = extractTiktokUrls(msg.body);
        if (urls.length === 0) continue;

        let sender = 'Unknown';
        try {
            const contact = await msg.getContact();
            sender = contact.pushname || contact.name || msg.author || 'Unknown';
        } catch {}

        for (const url of urls) {
            const normalizedUrl = url.split('?')[0].split('#')[0].replace(/\/$/, '');
            if (seenUrls.has(normalizedUrl)) continue;
            seenUrls.add(normalizedUrl);

            tiktokEntries.push({
                url,
                sender,
                timestamp: msg.timestamp,
            });
        }

        if (tiktokEntries.length >= MAX_TIKTOKS_TO_FETCH) break;
    }

    const toProcess = tiktokEntries.slice(0, MAX_TIKTOKS_TO_FETCH);
    log(`[BATCH] Found ${toProcess.length} unique TikTok link(s). Processing...`);
    log('');

    let restaurants = 0;
    let skipped = 0;
    let errors = 0;

    for (let i = 0; i < toProcess.length; i++) {
        const { url, sender } = toProcess[i];
        log(`[${i + 1}/${toProcess.length}] ${url} (from ${sender})`);

        try {
            const result = await postJSON('/process', {
                url,
                chat_name: group.name,
                sender,
            });

            if (result.category === 'restaurant') {
                restaurants++;
                log(`  ✓ RESTAURANT (${result.restaurants_found} spot(s)) — ${result.title || ''}`);
            } else if (result.status === 'skipped') {
                skipped++;
                log(`  ○ Skipped — ${result.message || ''}`);
            } else if (result.category === 'not_restaurant') {
                skipped++;
                log(`  ○ Not restaurant — ${result.title || ''}`);
            } else {
                log(`  → ${JSON.stringify(result)}`);
            }
        } catch (err) {
            errors++;
            log(`  ✗ ERROR: ${err.message}`);
        }
    }

    log('');
    log(`[BATCH] Done! ${restaurants} restaurants, ${skipped} skipped, ${errors} errors out of ${toProcess.length} total.`);

    // Save state with the newest message timestamp
    if (messages.length > 0) {
        const newestTimestamp = messages[messages.length - 1].timestamp;
        saveState({ last_processed_timestamp: newestTimestamp });
        log(`[BATCH] State saved. Last processed timestamp: ${newestTimestamp}`);
    }
}

// ============================================================
// PHASE B: Catch-Up (on restart — process missed messages)
// ============================================================

async function catchUp(group, lastTimestamp) {
    log(`[CATCHUP] Checking for messages since timestamp ${lastTimestamp}...`);

    const messages = await group.fetchMessages({ limit: 5000 });
    const missed = messages.filter(m => m.timestamp > lastTimestamp);

    log(`[CATCHUP] Found ${missed.length} new message(s) since last run.`);

    const tiktokEntries = [];
    const seenUrls = new Set();

    for (const msg of missed) {
        const urls = extractTiktokUrls(msg.body);
        if (urls.length === 0) continue;

        let sender = 'Unknown';
        try {
            const contact = await msg.getContact();
            sender = contact.pushname || contact.name || msg.author || 'Unknown';
        } catch {}

        for (const url of urls) {
            const normalizedUrl = url.split('?')[0].split('#')[0].replace(/\/$/, '');
            if (seenUrls.has(normalizedUrl)) continue;
            seenUrls.add(normalizedUrl);
            tiktokEntries.push({ url, sender, timestamp: msg.timestamp });
        }
    }

    if (tiktokEntries.length === 0) {
        log('[CATCHUP] No new TikTok links found.');
    } else {
        log(`[CATCHUP] Found ${tiktokEntries.length} TikTok link(s) to process.`);

        for (let i = 0; i < tiktokEntries.length; i++) {
            const { url, sender } = tiktokEntries[i];
            log(`[CATCHUP ${i + 1}/${tiktokEntries.length}] ${url} (from ${sender})`);

            try {
                const result = await postJSON('/process', {
                    url,
                    chat_name: group.name,
                    sender,
                });
                if (result.category === 'restaurant') {
                    log(`  ✓ RESTAURANT (${result.restaurants_found} spot(s))`);
                } else {
                    log(`  → ${result.status}: ${result.message || ''}`);
                }
            } catch (err) {
                log(`  ✗ ERROR: ${err.message}`);
            }
        }
    }

    // Update state
    if (messages.length > 0) {
        const newestTimestamp = messages[messages.length - 1].timestamp;
        saveState({ last_processed_timestamp: newestTimestamp });
    }
}

// ============================================================
// PHASE C: Real-Time Listener (continuous)
// ============================================================

function startRealTimeListener(group) {
    log(`[LIVE] Listening for new TikTok links in "${group.name}"...`);

    client.on('message_create', async (message) => {
        try {
            const chat = await message.getChat();
            if (!chat.isGroup || chat.name !== TARGET_GROUP_NAME) return;

            const urls = extractTiktokUrls(message.body);
            if (urls.length === 0) return;

            const contact = await message.getContact();
            const sender = contact.pushname || contact.name || message.author || 'Unknown';

            for (const url of urls) {
                log(`[LIVE] TikTok from ${sender}: ${url}`);

                try {
                    const result = await postJSON('/process', {
                        url,
                        chat_name: chat.name,
                        sender,
                    });

                    if (result.category === 'restaurant') {
                        log(`  ✓ RESTAURANT (${result.restaurants_found} spot(s)) — ${result.title || ''}`);
                    } else if (result.status === 'skipped') {
                        log(`  ○ Skipped — ${result.message || ''}`);
                    } else {
                        log(`  → ${result.status}: ${result.message || ''}`);
                    }
                } catch (err) {
                    log(`  ✗ ERROR: ${err.message}`);
                }
            }

            // Update state
            saveState({ last_processed_timestamp: message.timestamp });
        } catch (err) {
            log(`[LIVE] Error: ${err.message}`);
        }
    });
}

// ============================================================
// MAIN STARTUP FLOW
// ============================================================

client.on('qr', (qr) => {
    log('Scan this QR code with WhatsApp on your phone:');
    qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => {
    log('Authenticated successfully. Session saved.');
});

client.on('auth_failure', (msg) => {
    log(`Authentication failed: ${msg}`);
    log('Please restart and scan the QR code again.');
});

client.on('ready', async () => {
    log('WhatsApp client is ready!');
    log(`Looking for group "${TARGET_GROUP_NAME}"...`);

    try {
        // getChats() can be very slow on low-memory VMs — retry up to 3 times
        let chats;
        for (let attempt = 1; attempt <= 3; attempt++) {
            try {
                log(`Fetching chats (attempt ${attempt}/3)...`);
                chats = await client.getChats();
                log(`Fetched ${chats.length} chats.`);
                break;
            } catch (chatErr) {
                log(`getChats() attempt ${attempt} failed: ${chatErr.message}`);
                if (attempt === 3) throw chatErr;
                log('Waiting 15s before retry...');
                await new Promise(r => setTimeout(r, 15000));
            }
        }

        const group = chats.find(c => c.isGroup && c.name === TARGET_GROUP_NAME);

        if (!group) {
            log(`ERROR: Group "${TARGET_GROUP_NAME}" not found.`);
            log('Available groups:');
            chats.filter(c => c.isGroup).forEach(c => log(`  - ${c.name}`));
            process.exit(1);
        }

        log(`Found group "${group.name}".`);

        // Check for existing state
        const state = loadState();

        if (state && state.last_processed_timestamp) {
            // Phase B: Catch up on missed messages
            await catchUp(group, state.last_processed_timestamp);
        } else {
            // Phase A: Full batch scan
            await batchScan(group);
        }

        // Phase C: Start real-time listener
        startRealTimeListener(group);

    } catch (err) {
        log(`ERROR: ${err.message}`);
        console.error(err);
    }
});

client.on('disconnected', (reason) => {
    log(`Client disconnected: ${reason}`);
    log('Attempting to reconnect...');
    client.initialize();
});

log('Initializing WhatsApp client...');
client.initialize();
