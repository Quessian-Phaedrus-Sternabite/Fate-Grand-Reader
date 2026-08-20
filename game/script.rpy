define n = Character(None)

# target resolution
define config.screen_width = 1280
define config.screen_height = 720

# Calculate upscale ratio based on original game resolution so that it's always correct according to target resolution
define original_screen_width = 1024
define original_screen_height = 576
define upscale_ratio = config.screen_width / original_screen_width

# reader_dialogue screen variables
define dialogue_default_color = '#ffffff'
define dialogue_textbox_width = int(1024 * upscale_ratio)
define dialogue_textbox_height = int(155 * upscale_ratio)
define dialogue_nameplate_width = int(300 * upscale_ratio) 
define dialogue_nameplate_height = int(48 * upscale_ratio) 

# This can be used to change part.
default active_war = None

default war_list = []
default war_load_error = None
default selected_war_id = 0
default current_background_path = None
default current_scene_id = None
default current_characters = []
default current_chara_defs = {}
default backlog = []
default current_reader_marker = None
default resume_marker = None
default menu_notice = ""
default last_speaker = ""
default last_line = ""
default music_flag = None
default music_path = ""
default music_fade = 0.2

# fou running speed
define SPEED = 0.5

# Title Screens
# These should change by part
#define titleScreen = "part1" #Transfer to config.json
define part1title =  "gui/title_wallpaper.png"
define part1logoNA = "gui/NAPart1Logo.png"
define part1logoJP = "gui/JPPart1Logo.png"
define part1terminal = "gui/warTerminal.png"
define part2title = "gui/part2title.png"
define part2logo = "gui/logo_title_cil.png"
define part2terminal="gui/warTerminal2.png"


####################### 
image running_fou: # loading screen icon running
    "gui/loadIcon (1).png"
    SPEED
    "gui/loadIcon (2).png"
    SPEED
    "gui/loadIcon (3).png"
    SPEED
    "gui/loadIcon (4).png"
    SPEED
    repeat



