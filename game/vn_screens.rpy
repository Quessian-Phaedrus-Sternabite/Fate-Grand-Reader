# Reader, VN stage, menus, settings, and war-selection screens.
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

screen vn_stage():
    zorder 0

    # Main RT and sublayer RT quads are siblings in MargePanel, not nested cameras.
    for vn_item_kind, vn_item_key in vn_stage_items():
        if vn_item_kind == "main":
            fixed at vn_camera_transform():
                xysize (config.screen_width, config.screen_height)
                if vn_bg_prev_visible():
                    add vn_bg["prev"] xysize (config.screen_width, config.screen_height)
                if current_background_path:
                    add current_background_path xysize (config.screen_width, config.screen_height) at vn_bg_transform()
                else:
                    add Solid("#111318")
                fixed at Transform(zoom=upscale_ratio):
                    xysize (original_screen_width, original_screen_height)
                    yalign 1.0
                    for vn_slot in vn_draw_order():
                        if not current_chara_defs[vn_slot].get("sub_group"):
                            add vn_slot_image(vn_slot) at vn_slot_transform(vn_slot)
        else:
            fixed at Transform(zoom=upscale_ratio):
                xysize (original_screen_width, original_screen_height)
                yalign 1.0
                add vn_group_image(vn_item_key) at vn_group_transform(vn_item_key)

    if current_scene_id and not current_background_path:
        frame:
            align (0.02, 0.03)
            xpadding 10
            ypadding 6
            text "Scene [current_scene_id]" size 18

# [fadein]/[fadeout] quad: lives in the stage layer, i.e. under the message window.
screen vn_fade_layer():
    zorder 50
    if vn_fade:
        add Solid(vn_overlay_color("vn_fade")) at vn_overlay_transform("vn_fade")

# Wipes cover stage mutations (character moves/depth changes) and sit below the message UI.
screen vn_wipe_layer():
    zorder 60
    if vn_wipe:
        add Solid("#000000") at vn_overlay_transform("vn_wipe")

# CommonUI mask ([maskin]): above everything, including the message window.
screen vn_mask_layer():
    zorder 200
    if vn_mask:
        add Solid(vn_overlay_color("vn_mask")) at vn_overlay_transform("vn_mask")

# Persistent message window. It has no buttons: the tap target is reader_advance below it. slow_abortable
# is off on the message text, so a tap during typing completes it via VNAdvance (or fills the visible
# window in manual scroll) and the next tap advances, like FGO.
screen vn_message():
    zorder 90

    if vn_msg_visible:
        # BackSprite: img_talk_textbg 1019x144 (sliced), centred 13 px above the window origin. The repo PNG has
        # transparent margins, so its opaque box is mapped onto the sprite rect.
        add "gui/img_talk_textbg.png" crop (20, 5, 984, 150) xysize (vn_px(VN_BOX_W), vn_px(VN_BOX_H)) anchor (0.5, 0.5) pos (vn_px(512), vn_px(VN_BOX_CY))

        if vn_msg_speaker:
            # TalkNameBack (img_talk_namebg, sliced, pivot Left): width = max(220, text) + 80; the label's top-left
            # sits 25.5 px in from the plate's left edge, at the plate's top edge.
            frame:
                background Frame("gui/img_talk_namebg.png", Borders(int(16 * upscale_ratio), 0, int(70 * upscale_ratio), 0))
                xpos vn_px(VN_NAME_PLATE_LEFT)
                ypos vn_px(VN_NAME_PLATE_TOP)
                xminimum vn_px(VN_NAME_DEFAULT_W + VN_NAME_BASE_W)
                ysize vn_px(VN_NAME_PLATE_H)
                left_padding vn_px(VN_NAME_TEXT_X - VN_NAME_PLATE_LEFT)
                right_padding vn_px(VN_NAME_BASE_W - (VN_NAME_TEXT_X - VN_NAME_PLATE_LEFT))
                top_padding vn_px(VN_LINE_START_Y + VN_GLYPH_ADJUST)
                bottom_padding 0
                text vn_msg_speaker style "vn_name_text" yalign 0.0 substitute False

        if vn_msg_disp is not None:
            # dispPanel clip: 880x104 centred on the window origin (SoftClip in NGUI).
            viewport:
                xpos vn_px(512 - VN_DISP_W / 2.0)
                ypos vn_px(VN_WINDOW_CY - VN_DISP_H / 2.0)
                xysize (vn_text_width(), vn_visible_height())
                mousewheel False
                draggable False
                add vn_msg_disp

        if (vn_text_done_at is not None or vn_msg_is_holding()) and vn_show_next_mark:
            add "gui/img_arrow_under.png" at vn_next_mark

        # Manual scroll freezes the typewriter inside the body (a redraw, not a screen event), so poll
        # a few times a second while a gated message is mid-flight to refresh the next-mark at each gate.
        if vn_gate_active():
            timer 0.15 repeat True action NullAction()

