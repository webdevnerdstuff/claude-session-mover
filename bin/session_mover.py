#!/usr/bin/env python3
"""Move Claude desktop Code sessions between sidebar workspaces.

  session_mover.py list                 JSON of workspaces and sessions
  session_mover.py apply <moves.json>   quit Claude, apply, reopen Claude ("-": stdin)
  session_mover.py last                 JSON of the last apply's result, or null

A workspace is an environment preset (ccd-session-presets.json). The sidebar
groups a session by the app's Local Storage map
`persisted.ccd-started-environments`, so a move edits that map as well as the
session's own file. Both can only be written while Claude is quit, which is
why `apply` detaches, quits the app, waits for it to exit, then reopens it.
"""
import glob, json, os, re, shutil, struct, subprocess, sys, time

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, "Library/Application Support/Claude")
SESSIONS_DIR = os.path.join(APP_DIR, "claude-code-sessions")
PRESETS = os.path.join(APP_DIR, "ccd-session-presets.json")
LDB_DIR = os.path.join(APP_DIR, "Local Storage/leveldb")
PROJECTS_DIR = os.path.join(HOME, ".claude/projects")
BACKUP_ROOT = os.path.join(HOME, "claude-session-backups")
LAST_RESULT = os.path.join(BACKUP_ROOT, "last-apply.json")
APP_BIN = "/Applications/Claude.app/Contents/MacOS/Claude"
APP_ID = "com.anthropic.claudefordesktop"
MAP_KEY = b"persisted.ccd-started-environments"
BLOCK = 32768


# --- LevelDB (read tables and logs, append one write) ----------------------

def read_bytes(path):
	with open(path, "rb") as f:
		return f.read()


def read_json(path):
	with open(path) as f:
		return json.load(f)


def varint(b, i):
	r = s = 0
	while True:
		c = b[i]; i += 1; r |= (c & 0x7F) << s; s += 7
		if c < 0x80:
			return r, i


def put_varint(n):
	out = bytearray()
	while n >= 0x80:
		out.append((n & 0x7F) | 0x80); n >>= 7
	out.append(n)
	return bytes(out)


def snappy(b):
	_, i = varint(b, 0); out = bytearray()
	while i < len(b):
		t = b[i]; i += 1; k = t & 3
		if k == 0:
			l = t >> 2
			if l >= 60:
				nb = l - 59; l = int.from_bytes(b[i:i + nb], "little"); i += nb
			l += 1; out += b[i:i + l]; i += l; continue
		if k == 1:
			l = ((t >> 2) & 7) + 4; o = ((t >> 5) << 8) | b[i]; i += 1
		elif k == 2:
			l = (t >> 2) + 1; o = int.from_bytes(b[i:i + 2], "little"); i += 2
		else:
			l = (t >> 2) + 1; o = int.from_bytes(b[i:i + 4], "little"); i += 4
		for _ in range(l):
			out.append(out[-o])
	return bytes(out)


def block_entries(b):
	nr = struct.unpack("<I", b[-4:])[0]; end = len(b) - 4 - 4 * nr; i = 0; key = b""
	while i < end:
		sh, i = varint(b, i); ns, i = varint(b, i); vl, i = varint(b, i)
		key = key[:sh] + b[i:i + ns]; i += ns; v = b[i:i + vl]; i += vl
		yield key, v


def read_block(f, off, size):
	raw = f[off:off + size]
	return snappy(raw) if f[off + size] == 1 else raw


def table_entries(path):
	f = read_bytes(path)
	foot = f[-48:]; i = 0
	_, i = varint(foot, i); _, i = varint(foot, i)
	io, i = varint(foot, i); isz, i = varint(foot, i)
	for _, h in block_entries(read_block(f, io, isz)):
		o, j = varint(h, 0); s, j = varint(h, j)
		for key, v in block_entries(read_block(f, o, s)):
			tag = struct.unpack("<Q", key[-8:])[0]
			yield key[:-8], tag >> 8, tag & 0xFF, v


def log_records(path):
	f = read_bytes(path); pos = 0; buf = b""
	while pos + 7 <= len(f):
		left = BLOCK - pos % BLOCK
		if left < 7:
			pos += left; continue
		length, rtype = struct.unpack("<H", f[pos + 4:pos + 6])[0], f[pos + 6]
		if rtype == 0 and length == 0:
			pos += left; continue
		data = f[pos + 7:pos + 7 + length]; pos += 7 + length
		if rtype == 1:
			yield data
		elif rtype == 2:
			buf = data
		elif rtype == 3:
			buf += data
		elif rtype == 4:
			yield buf + data; buf = b""


