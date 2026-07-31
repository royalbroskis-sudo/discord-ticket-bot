const express = require('express');
const mineflayer = require('mineflayer');
const { Authflow, Titles } = require('prismarine-auth');
const path = require('path');
const fs = require('fs');

const app = express();
app.use(express.json());

const PORT = process.env.MC_BOT_PORT || 3001;

// In-memory state storage
const states = {};
const schedules = {}; // discordId -> array of schedule objects

// Ensure cache directory exists for auth tokens
const cacheDir = path.join(__dirname, 'cache');
if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
}

function getState(discordId) {
    if (!states[discordId]) {
        states[discordId] = {
            status: 'disconnected',
            mcUsername: null,
            ownsJava: false,
            server: null,
            version: 'latest',
            error: null,
            bot: null,
            chatMessages: [],
            chatIdCounter: 0,
            requires_auth: false,
            health: 20,
            maxHealth: 20,
            hunger: 20,
            level: 0,
            xp: 0,
            position: { x: 0, y: 0, z: 0 },
            code: null,
            url: null
        };
    }
    return states[discordId];
}

function cleanupBot(discordId) {
    const state = getState(discordId);
    if (state.bot) {
        try {
            state.bot.quit();
        } catch (e) {}
        state.bot = null;
    }
    if (!state.mcUsername) {
        state.status = 'disconnected';
    } else {
        state.status = 'idle'; // Logged in but not connected to a server
    }
    state.server = null;
    state.requires_auth = false;
}

// ─── AUTHENTICATION ──────────────────────────────────────────────────────
async function startLogin(discordId) {
    const state = getState(discordId);
    state.status = 'awaiting_auth';
    state.error = null;
    state.code = null;
    state.url = null;

    console.log(`[startLogin] Starting auth for ${discordId}`);

    try {
        // The options object with onMsaCode callback
        const flow = new Authflow(discordId, cacheDir, {
            authTitle: Titles.MinecraftNintendoSwitch,
            deviceType: 'Nintendo',
            flow: 'live'
        }, (codeInfo) => {
            console.log(`[startLogin] 🔑 Device code callback triggered! codeInfo:`, codeInfo);
            // Use snake_case property names (this is the 'live' flow's response shape)
            state.code = codeInfo.user_code;
            state.url = codeInfo.verification_uri;
            console.log(`[startLogin] state.code = ${state.code}, state.url = ${state.url}`);
        });

        console.log('[startLogin] Authflow created, now calling getMinecraftJavaToken()...');
        const token = await flow.getMinecraftJavaToken({ fetchProfile: true, fetchEntitlements: true });
        console.log('[startLogin] getMinecraftJavaToken() returned:', token ? 'yes' : 'null');

        if (token && token.profile && token.profile.name) {
            state.mcUsername = token.profile.name;
            console.log(`[startLogin] Profile name: ${state.mcUsername}`);
        } else {
            state.mcUsername = `Player_${discordId.slice(-4)}`;
            console.log(`[startLogin] No profile, using fallback: ${state.mcUsername}`);
        }

        // Real Java ownership check, based on actual entitlements rather than
        // guessing from whether a username happened to resolve.
        const items = token?.entitlements?.items || [];
        state.ownsJava = items.some(i => i.name === 'game_minecraft' || i.name === 'product_minecraft');
        console.log(`[startLogin] ownsJava: ${state.ownsJava}`);

        state.status = 'idle';
        state.code = null;   // clear after login
        state.url = null;
        console.log('[startLogin] Login successful, state set to idle.');
        return { ok: true };
    } catch (e) {
        console.error(`[startLogin] ❌ Error during auth:`, e);
        state.status = 'error';
        state.code = null;   // don't leave a stale device code on screen
        state.url = null;
        // prismarine-auth surfaces raw upstream HTTP failures as e.g.
        // "502 Bad Gateway <html>...". These are almost always a transient
        // blip from Microsoft/Xbox's auth services, not a real problem.
        const httpFail = /^\d{3}\s/.test(e.message);
        state.error = httpFail
            ? `Microsoft's auth service had a hiccup (${e.message.slice(0, 3)}). Please try logging in again.`
            : e.message;
        return { ok: false, error: state.error };
    }
}