# Invisible tap target for [k]; only exists while a message waits for the reader.
screen reader_advance():
    zorder 80
    modal True

    button:
        background None
        xfill True
        yfill True
        action VNAdvance()

    key "dismiss" action VNAdvance()
    key "K_SPACE" action VNAdvance()
    key "K_RETURN" action VNAdvance()
    key "K_TAB" action ToggleVariable("vn_skip_mode")

    # Skip mode: auto-advance rapidly
    if vn_skip_mode:
        timer 0.02 action Return(True)
    # Auto message: after the text finishes, wait GetAutoWaitTime(chars) then advance.
    elif vn_auto_mode and vn_text_done_at is not None:
        timer vn_auto_wait_seconds() action Return(True)

screen reader_choice(choices, auto_pick=None):
    modal True
    zorder 85
    default chosen = auto_pick

    $ vn_choice_pitch = {5: 95, 6: 77}.get(len(choices), 100)
    $ vn_choice_shift = 22 if len(choices) >= 5 else 0
    $ vn_choice_w = vn_px(970)
    $ vn_choice_h = vn_px(90)

    button:
        background None
        xfill True
        yfill True
        action NullAction()

    if chosen is not None:
        # SetMode(INPUT): the callback fires once the chosen item's EndMove runs (0.1 + 0.5 + 0.6 s).
        timer 1.2 action Return(chosen)

    for i, choice in enumerate(choices):
        $ vn_choice_cy = vn_px(VN_SELECT_CENTER_Y - vn_choice_shift - (len(choices) - 1) * vn_choice_pitch / 2.0 + i * vn_choice_pitch)
        fixed:
            xanchor 0.5
            xpos 0.5
            yanchor 0.5
            ypos vn_choice_cy
            xysize (vn_choice_w, vn_choice_h)

            if chosen is None:
                button:
                    xysize (vn_choice_w, vn_choice_h)
                    background Frame("gui/img_talk_selectbg.png", 18, 18)
                    hover_background Frame("gui/img_talk_selectbg.png", 18, 18)
                    action SetScreenVariable("chosen", i)
                    text choice style "vn_name_text" xalign 0.5 yalign 0.5 xmaximum (vn_choice_w - vn_px(72) * 2) substitute False
            elif i == chosen:
                fixed at vn_choice_fade_out(0.6, 0.6):
                    xysize (vn_choice_w, vn_choice_h)
                    add Frame("gui/img_talk_selectbg.png", 18, 18) xysize (vn_choice_w, vn_choice_h)
                    text choice style "vn_name_text" xalign 0.5 yalign 0.5 xmaximum (vn_choice_w - vn_px(72) * 2) substitute False
                text choice style "vn_name_text" color VN_SELECT_COLOR xmaximum (vn_choice_w - vn_px(72) * 2) substitute False at vn_choice_burst
            else:
                fixed at vn_choice_fade_out(0.0, 0.5):
                    xysize (vn_choice_w, vn_choice_h)
                    add Frame("gui/img_talk_selectbg.png", 18, 18) xysize (vn_choice_w, vn_choice_h)
                    text choice style "vn_name_text" xalign 0.5 yalign 0.5 xmaximum (vn_choice_w - vn_px(72) * 2) substitute False

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
        xysize (1100, 620)
        xpadding 22
        ypadding 14
        has vbox
        spacing 10
        text "Settings":
            color "#befbff"
            xalign 0.5

            size 34
        hbox:
            spacing 50
            vbox:
                spacing 10
                #Music Row
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    xalign 0.5
                    yalign 0.5

                    text "Music Volume : ":
                        size 22
                        xalign 0.0
                    frame:
                        xalign 0.5
                        yalign 0.5
                        xysize (260, 16)
                        bar value Preference("music volume"):
                            base_bar Frame("gui/barFrame.png", Borders(4, 4, 4, 4))
                            xmaximum 260
                            ysize 10
                            xalign 0.5
                            yalign 0.5
                            left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                            right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                        add "gui/barFrame.png":
                            xalign 0.5
                            yalign 0.5
                            xysize (260, 16)
                #Sound Row
                frame:
                    xalign 0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    xalign 0.5
                    yalign 0.5

                    text "Sound Volume : ":
                        size 22
                        xalign 0.0
                    frame:
                        xalign 0.5
                        yalign 0.5
                        xysize (260, 16)
                        bar value Preference("sound volume"):
                            xmaximum 260
                            ysize 10
                            xalign 0.5
                            yalign 0.5
                            left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                            right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                        add "gui/barFrame.png":
                            xalign 0.5
                            yalign 0.5
                            xysize (260, 16)
                #Language Selector
                frame:
                    xalign 0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18
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
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18
                    text "Rayshift : ":
                        size 22
                        xalign 0.0
                #Fetch Row (Online = fetch missing wars/scripts/assets from Atlas Academy; Offline = cache only)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18

                    text "Fetch : ":
                        size 22
                        xalign 0.0
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if not vn_offline else "gui/button.png")
                                action [SetVariable("vn_offline", False), Function(vn_settings_changed)]
                            text "Online":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if not vn_offline else "#ffffff")
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if vn_offline else "gui/button.png")
                                action [SetVariable("vn_offline", True), Function(vn_settings_changed)]
                            text "Offline":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if vn_offline else "#ffffff")
            vbox:
                spacing 10
                #Text Speed Row (Ren'Py text speed preference, characters per second; 0 = instant, FGO default 30)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    xalign 0.5
                    yalign 0.5

                    text "Text Speed : ":
                        size 22
                        xalign 0.0
                    frame:
                        xalign 0.5
                        yalign 0.5
                        xysize (260, 16)
                        bar value Preference("text speed"):
                            base_bar Frame("gui/barFrame.png", Borders(4, 4, 4, 4))
                            xmaximum 260
                            ysize 10
                            xalign 0.5
                            yalign 0.5
                            left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                            right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                        add "gui/barFrame.png":
                            xalign 0.5
                            yalign 0.5
                            xysize (260, 16)
                #Auto-Forward Row (FGO ScenarioAutoWaitTime 0..3; used by the AUTO button in the reader)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    xalign 0.5
                    yalign 0.5

                    text "Auto Wait : ":
                        size 22
                        xalign 0.0
                    frame:
                        xalign 0.5
                        yalign 0.5
                        xysize (260, 16)
                        bar value VariableValue("vn_auto_wait", range=3, step=1, force_step=True, action=Function(vn_settings_changed)):
                            base_bar Frame("gui/barFrame.png", Borders(4, 4, 4, 4))
                            xmaximum 260
                            ysize 10
                            xalign 0.5
                            yalign 0.5
                            left_bar Frame("gui/progressBarfull.png", Borders(4, 4, 4, 4))
                            right_bar Frame("gui/progressBarempty.png", Borders(4, 4, 4, 4))
                        add "gui/barFrame.png":
                            xalign 0.5
                            yalign 0.5
                            xysize (260, 16)
                #Scroll Mode Row (Auto = roll to the next line automatically; Manual = wait for a tap,
                # like FGO's manual scroll. AUTO message mode ignores this and always auto-scrolls.)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18

                    text "Scroll : ":
                        size 22
                        xalign 0.0
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if not vn_require_input else "gui/button.png")
                                action [SetVariable("vn_require_input", False), Function(vn_settings_changed)]
                            text "Auto":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if not vn_require_input else "#ffffff")
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if vn_require_input else "gui/button.png")
                                action [SetVariable("vn_require_input", True), Function(vn_settings_changed)]
                            text "Manual":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if vn_require_input else "#ffffff")
                #Master Name Row ([%1] in the scripts)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18

                    text "Master Name : ":
                        size 22
                        xalign 0.0
                    frame:
                        yalign 0.5
                        xysize (190, 44)
                        xpadding 8
                        ypadding 4
                        input value VariableInputValue("vn_player_name", returnable=False) length 14 size 22 color "#befbff" copypaste True
                #Master Gender Row ([&he:she] in the scripts)
                frame:
                    xalign 0.0
                    xysize (440, 78)
                    xpadding 20
                    ypadding 10

                    has hbox
                    yalign 0.5
                    spacing 18

                    text "Gender : ":
                        size 22
                        xalign 0.0
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if vn_player_gender == 0 else "gui/button.png")
                                action [SetVariable("vn_player_gender", 0), Function(vn_settings_changed)]
                            text "Male":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if vn_player_gender == 0 else "#ffffff")
                    frame:
                        fixed:
                            yalign 0.5
                            xysize (110, 60)
                            imagebutton:
                                align (0.5, 0.5)
                                idle ("gui/buttonActivate.png" if vn_player_gender == 1 else "gui/button.png")
                                action [SetVariable("vn_player_gender", 1), Function(vn_settings_changed)]
                            text "Female":
                                align (0.5, 0.5)
                                size 22
                                color ("#befbff" if vn_player_gender == 1 else "#ffffff")
        textbutton "Close":
            action [Function(vn_settings_changed), Hide("settings_screen")]
            xalign 0.0
            text_color "#ffffff"
            text_hover_color "#2c465e"

