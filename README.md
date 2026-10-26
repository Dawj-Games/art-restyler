# Art Restyler for Unity

**Restyle your Unity project's 2D art with an AI-assisted workflow you can review and roll back.**

Art Restyler catalogs textures, follows their GUID references, prepares image-generation prompts and inputs, imports replacement images, and checks their layout before you accept and apply them. It runs locally as a Python web app alongside your Unity project.

Built by [Dawj Games](https://github.com/Dawj-Games). Open source under the [MIT License](LICENSE).

## What it does

- Catalogs textures, sprite slices, and references in text-serialized Unity assets.
- Groups assets using configurable name patterns, paths, and usage hints.
- Supports regeneration, recoloring, and constrained animation part sheets.
- Detects Spine text atlases, including rotated packed regions.
- Builds per-asset prompts combining your style, gameplay rules, and layout constraints.
- Accepts images from Codex, Claude Code, other image-capable agents, or manual uploads. A bundled OpenAI image API adapter is optional.
- Checks transparency, silhouette, part placement, grayscale, and visual change where applicable.
- Writes accepted replacements while preserving `.meta` files, GUIDs, pivots, and slice definitions.
- Backs up original image bytes and supports rollback, including linked duplicate textures.

**Initial release: v0.1.0.** Focused on 2D texture workflows. Automatic checks help catch layout problems; visual review and animation checks in Unity are still necessary. This is a standalone developer tool, not a Unity Package Manager package or an MCP server.

## Quick start

Requires Git and Python 3.10+. Unity does not have to be running for scanning. Use one clone of the tool per Unity project.

Clone into your game's `Tools` folder (or clone anywhere and point it at your game):

```sh
git clone https://github.com/Dawj-Games/art-restyler.git Tools/ArtRestyler
cd Tools/ArtRestyler
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe init_project.py --project "C:\Projects\MyGame"
.\.venv\Scripts\python.exe server.py
```

macOS / Linux:

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python init_project.py --project "/path/to/MyGame"
.venv/bin/python server.py
```

Use `python3` instead of `python` where needed. The project directory must contain `Assets/` and `ProjectSettings/`. The app opens at **http://127.0.0.1:8765/** and scans on first launch.

After configuration, Windows users can double-click `Start Art Restyler.bat`; macOS / Linux users can run `sh start.sh`. These launchers create the virtual environment and install dependencies if needed. Use `server.py --no-browser` to run without opening a window.

If installing inside a game repository, add `Tools/ArtRestyler/` to the game's `.gitignore`, or deliberately manage the tool as a Git submodule. Keep replacement game assets in your game's Git history. See [installation](docs/installation.md).

## Your first restyle

1. Commit or back up the Unity project before changing art.
2. Open **Settings**. Write the target art style and the gameplay rules that must survive: rarity colors, recognizable icons, readable text, team colors, rig constraints, and so on.
3. Inspect the catalog. Adjust categories and modes. Exclude third-party folders and mark utility textures as skipped.
4. Select a small group of assets and queue them with the **manual** source.
5. Use the Manual queue's prompt and prepared input with your image tool or agent. Save its PNG to the exact inbox path in the prompt, or upload it through the app.
6. Inspect the generated attempt and check results. Reject with feedback or accept the result after visual review.
7. Apply accepted assets. Inspect the result in Unity, including animations, slicing, UI scaling, and runtime tinting. Roll back from the app when needed.

Start with a few assets before doing a whole project. Keep `data/backup/` until you no longer need rollback.

## Use with Codex, Claude Code, or another agent

The local integration uses files and a loopback HTTP API. There is no required model vendor or agent plugin. The agent needs access to the tool/project folders and an image-generation or image-editing capability to produce art. A text-only model can help plan and categorize but cannot generate replacement images by itself.

Paste this into your agent, replacing the paths:

> Help me restyle this Unity project's 2D art using Art Restyler at `<tool directory>`. Read its README and `docs/agents.md` first. The Unity project is `<project directory>`. Inspect the catalog and original assets, identify gameplay and animation constraints, and help define the style and design rules. Prepare a small manual batch. Use an available image tool to generate the requested outputs and save them to the exact filenames provided. If image generation is unavailable, explain what capability is missing. Leave visual approval and applying replacements to me.

For portable batches, with the server running and assets queued manually:

```sh
python batch_folder.py export --limit 10 --name first_batch
```

Have the agent read `data/batch/first_batch/INSTRUCTIONS.md`, follow `manifest.json`, and save the named outputs. Then import or watch the folder:

```sh
python batch_folder.py import data/batch/first_batch
python batch_folder.py watch data/batch/first_batch
```

Run these with your virtual environment's Python. Keep the app running to process the inbox. Files must be at least two seconds old before the batch importer copies them. See [agent workflows](docs/agents.md) and [API reference](docs/api.md).

## Compatibility and limits

| Area | Current support |
| --- | --- |
| Unity versions | Reads project files directly; no fixed Editor-version dependency. Not certified across every Unity version or serialization layout. |
| References | GUID-based analysis of text-serialized scenes, prefabs, materials, and other supported assets. Binary serialized files and dynamic runtime loading may be missed. |
| Image scanning | PNG, JPEG, TGA, and PSD names are considered; decoding depends on Pillow support. |
| Applying images | PNG and JPEG only. Convert other source formats deliberately outside the tool if necessary. |
| Sprite / Spine layouts | Existing slice and atlas slots are preserved; no skeleton, mesh, animation, or atlas repacking. Runtime verification is required. |
| 3D restyling | No mesh, shader, lighting, or scene redesign automation. |
| Platforms | Python launch paths are provided for Windows, macOS, and Linux. See CI for actual test results. |
| AI providers | Manual/file workflow is provider-independent. The bundled API adapter needs your own key and a compatible model; provider availability and costs vary. |

## Documentation

- [Installation, updates, and troubleshooting](docs/installation.md)
- [Configuration and categorization](docs/configuration.md)
- [Agent workflows and prompt guidance](docs/agents.md)
- [Local HTTP API](docs/api.md)
- [Contributing](CONTRIBUTING.md), [security](SECURITY.md), and [changelog](CHANGELOG.md)

`project.json`, `rules.json`, and the entire `data/` folder are local and ignored by Git. This includes keys, catalogs, prompts, source-image copies, generated outputs, and backups. **Do not delete `data/` to update the app.**

The tool runs on loopback for a trusted local user; do not expose it as a shared or Internet-facing service. Art sent to an external image provider leaves your machine. The MIT license covers this repository's code and documentation, not artwork in projects you connect.
