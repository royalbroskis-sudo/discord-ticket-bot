// mc-bot/index.js
//
// Multi-tenant Minecraft bridge. Each Discord user who links their own
// Microsoft/Minecraft account gets their own persistent mineflayer bot
// instance, keyed by their Discord user ID. Nobody can see or drive
// anybody else's account — every route is scoped to a :discordId.

const mineflayer      = require('mineflayer')
const express         = require('express')
const { MongoClient } = require('mongodb')
const fs              = require('fs')
const path            = require('path')

const app = express()
app.use(express.json())

// ── MongoDB ───────────────────────────────────────────────────────────────────
const mongoClient = new MongoClient(process.env.MONGO_URI)
let db = null
const LINKS_COLLECTION = 'mc_ms_sessions' // one document per discordId — Microsoft/mineflayer session data.
                                           // NOTE: deliberately NOT named "mc_links" — that collection is
                                           // already used by cogs/mcpay.py for unrelated IGN bookkeeping.

async function connectMongo() {
  await mongoClient.connect()
  db = mongoClient.db('discord_bot')
  console.log('[MC-BOT] ✅ Connected to MongoDB')
}

// ── Per-user profiles folders (mineflayer caches MS tokens here as JSON) ──────
const PROFILES_ROOT = path.join(__dirname, '.mc_profiles')
if (!fs.existsSync(PROFILES_ROOT)) fs.mkdirSync(PROFILES_ROOT)

function profilesDirFor(discordId) {
  const dir = path.join(PROFILES_ROOT, discordId)
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
  return dir
}

// Persist a user's profiles folder to MongoDB after login
async function backupProfiles(discordId) {
  try {
    const dir = profilesDirFor(discordId)
    const files = {}
    for (const f of fs.readdirSync(dir)) {
      files[f] = fs.readFileSync(path.join(dir, f), 'utf8')
    }
    if (Object.keys(files).length > 0) {
      await db.collection(LINKS_COLLECTION).updateOne(
        { _id: discordId },
        { $set: { profiles: files, updated_at: new Date() } },
        { upsert: true }
      )
      console.log(`[MC-BOT] [${discordId}] 💾 Profiles backed up to MongoDB`)
    }
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to backup profiles:`, e.message)
  }
}

// Restore a user's profiles from MongoDB to disk before connecting
async function restoreProfiles(discordId) {
  try {
    const doc = await db.collection(LINKS_COLLECTION).findOne({ _id: discordId })
    if (!doc?.profiles) return false
    const dir = profilesDirFor(discordId)
    for (const [name, content] of Object.entries(doc.profiles)) {
      fs.writeFileSync(path.join(dir, name), content, 'utf8')
    }
    return true
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to restore profiles:`, e.message)
    return false
  }
}

// Fully wipe a user's link — disk + MongoDB
async function clearProfiles(discordId) {
  try {
    const dir = profilesDirFor(discordId)
    for (const f of fs.readdirSync(dir)) fs.unlinkSync(path.join(dir, f))
  } catch (_) {}
  try {
    await db.collection(LINKS_COLLECTION).deleteOne({ _id: discordId })
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to clear link:`, e.message)
  }
}

async function saveLook(discordId, yaw, pitch) {
  try {
    await db.collection(LINKS_COLLECTION).updateOne(
      { _id: discordId },
      { $set: { look: { yaw, pitch }, updated_at: new Date() } },
      { upsert: true }
    )
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to save look:`, e.message)
  }
}

async function loadLook(discordId) {
  try {
    const doc = await db.collection(LINKS_COLLECTION).findOne({ _id: discordId })
    return doc?.look ?? null
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to load look:`, e.message)
    return null
  }
}

async function saveMcUsername(discordId, username) {
  try {
    await db.collection(LINKS_COLLECTION).updateOne(
      { _id: discordId },
      { $set: { mc_username: username }, $setOnInsert: { linked_at: new Date() } },
      { upsert: true }
    )
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to save mc username:`, e.message)
  }
}