def log_entries(path):
	for rec in log_records(path):
		seq = struct.unpack("<Q", rec[:8])[0]; count = struct.unpack("<I", rec[8:12])[0]; i = 12
		for n in range(count):
			t = rec[i]; i += 1
			kl, i = varint(rec, i); k = rec[i:i + kl]; i += kl
			v = b""
			if t == 1:
				vl, i = varint(rec, i); v = rec[i:i + vl]; i += vl
			yield k, seq + n, t, v


def crc32c(data):
	crc = 0xFFFFFFFF
	for byte in data:
		crc ^= byte
		for _ in range(8):
			crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
	return crc ^ 0xFFFFFFFF


def masked_crc(data):
	c = crc32c(data)
	return (((c >> 15) | (c << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def append_record(path, payload):
	size = os.path.getsize(path); out = bytearray(); first = True
	while True:
		left = BLOCK - (size + len(out)) % BLOCK
		if left < 7:
			out += b"\x00" * left; continue
		frag = payload[:left - 7]; payload = payload[left - 7:]
		last = not payload
		rtype = 1 if first and last else 2 if first else 4 if last else 3
		out += struct.pack("<IHB", masked_crc(bytes([rtype]) + frag), len(frag), rtype) + frag
		first = False
		if last:
			break
	with open(path, "ab") as fh:
		fh.write(out)


def read_env_map(db=LDB_DIR):
	"""The newest value of the started-environments map: (map, raw, key, max_seq)."""
	best = None; max_seq = 0
	paths = sorted(glob.glob(os.path.join(db, "*.ldb"))) + sorted(glob.glob(os.path.join(db, "*.log")))
	for path in paths:
		try:
			it = table_entries(path) if path.endswith(".ldb") else log_entries(path)
			for k, seq, t, v in it:
				max_seq = max(max_seq, seq)
				if k.endswith(MAP_KEY) and (best is None or seq > best[0]):
					best = (seq, k, t, v)
		except Exception:
			continue
	if not best or best[2] != 1:
		return {}, None, None, max_seq
	_, key, _, raw = best
	text = raw[1:].decode("utf-16-le") if raw[:1] == b"\x00" else raw[1:].decode("latin-1")
	return json.loads(text), raw, key, max_seq


def write_env_map(changes, db=LDB_DIR):
	"""Point each session at its new workspace in one LevelDB write."""
	data, raw, key, max_seq = read_env_map(db)
	if raw is None:
		return "no map"
	m = data["state"]["bySessionId"]
	todo = {sid: env for sid, env in changes.items() if sid in m and m[sid] != env}
	if not todo:
		return "unchanged"
	m.update(todo)
	text = json.dumps(data, separators=(",", ":"))
	enc = raw[:1] + (text.encode("utf-16-le") if raw[:1] == b"\x00" else text.encode("latin-1"))
	seq = max_seq + 1000
	batch = struct.pack("<QI", seq, 1) + b"\x01" + put_varint(len(key)) + key + put_varint(len(enc)) + enc
	log = sorted(glob.glob(os.path.join(db, "*.log")))[-1]
	append_record(log, batch)
	got = [v for k, s, t, v in log_entries(log) if k == key and s == seq]
	if got != [enc]:
		raise RuntimeError("Local Storage write did not read back")
	return "updated %d" % len(todo)


# --- Sessions and workspaces -----------------------------------------------

def workspaces():
	out = []
	try:
		presets = read_json(PRESETS)
	except Exception:
		return out
	for acc in presets.get("byAccount", {}).values():
		for p in acc.get("presets", []):
			t = p.get("target") or {}
			if t.get("kind") == "local" and t.get("cwd"):
				out.append({"id": p["id"], "name": p.get("name") or t["cwd"], "cwd": t["cwd"]})
	return out


def session_files():
	return glob.glob(os.path.join(SESSIONS_DIR, "*", "*", "local_*.json"))


def list_sessions():
	spaces = workspaces()
	by_cwd = {}
	for w in spaces:
		by_cwd.setdefault(w["cwd"], w["id"])
	env_map = (read_env_map()[0].get("state") or {}).get("bySessionId", {})
	rows = []
	for path in session_files():
		try:
			d = read_json(path)
		except Exception:
			continue
		if d.get("isArchived"):
			continue
		sid = d.get("sessionId")
		ws = env_map.get(sid) or d.get("envScopeId") or by_cwd.get(d.get("cwd"))
		rows.append({
			"id": sid,
			"title": d.get("title") or "Untitled",
			"cwd": d.get("cwd"),
			"workspaceId": ws,
			"lastActivityAt": d.get("lastActivityAt") or 0,
		})
	rows.sort(key=lambda r: -r["lastActivityAt"])
	return {"workspaces": spaces, "sessions": rows}


def project_dir(cwd):
	return os.path.join(PROJECTS_DIR, re.sub(r"[^a-zA-Z0-9]", "-", cwd))


def git_anchor(cwd, trusted_at):
	try:
		root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
		common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, timeout=10)
	except Exception:
		return []
	if root.returncode or common.returncode:
		return []
	return [{"gitRoot": root.stdout.strip(), "commonDir": common.stdout.strip(), "trustedAt": trusted_at}]


def move_session(path, ws, backup):
	d = read_json(path)
	old_cwd = d.get("cwd"); new_cwd = ws["cwd"]
	shutil.copy2(path, backup)
	cli = d.get("cliSessionId")
	if old_cwd != new_cwd:
		trusted = next((a.get("trustedAt") for a in d.get("gitAnchors") or []), int(time.time() * 1000))
		d["cwd"] = new_cwd
		d["originCwd"] = new_cwd
		d["gitAnchors"] = git_anchor(new_cwd, trusted)
	d["envScopeId"] = ws["id"]
	d["startedFromEnvironmentId"] = ws["id"]
	with open(path, "w") as f:
		json.dump(d, f)
	# Copy the conversation so the session resumes from its new folder.
	if cli and old_cwd and old_cwd != new_cwd:
		src, dst = project_dir(old_cwd), project_dir(new_cwd)
		os.makedirs(dst, exist_ok=True)
		if os.path.exists(os.path.join(src, cli + ".jsonl")) and not os.path.exists(os.path.join(dst, cli + ".jsonl")):
			shutil.copy2(os.path.join(src, cli + ".jsonl"), dst)
		if os.path.isdir(os.path.join(src, cli)) and not os.path.exists(os.path.join(dst, cli)):
			shutil.copytree(os.path.join(src, cli), os.path.join(dst, cli))
	return d.get("title") or d.get("sessionId")


# --- Apply (detached, around a restart of Claude) ---------------------------

def app_running():
	out = subprocess.run(["ps", "-axo", "comm"], capture_output=True, text=True).stdout
	return APP_BIN in out.splitlines()


def detach(log_path):
	"""Leave the app's process tree so quitting Claude does not end this."""
	if os.fork():
		os._exit(0)
	os.setsid()
	if os.fork():
		os._exit(0)
	fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
	null = os.open(os.devnull, os.O_RDONLY)
	os.dup2(null, 0); os.dup2(fd, 1); os.dup2(fd, 2)


def apply(moves_path):
	moves = json.load(sys.stdin) if moves_path == "-" else read_json(moves_path)
	stamp = time.strftime("%Y%m%d-%H%M%S")
	backup = os.path.join(BACKUP_ROOT, "mover-" + stamp)
	os.makedirs(backup, exist_ok=True)
	with open(os.path.join(backup, "moves.json"), "w") as f:
		json.dump(moves, f)
	log_path = os.path.join(backup, "apply.log")
	print(json.dumps({"backup": backup, "log": log_path}))
	sys.stdout.flush()
	detach(log_path)
	result = {"at": int(time.time() * 1000), "backup": backup, "moved": [], "error": None}
	try:
		time.sleep(1)
		subprocess.run(["osascript", "-e", 'tell application id "%s" to quit' % APP_ID], timeout=30)
		for _ in range(240):
			if not app_running():
				break
			time.sleep(0.5)
		else:
			raise RuntimeError("Claude did not quit within 2 minutes; nothing was changed.")
		time.sleep(1)
		spaces = {w["id"]: w for w in workspaces()}
		files = {os.path.basename(p)[:-5]: p for p in session_files()}
		shutil.copytree(LDB_DIR, os.path.join(backup, "leveldb"))
		changes = {}
		for m in moves:
			ws = spaces.get(m["workspaceId"]); path = files.get(m["sessionId"])
			if not ws or not path:
				print("skip", m); continue
			title = move_session(path, ws, backup)
			changes[m["sessionId"]] = ws["id"]
			result["moved"].append({"title": title, "workspace": ws["name"]})
			print("moved", title, "->", ws["name"])
		print("local storage:", write_env_map(changes))
	except Exception as err:
		result["error"] = str(err)
		print("error:", err)
	finally:
		with open(LAST_RESULT, "w") as f:
			json.dump(result, f)
		subprocess.run(["open", "-b", APP_ID])


if __name__ == "__main__":
	if len(sys.argv) >= 2 and sys.argv[1] == "list":
		print(json.dumps(list_sessions()))
	elif len(sys.argv) >= 2 and sys.argv[1] == "last":
		print(read_bytes(LAST_RESULT).decode() if os.path.exists(LAST_RESULT) else "null")
	elif len(sys.argv) >= 3 and sys.argv[1] == "apply":
		apply(sys.argv[2])
	else:
		sys.exit(__doc__)
