"""Create local configuration for one Unity project. No Unity files are modified."""
import argparse
import json
from pathlib import Path
import shutil

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Unity project directory")
    parser.add_argument("--name", help="Display name; defaults to the project folder name")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--exclude", action="append", default=[], help="Assets-relative folder; repeatable")
    args = parser.parse_args()
    project = args.project.expanduser().resolve()
    if not (project / "Assets").is_dir() or not (project / "ProjectSettings").is_dir():
        parser.error("The project must contain Assets/ and ProjectSettings/ directories.")
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535.")
    config_path = HERE / "project.json"
    if config_path.exists() and (HERE / "data/restyler.db").exists():
        previous = json.loads(config_path.read_text(encoding="utf-8"))
        old_root = (HERE / previous["project_root"]).resolve()
        if old_root != project:
            parser.error("This installation already has another project's catalog. Use a separate clone.")
    excluded = ["Assets/Plugins/", "Assets/Editor/", "Assets/TextMesh Pro/"]
    for value in args.exclude:
        value = value.replace("\\", "/").rstrip("/") + "/"
        if not value.startswith("Assets/") or ".." in value.split("/"):
            parser.error("Excluded directories must start with Assets/ and contain no '..'.")
        excluded.append(value)
    config = {"name": args.name or project.name, "project_root": str(project),
              "port": args.port, "excluded_dirs": list(dict.fromkeys(excluded))}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if not (HERE / "rules.json").exists():
        shutil.copyfile(HERE / "rules.example.json", HERE / "rules.json")
    print(f"Configured {config['name']} at {project}")
    print(f"Run python server.py and open http://127.0.0.1:{args.port}/")


if __name__ == "__main__":
    main()