// Whether this user *should* be connected after a restart (persisted so a
// process restart brings everyone back to the state they left in).
async function setDesired(discordId, desired) {
  try {
    await db.collection(LINKS_COLLECTION).updateOne(
      { _id: discordId },
      { $set: { desired, updated_at: new Date() } },
      { upsert: true }
    )
  } catch (e) {
    console.error(`[MC-BOT] [${discordId}] Failed to save desired state:`, e.message)
  }
}

async function getAllLinkedUsers() {
  try {
    return await db.collection(LINKS_COLLECTION)
      .find({ profiles: { $exists: true } }, { projection: { _id: 1, desired: 1, mc_username: 1 } })
      .toArray()
  } catch (e) {
    console.error('[MC-BOT] Failed to list linked users:', e.message)
    return []
  }
}

// ── Per-user in-memory session state ───────────────────────────────────────
// sessions.get(discordId) = {
//   bot, botReady, manualDisconnect, lookSaveInterval, reconnectTimer,
//   state: { status, code, url, error, mcUsername }
// }
// status: disconnected | awaiting_auth | awaiting_discord_auth | connecting | ready | error
const sessions = new Map()

function getSession(discordId) {
  if (!sessions.has(discordId)) {
    sessions.set(discordId, {
      bot: null,
      botReady: false,
      manualDisconnect: false,
      lookSaveInterval: null,
      reconnectTimer: null,
      state: { status: 'disconnected', code: null, url: null, error: null, mcUsername: null, discordId: discordId },
      chatLog: [],   // rolling buffer of console/chat lines — { id, text, type, ts }
      chatSeq: 0,    // monotonically increasing id, used as a poll cursor
    })
  }
  return sessions.get(discordId)
}

function setState(discordId, patch) {
  const s = getSession(discordId)
  s.state = { ...s.state, ...patch }
  console.log(`[MC-BOT] [${discordId}] status → ${s.state.status}${s.state.code ? ` code=${s.state.code}` : ''}`)
}

// ── Live console/chat buffer (per user) ─────────────────────────────────────
// type: 'game' (incoming chat/system message), 'sent' (command we sent),
// 'system' (connection lifecycle notices, e.g. connected/kicked/error)
const CHAT_LOG_MAX = 300

function pushChat(discordId, text, type = 'game') {
  const s = getSession(discordId)
  s.chatSeq += 1
  s.chatLog.push({ id: s.chatSeq, text, type, ts: Date.now() })
  if (s.chatLog.length > CHAT_LOG_MAX) {
    s.chatLog.splice(0, s.chatLog.length - CHAT_LOG_MAX)
  }
}

// ── Bot lifecycle (per user) ────────────────────────────────────────────────

function scheduleReconnect(discordId, ms = 15000) {
  const s = getSession(discordId)
  if (s.reconnectTimer) return
  s.reconnectTimer = setTimeout(async () => {
    s.reconnectTimer = null
    // Always try to restore this user's saved profile — never trigger a
    // fresh MS login on an automatic reconnect.
    const hasProfiles = await restoreProfiles(discordId)
    if (!hasProfiles) {
      console.log(`[MC-BOT] [${discordId}] No saved profile to reconnect with — staying disconnected.`)
      setState(discordId, { status: 'disconnected', code: null, url: null, error: null })
      return
    }
    startBot(discordId, true)
  }, ms)
}

