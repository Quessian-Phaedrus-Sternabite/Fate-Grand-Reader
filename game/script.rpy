# Thin entrypoint: core configuration, store defaults, and shared setup.
define n = Character(None)

# target resolution
define config.screen_width = 1280
define config.screen_height = 720

# Calculate upscale ratio based on original game resolution so that it's always correct according to target resolution

define config.rollback_enabled = False
# The message labels use a custom text shader; without a default textshader Ren'Py would drop the
# "typewriter" shader that hides not-yet-typed glyphs, making slow text appear instantly.
define config.default_textshader = "typewriter"
# Ren'Py's own text speed preference (characters per second, 0 = instant) drives the typewriter;
# FGO's default setting is ~30 cps.
default preferences.text_cps = 30
define config.has_autosave = False
define config.autosave_on_choice = False

# This can be used to change part.
default active_war = None

default war_list = []
default persistent.wars_fetched_once = False
default war_load_error = None
default selected_war_id = 0
# Chapter-select column state (right-hand pane of war_selectMainStory).
default chapter_war_id = None
default chapter_war_name = ""
default chapter_rows = []
default chapter_error = ""
default current_background_path = None
default current_scene_id = None
default current_chara_defs = {}
default backlog = []
default current_reader_marker = None
default resume_marker = None
default menu_notice = ""
default vn_language_changed = False

# VN engine state (all plain data so it survives store copies)
default vn_msg_visible = False          # ScriptMessageCommonManager rootPanel.alpha == 1
default vn_msg_speaker = ""
default vn_msg_text = ""
default vn_msg_disp = None              # per-message displayable (AdjustTimes -> Transform -> Text)
default vn_msg_last_name = ""           # messageManager.talkName  (cleared by ClearText)
default vn_msg_last_slot = None         # messageManager.talkNameIndex
default vn_msg_last_line_y = 0          # y of the last laid-out line, for PageScroll distance
default vn_text_done_at = None          # wall-clock time the slow text finished (key delay)
default vn_talk_mask_enabled = True     # ScriptManager.isTalkMask  ([charaTalk on/off])
default vn_talk_depth_enabled = True    # ScriptManager.isTalkDepth
default vn_talk_mask_name = ""          # ScriptManager.talkMaskName
default vn_talk_index = None            # slot currently highlighted
default vn_bg = {}                      # background cross-fade state {"prev","start","dur"}
default vn_fade = None                  # stage fade quad state {"color","from","to","start","dur"}
default vn_mask = None                  # CommonUI mask state (covers the message window too)
default vn_busy = {}                    # command tag -> wall-clock time it finishes ([wait TAG])
default vn_camera = None                # cameraMove state: {"pos_anim", "scale_anim"} (stage pan/zoom)
default vn_blur = None                  # [blur]/[blurOff]/[cameraFilter] {"filter","mat","tween","cam","cam_params"} (vn_blur.rpy)
default vn_back_camera_color = None     # [backCameraColor] MargeCamera clear colour "#rrggbb" or None
default vn_flash = None                 # [flashin] state (vn_flash.rpy); None = InitFlashCommand colour (1,1,1,0)
default vn_bgm_name = None              # name of the playing [bgm] (bgmStop/bgmStopEnd only stop a matching one)
default vn_auto_voice_hold = False      # AUTO timer elapsed but a [voice] is still playing
default vn_wipe = None                 # WipeMesh state (vn_wipe.rpy): mask, volume tween, filter
default vn_wipe_gradation = 0.0        # WipeMaterial _Gradation (kept across wipes, like the shared material)
default vn_shake = None                 # screen-level shake {"x","y","cycle","start","end"}
default vn_msg_shake = None             # [messageShake] on the MessageShake node {"x","y","cycle","start","end"}
default vn_msg_shake_pending = []       # inline [messageShake]s of the current message: [typewriter_time, token]
default vn_msg_pauses = []              # mid-line [wt] holds of the current message: (start, end, typewriter_time)
default vn_pictureframe = None          # pictureFrame/pictureFrameTop sprite {"path", "name", "top", "crop"}
default vn_pictureframe_width = 1025    # PictureFrame widget width in NGUI px (VN_PICTURE_FRAME_WIDTH_*)
default vn_pictureframe_force_size = False  # ScriptManager.isPictureFrameForceChangeSize (static: survives scripts)
default vn_script_full_screen = False   # the current script contains [enableFullScreen] (ForceObi_16_9 off)
default vn_substretch_enabled = True
default vn_sublayers = {}               # sub-render/cutin groups: key "#A".. -> ScriptSubLayer state dict
default vn_phase_fade_seen = False
default vn_wait_until = 0.0            # wall-clock end of the current hard wait
default vn_show_next_mark = False       # NextMark visible only while waiting for a tap
default vn_msg_body = None              # current VNTextBody (for the tap-to-complete snap)
default vn_msg_forced = False           # isFastMessageRequest: text completed by a tap
default vn_msg_scroll = 0               # final scroll offset of the on-screen message (for the roll-out)
default vn_msg_gates_released = 0       # manual-scroll: rolls the reader has released this message
default vn_msg_start_wall = 0.0         # manual-scroll: wall-clock time the current message began typing
default vn_msg_last_release_wall = 0.0  # manual-scroll: wall-clock time the last gate was released
default vn_auto_wait = vn_settings_loaded["autoWait"]       # OptionScenarioAutoWaitTimeValue (0..3)
default vn_player_name = vn_settings_loaded["masterName"]
default vn_player_gender = vn_settings_loaded["masterGender"]   # 0 = male, 1 = female
default vn_auto_mode = False            # FGO auto-message button (not persisted, like the game)
default vn_skip_mode = False            # custom skip mode (Tab to toggle; not persisted)
default vn_skip_allowed = True          # script [skip false/true] temporarily gates fast-forward
default vn_unrendered_wait = False      # next [wt] belongs to an effect/SFX the port did not draw/play
default vn_unrendered_movie = False     # suppress a movie's pacing waits until its replacement scene arrives
default vn_script_flags = {}            # ScriptManager flagDataList ([flag NAME V], INPUT_SELECT_BRANCH)
default vn_select_branch_index = 0      # ScriptManager.SetSelectBranchIndex (static: survives scripts)
default vn_select_branch_pending = False  # isSelectBranch: [input selectBranch] seen, next choice records its index
default vn_selection_use = None         # pending [selectionUse masterMale|masterFemale] gate for the next menu
default vn_require_input = vn_settings_loaded["requireInput"]   # manual scroll past the visible window
default vn_offline = vn_settings_loaded["offline"]              # skip all network requests (cache only)