screen reader_nav():
    zorder 100

    key "K_TAB" action ToggleVariable("vn_skip_mode")

    hbox:
        align (0.98, 0.03)
        spacing 8

        textbutton "War List" action Jump("war_list_flow")
        textbutton "Settings" action Show("settings_screen")
        textbutton "LOG" action Show("backlog_screen")
        textbutton "Save" action Function(save_current_marker)
        textbutton "Quit" action Quit(confirm=False)

    if vn_skip_mode:
        frame:
            align (0.02, 0.97)
            background Frame(Solid("#000000cc"), 0, 0)
            xpadding 12
            ypadding 6
            text "SKIP" size 22 color "#ff6666" outlines [(1, "#000000", 0, 0)]

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

# ---------------------------------------------------------------------------
# War / chapter select
#
# The two-column layout, the FGO plate used for the war/chapter bars, and the
# free-standing scrollbars are ported from the FGO Offline Reader's story-select
# screen. Deliberately NOT ported from it: its background solid and grey backing
# panel (this reader keeps its own terminal art), its header bar and its settings
# UI. The black FETCH/Quit bar along the bottom is this project's own and stays.
# ---------------------------------------------------------------------------

screen war_select_topbar(caption):
    frame:
        background Solid("#000000")
        xfill True
        ysize sel_top_bar_h
        align (0.0, 0.0)
        xpadding 16
        hbox:
            xfill True
            yalign 0.5
            text caption size 30 color "#ffffff" substitute False
            hbox:
                xalign 1.0
                spacing 16
                textbutton "Settings":
                    action Show("settings_screen")
                    text_color "#befbff"


