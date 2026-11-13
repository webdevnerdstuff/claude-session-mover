import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Listing, SessionRow } from '../types'

const PANE = 'session-mover'
const COMMAND = 'move-sessions'
const NONE = '__none'
// One drawing holds 100,000 serialized characters; past it the desktop keeps
// the last drawing, whose buttons no longer answer. Each row carries its own
// copy of the workspace options, so the rows on a page share this budget.
const ROWS_BUDGET = 60_000
// A row's characters besides its options: title, keys, handler handles.
const ROW_BASE = 600
const MIN_ROWS = 5
const MAX_ROWS = 60

const listing = atom({ plugin: 'session-mover', key: 'listing' } as const, null, {
	shape: 'listing-1',
})
const queue = atom({ plugin: 'session-mover', key: 'queue' } as const, {}, {
	shape: 'queue-1',
})
const filter = atom({ plugin: 'session-mover', key: 'filter' } as const, 'all')
const page = atom({ plugin: 'session-mover', key: 'page' } as const, 0)
const isConfirming = atom({ plugin: 'session-mover', key: 'isConfirming' } as const, false)
const message = atom({ plugin: 'session-mover', key: 'message' } as const, '')

function helper($: EngineInterface, args: string[], stdin?: string) {
	return $.process.run(['python3', `${$.plugin.root}/bin/session_mover.py`, ...args], {
		stdin,
		timeoutMs: 60_000,
	})
}

async function load($: EngineInterface) {
	const ran = await helper($, ['list'])
	if (ran.exitCode !== 0) {
		await update($, message, () => `Could not read sessions: ${ran.stderr.trim()}`)

		return
	}
	const next = JSON.parse(ran.stdout) as Listing
	await update($, listing, () => next)
	await update($, message, () => '')
}

async function applyMoves($: EngineInterface) {
	const moves = Object.entries(await read($, queue)).map(([sessionId, workspaceId]) => ({
		sessionId,
		workspaceId,
	}))
	if (moves.length === 0) return
	const ran = await helper($, ['apply', '-'], JSON.stringify(moves))
	await update($, isConfirming, () => false)
	if (ran.exitCode !== 0) {
		await update($, message, () => `Could not start the move: ${ran.stderr.trim()}`)

		return
	}
	await update($, queue, () => ({}))
	await update($, message, () => 'Quitting Claude to move the sessions; it reopens by itself.')
}

// Says once, in the first session after a restart, what the last apply did.
async function reportLastApply($: EngineInterface) {
	const ran = await helper($, ['last'])
	const last = JSON.parse(ran.stdout || 'null') as {
		at: number
		moved: { title: string; workspace: string }[]
		error: string | null
	} | null
	if (!last || last.at <= Number((await $.store.get('seenAt')) ?? 0)) return
	await $.store.set('seenAt', last.at)
	if (last.error) {
		$.ui.toast(`Session move failed: ${last.error}`)
	} else {
		const names = last.moved.map(one => `${one.title} → ${one.workspace}`).join(', ')
		$.ui.toast(`Moved ${last.moved.length} session(s): ${names}`)
	}
}

function short(text: string, size = 56) {
	return text.length > size ? `${text.slice(0, size - 1)}…` : text
}

