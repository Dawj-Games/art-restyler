# Working with an AI agent

Art Restyler separates asset management from image generation. An agent can use the local API and files; image generation comes from whatever image tool the agent can access. Claude Code, Codex, or another coding assistant is not automatically an image generator merely because it can edit files.

## Establish the game's constraints

Ask the agent to read the README, inspect the catalog and representative originals, and identify UI, character, environment, rig, grayscale, and utility textures. Use contact sheets if useful. Let it propose `rules.json` changes and explain uncertain classifications.

Define style and design rules with the user. Record gameplay colors, text, icon meanings, asset-family consistency, runtime tinting, nine-slice borders, and attachment constraints. Save these through Settings or `POST /api/settings`. Restart and rescan after changing JSON categorization rules.

## Single assets

Queue assets with `source: "manual"`. `GET /api/manual/<guid>` prepares an input and returns the prompt, including file paths when agent mode is enabled. The agent should inspect the prepared input before generating. The generation canvas can be larger than the destination texture; preserve its dimensions and arrangement. Art Restyler fits the result back to the destination and runs checks.

Save the PNG at the exact inbox path in the prompt, upload it through the UI, or send PNG bytes to `POST /api/import/<guid>`. If an image tool only returns a file in its own output directory, copy that file explicitly to the requested destination. Do not guess which generated file belongs to which asset.

## Batches

1. Keep the app running and queue a small group using the manual source.
2. Run `python batch_folder.py export --limit 10 --name pilot` with the virtual environment's Python.
3. Give the agent `data/batch/pilot/INSTRUCTIONS.md`. Each manifest entry contains the prepared input filename, exact output filename, GUID, and full prompt.
4. Ask the agent to generate entries in order and report failures. Preserve per-entry animation and mode constraints.
5. Run `python batch_folder.py import data/batch/pilot`, or use `watch` while generating. Import skips outputs modified within the last two seconds and renames copied outputs with `.imported`.
6. Review attempts in the UI, accept the desired results, then apply them. Check the game in Unity.

The exported folder is self-contained for generation. Prompts can include local paths; review them and the art before sharing the batch outside your machine. To import results, keep the manifest attached to its original catalog/installation.

## Example agent instruction

> Read `INSTRUCTIONS.md` and `manifest.json` in the supplied batch directory. Inspect each prepared input, generate a replacement following its full prompt and the project's style/design rules, and save the PNG to its exact manifest output filename. Preserve transparency and layout. For rig sheets, preserve every part's slot, edges, rotation, and attachment features. If your image tool saves elsewhere, copy the generated output to the destination. Report missing image capabilities or failures. Leave acceptance, application, and Unity runtime checks to the user.

## What the checks mean

Checks compare layout, alpha masks, part bounds, grayscale, fill, and visual differences. They do not prove animation quality, gameplay readability, style consistency, or ownership of generated artwork. A passing check still needs visual review. A failed check is useful feedback, not something an agent should routinely override.

## Optional direct provider

The bundled API adapter can generate from the prepared image and prompt when configured in Settings. It is optional and independent of a coding agent's subscription or login. Requests can send project art to an external service. No paid calls are needed to run the synthetic tests.

## Future integrations

This release provides a documented local HTTP interface, not an MCP server or automatic connections to hosted chats. An MCP bridge or additional provider adapter can be added separately without changing the file-based workflow.