screen war_select():
    add Solid("#000000")
    zorder 0
    # titleScreen is "Part1"/"Part2" (capitalised, from config.json "Title"); match that case and size the
    # terminal art to fill the screen, the same way title_menu adds its wallpaper.
    if titleScreen == "Part2":
        add part2terminal:
            xysize (config.screen_width, config.screen_height)
    else:
        add part1terminal:
            xysize (config.screen_width, config.screen_height)
    tag menu
    modal True

    use war_select_topbar("Select a War")

    if war_load_error:
        vbox:
            align (0.5, 0.5)
            spacing 12
            text war_load_error style "sel_pane_label" size 24 substitute False
            textbutton "Retry" action Return("refetch")
    else:
        # The part/group wars: ids 11000, 12000, 13000, 14000. One centred column
        # -- there are no chapters to put beside it, so this screen stays single
        # column and only borrows the bar and scrollbar styling.
        $ parts = [w for w in war_list if w.get("id") and w["id"] // 1000 >= 11 and w["id"] % 1000 == 0]
        $ part_x = (config.screen_width - sel_banner_w) // 2
        $ part_bar_x = part_x + sel_banner_w + 20
        $ part_h = config.screen_height - sel_bottom_bar_h - sel_top
        $ part_bar_h = part_h - sel_banner_gap * 2
        $ part_scrolls = len(parts) * (sel_banner_h + sel_banner_gap) > part_h

        viewport id "war_list_view":
            xpos 0
            ypos sel_top
            xsize config.screen_width
            ysize part_h
            mousewheel True
            draggable True
            scrollbars None
            yinitial 0.0

            vbox:
                spacing sel_banner_gap
                ypos sel_banner_gap

                for war in parts:
                    $ valueID = war["id"] // 1000 - 10
                    button:
                        xpos part_x
                        xysize (sel_banner_w, sel_banner_h)
                        background None
                        action [Function(vn_clear_chapter_select),
                                Show("war_selectMainStory", target_id=valueID)]

                        if vn_war_banner(war):
                            hover_background Solid("#ffffff30")
                            add vn_war_banner(war):
                                xysize (sel_banner_w, sel_banner_h)
                                align (0.5, 0.5)
                        else:
                            # Atlas hosts no art for some group wars, so fall back
                            # to the same plate the bars use, with the name on it.
                            add fgo_plate(sel_banner_w, sel_banner_h, sel_chapter_plate)
                            text vn_war_title(war):
                                style "sel_chapter_label"
                                align (0.5, 0.5)
                                yoffset sel_chapter_text_y
                                xmaximum sel_banner_w - 60
                                substitute False

                null height sel_banner_gap

        # Only drawn when the list actually scrolls, to match the bar's own
        # `unscrollable "hide"` -- otherwise an empty groove would be left sitting
        # there with no handle on it.
        if part_scrolls:
            add fgo_scrollbar_groove(part_bar_h - fgo_scrollbar_track_inset * 2):
                xpos part_bar_x
                ypos sel_top + sel_banner_gap + fgo_scrollbar_track_inset

        vbar value YScrollValue("war_list_view"):
            xpos part_bar_x
            ypos sel_top + sel_banner_gap
            xsize sel_scrollbar_w
            ysize part_bar_h
            base_bar fgo_scrollbar_track()
            thumb Frame(fgo_slider_thumb, Borders(0, 12, 0, 12))
            unscrollable "hide"

    # Black bottom bar covering the lower portion of the screen
    frame:
        background Solid("#000000")
        xfill True
        ysize sel_bottom_bar_h
        align (0.0, 1.0)
        xpadding 16
        hbox:
            xfill True
            yalign 0.5
            # Left side: fetch button for manual network refresh
            textbutton "FETCH NEW WARS":
                action [Function(vn_forget_war_list), SetVariable("war_list", []), SetVariable("war_load_error", None), SetVariable("resume_marker", None), SetVariable("selected_war_id", 0), Function(vn_clear_chapter_select), Return("refetch")]
                text_color "#befbff"
            # Right side: quit
            hbox:
                xalign 1.0
                spacing 16
                textbutton "Quit" action Quit(confirm=False)


screen war_selectMainStory(target_id):
    add Solid("#000000")
    zorder 0
    if titleScreen == "Part2":
        add part2terminal:
            xysize (config.screen_width, config.screen_height)
    else:
        add part1terminal:
            xysize (config.screen_width, config.screen_height)
    tag menu
    modal True

    use war_select_topbar("Select a Chapter")

    if war_load_error:
        vbox:
            align (0.5, 0.5)
            spacing 12
            text war_load_error style "sel_pane_label" size 24 substitute False
            textbutton "Retry" action Return("refetch")
    else:
        $ part_wars = [w for w in war_list if w.get("id") and w["id"] // 100 == target_id]
        $ col_h = config.screen_height - sel_bottom_bar_h - sel_top

        # ---- left column: the wars in this part ---------------------------
        viewport id "war_list_view":
            xpos 0
            ypos sel_top
            xsize sel_left_w
            ysize col_h
            mousewheel True
            draggable True
            scrollbars None
            yinitial 0.0

            vbox:
                spacing sel_banner_gap
                ypos sel_banner_gap

                for war in part_wars:
                    button:
                        xpos sel_banner_x
                        xysize (sel_banner_w, sel_banner_h)
                        background None
                        # Fills the chapter column; it does not leave the screen.
                        action Function(vn_select_chapter_war, war["id"], vn_war_title(war))

                        if vn_war_banner(war):
                            hover_background Solid("#ffffff30")
                            add vn_war_banner(war):
                                xysize (sel_banner_w, sel_banner_h)
                                align (0.5, 0.5)
                        else:
                            add fgo_plate(sel_banner_w, sel_banner_h, sel_chapter_plate)
                            text vn_war_title(war):
                                style "sel_chapter_label"
                                align (0.5, 0.5)
                                yoffset sel_chapter_text_y
                                xmaximum sel_banner_w - 60
                                substitute False

                        if chapter_war_id == war["id"]:
                            add Solid("#ffcc4d"):
                                xysize (6, sel_banner_h)
                                xalign 0.0

                null height sel_banner_gap

        # Only drawn when the list actually scrolls, to match the bar's own
        # `unscrollable "hide"` -- otherwise an empty groove would be left sitting
        # there with no handle on it.
        $ ev_bar_h = col_h - sel_banner_gap * 2
        $ ev_scrolls = len(part_wars) * (sel_banner_h + sel_banner_gap) > col_h

        if ev_scrolls:
            add fgo_scrollbar_groove(ev_bar_h - fgo_scrollbar_track_inset * 2):
                xpos sel_scrollbar_x
                ypos sel_top + sel_banner_gap + fgo_scrollbar_track_inset

        vbar value YScrollValue("war_list_view"):
            xpos sel_scrollbar_x
            ypos sel_top + sel_banner_gap
            xsize sel_scrollbar_w
            ysize ev_bar_h
            base_bar fgo_scrollbar_track()
            thumb Frame(fgo_slider_thumb, Borders(0, 12, 0, 12))
            unscrollable "hide"

        # ---- right column: the chapters of the selected war ----------------
        # Pane width minus the scrollbar and the gap that keeps it clear of the bars.
        $ sel_chapter_w = (config.screen_width - sel_left_w - sel_gutter
                           - sel_chapter_pad * 2 - fgo_scrollbar_thumb_w
                           - sel_chapter_bar_gap)
        $ ch_text_x = sel_left_w + sel_gutter + sel_chapter_pad
        $ ch_text_w = config.screen_width - sel_left_w - sel_gutter - sel_chapter_pad * 2
        $ ch_bar_x = ch_text_x + sel_chapter_w + sel_chapter_bar_gap
        # +96 clears a two-line war name; FGO longNames routinely wrap.
        $ ch_bar_y = sel_top + 96
        $ ch_bar_h = config.screen_height - sel_bottom_bar_h - 12 - ch_bar_y

        if chapter_war_id is None:
            text "Select a war on the left to see its chapters.":
                style "sel_pane_label"
                xpos ch_text_x
                ypos sel_top + 40
                xmaximum ch_text_w
                size 22

        elif chapter_error:
            text chapter_error:
                style "sel_pane_label"
                xpos ch_text_x
                ypos sel_top + 40
                xmaximum ch_text_w
                size 22
                substitute False

        elif not chapter_rows:
            text "This war has no readable chapters.":
                style "sel_pane_label"
                xpos ch_text_x
                ypos sel_top + 40
                xmaximum ch_text_w
                size 22

        else:
            vbox:
                xpos ch_text_x
                ypos sel_top + 22
                spacing 6

                text chapter_war_name:
                    style "sel_pane_label"
                    xmaximum ch_text_w
                    size 22
                    substitute False

            $ ch_scrolls = len(chapter_rows) * (sel_chapter_h + sel_chapter_gap) > ch_bar_h

            viewport id "chapter_list":
                xpos ch_text_x
                ypos ch_bar_y
                xsize ch_text_w
                ysize ch_bar_h
                mousewheel True
                draggable True
                scrollbars None

                vbox:
                    spacing sel_chapter_gap

                    for ch_idx in range(len(chapter_rows)):
                        $ chapter = chapter_rows[ch_idx]
                        button:
                            # An explicit width, not xfill. The plate has to be
                            # built at the button's real width -- built any wider
                            # and its right cap lands outside the button and is
                            # clipped away, which is what removes the bar's
                            # rounded end.
                            xsize sel_chapter_w
                            ysize sel_chapter_h
                            background None
                            # play_war indexes story_quests positionally, so the
                            # marker carries the row index, not the quest id.
                            action [SetVariable("selected_war_id", chapter_war_id),
                                    SetVariable("resume_marker", {"war_id": chapter_war_id, "quest_idx": ch_idx, "phase_idx": 0}),
                                    Return(True)]

                            add fgo_plate(sel_chapter_w, sel_chapter_h, sel_chapter_plate)

                            text chapter["name"]:
                                style "sel_chapter_label"
                                align (0.5, 0.5)
                                yoffset sel_chapter_text_y
                                xmaximum sel_chapter_w - 90
                                substitute False

                    null height 12

            # After the viewport, not before: YScrollValue can only reference an
            # id that has already been declared.
            if ch_scrolls:
                add fgo_scrollbar_groove(ch_bar_h - fgo_scrollbar_track_inset * 2):
                    xpos ch_bar_x
                    ypos ch_bar_y + fgo_scrollbar_track_inset

                vbar value YScrollValue("chapter_list"):
                    xpos ch_bar_x
                    ypos ch_bar_y
                    xsize fgo_scrollbar_thumb_w
                    ysize ch_bar_h
                    base_bar fgo_scrollbar_track()
                    thumb Frame(fgo_slider_thumb, Borders(0, 12, 0, 12))

    # Black bottom bar covering the lower portion of the screen
    frame:
        background Solid("#000000")
        xfill True
        ysize sel_bottom_bar_h
        align (0.0, 1.0)
        xpadding 16
        hbox:
            xfill True
            yalign 0.5
            # Left side: fetch button for manual network refresh
            textbutton "FETCH NEW WARS":
                action [Function(vn_forget_war_list), SetVariable("war_list", []), SetVariable("war_load_error", None), SetVariable("resume_marker", None), SetVariable("selected_war_id", 0), Function(vn_clear_chapter_select), Return("refetch")]
                text_color "#befbff"
            # Right side: return and quit
            hbox:
                xalign 1.0
                spacing 16
                textbutton "Return" action [Function(vn_clear_chapter_select), Show("war_select")]
                textbutton "Quit" action Quit(confirm=False)

