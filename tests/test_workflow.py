"""Exercise real disk/SQLite/HTTP workflows using synthetic art only."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
GUID = "a" * 32
DUP = "b" * 32


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="art-restyler-test-")
        self.base = Path(self.temporary.name)
        self.tool = self.base / "tool"
        self.tool.mkdir()
        for source in ROOT.glob("*.py"):
            shutil.copyfile(source, self.tool / source.name)
        shutil.copyfile(ROOT / "rules.example.json", self.tool / "rules.json")
        shutil.copyfile(ROOT / "rules.example.json", self.tool / "rules.example.json")
        shutil.copytree(ROOT / "web", self.tool / "web")
        self.project = self.base / "Unity Game"
        (self.project / "Assets").mkdir(parents=True)
        (self.project / "ProjectSettings").mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        (self.tool / "project.json").write_text(json.dumps({
            "project_root": str(self.project), "port": self.port, "excluded_dirs": []}), encoding="utf-8")
        image = Image.new("RGBA", (64, 64))
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 12, 51, 51), fill=(210, 50, 20, 255))
        image.save(self.project / "Assets/hero.png")
        shutil.copyfile(self.project / "Assets/hero.png", self.project / "Assets/hero_copy.png")
        for filename, guid in (("hero.png", GUID), ("hero_copy.png", DUP)):
            (self.project / "Assets" / (filename + ".meta")).write_text(
                f"fileFormatVersion: 2\nguid: {guid}\nTextureImporter:\n  spriteMode: 1\n", encoding="utf-8")
        self.saved_modules = {name: sys.modules.pop(name, None) for name in ("scanner", "engine", "batch_folder")}
        sys.path.insert(0, str(self.tool))
        self.scanner = importlib.import_module("scanner")
        self.engine = importlib.import_module("engine")
        self.batch = importlib.import_module("batch_folder")
        self.scanner.scan(log=lambda _: None)
        self.original = (self.project / "Assets/hero.png").read_bytes()
        self.metadata = (self.project / "Assets/hero.png.meta").read_bytes()

    def tearDown(self):
        sys.path.remove(str(self.tool))
        for name, previous in self.saved_modules.items():
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous
        self.temporary.cleanup()

    def asset(self, guid=GUID):
        with contextlib.closing(self.engine.connect()) as db:
            return self.engine.get_asset(db, guid)

    def generate(self):
        asset = self.asset()
        info = self.engine.prepare_input(asset)
        raw = Image.open(self.tool / "data/work" / GUID / "input.png").convert("RGBA")
        pixels = raw.load()
        for y in range(raw.height):
            for x in range(raw.width):
                r, g, b, a = pixels[x, y]
                if a:
                    pixels[x, y] = (20, 90, 230, a)
        body = io.BytesIO()
        raw.save(body, "PNG")
        result = self.engine.import_manual(GUID, body.getvalue())
        return info, result

    def test_scan_duplicates_and_manual_edits_survive(self):
        self.assertEqual(self.asset(DUP)["dup_of"], GUID)
        with contextlib.closing(self.engine.connect()) as db:
            db.execute("UPDATE assets SET category='Custom', mode='recolor', manual=1 WHERE guid=?", (GUID,))
            db.commit()
        self.scanner.scan(log=lambda _: None)
        self.assertEqual(self.asset()["category"], "Custom")
        self.assertEqual(self.asset()["mode"], "recolor")

    def test_apply_requires_acceptance_and_rollback_preserves_metadata(self):
        self.engine.queue([GUID], "manual")
        info, result = self.generate()
        self.assertIn("checks", result)
        self.assertEqual(info["canvas"], [1024, 1024])
        self.assertEqual(self.engine.apply([GUID])["applied"], 0)
        self.engine.review(GUID, "accept")
        applied = self.engine.apply([GUID])
        self.assertEqual(applied, {"applied": 2, "skipped": []})
        self.assertNotEqual((self.project / "Assets/hero.png").read_bytes(), self.original)
        self.assertEqual((self.project / "Assets/hero.png.meta").read_bytes(), self.metadata)
        with Image.open(self.project / "Assets/hero.png") as image:
            self.assertEqual(image.size, (64, 64))
        self.assertEqual(self.engine.rollback([GUID]), {"restored": 2, "skipped": []})
        for filename in ("hero.png", "hero_copy.png"):
            self.assertEqual((self.project / "Assets" / filename).read_bytes(), self.original)
        self.assertEqual((self.project / "Assets/hero.png.meta").read_bytes(), self.metadata)

    def test_apply_refuses_external_edit(self):
        self.generate()
        self.engine.review(GUID, "accept")
        target = self.project / "Assets/hero.png"
        target.write_bytes(b"external edit")
        result = self.engine.apply([GUID])
        self.assertTrue(any("changed outside" in item["why"] for item in result["skipped"]))
        self.assertEqual(target.read_bytes(), b"external edit")

    def test_rollback_refuses_external_edit(self):
        self.generate()
        self.engine.review(GUID, "accept")
        self.engine.apply([GUID])
        target = self.project / "Assets/hero.png"
        target.write_bytes(b"newer external edit")
        result = self.engine.rollback([GUID])
        self.assertTrue(any("changed outside" in item["why"] for item in result["skipped"]))
        self.assertEqual(target.read_bytes(), b"newer external edit")
        self.assertEqual((self.tool / "data/backup" / GUID / "original.png").read_bytes(), self.original)

    def test_spine_rotated_regions(self):
        (self.project / "Assets/hero.atlas").write_text(
            "hero.png\nsize: 64,64\nformat: RGBA8888\nhead\n  rotate: true\n  xy: 4,8\n  size: 10,20\n", encoding="utf-8")
        self.scanner.scan(log=lambda _: None)
        self.assertEqual(self.asset()["kind"], "spine")
        self.assertEqual(self.asset()["mode"], "part_sheet")
        with contextlib.closing(self.engine.connect()) as db:
            region = dict(db.execute("SELECT * FROM slices WHERE texture_guid=?", (GUID,)).fetchone())
        self.assertEqual((region["w"], region["h"]), (20, 10))
        self.assertIn("rotated", region["name"])

    def test_secrets_masked_and_not_in_database(self):
        fake = "synthetic-test-key-not-a-real-credential"
        self.engine.save_settings({"openai_api_key": fake, "style_prompt": "Watercolor"})
        self.assertNotIn(fake, json.dumps(self.engine.public_settings()))
        self.assertNotIn(fake.encode(), (self.tool / "data/restyler.db").read_bytes())
        self.assertEqual(json.loads((self.tool / "data/secrets.json").read_text())["openai_api_key"], fake)

    def test_init_project_and_catalog_isolation(self):
        result = subprocess.run([sys.executable, "init_project.py", "--project", str(self.project),
                                 "--port", "8768", "--exclude", "Assets/ThirdParty/"],
                                cwd=self.tool, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads((self.tool / "project.json").read_text())
        self.assertEqual(config["port"], 8768)
        other = self.base / "Other Game"
        (other / "Assets").mkdir(parents=True)
        (other / "ProjectSettings").mkdir()
        result = subprocess.run([sys.executable, "init_project.py", "--project", str(other)],
                                cwd=self.tool, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another project's catalog", result.stderr)
        self.assertEqual(json.loads((self.tool / "project.json").read_text()), config)

    def test_batch_export_preserves_prompts_and_imports_by_guid(self):
        self.engine.queue([GUID], "manual")
        asset = self.asset()
        self.engine.prepare_input(asset)
        full_prompt = "Keep the exact rig slots. My unique gameplay rules."
        def response(path):
            return {"assets": [asset]} if path == "/api/assets" else {"prompt": full_prompt}
        with patch.object(self.batch, "api", side_effect=response):
            self.batch.export(10, "pilot")
        folder = self.tool / "data/batch/pilot"
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(manifest[0]["prompt"], full_prompt)
        output = folder / manifest[0]["output"]
        shutil.copyfile(folder / manifest[0]["input"], output)
        os.utime(output, (time.time() - 10, time.time() - 10))
        self.assertEqual(self.batch.import_folder(folder), 1)
        self.assertEqual(self.batch.import_folder(folder), 0)
        self.assertEqual(self.engine.inbox_target(f"asset__{GUID[:8]}.png"), GUID)
        self.assertTrue((self.tool / "data/inbox" / f"asset__{GUID[:8]}.png").is_file())
        self.assertEqual(self.batch.API, f"http://127.0.0.1:{self.port}")

    def test_server_http_and_origin_protection(self):
        process = subprocess.Popen([sys.executable, "server.py", "--no-browser"], cwd=self.tool,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        base = f"http://127.0.0.1:{self.port}"
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base + "/api/assets", timeout=1) as response:
                        catalog = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    if process.poll() is not None:
                        self.fail(process.stderr.read().decode())
                    time.sleep(0.1)
            else:
                self.fail("Server did not start")
            self.assertEqual(len(catalog["assets"]), 2)
            with urllib.request.urlopen(base) as response:
                self.assertIn(b"Art Restyler", response.read())
            for headers in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"},
                            {"Sec-Fetch-Site": "cross-site"}):
                request = urllib.request.Request(base + "/api/settings", b"{}", headers=headers)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
            request = urllib.request.Request(base + "/api/settings", b'{"style_prompt":"Ink"}',
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["style_prompt"], "Ink")
            def post(path, value):
                request = urllib.request.Request(base + path, json.dumps(value).encode(),
                                                 headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            self.assertEqual(post("/api/queue", {"guids": [GUID], "source": "manual"})["queued"], 1)
            with urllib.request.urlopen(base + "/api/manual/" + GUID) as response:
                self.assertIn("prompt", json.load(response))
            prepared = self.tool / "data/work" / GUID / "input.png"
            with Image.open(prepared) as image:
                output = Image.new("RGBA", image.size, (20, 90, 230, 0))
                output.putalpha(image.getchannel("A"))
                png = io.BytesIO()
                output.save(png, "PNG")
            request = urllib.request.Request(base + "/api/import/" + GUID, png.getvalue(),
                                             headers={"Content-Type": "image/png"})
            with urllib.request.urlopen(request) as response:
                self.assertIn("checks", json.load(response))
            post("/api/review", {"guid": GUID, "verdict": "accept"})
            self.assertEqual(post("/api/apply", {"guids": [GUID]})["applied"], 2)
            self.assertEqual(post("/api/rollback", {"guids": [GUID]})["restored"], 2)
            self.assertEqual((self.project / "Assets/hero.png").read_bytes(), self.original)
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
