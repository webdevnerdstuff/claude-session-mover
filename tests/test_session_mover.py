"""Tests for bin/session_mover.py against temporary copies of the app's files."""
import json, os, struct, sys, tempfile, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import session_mover as m  # noqa: E402

def read_json(path):
	with open(path) as f:
		return json.load(f)


def write_text(path, text):
	with open(path, "w") as f:
		f.write(text)


KEY = b"_https://claude.ai\x00\x01" + m.MAP_KEY


def write_map(db, by_session, seq=10):
	"""A LevelDB log holding one write of the map, as the app stores it."""
	text = json.dumps({"state": {"bySessionId": by_session}, "version": 0})
	value = b"\x01" + text.encode("latin-1")
	batch = struct.pack("<QI", seq, 1) + b"\x01" + m.put_varint(len(KEY)) + KEY + m.put_varint(len(value)) + value
	path = os.path.join(db, "000003.log")
	open(path, "wb").close()
	m.append_record(path, batch)


class EnvMapTest(unittest.TestCase):
	def test_reads_back_a_change(self):
		with tempfile.TemporaryDirectory() as db:
			write_map(db, {"local_a": "ws-old", "local_b": "ws-old"})
			self.assertEqual(m.write_env_map({"local_a": "ws-new"}, db), "updated 1")
			got = m.read_env_map(db)[0]["state"]["bySessionId"]
			self.assertEqual(got, {"local_a": "ws-new", "local_b": "ws-old"})

	def test_leaves_unknown_sessions_out(self):
		# Adding a key would change the stored size the app keeps beside the map.
		with tempfile.TemporaryDirectory() as db:
			write_map(db, {"local_a": "ws-old"})
			self.assertEqual(m.write_env_map({"local_z": "ws-new"}, db), "unchanged")

	def test_spans_log_blocks(self):
		# A map larger than one 32 KiB block is written as fragments.
		with tempfile.TemporaryDirectory() as db:
			many = {"local_%05d" % n: "ws-old" for n in range(2000)}
			write_map(db, many)
			m.write_env_map({"local_00042": "ws-new"}, db)
			self.assertEqual(m.read_env_map(db)[0]["state"]["bySessionId"]["local_00042"], "ws-new")


class MoveSessionTest(unittest.TestCase):
	def test_points_the_session_at_the_workspace(self):
		with tempfile.TemporaryDirectory() as root:
			projects = os.path.join(root, "projects"); m.PROJECTS_DIR = projects
			old_cwd, new_cwd = os.path.join(root, "old"), os.path.join(root, "new")
			os.makedirs(old_cwd); os.makedirs(new_cwd)
			src = m.project_dir(old_cwd); os.makedirs(os.path.join(src, "cli-1"))
			write_text(os.path.join(src, "cli-1.jsonl"), "{}\n")
			path = os.path.join(root, "local_a.json")
			write_text(path, json.dumps({"sessionId": "local_a", "cliSessionId": "cli-1", "title": "A", "cwd": old_cwd, "originCwd": old_cwd, "envScopeId": "ws-old"}))
			backup = os.path.join(root, "backup"); os.makedirs(backup)

			m.move_session(path, {"id": "ws-new", "name": "New", "cwd": new_cwd}, backup)

			d = read_json(path)
			self.assertEqual((d["cwd"], d["originCwd"], d["envScopeId"], d["startedFromEnvironmentId"]), (new_cwd, new_cwd, "ws-new", "ws-new"))
			self.assertTrue(os.path.exists(os.path.join(m.project_dir(new_cwd), "cli-1.jsonl")))
			self.assertTrue(os.path.isdir(os.path.join(m.project_dir(new_cwd), "cli-1")))
			self.assertEqual(read_json(os.path.join(backup, "local_a.json"))["cwd"], old_cwd)


	def test_linux_folder_move(self):
		# A Linux session file: no gitAnchors (looked up by the app), the folder kept in folderPlace too.
		with tempfile.TemporaryDirectory() as root:
			m.PROJECTS_DIR = os.path.join(root, "projects")
			old_cwd, new_cwd = os.path.join(root, "old"), os.path.join(root, "new")
			os.makedirs(old_cwd); os.makedirs(new_cwd)
			path = os.path.join(root, "local_b.json")
			write_text(path, json.dumps({"sessionId": "local_b", "cwd": old_cwd, "originCwd": old_cwd, "gitAnchorsLookupOnly": True,
				"gitAnchorsFolderRealpath": old_cwd, "folderPlace": {"cwd": old_cwd, "realPath": old_cwd}, "startedFromEnvironmentId": None}))
			backup = os.path.join(root, "backup"); os.makedirs(backup)

			m.move_session(path, {"id": m.FOLDER + new_cwd, "name": "new", "cwd": new_cwd}, backup)

			d = read_json(path)
			self.assertEqual((d["cwd"], d["originCwd"], d["gitAnchorsFolderRealpath"]), (new_cwd, new_cwd, os.path.realpath(new_cwd)))
			self.assertEqual(d["folderPlace"], {"cwd": new_cwd, "realPath": os.path.realpath(new_cwd)})
			self.assertNotIn("gitAnchors", d)
			self.assertNotIn("envScopeId", d)
			self.assertIsNone(d["startedFromEnvironmentId"])


class WorkspacesTest(unittest.TestCase):
	def test_linux_offers_each_folder_once(self):
		with tempfile.TemporaryDirectory() as root:
			sessions = os.path.join(root, "claude-code-sessions", "acc", "org"); os.makedirs(sessions)
			saved = (m.LINUX, m.SESSIONS_DIR, m.PRESETS)
			m.LINUX, m.SESSIONS_DIR, m.PRESETS = True, os.path.join(root, "claude-code-sessions"), os.path.join(root, "presets.json")
			try:
				write_text(m.PRESETS, json.dumps({"byAccount": {"acc": {"presets": [{"id": "p1", "name": "Tools", "target": {"kind": "local", "cwd": "/w/tools"}}]}}}))
				for n, cwd in enumerate(["/w/tools", "/w/site", "/w/site", "/w/game"]):
					write_text(os.path.join(sessions, "local_%d.json" % n), json.dumps({"sessionId": "local_%d" % n, "cwd": cwd}))
				got = [(w["id"], w["name"]) for w in m.all_workspaces()]
				self.assertEqual(got, [("p1", "Tools"), ("folder:/w/game", "game"), ("folder:/w/site", "site")])
			finally:
				m.LINUX, m.SESSIONS_DIR, m.PRESETS = saved


class BackupTest(unittest.TestCase):
	def test_two_runs_in_one_second_get_two_folders(self):
		with tempfile.TemporaryDirectory() as root:
			saved = m.BACKUP_ROOT; m.BACKUP_ROOT = root
			try:
				self.assertNotEqual(m.backup_dir(), m.backup_dir())
			finally:
				m.BACKUP_ROOT = saved


if __name__ == "__main__":
	unittest.main()