function startBot(discordId, hasProfiles = false) {
  const s = getSession(discordId)
  if (s.bot) { try { s.bot.end() } catch (_) {} }
  s.bot      = null
  s.botReady = false
  setState(discordId, { status: 'connecting', code: null, url: null, error: null })

  const opts = {
    host:           process.env.MC_SERVER_HOST || 'play.donutsmp.net',
    version:        process.env.MC_VERSION     || '1.21',
    auth:           'microsoft',
    profilesFolder: profilesDirFor(discordId), // isolates each user's cached MS token on disk
  }

  // Only set up device-code flow when we have NO saved session for this user.
  // If hasProfiles is true, mineflayer will silently reuse the cached token.
  if (!hasProfiles) {
    opts.onMsaCode = ({ user_code, verification_uri }) => {
      console.log(`[MC-BOT] [${discordId}] 🔑 Device code: ${user_code}`)
      console.log(`[MC-BOT] [${discordId}] 🔗 URL: ${verification_uri}`)
      setState(discordId, {
        status: 'awaiting_auth',
        code:   user_code,
        url:    verification_uri,
        error:  null,
      })
    }
  } else {
    console.log(`[MC-BOT] [${discordId}] 🔄 Using cached MS token — no login required`)
  }

  const bot = mineflayer.createBot(opts)
  s.bot = bot

  // Persistent console feed — every chat/system message the bot receives,
  // whether or not anyone is actively running a command right now.
  bot.on('message', (jsonMsg) => {
    if (s.bot !== bot) return
    try {
      const text = jsonMsg.toString().trim()
      if (text) pushChat(discordId, text, 'game')
    } catch (_) {}
  })

  bot.on('spawn', async () => {
    // Guard against a stale bot's late events after a restart/replace
    if (s.bot !== bot) return
    s.botReady = true
    setState(discordId, { status: 'ready', code: null, url: null, error: null, mcUsername: bot.username })
    pushChat(discordId, `✅ Connected as ${bot.username}`, 'system')
    await backupProfiles(discordId)
    await saveMcUsername(discordId, bot.username)
    await setDesired(discordId, 'connected')

    try {
      const saved = await loadLook(discordId)
      if (saved && s.bot === bot) {
        bot.look(saved.yaw, saved.pitch, true)
        console.log(`[MC-BOT] [${discordId}] 🧭 Restored facing direction`)
      }
    } catch (e) {
      console.error(`[MC-BOT] [${discordId}] Failed to restore look:`, e.message)
    }

    if (s.lookSaveInterval) clearInterval(s.lookSaveInterval)
    s.lookSaveInterval = setInterval(() => {
      if (s.bot === bot && bot.entity) saveLook(discordId, bot.entity.yaw, bot.entity.pitch)
    }, 5000)
  })

  bot.on('kicked', (reason) => {
    if (s.bot !== bot) return
    const msg = typeof reason === 'object' ? JSON.stringify(reason) : String(reason)
    console.log(`[MC-BOT] [${discordId}] Kicked: ${msg}`)
    pushChat(discordId, `⚠️ Kicked: ${msg}`, 'system')
    s.botReady = false
    const isAuthKick = msg.toLowerCase().includes('discord') ||
                       msg.toLowerCase().includes('verify')  ||
                       msg.toLowerCase().includes('authoriz')
    if (isAuthKick) {
      setState(discordId, { status: 'awaiting_discord_auth', code: null, url: null, error: null })
    }
  })

  bot.on('error', (err) => {
    if (s.bot !== bot) return
    console.error(`[MC-BOT] [${discordId}] Error:`, err.message)
    pushChat(discordId, `❌ Error: ${err.message}`, 'system')
    s.botReady = false
    setState(discordId, { status: 'error', code: null, url: null, error: err.message })
  })

  bot.on('end', (reason) => {
    if (s.bot !== bot) return
    console.log(`[MC-BOT] [${discordId}] Disconnected: ${reason}`)
    pushChat(discordId, `🔌 Disconnected: ${reason}`, 'system')
    s.botReady = false
    if (s.lookSaveInterval) { clearInterval(s.lookSaveInterval); s.lookSaveInterval = null }
    if (s.manualDisconnect) {
      setState(discordId, { status: 'disconnected', code: null, url: null, error: null })
      return
    }
    if (s.state.status === 'awaiting_discord_auth') {
      // Don't reconnect — waiting for the user to click "I Authorized"
      return
    }
    // Unexpected drop — schedule reconnect using this user's saved profile.
    // "Persistent" mode: we always try to bring their bot back.
    setState(discordId, { status: 'disconnected', code: null, url: null, error: null })
    scheduleReconnect(discordId, 15000)
  })
}

