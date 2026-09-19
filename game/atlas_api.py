import json
import os
import ssl
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from cache import CacheManager
from fgo_parser import parse_script_text


class AtlasAPI:
    # To read Config.json
    _base_dir = os.path.dirname(__file__)
    _config_path = os.path.join(_base_dir, 'config.json')

    with open(_config_path, "r", encoding="utf-8") as file:
        config = json.load(file)

    # Defining variable from config with form <var_name> = config.get("data","default value")
    lang = config.get("language", "NA")

    # API URLs
    API_BASE_URL = "https://api.atlasacademy.io"
    STATIC_BASE_URL = "https://static.atlasacademy.io"
    EXPORT_WAR_LIST_PATH = f"export/{lang}/nice_war.json"
    EXPORT_BGM_LIST_PATH = f"export/{lang}/nice_bgm.json"

    def __init__(self, region: str = lang, lang: str = "en"):
        self.region = region
        self.lang = lang
        self.cache = CacheManager()
        self.active_war = None
        self.last_error = ""
        self._ssl_context = self._build_ssl_context()
        self._fallback_ssl_context = None
        self._bgm_index = None
        # Parsed nice_war.json, held for the session (get_api() keeps one AtlasAPI), and the
        # per-war chapter lists built off it. See get_war_list / get_war_chapters.
        self._war_list_cache = None
        self._chapter_cache = {}
        # Offline mode: when True, only cached data loads and no network requests are made. Seeded from
        # config.json ("offline"); the reader's Settings toggle flips it live via get_api()/api.offline.
        self.offline = bool(AtlasAPI.config.get("offline", False))

    def _build_ssl_context(self):
        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _build_fallback_ssl_context(self):
        if self._fallback_ssl_context is None:
            self._fallback_ssl_context = ssl._create_unverified_context()
        return self._fallback_ssl_context

    def _is_certificate_error(self, exc: URLError) -> bool:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return True
        return "CERTIFICATE_VERIFY_FAILED" in str(reason)

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        path = path.lstrip("/")
        url = f"{self.API_BASE_URL.rstrip('/')}/{path}"
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        return url

    def _request_bytes(self, url: str) -> bytes | None:
        # Offline mode (toggled from the reader's Settings): skip the network entirely. Cached data
        # still loads via fetch_json/fetch_text/get_cached_asset; anything not cached simply fails.
        if self.offline:
            self.last_error = "Network requests disabled (offline mode)"
            return None

        self.last_error = ""
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "RenPy FGO Wars Reader"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self._ssl_context) as response:
                return response.read()
        except HTTPError as exc:
            self.last_error = f"Atlas Academy returned HTTP {exc.code} for {url}"
            return None
        except URLError as exc:
            if self._is_certificate_error(exc):
                try:
                    with urllib.request.urlopen(
                        request,
                        timeout=30,
                        context=self._build_fallback_ssl_context(),
                    ) as response:
                        return response.read()
                except (HTTPError, URLError, OSError) as retry_exc:
                    self.last_error = f"SSL certificate verification failed for {url}: {retry_exc}"
                    return None

            self.last_error = f"Unable to reach Atlas Academy: {exc.reason}"
            return None
        except OSError as exc:
            self.last_error = f"Network error while contacting Atlas Academy: {exc}"
            return None

    def fetch_json(self, url: str, force_refresh: bool = False):
        if not force_refresh:
            cached = self.cache.load_json(url)
            if cached is not None:
                return cached

        content = self._request_bytes(url)
        if content is None:
            return None

        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self.last_error = f"Atlas Academy returned invalid JSON for {url}: {exc}"
            return None

        self.cache.save_json(url, data)
        return data

    def fetch_text(self, url: str, force_refresh: bool = False):
        if not force_refresh:
            cached = self.cache.load_text(url)
            if cached is not None:
                return cached

        content = self._request_bytes(url)
        if content is None:
            return None

        text = content.decode("utf-8", errors="replace")
        self.cache.save_text(url, text)
        return text

    def get_war_list(self, force_refresh: bool = False):
        # The export is ~41 MB and json.loads on it costs ~0.3 s. get_export_war scans it, so
        # the chapter-select screen would pay that on every war the user browses. Hold the
        # parsed list on the instance; force_refresh still goes back through fetch_json.
        if self._war_list_cache is not None and not force_refresh:
            return self._war_list_cache

        url = self._build_url(self.EXPORT_WAR_LIST_PATH)
        data = self.fetch_json(url, force_refresh)
        if data:
            self._war_list_cache = data
            if force_refresh:
                # A refreshed export can move quests about, and chapter indices are positional.
                self._chapter_cache = {}
        return data

    def get_bgm_index(self, force_refresh: bool = False):
        if self._bgm_index is not None and not force_refresh:
            return self._bgm_index

        url = self._build_url(self.EXPORT_BGM_LIST_PATH)
        data = self.fetch_json(url, force_refresh)
        if not data:
            return {}

        self._bgm_index = {
            item.get("fileName"): item.get("audioAsset")
            for item in data
            if item.get("fileName") and item.get("audioAsset")
        }
        return self._bgm_index

    def get_export_war(self, war_id: int, force_refresh: bool = False):
        war_list = self.get_war_list(force_refresh=force_refresh)
        if not war_list:
            return None

        for war in war_list:
            if war.get("id") == war_id:
                return war
        return None
    
    def get_banner_path(self, banner_url: str, force_refresh: bool = False):
        if not banner_url:
            return None
        return self.get_cached_asset(banner_url, force_refresh=force_refresh)

    def get_war_detail(self, war_id: int, force_refresh: bool = False):
        url = self._build_url(f"nice/{self.region}/war/{war_id}", {"lang": self.lang})
        return self.fetch_json(url, force_refresh)

    def get_script_info(self, script_id: str, force_refresh: bool = False):
        url = self._build_url(f"nice/{self.region}/script/{script_id}", {"lang": self.lang})
        return self.fetch_json(url, force_refresh)

    def get_raw_script(self, script_url: str, force_refresh: bool = False):
        if not script_url:
            return None
        return self.fetch_text(script_url, force_refresh)

    def get_background_path(self, scene_id: str, force_refresh: bool = False):
        if not scene_id:
            return None
        url = f"{self.STATIC_BASE_URL}/{self.region}/Back/back{scene_id}.png"
        return self.get_cached_asset(url, force_refresh=force_refresh)

    def get_script_image_path(self, image_name: str, force_refresh: bool = False):
        """A named Image/ asset (e.g. a subCameraFilter maskEdge mask like 'cut359_mask05').

        Bundled masks under game/gui/cutin/ win over the network copy. The Lostbelt 7
        cutin masks (the cut359_* family) ship with the reader, so they are present on a
        fresh install and survive a cache clear; anything else still downloads as before.
        Returns a game-relative path either way, which is what the caller feeds to
        renpy.load_surface / im.MatrixColor.
        """
        if not image_name:
            return None
        if not force_refresh:
            bundled = os.path.join(self.cache.game_dir, "gui", "cutin", f"{image_name}.png")
            if os.path.exists(bundled):
                return f"gui/cutin/{image_name}.png"
        url = f"{self.STATIC_BASE_URL}/{self.region}/Image/{image_name}/{image_name}.png"
        return self.get_cached_asset(url, force_refresh=force_refresh)

    def get_character_path(self, chara_id: str, force_refresh: bool = False):
        if not chara_id:
            return None
        url = f"{self.STATIC_BASE_URL}/{self.region}/CharaFigure/{chara_id}/{chara_id}_merged.png"
        return self.get_cached_asset(url, force_refresh=force_refresh)

    def get_chara_script_offsets(self, chara_id: str, force_refresh: bool = False):
        if not chara_id:
            return None
        url = self._build_url("raw/JP/svtScript", {"charaId": str(chara_id)})
        data = self.fetch_json(url, force_refresh)
        if not data or not isinstance(data, list):
            return None
        entry = next((e for e in data if e.get("id") == 0), data[0])
        extend_data = entry.get("extendData") or {}
        try:
            face_size = int(extend_data.get("faceSize") or 256)
        except (TypeError, ValueError):
            face_size = 256
        return {
            "face_x": entry.get("faceX", 0),
            "face_y": entry.get("faceY", 0),
            # Most figures use 256px expression cells, but this is data-driven.
            # Notably, Limbo uses 336px cells and Caenis 3041001 uses 320px cells.
            "face_size": max(1, face_size),
            "offset_x": entry.get("offsetX", 0),
            "offset_y": entry.get("offsetY", 0),
            "scale": entry.get("scale", 1)
        }

    def get_bgm_path(self, bgm_name: str, force_refresh: bool = False):
        if not bgm_name:
            return None
        bgm_url = self.get_bgm_index(force_refresh=force_refresh).get(bgm_name)
        if not bgm_url:
            bgm_url = f"{self.STATIC_BASE_URL}/{self.region}/Audio/Bgm/{bgm_name}/{bgm_name}.mp3"
        return self.get_cached_asset(bgm_url, force_refresh=force_refresh)

    def load_script_nodes(self, script_url: str, force_refresh: bool = False):
        raw_script_text = self.get_raw_script(script_url, force_refresh=force_refresh)
        if raw_script_text is None:
            return None
        return parse_script_text(raw_script_text)

    def get_story_quests(self, war_data: dict):
        story_quests = []
        
        # Add the intro Script. It is pinned to the front below rather than sorted
        # with the rest: its synthetic id is not drawn from the same numbering as
        # the real quest ids, so it cannot be ordered against them.
        opening_script = war_data.get("script")
        opening_script_id = war_data.get("scriptId")
        intro_quest = None
        if opening_script is not None and opening_script_id != "NONE":
            # targetId can be short or absent; str()[:-2] then raises ValueError.
            try:
                intro_id = int(str(war_data.get("targetId"))[:-2])
            except (TypeError, ValueError):
                intro_id = war_data.get("id") or 0
            intro_quest = (
                {
                    "id": intro_id,
                    "name": war_data.get("name") or "Unnamed Quest",
                    "warLongName": war_data.get("longName") or war_data.get("name") or "",
                    "spotName": war_data.get("longName") or war_data.get("name") or "",
                    "chapterId": 1,
                    "chapterSubId": 1,
                    "priority": 0,
                    "phase_scripts": [{
                                "phase": 1,
                                "scriptId": opening_script_id,
                                "script": opening_script,
                            }],
                }
            )

        for spot in war_data.get("spots", []):
            for quest in spot.get("quests", []):
                if quest.get("type") != "main":
                    continue
                phase_scripts = []
                for phase_group in quest.get("phaseScripts", []):
                    phase = phase_group.get("phase")
                    for script in phase_group.get("scripts", []):
                        script_url = script.get("script")
                        if not script_url or script_url.endswith("/NONE.txt"):
                            continue
                        phase_scripts.append(
                            {
                                "phase": phase,
                                "scriptId": script.get("scriptId"),
                                "script": script_url,
                            }
                        )
                if not phase_scripts:
                    continue

                story_quests.append(
                    {
                        "id": quest.get("id"),
                        "name": quest.get("name") or "Unnamed Quest",
                        "warLongName": quest.get("warLongName") or war_data.get("longName") or war_data.get("name") or "",
                        "spotName": quest.get("spotName") or spot.get("name") or "",
                        "chapterId": quest.get("chapterId", 0),
                        "chapterSubId": quest.get("chapterSubId", 0),
                        "priority": quest.get("priority", 0),
                        "phase_scripts": phase_scripts,
                    }
                )

        # Order by quest id.
        #
        # The two fields that look like they should order this both lie, and they
        # lie in different wars, so neither can be used even as a tiebreak:
        #   * chapterSubId -- the "Fragment/N" side quests are numbered in their own
        #     sequence that collides with the real sub-ids (war 308 has two quests at
        #     sub 12 and two at sub 14, and Fragments at 98/99).
        #   * priority -- usually a clean descending run, but three wars (201, 301,
        #     9029) use small NEGATIVE sentinels for some quests, which a descending
        #     sort flings to the end of the list.
        # Quest id is unique in all 132 wars that have a main story, is what the Atlas
        # DB itself orders by, and is correct in every war where the other two break.
        story_quests.sort(key=lambda item: item.get("id") or 0)

        # Drop quests that replay a script sequence already listed. These are the
        # `branch` variants -- an alternate-route or scripted-loss copy of a battle
        # pointing at the same script (war 308's 3000991 duplicates 3000907). They are
        # separate quests to play, but the same thing to *read*, so a reader that
        # listed both would just show the chapter twice.
        def signature(quest):
            return tuple(ps.get("script") for ps in quest["phase_scripts"])

        deduped = []
        seen_scripts = set()
        for quest in story_quests:
            sig = signature(quest)
            if sig in seen_scripts:
                continue
            seen_scripts.add(sig)
            deduped.append(quest)

        # Three wars (203, 301, 9010) list their war-level opening script again as a
        # real quest. Prefer the real quest -- it carries the proper chapter name and
        # spot, where the synthetic row can only repeat the war's own name.
        if intro_quest is not None and signature(intro_quest) not in seen_scripts:
            deduped.insert(0, intro_quest)

        return deduped

    def get_cached_asset(self, url: str, force_refresh: bool = False):
        local_path = self.cache.get_asset_path(url)
        if local_path and not force_refresh:
            return local_path
        content = self._request_bytes(url)
        if content is None:
            return None
        return self.cache.save_asset(url, content)

    def get_war_chapters(self, war_id: int, force_refresh: bool = False):
        """The story-quest list for one war, for the chapter-select UI.

        Costs no extra bandwidth over what load_war already fetches: both resolve the war
        from the export first and use the detail endpoint only as a fallback.

        Going through that same path matters for correctness, not just tidiness --
        play_war indexes active_war["story_quests"] *positionally*, so the list the screen
        numbers its rows from has to be the identical list load_war will later build.
        """
        if not force_refresh and war_id in self._chapter_cache:
            return self._chapter_cache[war_id]

        war_data = self._resolve_war(war_id, force_refresh=force_refresh)
        if war_data is None:
            return None

        chapters = self.get_story_quests(war_data)
        self._chapter_cache[war_id] = chapters
        return chapters

    def _resolve_war(self, war_id: int, force_refresh: bool = False):
        """The war record, from the export if it has one, else the detail endpoint.

        Export first on purpose. `nice/<region>/war/<id>` returns byte-for-byte what the
        export entry already holds -- verified identical for every war whose detail is
        cached here -- so calling it costs 90-470 KB per war to learn nothing. Skipping
        it is what lets the chapter column fill instantly instead of stalling on a
        request. The endpoint stays as the fallback for a war the export omits.

        `load_war` and `get_war_chapters` must keep resolving the war the *same* way:
        `play_war` indexes `story_quests` positionally, so the list the chapter screen
        numbered its rows from has to be the list `load_war` later rebuilds.
        """
        war_data = self.get_export_war(war_id, force_refresh=force_refresh)
        if war_data is None:
            war_data = self.get_war_detail(war_id, force_refresh=force_refresh)
        return war_data

    def load_war(self, war_id: int, force_refresh: bool = False):
        war_data = self._resolve_war(war_id, force_refresh=force_refresh)
        if war_data is None:
            return None

        script_nodes = []
        raw_script_text = None
        script_id = war_data.get("scriptId")
        script_url = war_data.get("script")
        story_quests = self.get_story_quests(war_data)

        if story_quests:
            self.last_error = ""
        elif script_id == "NONE" or (script_url and script_url.endswith("/NONE.txt")):
            self.last_error = "The selected war does not have a readable story script."
        elif script_url:
            raw_script_text = self.get_raw_script(script_url, force_refresh=force_refresh)
            if raw_script_text is not None:
                script_nodes = parse_script_text(raw_script_text)

        if not script_nodes and script_id and script_id != "NONE":
            script_info = self.get_script_info(script_id, force_refresh=force_refresh)
            if script_info and script_info.get("script"):
                raw_script_text = self.get_raw_script(script_info.get("script"), force_refresh=force_refresh)
                script_nodes = parse_script_text(raw_script_text or "")

        self.active_war = {
            "war": war_data,
            "script_id": script_id,
            "script_url": script_url,
            "story_quests": story_quests,
            "script_nodes": script_nodes,
            "raw_script": raw_script_text,
        }
        return self.active_war
