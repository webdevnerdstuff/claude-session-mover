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

function engine(on: On, runs: { argv: readonly string[]; stdin?: string }[]) {
	on('command.register', () => ({ value: { command: 'move-sessions' } }))
	on('ui.open', () => ({ value: { isPlaced: true } }))
	on('session.start', (_, e) => ({ cwd: e.cwd }))
	on('process.run', (_, e) => {
		runs.push({ argv: e.argv, stdin: e.init?.stdin })
		const stdout = e.argv.includes('list') ? JSON.stringify(LISTING) : e.argv.includes('last') ? 'null' : '{}'

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