// ── Scheduled Commands System ──────────────────────────────────────────────
// Each user can schedule commands to run repeatedly
// Collection: scheduled_commands
// Document: { discordId, command, interval, nextRun, enabled, createdAt }

async function getScheduledCommands(discordId) {
  try {
    return await db.collection('scheduled_commands')
      .find({ discordId, enabled: true })
      .toArray()
  } catch (e) {
    console.error('[MC-BOT] Failed to get scheduled commands:', e.message)
    return []
  }
}

async function saveScheduledCommand(discordId, command, interval) {
  try {
    const now = new Date()
    const doc = {
      discordId,
      command,
      interval, // in milliseconds
      nextRun: new Date(now.getTime() + interval),
      enabled: true,
      createdAt: now
    }
    const result = await db.collection('scheduled_commands').insertOne(doc)
    return { ...doc, _id: result.insertedId }
  } catch (e) {
    console.error('[MC-BOT] Failed to save scheduled command:', e.message)
    throw e
  }
}

async function deleteScheduledCommand(discordId, command) {
  try {
    const result = await db.collection('scheduled_commands').deleteOne({
      discordId,
      command,
      enabled: true
    })
    return result.deletedCount > 0
  } catch (e) {
    console.error('[MC-BOT] Failed to delete scheduled command:', e.message)
    return false
  }
}

async function disableScheduledCommand(discordId, command) {
  try {
    const result = await db.collection('scheduled_commands').updateOne(
      { discordId, command, enabled: true },
      { $set: { enabled: false } }
    )
    return result.modifiedCount > 0
  } catch (e) {
    console.error('[MC-BOT] Failed to disable scheduled command:', e.message)
    return false
  }
}

// ── Scheduler Loop ──────────────────────────────────────────────────────────
let schedulerInterval = null

async function runScheduler() {
  try {
    const now = new Date()
    const dueCommands = await db.collection('scheduled_commands')
      .find({
        enabled: true,
        nextRun: { $lte: now }
      })
      .toArray()

    for (const task of dueCommands) {
      const discordId = task.discordId
      const s = getSession(discordId)
      
      // Only run if bot is connected
      if (s.botReady && s.bot) {
        console.log(`[MC-BOT] [${discordId}] ⏰ Running scheduled: ${task.command}`)
        const output = []
        const onMessage = (jsonMsg) => {
          try {
            const text = jsonMsg.toString().trim()
            if (text) output.push(text)
          } catch (_) {}
        }
        try {
          s.bot.on('message', onMessage)
          s.bot.chat(task.command)
          pushChat(discordId, `⏰ » ${task.command}`, 'auto')
          await new Promise((resolve) => setTimeout(resolve, 1500))
          console.log(`[MC-BOT] [${discordId}] ✅ Scheduled command executed`)
        } catch (err) {
          console.error(`[MC-BOT] [${discordId}] Scheduled command failed:`, err.message)
        } finally {
          s.bot.removeListener('message', onMessage)
        }
      } else {
        console.log(`[MC-BOT] [${discordId}] ⏰ Skipping scheduled command (bot not ready): ${task.command}`)
      }

      // Update next run time
      await db.collection('scheduled_commands').updateOne(
        { _id: task._id },
        { $set: { nextRun: new Date(now.getTime() + task.interval) } }
      )
    }
  } catch (e) {
    console.error('[MC-BOT] Scheduler error:', e.message)
  }
}

// Start the scheduler (runs every 5 seconds)
function startScheduler() {
  if (schedulerInterval) return
  schedulerInterval = setInterval(runScheduler, 5000)
  console.log('[MC-BOT] ⏰ Scheduler started (check every 5s)')
}

// ── Routes ────────────────────────────────────────────────────────────────────
// Every route is scoped to a specific Discord user's own session.

app.get('/status/:discordId', (req, res) => {
  const state = getSession(req.params.discordId).state
  res.json({ ...state, discordId: req.params.discordId })
})