// ─── BOT CREATION & SERVER CONNECTION ────────────────────────────────────
async function connectToServer(discordId, server, version) {
    const state = getState(discordId);
    
    if (!state.mcUsername) {
        return { ok: false, error: 'Not logged in. Please link Microsoft account first.' };
    }

    cleanupBot(discordId); // Destroy any existing bot instance

    state.server = server;
    state.version = version || 'latest';
    state.status = 'connecting';
    state.requires_auth = false;
    state.error = null;

    try {
        const bot = mineflayer.createBot({
            host: server,
            port: 25565,
            username: discordId, 
            // mineflayer expects an actual version string (e.g. "1.20.4") or
            // no `version` field at all to auto-negotiate with the server.
            // The literal string "latest" isn't a real protocol version.
            ...(state.version && state.version !== 'latest' ? { version: state.version } : {}),
            auth: 'microsoft',
            profilesFolder: cacheDir,
            hideErrors: false
        });
        
        state.bot = bot;

        bot.on('spawn', () => {
            state.status = 'ready';
            state.error = null;
            state.health = bot.health;
            state.maxHealth = bot.game?.maxHealth || 20;
            state.hunger = bot.food;
            state.level = bot.experience?.level || 0;
            state.xp = bot.experience?.points || 0;
            state.position = bot.entity.position;
        });

        bot.on('health', () => {
            state.health = bot.health;
            state.hunger = bot.food;
            if (state.status === 'ready' && bot.health <= 0) {
                state.status = 'connecting'; // Respawning
                setTimeout(() => {
                    if (state.bot) state.status = 'ready';
                }, 2000);
            }
        });

        bot.on('experience', () => {
            state.level = bot.experience?.level || 0;
            state.xp = bot.experience?.points || 0;
        });

        bot.on('move', () => {
            if (bot.entity) {
                state.position = bot.entity.position;
            }
        });

        bot.on('messagestr', (message) => {
            if (message.includes('Discord') && (message.includes('authorize') || message.includes('verify') || message.includes('link'))) {
                state.status = 'awaiting_discord_auth';
                state.requires_auth = true;
            }
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: message,
                type: 'system'
            });
        });

        bot.on('chat', (username, message) => {
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: `<${username}> ${message}`,
                type: 'chat'
            });
        });

        bot.on('kicked', (reason) => {
            const reasonStr = reason.toString();
            state.error = `Kicked: ${reasonStr}`;
            
            if (reasonStr.includes('Discord') || reasonStr.includes('authorize') || reasonStr.includes('verify')) {
                state.status = 'awaiting_discord_auth';
                state.requires_auth = true;
            } else {
                state.status = 'idle';
                state.bot = null;
            }
        });

        bot.on('error', (err) => {
            state.status = 'error';
            state.error = err.message;
            state.bot = null;
        });

        bot.on('end', () => {
            if (state.status !== 'awaiting_discord_auth' && state.status !== 'error') {
                state.status = 'idle';
            }
            state.bot = null;
        });

        return { ok: true, status: 'connecting' };

    } catch (e) {
        state.status = 'error';
        state.error = e.message;
        return { ok: false, error: e.message };
    }
}

// ─── SCHEDULED COMMANDS ──────────────────────────────────────────────────
function loadSchedulesForUser(discordId) {
    if (!schedules[discordId]) schedules[discordId] = [];
}

function checkSchedules() {
    const now = Date.now();
    for (const discordId in schedules) {
        const userSchedules = schedules[discordId];
        const state = getState(discordId);

        userSchedules.forEach(s => {
            if (s.enabled && state.status === 'ready' && state.bot) {
                if (now >= s.nextRun) {
                    try {
                        state.bot.chat(s.command);
                        s.nextRun = now + s.interval;
                    } catch (e) {}
                }
            }
        });
    }
}
setInterval(checkSchedules, 5000);

// ─── API ROUTES ──────────────────────────────────────────────────────────

app.get('/status/:discordId', (req, res) => {
    const state = getState(req.params.discordId);
    const response = {
        status: state.status,
        mcUsername: state.mcUsername,
        ownsJava: state.ownsJava,
        server: state.server,
        version: state.version,
        error: state.error,
        requires_auth: state.requires_auth,
        health: state.health,
        maxHealth: state.maxHealth,
        hunger: state.hunger,
        xpLevel: state.level,
        xpProgress: state.xp,
        position: state.position,
        ping: state.bot?.player?.ping || 0,
        code: state.code || null,
        url: state.url || null
    };
    console.log(`[status] Returning for ${req.params.discordId}: status=${response.status}, code=${response.code}, url=${response.url}`);
    res.json(response);
});

