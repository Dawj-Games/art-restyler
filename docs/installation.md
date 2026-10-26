# Installation and maintenance

## Choose a location

Clone into `<UnityProject>/Tools/ArtRestyler`, use a Git submodule there, or keep a separate clone outside the Unity repository. Initialization writes an absolute project path, so the tool does not depend on a particular folder depth.

Each clone has one `project.json` and one `data/` directory. Use separate clones for separate games. Initialization refuses to repoint an installation with an existing catalog to a different project. Give each simultaneously running installation a unique port:

```sh
python init_project.py --project "/path/to/game" --port 8766 --name "My Game" --exclude "Assets/ThirdParty/"
```

If using a submodule, from the Unity repository:

```sh
git submodule add https://github.com/Dawj-Games/art-restyler.git Tools/ArtRestyler
git submodule update --init --recursive
```

Collaborators clone that game with `git clone --recurse-submodules`. Each person initializes their own local configuration. Submodules track the tool version separately from game assets.

## Python setup

Install Python 3.10 or newer. On Windows, make Python available on PATH. On Linux, your distribution may require its Python venv package. Create a `.venv` and install `requirements.txt`; see the README for platform commands. Activation is optional when you use the virtual environment's Python path directly.

Run `init_project.py` once, then `server.py`. Browser opening is optional; `python server.py --no-browser` prints the local URL. No Unity Editor package, provider key, or paid service is required for scanning and manual import.

## Updates

Stop the server, back up local configuration and `data/`, then:

```sh
git pull --ff-only
python -m pip install -r requirements.txt
python server.py
```

Use the virtual environment's Python. For a submodule, choose the desired tool commit and commit the updated submodule reference in the parent game repository. Keep the local data folder: it contains original-image backups and attempt history. Do not copy a live SQLite database between machines; stop the app first. Do not have two servers share one catalog.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| Missing project configuration | Run `init_project.py --project "..."`. The directory must contain Assets and ProjectSettings. |
| Port in use | Stop the existing server, or change `port` in `project.json` and restart. |
| Empty catalog | Check the root path and exclusions. Images need their Unity `.meta` files with GUIDs. |
| Missing references | Use text serialization for relevant Unity files. Runtime-selected assets may have no static reference. |
| Unexpected categories | Edit local `rules.json`, restart, then rescan. Manual category/mode changes survive rescans. |
| Result fails checks | Read check details, correct the prompt, and regenerate. Inspect rigs in motion before overriding checks. |
| Output not imported | Keep the server running; check PNG format, exact inbox filename, and queue state. For batch import, wait at least two seconds after writing the output. |
| Cannot apply PSD or TGA | Applying currently supports PNG/JPEG only. Other readable formats may still appear in the catalog. |
| Apply detects an external change | Review the external edit before rescanning. The tool refuses to silently overwrite unexpected image bytes. |
| Rollback detects an external change | Recover the original from `data/backup/<guid>/original.<ext>` manually after reviewing the newer edit. |
| Optional API generation fails | Check the configured key/model and provider error. Manual import remains available. |

## Uninstall

Stop the server. Roll back applied images first if that is your intent. Archive the configuration and backups, then remove the tool clone or submodule using your normal repository workflow. Removing the tool does not undo already-applied art.