export const register: Register = on => {
	on('session.start', async ($, e, next) => {
		await $.command.register({
			name: COMMAND,
			description: 'Move sessions between sidebar workspaces',
		})
		void reportLastApply($).catch(() => undefined)

		return next(e)
	})

	on('command.run', { command: COMMAND }, async $ => {
		await $.ui.open({ id: PANE, title: 'Move sessions' })
		await load($)

		return { text: 'Move sessions pane opened.' }
	})

	on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
		const table = $.ui.resolve(e)
		// Mobile has no Select; moving needs the computer the sessions live on anyway.
		if (e.surface === 'mobile' || !('Select' in table)) {
			const { Text } = table

			return <Text dimColor>Open this pane in Claude on your computer to move sessions.</Text>
		}
		const { Box, Button, Select, Text } = table
		const data = await read($, listing)
		const queued = await read($, queue)
		const shown = await read($, filter)
		const wanted = await read($, page)
		const confirming = await read($, isConfirming)
		const note = await read($, message)

		if (!data) {
			return (
				<Box flexDirection="column">
					<Text dimColor>{note || 'Loading sessions…'}</Text>
					<Button key="refresh" label="Reload" onPress={() => void load($)} />
				</Box>
			)
		}

		const names = new Map(data.workspaces.map(w => [w.id, w.name]))
		const where = (row: SessionRow) =>
			row.workspaceId && names.has(row.workspaceId) ? row.workspaceId : NONE
		const count = (id: string) => data.sessions.filter(row => where(row) === id).length
		const filterOptions = [
			{ value: 'all', label: `All workspaces (${data.sessions.length})` },
			...data.workspaces.map(w => ({ value: w.id, label: `${w.name} (${count(w.id)})` })),
			{ value: NONE, label: `No workspace (${count(NONE)})` },
		]
		const moveOptions = [
			...data.workspaces.map(w => ({ value: w.id, label: w.name })),
			{ value: NONE, label: 'No workspace' },
		]
		const list = shown === 'all' ? data.sessions : data.sessions.filter(row => where(row) === shown)
		const rowCost = JSON.stringify(moveOptions).length + ROW_BASE
		const perPage = Math.max(MIN_ROWS, Math.min(MAX_ROWS, Math.floor(ROWS_BUDGET / rowCost)))
		const pages = Math.max(1, Math.ceil(list.length / perPage))
		const current = Math.min(Math.max(wanted, 0), pages - 1)
		const rows = list.slice(current * perPage, (current + 1) * perPage)
		const turn = (to: number) => update($, page, () => to)
		const byId = new Map(data.sessions.map(row => [row.id, row]))
		const pending = Object.entries(queued).filter(([id]) => byId.has(id))

		const pick = (row: SessionRow, value: string) =>
			update($, queue, list => {
				const rest = { ...list }
				delete rest[row.id]
				// Picking where it already is, or nowhere, undoes the move.
				if (value === NONE || value === where(row)) return rest

				return { ...rest, [row.id]: value }
			})

		return (
			<Box flexDirection="column" gap={1}>
				<Box flexDirection="row" gap={1}>
					<Select
						key="filter"
						label="Show"
						options={filterOptions}
						value={shown}
						onSelect={value => {
							void update($, filter, () => value)
							void turn(0)
						}}
					/>
					<Button key="refresh" label="Reload" onPress={() => void load($)} />
				</Box>

				{note !== '' && <Text color="yellow">{note}</Text>}

				<Box flexDirection="column">
					{rows.length === 0 && <Text dimColor>No sessions here.</Text>}
					{rows.map(row => (
						<Box key={`row-${row.id}`} flexDirection="row" gap={1}>
							<Box flexGrow={1} flexDirection="column">
								<Text>{short(row.title)}</Text>
								{shown === 'all' && (
									<Text dimColor>{names.get(where(row)) ?? 'No workspace'}</Text>
								)}
							</Box>
							<Select
								key={`move-${row.id}`}
								label="Move to"
								options={moveOptions}
								value={queued[row.id] ?? where(row)}
								onSelect={value => void pick(row, value)}
							/>
						</Box>
					))}
					{pages > 1 && (
						<Box flexDirection="row" gap={1}>
							{current > 0 && (
								<Button key="prev-page" label="Previous" onPress={() => void turn(current - 1)} />
							)}
							<Text dimColor>
								Page {current + 1} of {pages} · {current * perPage + 1}–
								{current * perPage + rows.length} of {list.length}
							</Text>
							{current < pages - 1 && (
								<Button key="next-page" label="Next" onPress={() => void turn(current + 1)} />
							)}
						</Box>
					)}
				</Box>

				<Box flexDirection="column">
					<Text bold>Queued moves: {pending.length}</Text>
					{pending.map(([id, target]) => (
						<Box key={`queued-${id}`} flexDirection="row" gap={1}>
							<Text>
								{short(byId.get(id)?.title ?? id, 40)} → {names.get(target) ?? target}
							</Text>
							<Button
								key={`undo-${id}`}
								label="Undo"
								onPress={() =>
									void update($, queue, list => {
										const rest = { ...list }
										delete rest[id]

										return rest
									})
								}
							/>
						</Box>
					))}
				</Box>

				{pending.length > 0 && !confirming && (
					<Button
						key="apply"
						label="Apply & restart Claude"
						onPress={() => void update($, isConfirming, () => true)}
					/>
				)}
				{pending.length > 0 && confirming && (
					<Box flexDirection="column">
						<Text color="yellow">
							Claude will quit, move {pending.length} session(s), and reopen. Anything running
							in Claude stops. Backups go to ~/claude-session-backups.
						</Text>
						<Box flexDirection="row" gap={1}>
							<Button key="confirm" label="Quit and move" onPress={() => void applyMoves($)} />
							<Button
								key="cancel"
								label="Cancel"
								onPress={() => void update($, isConfirming, () => false)}
							/>
						</Box>
					</Box>
				)}
			</Box>
		)
	})
}
