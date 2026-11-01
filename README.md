# Fate/Grand Order Wars VN Reader

This Ren'Py project reads Fate/Grand Order NA and JP story scripts as a visual novel.

## Features
- Fetches Atlas Academy NA or JP war metadata from the live API
- Downloads and caches raw script text locally
- Parses FGO script dialogue markers into Ren'Py dialogue flow
- Displays war name, script ID, and parsed dialogue in Ren'Py

## Structure
- `game/script.rpy` — Ren'Py entrypoint and playback flow
- `game/atlas_api.py` — Atlas Academy fetch and cache manager
- `game/fgo_parser.py` — basic FGO raw script parser
- `game/cache.py` — file-based HTTP cache for JSON and text

## Usage
1. Open this workspace as a Ren'Py project.
2. Launch Ren'Py and select the project.
3. Run the game.
4. Choose a war and chapter from the reader UI.

## Event promo videos

Optional event promo links are stored in `game/war_promos.json`, keyed by Atlas war ID. A configured
promo is shown as the first plate in that war's chapter list and opens in the user's browser.

```json
{
    "promos": {
        "WAR_ID": {
            "title": "Watch Event Promo",
            "url": "https://www.youtube.com/watch?v=VIDEO_ID"
        }
    }
}
```

Only HTTPS links on `youtube.com` or `youtu.be` are accepted. Add another entry to the `promos`
object when a future event needs a video; no Ren'Py code change is required.

## Reader features
- Main menu, war and chapter selection, settings screen (text speed, auto wait, master name/gender) backed by `game/config.json`
- Script playback engine split into `game/vn_*.rpy`: backgrounds, characters, cut-ins, blur, flashes, screen effects, special figure shaders, voice and sound effects (sound effects are downloaded from Atlas Academy on demand into `game/assets/se/`)
- Optional offline mode (`"offline": true` in `game/config.json`) that disables network requests (also toggled from Settings)

## Talk effects
FGO talk-scene effects (`effect`, `backeffect`, `forwardeffect`, `charaeffect`, ...) are drawn by a particle runtime in `game/fgo_fx_runtime.rpy`.
- `game/fx_generated/` holds one small data file per supported effect (generated from the game's effect prefabs).
- `game/assets/fx/gen/` holds the textures those effects draw, extracted from the game data.
- If an effect's textures are missing, the reader logs a line to `log.txt` and skips that effect; the script keeps playing.
- Special figure effects use two masks in `game/assets/special_fx/` that are not shipped; without them the figure is drawn unmasked.

## Notes
- The parser currently supports basic dialogue and choice lines from Atlas Academy script text.
- Cache files are stored under `cache/<region>/atlas_academy/` inside the Ren'Py game directory.
- The frontend is intentionally minimal so the core fetching/caching/parser flow can be validated first.

## Next steps
- Add a scrollable war selection UI
- Add background and character asset download/render support
- Improve parser support for additional FGO script tags and visual commands