init -100 python:
    import json
    import math
    import os
    import time
    from atlas_api import AtlasAPI
    # To read Config.json
    configJSON = json.load(renpy.open_file('config.json'))
    store.lang = str(configJSON.get('language', 'NA')).upper()
    if store.lang not in ("NA", "JP"):
        store.lang = "NA"
    store.titleScreen = configJSON.get('Title','Part1')
    # Reader settings (FGO option ranges): text speed 0.5..5 (default 3), auto wait 0..3 (default 0),
    # master name / gender for the [%1] and [&he:she] script tags.
    vn_settings_loaded = {
        "autoWait": float(configJSON.get("autoWait", 0.0)),
        "masterName": str(configJSON.get("masterName", "Fujimaru")),
        "masterGender": int(configJSON.get("masterGender", 0)),
        # When True, the message stops at the bottom of the visible window and waits for a tap
        # before rolling to the next line (FGO's "manual scroll"); False keeps the auto-scroll.
        "requireInput": bool(configJSON.get("requireInput", False)),
        # When True, no network requests are made (only cached wars/scripts/assets load).
        "offline": bool(configJSON.get("offline", False)),
    }
    #config Updating function
    def config_update(key,value):
        config_path = os.path.join(config.gamedir, "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as file:
                current_data = json.load(file)
        current_data[key] = value
        with open(config_path, "w", encoding="utf-8") as file:
            json.dump(current_data, file, ensure_ascii=False, indent=4)

    # NGUI UILabel gradient (MainPrefab/RubyPrefab: mApplyGradient, top white, bottom 0.7 grey), per glyph
    # from the font ascent line to the descent line.
    renpy.register_textshader(
        "vn_gradient",
        variables="""
        uniform float u__top;
        uniform float u__bottom;
        attribute vec2 a_text_center;
        attribute float a_text_ascent;
        attribute float a_text_descent;
        varying float v__grad;
        """,
        vertex_25="""
        v__grad = clamp((gl_Position.y - (a_text_center.y - a_text_ascent)) / max(a_text_ascent + a_text_descent, 1.0), 0.0, 1.0);
        """,
        fragment_300="""
        gl_FragColor.rgb *= mix(u__top, u__bottom, v__grad);
        """,
        u__top=1.0,
        u__bottom=0.7,
        doc="FGO message label vertical gradient.",
    )

    def get_api():
        desired_region = str(getattr(renpy.store, "lang", "NA") or "NA").upper()
        if desired_region not in ("NA", "JP"):
            desired_region = "NA"
        api = getattr(renpy.store, "_atlas_api", None)
        if api is None or api.region != desired_region:
            # AtlasAPI and CacheManager both carry region-specific URL/cache state,
            # so changing the setting must replace the live instance.
            renpy.store._atlas_api = AtlasAPI(region=desired_region)
        # Keep the live API in sync with the reader's offline toggle.
        renpy.store._atlas_api.offline = bool(getattr(renpy.store, "vn_offline", False))
        vn_apply_player_settings()
        return renpy.store._atlas_api

    def normalize_choice_text(text):
        text = str(text or "").strip()
        text = text.lstrip("0123456789,，")
        text = text.lstrip(":：").strip()
        return text

    def collect_choice_options(nodes, start_idx):
        choices = []
        idx = start_idx
        while idx < len(nodes) and nodes[idx].get("type") == "choice":
            text = normalize_choice_text(nodes[idx].get("text"))
            if text and text not in ("!", "！"):
                choices.append(text)
            idx += 1
        return choices, idx

    def record_dialogue(speaker, line):
        entry = {"speaker": speaker or "", "line": line or ""}
        renpy.store.backlog.append(entry)
        if len(renpy.store.backlog) > 200:
            renpy.store.backlog = renpy.store.backlog[-200:]


init 999 python:
    config.quit_action = Quit(confirm=False)
    style.default.font = "fonts/FGO-Main-Font.otf"