init python:
    import json
    import os
    from atlas_api import AtlasAPI
    # To read Config.json
    configJSON = json.load(renpy.open_file('config.json'))
    store.lang = configJSON.get('language', 'NA')
    store.rayshift = configJSON.get('Rayshift','Yes')    
    store.titleScreen = configJSON.get('Title','Part1')
    #config Updating function
    def config_update(key,value):
        config_path = os.path.join(config.gamedir, "config.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as file:
                current_data = json.load(file)
        current_data[key] = value
        with open(config_path, "w", encoding="utf-8") as file:
            json.dump(current_data, file, ensure_ascii=False, indent=4)
    
    def get_api():
        if not hasattr(renpy.store, "_atlas_api") or renpy.store._atlas_api is None:
            renpy.store._atlas_api = AtlasAPI()
        return renpy.store._atlas_api

    def normalize_choice_text(text):
        text = str(text or "").strip()
        text = text.lstrip("0123456789")
        text = text.lstrip(":\uff1a").strip()
        return text

    def collect_choice_options(nodes, start_idx):
        choices = []
        idx = start_idx
        while idx < len(nodes) and nodes[idx].get("type") == "choice":
            text = normalize_choice_text(nodes[idx].get("text"))
            if text and text not in ("!", "\uff01"):
                choices.append(text)
            idx += 1
        return choices, idx

    def record_dialogue(speaker, line):
        entry = {"speaker": speaker or "", "line": line or ""}
        renpy.store.backlog.append(entry)
        if len(renpy.store.backlog) > 200:
            renpy.store.backlog = renpy.store.backlog[-200:]

    # Maps FGO script position index to Unity pixel offset based on original 1024-wide resolution.
    CHARA_POSITION_X_COORD = {
        0: -256,  # Left
        1:    0,  # Center
        2:  256,  # Right
        3: -438,  # Far left
        4: -512,  # Furthest left
        5:  438,  # Far right
        6:  512,  # Furthest right
    }

    def get_slot_position_offset(slot, position=None):
        value = position
        if value is None:
            value = renpy.store.current_chara_defs.get(slot, {}).get("position")
        try:
            pos = int(value)
        except Exception:
            pos = 1
        return CHARA_POSITION_X_COORD.get(pos, 0)

    def refresh_visible_characters():
        visible = []
        for slot, data in sorted(renpy.store.current_chara_defs.items()):
            if data.get("visible") and data.get("path"):
                visible.append(
                    {
                        "slot": slot,
                        "path": data.get("path"),
                        "face": data.get("face") or "0",
                        "position_offset": get_slot_position_offset(slot, data.get("position")),
                        "face_x": data.get("face_x", 0),
                        "face_y": data.get("face_y", 0),
                        "offset_x": data.get("offset_x", 0),
                        "offset_y": data.get("offset_y", 0),
                        "scale": data.get("scale", 1),
                        "matrixcolor": data.get("matrixcolor", None)
                    }
                )
        renpy.store.current_characters = visible

    def chara_face_crop(face):
        try:
            face_index = max(1, int(face))
        except Exception:
            face_index = 1
        # Character sheets are 1024 wide: four 256px face tiles per row below the 768px body.
        zero_idx = face_index - 1
        col = zero_idx % 4
        row = zero_idx // 4
        return (col * 256, 768 + row * 256, 256, 256)

    def apply_reader_node(node, api):
        node_type = node.get("type")
        if node_type == "scene":
            renpy.store.current_scene_id = node.get("scene_id")
            path = api.get_background_path(renpy.store.current_scene_id)
            renpy.store.current_background_path = path.replace("\\", "/") if path else None
            return True
        if node_type == "chara_set":
            slot = node.get("slot")
            if slot:
                # Keep the sheet; the stage crops the body and overlays face 0.
                char_path = api.get_character_path(node.get("chara_id"))
                offsets = api.get_chara_script_offsets(node.get("chara_id")) or {}
                renpy.store.current_chara_defs[slot] = {
                    "id": node.get("chara_id"),
                    "name": node.get("name"),
                    "position": node.get("position"),
                    "face": "0",
                    "path": char_path.replace("\\", "/") if char_path else None,
                    "visible": False,
                    "face_x": offsets.get("face_x"),
                    "face_y": offsets.get("face_y"),
                    "offset_x": offsets.get("offset_x"),
                    "offset_y": offsets.get("offset_y"),
                    "scale": offsets.get("scale"),
                    "matrixcolor": 0,
                    "communication_chara": False
                }
                refresh_visible_characters()
            return True
        if node_type == "chara_face":
            slot = node.get("slot")
            if slot and slot in renpy.store.current_chara_defs:
                # Swap only the expression index; the sheet path stays the same.
                renpy.store.current_chara_defs[slot]["face"] = node.get("face") or "0"
                refresh_visible_characters()
            return True
        if node_type == "chara_show":
            slot = node.get("slot")
            if slot and slot in renpy.store.current_chara_defs:
                if node.get("position") is not None:
                    renpy.store.current_chara_defs[slot]["position"] = node.get("position")
                renpy.store.current_chara_defs[slot]["visible"] = True
                refresh_visible_characters()
            return True
        if node_type == "chara_hide":
            slot = node.get("slot")
            if slot and slot in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[slot]["visible"] = False
                refresh_visible_characters()
            return True
        if node_type == "chara_filter":
            slot = node.get("slot")
            chara_filter = node.get("filter")
            if slot and slot in renpy.store.current_chara_defs and chara_filter == "silhouette":
                renpy.store.current_chara_defs[slot]["matrixcolor"] = -1
                refresh_visible_characters()
            return True
        if node_type == "communication_chara":
            chara_id = node.get("chara_id")
            for slot_temp, data in sorted(renpy.store.current_chara_defs.items()):
                if data.get("id") == chara_id:
                    slot = slot_temp
            if slot and slot in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[slot]["communication_chara"] = True
                renpy.store.current_chara_defs[slot]["position"] = node.get("position")
                renpy.store.current_chara_defs[slot]["face"] = node.get("face") or "0"
                renpy.store.current_chara_defs[slot]["offset_y"] = 0
                renpy.store.current_chara_defs[slot]["visible"] = True
                refresh_visible_characters()
            return True
        if node_type == "communication_charaface":
            for slot_temp, data in sorted(renpy.store.current_chara_defs.items()):
                if data.get("communication_chara") == True:
                    slot = slot_temp
            if slot and slot in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[slot]["face"] = node.get("face") or "0"
                renpy.store.current_chara_defs[slot]["visible"] = True
                refresh_visible_characters()
            return True
        if node_type == "communication_charaloop":
            chara_id = node.get("chara_id")
            for slot_temp, data in sorted(renpy.store.current_chara_defs.items()):
                if data.get("id") == chara_id:
                    slot = slot_temp
            if slot and slot in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[slot]["communication_chara"] = True
                renpy.store.current_chara_defs[slot]["position"] = node.get("position")
                renpy.store.current_chara_defs[slot]["face"] = node.get("face") or "0"
                renpy.store.current_chara_defs[slot]["visible"] = True
                refresh_visible_characters()
            return True
        if node_type == "communication_characlear":
            for slot_temp, data in sorted(renpy.store.current_chara_defs.items()):
                if data.get("communication_chara") == True:
                    slot = slot_temp
            if slot and slot in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[slot]["visible"] = False
                refresh_visible_characters()
            return True
        return False

    def get_scene_music_flag(node, api):
        if node.get("type") == "bgm":
            parts = (node.get("tags") or {}).get("bgm", "").split()
            path = api.get_bgm_path(node.get("name"))
            if path:
                try:
                    fadein = float(parts[2]) if len(parts) >= 3 else 0.2
                except Exception:
                    fadein = 0.2
                return {"action": "play", "path": path.replace("\\", "/"), "fade": fadein}
        if node.get("type") == "audio_stop":
            token = next(iter((node.get("tags") or {}).values()), "")
            parts = token.split()
            try:
                fadeout = float(parts[-1]) if len(parts) >= 2 else 0.2
            except Exception:
                fadeout = 0.2
            return {"action": "stop", "fade": fadeout}

        # Music flags are embedded in scene text, so resolve them before the line is shown.
        for key, token in (node.get("tags") or {}).items():
            parts = (token or key).split()
            command = parts[0].lower() if parts else ""
            if command == "bgm" and len(parts) >= 2:
                path = api.get_bgm_path(parts[1])
                if path:
                    try:
                        fadein = float(parts[2]) if len(parts) >= 3 else 0.2
                    except Exception:
                        fadein = 0.2
                    return {"action": "play", "path": path.replace("\\", "/"), "fade": fadein}
            elif command in ("bgmstop", "soundstopall"):
                try:
                    fadeout = float(parts[2]) if len(parts) >= 3 else 0.2
                except Exception:
                    fadeout = 0.2
                return {"action": "stop", "fade": fadeout}
        return None

    def save_current_marker():
        if renpy.store.current_reader_marker:
            persistent.reader_marker = dict(renpy.store.current_reader_marker)
            renpy.save_persistent()
            renpy.store.menu_notice = "Saved reading marker."

init 999 python:
    config.quit_action = Quit(confirm=False)
    style.default.font = "fonts/FGO-Main-Font.otf"

screen loading_screen(message):
    tag loading
    frame:
        align (0.5, 0.5)
        xpadding 20
        ypadding 20
        xminimum 400
        yminimum 120
        has vbox
        
        text message size 28
        text "Please wait (May take a while on your first load)..." size 22
    add "running_fou":
            align(0.95,0.90)
    add "gui/underbar.png":
        align(1,0.90)
        xsize(config.screen_width)


screen message_screen(message):
    tag menu
    modal True
    frame:
        align (0.5, 0.5)
        xpadding 20
        ypadding 20
        xminimum 400
        yminimum 140
        has vbox

        text message size 26 substitute False
        textbutton "OK" action Return()

screen confirm(message, yes_action, no_action):
    modal True
    zorder 200

    frame:
        align (0.5, 0.5)
        xpadding 20
        ypadding 20
        xminimum 420
        has vbox

        text message size 26 substitute False
        hbox:
            spacing 16
            textbutton "Yes" action yes_action
            textbutton "No" action no_action

screen vn_stage(background_path, scene_id, characters):
    zorder 0

    if background_path:
        add background_path xysize (config.screen_width, config.screen_height)
    else:
        add Solid("#111318")

    for chara in characters:
        fixed at Transform(zoom=upscale_ratio):
            xysize (original_screen_width, original_screen_height)
            yalign 1.0
            # TODO: Unsure about the character-individual scaling here, would need to find a character whose scale value isn't 1 to test
            add chara["path"] crop (0, 0, 1024 * chara["scale"], 768 * chara["scale"]) xpos chara["position_offset"] + chara["offset_x"] ypos -chara["offset_y"] matrixcolor BrightnessMatrix( + chara["matrixcolor"])
            if chara["face"] != "0" and chara["face"] != 0:
                add chara["path"] crop chara_face_crop(chara["face"]) xpos chara["position_offset"] + chara["face_x"] + chara["offset_x"] ypos chara["face_y"] - chara["offset_y"] matrixcolor BrightnessMatrix( + chara["matrixcolor"])

    if scene_id and not background_path:
        frame:
            align (0.02, 0.03)
            xpadding 10
            ypadding 6
            text "Scene [scene_id]" size 18

screen reader_dialogue(speaker, line):
    modal True
    zorder 80

    button:
        background None
        xfill True
        yfill True
        action Return(True)

    fixed:
        align (0.5, 1.0)
        xysize (dialogue_textbox_width, dialogue_textbox_height)

        add "gui/img_talk_textbg.png" xysize (dialogue_textbox_width, dialogue_textbox_height) ypos -18
        add "gui/img_talk_namebg.png" xysize (dialogue_nameplate_width, dialogue_nameplate_height) xpos 0 ypos -int(dialogue_nameplate_height - 8)

        if speaker:
            text speaker xpos 30 ypos (-int(dialogue_nameplate_height - 8) + (dialogue_nameplate_height - int(29 * upscale_ratio)) // 2) xmaximum (dialogue_nameplate_width - 30) size int(29 * upscale_ratio) color dialogue_default_color font "fonts/FGO-Main-Font.otf" substitute False outlines [(1, "#000000aa", 1, 2)]

        text line xpos int(72 * upscale_ratio) ypos int(dialogue_nameplate_height / 2) xmaximum (dialogue_textbox_width - (int(72 * upscale_ratio) * 2)) size int(29 * upscale_ratio) line_leading int(15 * upscale_ratio) color dialogue_default_color font "fonts/FGO-Main-Font.otf" substitute False slow_cps 85 slow_abortable True outlines [(1, "#00000066", 1, 1)]

    key "dismiss" action Return(True)
    key "K_SPACE" action Return(True)
    key "K_RETURN" action Return(True)

# TODO: This is extremely ugly code-wise, but it works (someone who knows renpy please fix) 
screen reader_dialogue_static(speaker, line):
    zorder 80

    fixed:
        align (0.5, 1.0)
        xysize (dialogue_textbox_width, dialogue_textbox_height)

        add "gui/img_talk_textbg.png" xysize (dialogue_textbox_width, dialogue_textbox_height) ypos -18
        add "gui/img_talk_namebg.png" xysize (dialogue_nameplate_width, dialogue_nameplate_height) xpos 0 ypos -int(dialogue_nameplate_height - 8)

        if speaker:
            text speaker xpos 30 ypos (-int(dialogue_nameplate_height - 8) + (dialogue_nameplate_height - int(29 * upscale_ratio)) // 2) xmaximum (dialogue_nameplate_width - 30) size int(29 * upscale_ratio) color dialogue_default_color font "fonts/FGO-Main-Font.otf" substitute False outlines [(1, "#000000aa", 1, 2)]

        text line xpos int(72 * upscale_ratio) ypos int(dialogue_nameplate_height / 2) xmaximum (dialogue_textbox_width - (int(72 * upscale_ratio) * 2)) size int(29 * upscale_ratio) line_leading int(15 * upscale_ratio) color dialogue_default_color font "fonts/FGO-Main-Font.otf" substitute False outlines [(1, "#00000066", 1, 1)]

screen reader_choice(choices):
    modal True
    zorder 85

    button:
        background None
        xfill True
        yfill True
        action NullAction()

    vbox:
        align (0.5, 0.28)
        spacing 13

        for i, choice in enumerate(choices):
            button:
                xysize (int(970 * upscale_ratio), 90)
                background Frame("gui/img_talk_selectbg.png", 18, 18)
                hover_background Frame("gui/img_talk_selectbg.png", 18, 18)
                action Return(i)

                text choice xalign 0.5 yalign 0.5 xmaximum (int(970 * upscale_ratio) - (int(72 * upscale_ratio) * 2)) size int(29 * upscale_ratio) color dialogue_default_color font "fonts/FGO-Main-Font.otf" substitute False outlines [(1, "#00000066", 1, 1)]

screen backlog_screen():
    modal True
    zorder 160
    add "gui/backlog.png":
        xysize(config.screen_width,config.screen_height)
    frame:
        align (0.5, 0.5)
        xysize (720, 520)
        xpadding 20
        ypadding 18
        has vbox

        hbox:
            xfill True
            text "Backlog" size 34
            textbutton "Close" xalign 1.0 action Hide("backlog_screen")

        viewport mousewheel True draggable True:
            vbox:
                spacing 10
                for entry in backlog:
                    if entry.get("speaker"):
                        text entry.get("speaker") size 18 color "#9fc7ff" font "fonts/FGO-Main-Font.otf" substitute False
                        text entry.get("line") size 20 font "fonts/FGO-Main-Font.otf" substitute False

screen settings_screen():
    #The settings background
    
    add "gui/myRoom.png" xysize (config.screen_width, config.screen_height)
    add "gui/settings.png":
        xysize (config.screen_width,config.screen_height)
        align (0.0, 0.5)
    modal True
    zorder 170
    
    frame:
        background None
        style_prefix "navigation"
        # Modified by 608
        if renpy.get_screen("settings_screen"):
            xalign 0.5
        else:
            xpos gui.navigation_xpos
        yalign 0.5
        xysize (1100, 650)
        xpadding 22
        ypadding 18
        has vbox
        spacing 15
        text "Settings":
            color "#befbff"
            xalign 1
            
            size 34 
        #Music Row
        frame:
            xalign 0.0
            xysize (400, 110)
            xpadding 20
            ypadding 15
            
            has hbox
            xalign 0.5
            yalign 0.5
            
            text "Music Volume : ":
                size 22
                xalign 0.0
            frame:
                xalign 0.5
                yalign 0.5
                xysize (320, 16)
                bar value Preference("music volume"):
                    base_bar Frame("gui/barFrame.png", Borders(4, 4, 4, 4))
                    xmaximum 320
                    ysize 10
                    xalign 0.5
                    yalign 0.5
                    left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                    right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                add "gui/barFrame.png":
                    xalign 0.5
                    yalign 0.5
                    xysize (320, 16)
        #Sound Row
        frame:
            xalign 0
            xysize (400, 110)
            xpadding 20
            ypadding 15
            
            has hbox
            xalign 0.5
            yalign 0.5
            
            text "Sound Volume : ":
                #color "#000000"
                size 22
                xalign 0.5
            frame:
                xalign 0.5
                yalign 0.5
                xysize (320, 16)
                bar value Preference("sound volume"):
                    xmaximum 320
                    ysize 10
                    xalign 0.5
                    yalign 0.5
                    left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                    right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                add "gui/barFrame.png":
                    xalign 0.5
                    yalign 0.5
                    xysize (320, 16)
        #Language Selector
        frame:
            xalign 0
            xysize (400, 110)
            xpadding 20
            ypadding 15
            
            has hbox
            yalign 0.5
            spacing 22
            text "Language : ":
                size 22
                xalign 0.0
            # English Language selector 
            frame:
                fixed:
                    yalign 0.5
                    xysize (150, 60)  
                    imagebutton:
                        align (0.5, 0.5)
                        idle ("gui/buttonActivate.png" if lang == "NA" else "gui/button.png")
                        action [SetVariable("lang", "NA"), Function(config_update, "language", "NA")]
                    
                    text "English":
                        align (0.5, 0.5)
                        size 22
                        color ("#befbff" if lang == "NA" else "#ffffff")
            #Japanese
            frame:
                fixed:
                    yalign 0.5
                    xysize (150, 60)  
                    imagebutton:
                        align (0.5, 0.5)
                        idle ("gui/buttonActivate.png" if lang == "JP" else "gui/button.png")
                        action [SetVariable("lang", "NA"), Function(config_update, "language", "JP")]
                    
                    text "Japanese":
                        align (0.5, 0.5)
                        size 22
                        color ("#befbff" if lang == "JP" else "#ffffff")
        #For Rayshift Fan Translation
        frame:
            xalign 0
            xysize (400, 110)
            xpadding 20
            ypadding 15
            
            has hbox
            yalign 0.5
            spacing 25
            text "Rayshift : ":
                size 22
                xalign 0.0
        textbutton "Close":
            action Hide("settings_screen")
            xalign 0.0
            text_color "#ffffff"
            text_hover_color "#2c465e"

screen reader_nav():
    zorder 100

    hbox:
        align (0.98, 0.03)
        spacing 8

        textbutton "War List" action Jump("war_list_flow")
        textbutton "LOG" action Show("backlog_screen")
        textbutton "Save" action Function(save_current_marker)
        textbutton "Quit" action Quit(confirm=False)

    if menu_notice:
        frame:
            align (0.5, 0.04)
            xpadding 10
            ypadding 6
            text menu_notice size 18 substitute False

screen title_menu():
    tag menu
    
    if titleScreen == "Part1":
        add part1title:
            xysize (config.screen_width, config.screen_height) crop (20, 0, config.screen_width, config.screen_height)
        if lang == 'JP':
            add part1logoJP align (0.5, 0.25)
        else:
            add part1logoNA align (0.5, 0.25)
    elif titleScreen == "Part2":
        add part2title:
            xysize (config.screen_width*2, config.screen_height*2) crop (20, 0.0, config.screen_width, config.screen_height)
        add part2logo:
            align (0.5, 0.25)

    hbox:
        align (0.5, 0.90)
        spacing 50

        textbutton "Start" action Jump("war_list_flow")
        if getattr(persistent, "reader_marker", None):
            textbutton "Continue" action [
                SetVariable("resume_marker", persistent.reader_marker),
                SetVariable("selected_war_id", persistent.reader_marker.get("war_id")),
                Jump("load_war"),
            ]

            textbutton "Load" action [
                SetVariable("resume_marker", persistent.reader_marker),
                SetVariable("selected_war_id", persistent.reader_marker.get("war_id")),
                Jump("load_war"),
            ]
        else:
            textbutton "Continue" action NullAction()
            textbutton "Load" action NullAction()
        textbutton "Settings" action Show("settings_screen")
        textbutton "Quit" action Quit(confirm=False)

    if menu_notice:
        frame:
            align (0.5, 0.94)
            xpadding 14
            ypadding 8
            text menu_notice substitute False

screen quest_title_card(quest):
    modal True
    zorder 90

    frame:
        align (0.5, 0.5)
        xmaximum 680
        xpadding 28
        ypadding 24
        has vbox

        $ war_title = quest.get("warLongName") or ""
        $ quest_name = quest.get("name") or "Unnamed Quest"
        $ spot_name = quest.get("spotName") or ""

        text war_title size 24 text_align 0.5 xalign 0.5 substitute False
        text quest_name size 34 text_align 0.5 xalign 0.5 substitute False
        if spot_name:
            text spot_name size 24 text_align 0.5 xalign 0.5 substitute False

        null height 16

        hbox:
            xalign 0.5
            spacing 12
            textbutton "Continue" action Return(True)
            textbutton "War List" action Jump("war_list_flow")
            textbutton "Quit" action Quit(confirm=False)

screen war_select():
    add Solid("#000000")
    zorder 0
    if titleScreen == "part1":
        add part1terminal:
            align(config.screen_width,config.screen_height)
    elif titleScreen == "part2":
        add part1terminal:
            align(config.screen_width,config.screen_height)
    tag menu
    modal True
    frame:
        align (0.5, 0.5)
        xpadding 20
        ypadding 20
        xmaximum 900
        ymaximum 700
        has vbox

        text "Select a War" size 36

        if war_load_error:
            text war_load_error size 24 substitute False
            textbutton "Retry" action Return(False)
        else:
            viewport id "war_list_view" mousewheel True draggable True:
                vbox:
                    for war in war_list:
                        # Show the Atlas banner as the war select button.
                        $ war_id = war.get("id")
        
                        if war_id and (war_id // 1000 >= 11) and (war_id % 1000 == 0):
                            $ valueID =  war_id // 1000 - 10
                            $ title = war.get("longName") or war.get("name") or "Unnamed War"
                            $ banner = war.get("banner")
                        
                            if banner:
                                imagebutton:
                                    idle banner
                                    hover banner
                                    action Show("war_selectMainStory", target_id=valueID)
                                    xsize 600
                                    ysize 120
                            else:
                                textbutton title:
                                    action Show("war_selectMainStory", target_id=valueID)
                                    text_color "#000000"
                            null height 8
                        

        hbox:
            spacing 16
            textbutton "Refresh" action [SetVariable("war_list", []), SetVariable("war_load_error", None), Return(False)]
            textbutton "Quit" action Quit(confirm=False)
screen war_selectMainStory(target_id):
    add Solid("#000000")
    zorder 0
    add part1terminal:
        align(config.screen_width,config.screen_height)
    tag menu
    modal True
    frame:
        align (0.5, 0.5)
        xpadding 20
        ypadding 20
        xmaximum 900
        ymaximum 700
        has vbox

        text "Select a War" size 36

        if war_load_error:
            text war_load_error size 24 substitute False
            textbutton "Retry" action Return(False)
        else:
            viewport id "war_list_view" mousewheel True draggable True:
                vbox:
                    for war in war_list:
                        $ war_id = war.get("id")
                        if war_id and (war_id // 100 == target_id ):
                            $ title = war.get("longName") or war.get("name") or "Unnamed War"
                            $ banner = war.get("banner")
                        
                            if banner:
                                imagebutton:
                                    idle banner
                                    hover banner
                                    action [SetVariable("selected_war_id", war_id), Return(True)]
                                    xsize 600
                                    ysize 120
                            else:
                                textbutton title:
                                    action [SetVariable("selected_war_id",  war_id), Return(True)]
                                    text_color "#000000"
                            null height 8
        hbox:
            spacing 16
            textbutton "Return" action Show("war_select")
            textbutton "Refresh" action [SetVariable("war_list", []), SetVariable("war_load_error", None), Return(False)]
            textbutton "Quit" action Quit(confirm=False)
label start:
    $ menu_notice = ""
    call screen title_menu
    jump start

label war_list_flow:
    scene black
    with fade
    $ resume_marker = None

    if not war_list:
        show screen loading_screen("Fetching available NA Wars...")

        $ renpy.pause(0.1, hard=True)
        python:
            api = get_api()
            war_list_data = api.get_war_list()
        hide screen loading_screen

        if war_list_data is None:
            $ war_load_error = api.last_error or "Unable to fetch the war list. Check your internet connection."
            call screen message_screen(war_load_error)
            jump start

        python:
            war_list = sorted(war_list_data, key=lambda item: item.get("id", 0))
            war_list = [
                {
                    "id": item.get("id", 0),
                    "name": item.get("name", ""),
                    "longName": item.get("longName", ""),
                    "scriptId": item.get("scriptId", ""),
                    "script": item.get("script", ""),
                    # Cache the banner URL so Ren'Py can render it as a local image.
                    "banner": (api.get_banner_path(item.get("banner")) or "").replace("\\", "/")
                }
                for item in war_list
            ]
            war_load_error = None

    call screen war_select

    if not selected_war_id:
        jump start

    jump load_war

label load_war:
    show screen loading_screen("Loading war [selected_war_id]...")
    $ renpy.pause(0.1, hard=True)
    python:
        api = get_api()
        active_war = api.load_war(selected_war_id)
    hide screen loading_screen

    if active_war is None:
        $ war_load_error = api.last_error or "Failed to load the selected war. Please try another."
        call screen message_screen(war_load_error)
        jump start

    jump play_war

label apply_music_flag:
    # Ren'Py music statements must live in script, so Python only resolves the flag data.
    if music_flag and music_flag.get("action") == "play":
        $ music_path = music_flag.get("path")
        $ music_fade = music_flag.get("fade", 0.2)
        play music music_path fadein music_fade
    elif music_flag and music_flag.get("action") == "stop":
        $ music_fade = music_flag.get("fade", 0.2)
        stop music fadeout music_fade
    return

label play_war:
    scene black
    with fade
    $ current_background_path = None
    $ current_scene_id = None
    $ current_characters = []
    $ current_chara_defs = {}
    $ backlog = []
    show screen vn_stage(current_background_path, current_scene_id, current_characters)
    show screen reader_nav

    $ war_name = active_war["war"].get("longName") or active_war["war"].get("name") or "Unknown War"
    n "Now playing: [war_name]"

    if active_war.get("story_quests"):
        # Modern wars are split into quest phases, so load each phase script as the reader reaches it.
        $ quest_idx = resume_marker.get("quest_idx", 0) if resume_marker else 0
        $ resume_phase_idx = resume_marker.get("phase_idx", 0) if resume_marker else 0
        while quest_idx < len(active_war["story_quests"]):
            $ quest = active_war["story_quests"][quest_idx]
            call screen quest_title_card(quest)

            $ phase_idx = resume_phase_idx if resume_marker and quest_idx == resume_marker.get("quest_idx", 0) else 0
            while phase_idx < len(quest["phase_scripts"]):
                $ phase_script = quest["phase_scripts"][phase_idx]
                $ current_reader_marker = {"war_id": selected_war_id, "quest_idx": quest_idx, "phase_idx": phase_idx, "script_id": phase_script.get("scriptId"), "script": phase_script.get("script")}
                show screen loading_screen("Loading phase [phase_script.get('phase')]...")
                $ renpy.pause(0.1, hard=True)
                python:
                    phase_nodes = api.load_script_nodes(phase_script.get("script"))
                hide screen loading_screen

                if phase_nodes is None:
                    $ war_load_error = api.last_error or "Failed to load a phase script."
                    call screen message_screen(war_load_error)
                else:
                    $ idx = 0
                    while idx < len(phase_nodes):
                        $ node = phase_nodes[idx]
                        if node["type"] == "dialogue":
                            $ speaker = node.get("speaker") or ""
                            $ text = node.get("text") or ""
                            if text:
                                $ music_flag = get_scene_music_flag(node, api)
                                call apply_music_flag
                                $ record_dialogue(speaker, text)
                                $ last_speaker = speaker
                                $ last_line = text
                                call screen reader_dialogue(speaker, text)
                        elif node["type"] == "choice":
                            $ music_flag = get_scene_music_flag(node, api)
                            call apply_music_flag
                            python:
                                choice_options, next_idx = collect_choice_options(phase_nodes, idx)
                            if choice_options:
                                show screen reader_dialogue_static(last_speaker, last_line)
                                call screen reader_choice(choice_options)
                                hide screen reader_dialogue_static
                            $ idx = next_idx - 1
                        elif node["type"] == "choice_block":
                            python:
                                cb_choices = node.get("choices", [])
                                cb_texts = [normalize_choice_text(c.get("text", "")) for c in cb_choices]
                            if cb_texts:
                                show screen reader_dialogue_static(last_speaker, last_line)
                                call screen reader_choice(cb_texts)
                                hide screen reader_dialogue_static
                                python:
                                    selected_branch_nodes = cb_choices[_return].get("nodes", []) if _return is not None and _return < len(cb_choices) else []
                                $ branch_idx = 0
                                while branch_idx < len(selected_branch_nodes):
                                    $ branch_node = selected_branch_nodes[branch_idx]
                                    if branch_node["type"] == "dialogue":
                                        $ bspeaker = branch_node.get("speaker") or ""
                                        $ btext = branch_node.get("text") or ""
                                        if btext:
                                            $ music_flag = get_scene_music_flag(branch_node, api)
                                            call apply_music_flag
                                            $ record_dialogue(bspeaker, btext)
                                            $ last_speaker = bspeaker
                                            $ last_line = btext
                                            call screen reader_dialogue(bspeaker, btext)
                                    else:
                                        $ music_flag = get_scene_music_flag(branch_node, api)
                                        call apply_music_flag
                                        if not music_flag and apply_reader_node(branch_node, api):
                                            show screen vn_stage(current_background_path, current_scene_id, current_characters)
                                    $ branch_idx += 1
                        else:
                            $ music_flag = get_scene_music_flag(node, api)
                            call apply_music_flag
                            if not music_flag and apply_reader_node(node, api):
                                show screen vn_stage(current_background_path, current_scene_id, current_characters)
                        $ idx += 1

                $ phase_idx += 1
                $ resume_marker = None

            $ quest_idx += 1

        n "End of story."
        hide screen reader_nav
        hide screen vn_stage
        jump start

    if active_war["script_id"]:
        n "Script ID: [active_war['script_id']]"

    if not active_war["script_nodes"]:
        n "No parsed script content was found for this war."
        if active_war.get("script_url"):
            n "Atlas listed [active_war['script_url']], but it did not contain readable dialogue."
        n "Returning to the war list."
        hide screen reader_nav
        hide screen vn_stage
        jump start

    $ idx = 0
    while idx < len(active_war["script_nodes"]):
        $ node = active_war["script_nodes"][idx]
        if node["type"] == "dialogue":
            $ speaker = node.get("speaker") or ""
            $ text = node.get("text") or ""
            if text:
                $ music_flag = get_scene_music_flag(node, api)
                call apply_music_flag
                $ record_dialogue(speaker, text)
                $ last_speaker = speaker
                $ last_line = text
                call screen reader_dialogue(speaker, text)
        elif node["type"] == "choice":
            $ music_flag = get_scene_music_flag(node, api)
            call apply_music_flag
            python:
                choice_options, next_idx = collect_choice_options(active_war["script_nodes"], idx)
            if choice_options:
                show screen reader_dialogue_static(last_speaker, last_line)
                call screen reader_choice(choice_options)
                hide screen reader_dialogue_static
            $ idx = next_idx - 1
        elif node["type"] == "choice_block":
            python:
                cb_choices = node.get("choices", [])
                cb_texts = [normalize_choice_text(c.get("text", "")) for c in cb_choices]
            if cb_texts:
                show screen reader_dialogue_static(last_speaker, last_line)
                call screen reader_choice(cb_texts)
                hide screen reader_dialogue_static
                python:
                    selected_branch_nodes = cb_choices[_return].get("nodes", []) if _return is not None and _return < len(cb_choices) else []
                $ branch_idx = 0
                while branch_idx < len(selected_branch_nodes):
                    $ branch_node = selected_branch_nodes[branch_idx]
                    if branch_node["type"] == "dialogue":
                        $ bspeaker = branch_node.get("speaker") or ""
                        $ btext = branch_node.get("text") or ""
                        if btext:
                            $ music_flag = get_scene_music_flag(branch_node, api)
                            call apply_music_flag
                            $ record_dialogue(bspeaker, btext)
                            $ last_speaker = bspeaker
                            $ last_line = btext
                            call screen reader_dialogue(bspeaker, btext)
                    else:
                        $ music_flag = get_scene_music_flag(branch_node, api)
                        call apply_music_flag
                        if not music_flag and apply_reader_node(branch_node, api):
                            show screen vn_stage(current_background_path, current_scene_id, current_characters)
                    $ branch_idx += 1
        else:
            $ music_flag = get_scene_music_flag(node, api)
            call apply_music_flag
            if not music_flag and apply_reader_node(node, api):
                show screen vn_stage(current_background_path, current_scene_id, current_characters)
        $ idx += 1

    n "End of script."
    hide screen reader_nav
    hide screen vn_stage
    jump start