// Admin/overview: every linked account and its current live state
app.get('/status', async (_req, res) => {
  const linked = await getAllLinkedUsers()
  res.json(linked.map(doc => ({ discordId: doc._id, ...getSession(doc._id).state })))
})

// Link / connect — reuse saved profile if available, fresh MS login only if none exists
app.post('/start-login/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const s = getSession(discordId)
  if (s.botReady) return res.json({ ok: true, message: 'Already connected' })
  s.manualDisconnect = false
  const hasProfiles = await restoreProfiles(discordId)
  if (hasProfiles) {
    console.log(`[MC-BOT] [${discordId}] 🔄 Saved session found — reconnecting silently...`)
    startBot(discordId, true)
  } else {
    console.log(`[MC-BOT] [${discordId}] 🔑 No saved session — starting fresh MS login...`)
    startBot(discordId, false)
  }
  res.json({ ok: true })
})

// "I Authorized" — reconnect using saved profile
app.post('/reconnect/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const s = getSession(discordId)
  console.log(`[MC-BOT] [${discordId}] Manual reconnect triggered`)
  if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null }
  s.manualDisconnect = false
  setState(discordId, { status: 'connecting', code: null, url: null, error: null })
  setTimeout(async () => {
    const hasProfiles = await restoreProfiles(discordId)
    startBot(discordId, hasProfiles)
  }, 2000)
  res.json({ ok: true })
})

// Leave server — keep the linked profile/token saved, will auto-reconnect
app.post('/logout/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const s = getSession(discordId)
  if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null }
  s.manualDisconnect = true
  await backupProfiles(discordId)
  if (s.bot?.entity) await saveLook(discordId, s.bot.entity.yaw, s.bot.entity.pitch)
  await setDesired(discordId, 'disconnected')
  try { s.bot?.end() } catch (_) {}
  s.bot      = null
  s.botReady = false
  setState(discordId, { status: 'disconnected', code: null, url: null, error: null })
  res.json({ ok: true })
})

// Full unlink — clear everything for this user, requires fresh MS login next time
app.post('/full-logout/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const s = getSession(discordId)
  if (s.reconnectTimer) { clearTimeout(s.reconnectTimer); s.reconnectTimer = null }
  s.manualDisconnect = true
  await clearProfiles(discordId)
  try { s.bot?.end() } catch (_) {}
  s.bot      = null
  s.botReady = false
  setState(discordId, { status: 'disconnected', code: null, url: null, error: null, mcUsername: null })
  sessions.delete(discordId)
  res.json({ ok: true })
})

// Run an in-game command as THIS user's own bot instance
app.post('/run-command/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const { command, captureMs } = req.body
  const s = getSession(discordId)

  if (!command || typeof command !== 'string')
    return res.status(400).json({ ok: false, error: 'Missing command' })
  if (!s.botReady || !s.bot)
    return res.status(503).json({ ok: false, error: 'Not connected — link/connect your account first.' })

  const waitMs = Math.min(Math.max(parseInt(captureMs, 10) || 2000, 500), 8000)
  const bot = s.bot
  const output = []
  const onMessage = (jsonMsg) => {
    try {
      const text = jsonMsg.toString().trim()
      if (text) output.push(text)
    } catch (_) {}
  }

  try {
    bot.on('message', onMessage)
    bot.chat(command)
    pushChat(discordId, `» ${command}`, 'sent')
    console.log(`[MC-BOT] [${discordId}] ▶ ${command}`)
    await new Promise((resolve) => setTimeout(resolve, waitMs))
    res.json({ ok: true, output })
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message })
  } finally {
    bot.removeListener('message', onMessage)
  }
})

// ── Live Chat / Console Routes ───────────────────────────────────────────────
// The dashboard polls GET /chat for anything newer than its last-seen id,
// and POSTs to /chat to send a raw chat/command line — fire-and-forget,
// the message shows up in the poll feed once mineflayer echoes it back.

