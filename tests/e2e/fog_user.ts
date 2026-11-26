// A headless Fog *user* for the live end-to-end test (tests/e2e/run_live.py drives the bot side).
// Run from a scratch directory with bun; FOG_REPO points at a fog checkout, FOG_SERVER at its server.
// It never touches the bot daemon's API: the hermes-fog adapter under test is the only consumer.
// Prints one JSON line per step: {"step": "...", "ok": true|false, ...}.
const FOG = process.env.FOG_REPO ?? `${process.env.HOME}/dev/fog`
const BOT = process.env.FOG_BOT_USERNAME ?? 'hermes_test'
await import(`${FOG}/bot/shim.ts`)
const { openStore } = await import(`${FOG}/bot/store.ts`)
const { useStore } = await import(`${FOG}/web/src/db.ts`)
const { rmSync } = await import('node:fs')
const dataDir = process.env.FOG_USER_DIR ?? './user-e2e'
rmSync(dataDir, { recursive: true, force: true })
useStore(openStore(dataDir))
const fog = await import(`${FOG}/web/src/fog.ts`)

const say = (step: string, ok: boolean, extra: Record<string, unknown> = {}) => console.log(JSON.stringify({ step, ok, ...extra }))
const until = async <T>(f: () => T | undefined | null | false, ms = 30_000): Promise<T | undefined> => {
  for (let t = 0; t < ms; t += 50) {
    const v = f()
    if (v) return v as T
    await Bun.sleep(50)
  }
  return undefined
}

await fog.boot()
await fog.createAccount(fog.newPhrase())
fog.skipUsername()
fog.setProfile({ name: 'E2E User' })
const peer = await fog.findPeer(BOT)
const botId = peer.id
fog.startChat(botId)
say('ready', true, { user: fog.getState().me!.id, bot: botId })

// 1. Inbound message -> adapter; streamed reply comes back live, then final without "edited".
await fog.sendMessage(botId, 'hello from e2e')
let sawLive = false
const msgs = () => fog.getState().chats[botId]?.messages ?? []
const final = await until(() => {
  for (const m of msgs()) if (!m.mine && m.live) sawLive = true
  return msgs().find((m) => !m.mine && m.text === 'Done **e2e**' && !m.live)
})
say('stream', !!final && sawLive && !final?.edited, { sawLive, edited: final?.edited ?? null, md: final?.md ?? null })

// 2. Approval card: tap "once", get the toast, card edited to resolved and buttons removed.
const card = await until(() => msgs().find((m) => !m.mine && m.kb?.some((r: any) => Array.isArray(r) && r.some((b: any) => String(b.d ?? '').startsWith('ea:once:')))))
if (card) {
  const btn = (card.kb as any[]).flat().find((b: any) => String(b.d ?? '').startsWith('ea:once:'))
  await fog.tap(botId, card.id, botId, btn.d)
  const toast = fog.getState().toast?.text
  const resolved = await until(() => {
    const m = msgs().find((x) => x.id === card.id)
    return m && !m.kb && String(m.card?.title ?? '').startsWith('✅') ? m : undefined
  })
  say('approval', !!resolved, { cardTitle: card.card?.title ?? null, toast: toast ?? null, resolvedTitle: resolved?.card?.title ?? null })
} else say('approval', false, { error: 'no approval card' })

// 3. Command menu with /skill autocomplete reached the user (inside the bot's encrypted profile).
const cmds = await until(() => fog.getState().people[botId]?.bot?.cmds?.length ? fog.getState().people[botId]!.bot!.cmds! : undefined)
const sugg = cmds?.some((c: any) => c.n === 'skill') ? await fog.suggest(botId, botId, 'skill', 'name', '') : []
say('commands', !!cmds && cmds.some((c: any) => c.n === 'skill'), { count: cmds?.length ?? 0, suggestions: sugg.length })

// 4. Group @mention -> the bot starts a thread on it and answers inside the thread.
const gid = await fog.createGroup('E2E team', [botId])
await Bun.sleep(1500)
const ask = await fog.sendMessage(gid, `@${BOT} check the build`, { men: [botId] })
const inThread = await until(() => fog.getState().chats[gid]?.messages.find((m) => !m.mine && m.th && m.text === 'thread reply'), 45_000)
const thread = inThread ? fog.getState().chats[gid]?.threads?.[inThread.th!] : undefined
say('thread', !!inThread && thread?.root === ask, { thread: thread?.name ?? null, root: thread?.root === ask })

// 5. A follow-up in that thread without a mention still reaches the bot (participation).
if (inThread) {
  await fog.sendMessage(gid, 'and the tests?', { th: inThread.th })
  const followUp = await until(() => fog.getState().chats[gid]?.messages.find((m) => !m.mine && m.th === inThread.th && m.text === 'follow-up seen'), 30_000)
  say('thread_followup', !!followUp)
}
process.exit(0)
