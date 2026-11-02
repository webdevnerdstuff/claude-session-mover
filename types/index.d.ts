export type Workspace = { id: string; name: string; cwd: string }

export type SessionRow = {
	id: string
	title: string
	cwd: string | null
	workspaceId: string | null
	lastActivityAt: number
}

export type Listing = { workspaces: Workspace[]; sessions: SessionRow[] }

declare module 'claude-code' {
	interface PluginState {
		'session-mover': {
			// What `session_mover.py list` answered; null until loaded.
			listing: Shaped<Listing | null>
			// Session id -> workspace id it should move to.
			queue: Shaped<Record<string, string>>
			// The workspace the pane shows, or 'all'.
			filter: string
			isConfirming: boolean
			message: string
		}
	}
}