app.get('/chat/:discordId', (req, res) => {
  const discordId = req.params.discordId
  const after = parseInt(req.query.after, 10) || 0
  const s = getSession(discordId)
  const messages = s.chatLog.filter(m => m.id > after)
  res.json({ ok: true, messages, lastId: s.chatSeq })
})

app.post('/chat/:discordId', (req, res) => {
  const discordId = req.params.discordId
  const { message } = req.body
  const s = getSession(discordId)

  if (!message || typeof message !== 'string')
    return res.status(400).json({ ok: false, error: 'Missing message' })
  if (!s.botReady || !s.bot)
    return res.status(503).json({ ok: false, error: 'Not connected — link/connect your account first.' })

  try {
    pushChat(discordId, `» ${message}`, 'sent')
    s.bot.chat(message)
    console.log(`[MC-BOT] [${discordId}] ▶ ${message}`)
    res.json({ ok: true, lastId: s.chatSeq })
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message })
  }
})

// ── Scheduled Commands Routes ──────────────────────────────────────────────

// Schedule a command to run repeatedly
app.post('/schedule/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const { command, interval } = req.body
  
  if (!command || typeof command !== 'string') {
    return res.status(400).json({ ok: false, error: 'Missing command' })
  }
  
  let intervalMs = parseInt(interval)
  if (isNaN(intervalMs) || intervalMs < 60000) {
    return res.status(400).json({ 
      ok: false, 
      error: 'Interval must be at least 60 seconds (60000ms)' 
    })
  }
  
  try {
    const doc = await saveScheduledCommand(discordId, command, intervalMs)
    res.json({ ok: true, message: `Scheduled "${command}" every ${intervalMs/60000}m`, command: doc })
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

// Get all scheduled commands for a user
app.get('/schedule/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  try {
    const commands = await getScheduledCommands(discordId)
    res.json({ ok: true, commands })
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

// Disable a scheduled command
app.post('/schedule/disable/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const { command } = req.body
  
  if (!command) {
    return res.status(400).json({ ok: false, error: 'Missing command' })
  }
  
  try {
    const success = await disableScheduledCommand(discordId, command)
    if (success) {
      res.json({ ok: true, message: `Disabled scheduled command: ${command}` })
    } else {
      res.json({ ok: false, error: `No active scheduled command found: ${command}` })
    }
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

// Delete a scheduled command completely
app.post('/schedule/delete/:discordId', async (req, res) => {
  const discordId = req.params.discordId
  const { command } = req.body
  
  if (!command) {
    return res.status(400).json({ ok: false, error: 'Missing command' })
  }
  
  try {
    const success = await deleteScheduledCommand(discordId, command)
    if (success) {
      res.json({ ok: true, message: `Deleted scheduled command: ${command}` })
    } else {
      res.json({ ok: false, error: `No scheduled command found: ${command}` })
    }
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message })
  }
})

// ── Start ─────────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.MC_BOT_PORT || '3001')

connectMongo().then(async () => {
  app.listen(PORT, '127.0.0.1', () =>
    console.log(`[MC-BOT] 🌐 Listening on 127.0.0.1:${PORT}`)
  )

  // Start the scheduler
  startScheduler()

  // Persistent mode: bring back every user who was connected before the
  // last restart. Anyone who explicitly logged out (desired='disconnected')
  // stays disconnected until they reconnect themselves.
  const linked = await getAllLinkedUsers()
  for (const doc of linked) {
    if (doc.desired === 'connected') {
      console.log(`[MC-BOT] [${doc._id}] 🔄 Reconnecting on boot (${doc.mc_username || 'unknown'})`)
      const hasProfiles = await restoreProfiles(doc._id)
      if (hasProfiles) startBot(doc._id, true)
    } else {
      getSession(doc._id) // seed disconnected state so /status works immediately
    }
  }
  console.log(`[MC-BOT] ℹ️  ${linked.length} linked account(s) known.`)
}).catch(err => {
  console.error('[MC-BOT] ❌ MongoDB connection failed:', err)
  process.exit(1)
})


