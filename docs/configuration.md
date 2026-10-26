# Configuration

`python init_project.py --project "..."` creates ignored local configuration. Tracked `.example.json` files show the public defaults. Never copy a previous game's database into a new game.

## project.json

| Key | Purpose |
| --- | --- |
| `name` | Your local project name. |
| `project_root` | Unity project path; absolute, or relative to the tool directory. |
| `port` | Loopback server port, normally 8765. Batch tools use the same value. |
| `excluded_dirs` | Case-sensitive Assets-relative folder prefixes using forward slashes and trailing `/`. |

Exclude purchased plugins, SDKs, editor assets, and artwork you do not intend to modify. Restart after editing configuration. Do not change project roots for an existing catalog; use a new clone instead. Re-running initialization for the same project rewrites its project settings with the supplied options/defaults, so preserve custom exclusions when doing so.

## rules.json

Name patterns are case-insensitive Python regular expressions. Subcategory tables use `[label, regex]` pairs; the first match wins. `path_rules` uses `[path_prefix, category, subcategory]` entries. Defaults are a starting point, not a reliable classification of every game.

```json
{
  "unused_as_skip": false,
  "path_rules": [["Assets/MyUI/", "UI", "panels"]],
  "game_sub": [["enemies", "enemy|monster"], ["heroes", "hero|player"]],
  "resources_folder_as": ["Game", "other"]
}
```

Local rules merge with built-in defaults. `skip_name` takes priority, then detected Spine pages, then path and usage rules. Spine pages use `part_sheet`. VFX and skipped/unsorted categories default to `skip`; grayscale and bordered textures default to `recolor`. Other assets use `regenerate` or `part_sheet` according to their slices and category. Review these choices before queuing.

Restart and rescan after changing rules. Explicit category or mode edits made in the UI are retained during rescans. A lack of static references does not prove an asset is unused, especially with Addressables, Resources, or code-built paths.

## Settings in the app

- **Style prompt:** the visual target shared by generated prompts.
- **Game design rules:** constraints such as rarity/team colors, symbols, text, runtime tinting, and consistent asset families.
- **Agent mode / file paths:** includes local prepared-input and inbox-output paths in prompts.
- **Generation source:** manual import, a local test provider, or the optional bundled API adapter when a key is configured.
- **API settings:** the bundled adapter's key, image model, and quality. A preset model name is not a guarantee of access; select a compatible model available to your account.

The test provider recolors an input to exercise the pipeline; it does not generate new AI artwork. Keys are kept in ignored `data/secrets.json`; ordinary settings live in the local SQLite catalog. This is local storage, not encryption. The UI masks configured keys rather than returning their contents.

## Data and rollback

`data/restyler.db` stores the catalog, settings, statuses, and attempts. `data/work/` holds prepared inputs and attempts. `data/inbox/` receives outputs. `data/backup/` holds original image bytes. All are ignored by the public repository.

Apply changes image bytes, including matching duplicate assets, and leaves Unity metadata alone. Backups are verified before replacement. Rollback restores original bytes and refuses to overwrite an image modified after the tool applied it. Keep game Git commits as a second recovery route.