app.post('/start-login/:discordId', (req, res) => {
    const discordId = req.params.discordId;

    // Don't await this: startLogin() blocks until the user actually completes
    // the Microsoft device-code flow (which can take minutes), but the caller
    // (the Flask dashboard) only waits ~10s for a response. Kick the flow off
    // in the background and respond immediately; the frontend polls
    // /status/:discordId to pick up the code/url once onMsaCode fires, and
    // to see the final success/error state once the flow resolves.
    startLogin(discordId).catch(e => {
        console.error(`[start-login] Unhandled error for ${discordId}:`, e);
        const state = getState(discordId);
        state.status = 'error';
        state.error = e.message;
    });

    res.json({ ok: true, status: 'started' });
});

app.post('/connect/:discordId', async (req, res) => {
    const { server, version } = req.body;
    if (!server) return res.status(400).json({ ok: false, error: 'Missing server IP' });
    
    const result = await connectToServer(req.params.discordId, server, version);
    res.json(result);
});

app.post('/reconnect/:discordId', async (req, res) => {
    const state = getState(req.params.discordId);
    if (state.server) {
        const result = await connectToServer(req.params.discordId, state.server, state.version);
        res.json(result);
    } else {
        res.json({ ok: false, error: 'No previous server to reconnect to.' });
    }
});

app.post('/logout/:discordId', (req, res) => {
    cleanupBot(req.params.discordId);
    res.json({ ok: true });
});

app.post('/full-logout/:discordId', (req, res) => {
    cleanupBot(req.params.discordId);
    states[req.params.discordId] = {
        status: 'disconnected',
        mcUsername: null,
        ownsJava: false,
        server: null,
        version: 'latest',
        error: null,
        bot: null,
        chatMessages: [],
        chatIdCounter: 0,
        code: null,
        url: null
    };
    res.json({ ok: true });
});

app.post('/run-command/:discordId', (req, res) => {
    const state = getState(req.params.discordId);
    const { command } = req.body;
    if (state.bot && state.status === 'ready') {
        try {
            state.bot.chat(command);
            res.json({ ok: true });
        } catch (e) {
            res.json({ ok: false, error: e.message });
        }
    } else {
        res.json({ ok: false, error: 'Bot is not connected.' });
    }
});

app.get('/chat/:discordId', (req, res) => {
    const state = getState(req.params.discordId);
    const after = parseInt(req.query.after) || 0;
    const messages = state.chatMessages.filter(m => m.id > after);
    res.json({ ok: true, messages });
});

app.post('/chat/:discordId', (req, res) => {
    const state = getState(req.params.discordId);
    const { message } = req.body;
    if (state.bot && state.status === 'ready') {
        try {
            state.bot.chat(message);
            state.chatMessages.push({
                id: ++state.chatIdCounter,
                text: `> ${message}`,
                type: 'sent'
            });
            res.json({ ok: true });
        } catch (e) {
            res.json({ ok: false, error: e.message });
        }
    } else {
        res.json({ ok: false, error: 'Bot is not connected.' });
    }
});

// ─── SCHEDULE ROUTES ─────────────────────────────────────────────────────
app.get('/schedule/:discordId', (req, res) => {
    loadSchedulesForUser(req.params.discordId);
    res.json({ ok: true, commands: schedules[req.params.discordId] });
});

app.post('/schedule/:discordId', (req, res) => {
    loadSchedulesForUser(req.params.discordId);
    const { command, interval } = req.body;
    
    schedules[req.params.discordId] = schedules[req.params.discordId].filter(s => s.command !== command);
    
    const newSchedule = {
        command,
        interval,
        enabled: true,
        nextRun: Date.now() + interval
    };
    
    schedules[req.params.discordId].push(newSchedule);
    res.json({ ok: true });
});

app.post('/schedule/disable/:discordId', (req, res) => {
    loadSchedulesForUser(req.params.discordId);
    const { command } = req.body;
    const sched = schedules[req.params.discordId].find(s => s.command === command);
    if (sched) {
        sched.enabled = false;
        res.json({ ok: true });
    } else {
        res.json({ ok: false, error: 'Schedule not found' });
    }
});

app.post('/schedule/delete/:discordId', (req, res) => {
    loadSchedulesForUser(req.params.discordId);
    const { command } = req.body;
    schedules[req.params.discordId] = schedules[req.params.discordId].filter(s => s.command !== command);
    res.json({ ok: true });
});

// ─── START SERVER ────────────────────────────────────────────────────────
app.listen(PORT, () => {
    console.log(`✅ MC Bot Service running on port ${PORT}`);
});