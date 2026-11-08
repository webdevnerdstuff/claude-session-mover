import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const PANE = {
	plugin: 'session-mover',
	component: 'Pane',
	requestId: 'session-mover',
	props: {
		title: 'Move sessions',
		isFocused: false,
		bodyColumns: 80,
		placement: 'dock',
		scroll: { offset: 0, bodyRows: 40 },
		view: {},
	},
} as const

const LISTING = {
	workspaces: [
		{ id: 'ws-ha', name: 'Home Assistant', cwd: '/ha' },
		{ id: 'ws-cc', name: 'Centcom Docker', cwd: '/cc' },
	],
	sessions: [
		{ id: 'local_1', title: 'Ubuntu 24 box', cwd: '/ha', workspaceId: 'ws-ha', lastActivityAt: 2 },
		{ id: 'local_2', title: 'Docker disk space', cwd: '/cc', workspaceId: 'ws-cc', lastActivityAt: 1 },
	],
}

// Sized like a real install: 25 workspaces with UUID ids, 400 sessions, 150 in one.
const uuid = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const BIG = {
	workspaces: Array.from({ length: 25 }, (_, n) => ({
		id: uuid(n),
		name: `Workspace number ${n}`,
		cwd: `/work/${n}`,
	})),
	sessions: Array.from({ length: 400 }, (_, n) => ({
		id: `local_${uuid(1000 + n)}`,
		title: `A session title long enough to look real, number ${n}`,
		cwd: `/work/${n < 150 ? 0 : n % 25}`,
		workspaceId: uuid(n < 150 ? 0 : n % 25),
		lastActivityAt: 400 - n,
	})),
}
// What the mod API lets one drawing hold, serialized.
const DRAWING_LIMIT = 100_000

function engine(
	on: On,
	runs: { argv: readonly string[]; stdin?: string }[],
	listing: typeof LISTING = LISTING,
) {
	on('command.register', () => ({ value: { command: 'move-sessions' } }))
	on('ui.open', () => ({ value: { isPlaced: true } }))
	on('session.start', (_, e) => ({ cwd: e.cwd }))
	on('process.run', (_, e) => {
		runs.push({ argv: e.argv, stdin: e.init?.stdin })
		const stdout = e.argv.includes('list') ? JSON.stringify(listing) : e.argv.includes('last') ? 'null' : '{}'

		return { value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
	})
}

test('a queued move is sent to the helper only after confirming', async ($, on) => {
	const runs: { argv: readonly string[]; stdin?: string }[] = []
	engine(on, runs)

	await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
	const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
	await ui.press({ key: 'refresh' })
	expect((await ui.find({ type: 'Text', text: /Ubuntu 24 box/ }))?.text).toBe('Ubuntu 24 box')

	await ui.select({ key: 'move-local_1', value: 'ws-cc' })
	expect((await ui.find({ type: 'Text', text: /Queued moves/ }))?.text).toBe('Queued moves: 1')

	await ui.press({ key: 'apply' })
	expect(runs.some(run => run.argv.includes('apply'))).toBe(false)

	await ui.press({ key: 'confirm' })
	const apply = runs.find(run => run.argv.includes('apply'))
	expect(JSON.parse(apply?.stdin ?? '[]')).toEqual([{ sessionId: 'local_1', workspaceId: 'ws-cc' }])
	expect((await ui.find({ type: 'Text', text: /Queued moves/ }))?.text).toBe('Queued moves: 0')
	await ui.unmount()
})

test('several queued moves are all sent to the helper', async ($, on) => {
	const runs: { argv: readonly string[]; stdin?: string }[] = []
	engine(on, runs)

	await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
	const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
	await ui.press({ key: 'refresh' })
	await ui.select({ key: 'move-local_1', value: 'ws-cc' })
	await ui.select({ key: 'move-local_2', value: 'ws-ha' })
	expect((await ui.find({ type: 'Text', text: /Queued moves/ }))?.text).toBe('Queued moves: 2')

	await ui.press({ key: 'apply' })
	await ui.press({ key: 'confirm' })
	const apply = runs.find(run => run.argv.includes('apply'))
	expect(JSON.parse(apply?.stdin ?? '[]')).toEqual([
		{ sessionId: 'local_1', workspaceId: 'ws-cc' },
		{ sessionId: 'local_2', workspaceId: 'ws-ha' },
	])
	await ui.unmount()
})

test('a large list stays within the drawing limit and pages', async ($, on) => {
	const runs: { argv: readonly string[]; stdin?: string }[] = []
	engine(on, runs, BIG)

	await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
	const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
	await ui.press({ key: 'refresh' })
	const last = BIG.sessions[399]
	await ui.select({ key: `move-${BIG.sessions[0].id}`, value: uuid(1) })
	await ui.select({ key: `move-${BIG.sessions[1].id}`, value: uuid(2) })
	expect(JSON.stringify(await ui.drawn()).length).toBeLessThan(DRAWING_LIMIT)
	expect(await ui.find({ key: `move-${last.id}` })).toBeUndefined()

	for (let n = 0; n < 40 && !(await ui.find({ key: `move-${last.id}` })); n++) {
		await ui.press({ key: 'next-page' })
		expect(JSON.stringify(await ui.drawn()).length).toBeLessThan(DRAWING_LIMIT)
	}
	await ui.select({ key: `move-${last.id}`, value: uuid(3) })

	await ui.select({ key: 'filter', value: uuid(0) })
	expect(JSON.stringify(await ui.drawn()).length).toBeLessThan(DRAWING_LIMIT)

	await ui.press({ key: 'apply' })
	await ui.press({ key: 'confirm' })
	const apply = runs.find(run => run.argv.includes('apply'))
	expect(JSON.parse(apply?.stdin ?? '[]')).toHaveLength(3)
	await ui.unmount()
})

test('picking the current workspace again undoes the move', async ($, on) => {
	engine(on, [])
	await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
	const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
	await ui.press({ key: 'refresh' })
	await ui.select({ key: 'move-local_1', value: 'ws-cc' })
	await ui.select({ key: 'move-local_1', value: 'ws-ha' })
	expect((await ui.find({ type: 'Text', text: /Queued moves/ }))?.text).toBe('Queued moves: 0')
	await ui.unmount()
})

test('mobile shows a note instead of pickers', async ($, on) => {
	engine(on, [])
	const ui = await $.ui.mount({ ...PANE, surface: 'mobile' })
	expect((await ui.find({ type: 'Text' }))?.text).toMatch(/on your Mac/)
	await ui.unmount()
})
