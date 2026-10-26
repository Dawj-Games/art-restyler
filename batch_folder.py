"""Export queued prompts and prepared inputs for any image-capable agent."""
import argparse
import json
from pathlib import Path
import shutil
import time
import urllib.request

import scanner

HERE = Path(__file__).resolve().parent
API = f"http://127.0.0.1:{int(scanner.CONFIG.get('port', 8765))}"


def api(path):
    with urllib.request.urlopen(API + path, timeout=120) as response:
        return json.load(response)


def export(limit=20, name=None):
    if name and (Path(name).name != name or name in (".", "..") or "/" in name or "\\" in name):
        raise ValueError("Batch name must be a single folder name")
    rows = [a for a in api("/api/assets")["assets"]
            if a["status"] in ("awaiting_manual", "checks_failed") and a["mode"] != "skip"][:limit]
    if not rows:
        print("Manual queue is empty. Queue assets with the manual source in the app first.")
        return
    folder = HERE / "data" / "batch" / (name or time.strftime("%Y%m%d_%H%M%S"))
    folder.mkdir(parents=True, exist_ok=False)
    manifest = []
    for index, asset in enumerate(rows, 1):
        prompt = api("/api/manual/" + asset["guid"])["prompt"]
        input_name, output_name = f"{index:03d}_input.png", f"{index:03d}_out.png"
        shutil.copyfile(HERE / "data/work" / asset["guid"] / "input.png", folder / input_name)
        manifest.append({"index": index, "guid": asset["guid"], "name": asset["name"],
                         "input": input_name, "output": output_name, "prompt": prompt})
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (folder / "INSTRUCTIONS.md").write_text(
        "# Art Restyler image batch\n\nRead manifest.json. For each entry, inspect the input PNG, "
        "follow its full prompt, and save a PNG to the entry's output filename in this folder. "
        "Use the prepared input canvas size. Preserve transparency, slots, gameplay "
        "symbols and animation constraints specified by the prompt. The original asset dimensions "
        "in the prompt describe the destination; the prepared canvas can be larger.\n\n"
        "If you lack an image generation tool, report that limitation. Do not create placeholder "
        "images. If your image tool saves elsewhere, copy the actual generated PNG to the exact "
        "output filename. Skip an output that already exists (including .imported). Report failed "
        "entries. Do not edit Unity assets or approve/apply results; the user reviews them in "
        "Art Restyler.\n", encoding="utf-8")
    print(folder)


def import_folder(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    inbox = HERE / "data/inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in manifest:
        if Path(entry["output"]).name != entry["output"]:
            raise ValueError("Output must be a filename within the batch folder")
        guid = entry["guid"]
        if len(guid) != 32 or any(c not in "0123456789abcdef" for c in guid):
            raise ValueError("Invalid asset GUID in manifest")
        source = folder / entry["output"]
        if not source.is_file() or time.time() - source.stat().st_mtime < 2:
            continue
        target = inbox / f"asset__{guid[:8]}.png"
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(target)
        source.rename(source.with_name(source.name + ".imported"))
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exp = commands.add_parser("export", help="Export the manual queue; the server must be running")
    exp.add_argument("--limit", type=int, default=20)
    exp.add_argument("--name")
    for command in ("import", "watch"):
        commands.add_parser(command).add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        if args.limit < 1:
            parser.error("--limit must be positive")
        export(args.limit, args.name)
    elif args.command == "import":
        print("Copied to inbox:", import_folder(args.folder))
    else:
        print("Watching outputs; Ctrl+C to stop. Keep the app running to import inbox files.")
        try:
            while True:
                count = import_folder(args.folder)
                if count:
                    print("Copied to inbox:", count, flush=True)
                time.sleep(3)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
