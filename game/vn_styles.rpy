# Styles, transforms, display constants, and coordinate helpers.
init offset = 1

define original_screen_width = 1024
define original_screen_height = 576
define upscale_ratio = config.screen_width / original_screen_width

# ---------------------------------------------------------------------------
# FGO VN-mode feel constants. Every value below comes from the decompiled
# game.
# ---------------------------------------------------------------------------
define VN_TEXT_SPEED_SETTING = 3.0      # BalanceConfig ScenarioTextSpeed 0.5..5.0 (5 = instant); 3.0 default = ~30 chars/s
define VN_SCROLL_SPEED_SETTING = 3.0    # BalanceConfig ScenarioScrollSpeed; 3.0 default = 0.3 s page roll
define VN_FONT_SIZE = 29                # ScriptMessageLabel default font size (1024x576 space)
# Window geometry from BattleScriptScene.unity (ScriptMessageWindow &2243 and its children), 1024x576 space,
# top-left origin. SetScreen puts the whole MessageBase at defaultWindowDispCenter (0, -218): screen centre + 218 down.
define VN_WINDOW_CY = 288 + 218         # window origin y (defaultWindowDispCenter.y = -218, NGUI +y up)
define VN_DISP_W = 880                  # defaultWindowDispSize: text rect, centred on the window origin
define VN_DISP_H = 104                  # = 2 + ruby 12 + 29 + 4 + ruby 12 + 29 + 4 ... -> two visible lines
define VN_RUBY_H = 12                   # RubyPrefab label height; reserved above every line even without furigana
define VN_LINE_START_Y = VN_RUBY_H + 2  # ScriptMessageCommonManager.startPosition = (0, -(rubyLineHeight + 2))
define VN_GLYPH_ADJUST = -3             # Ren'Py's line box has more space above the caps than NGUI's label; shift up
define VN_BETWEEN_LINE = 4              # defaultBetweenLineHeight
define VN_LINE_PITCH = VN_FONT_SIZE + VN_RUBY_H + VN_BETWEEN_LINE   # 45
# Space added *below* each line.  Ren'Py clips each line to its own box while
# slow text is typing, and the font reports a line height shorter than its
# tallest glyphs, so without spacing a descender (g, p, q, y) only appears
# once the following line is drawn.  Keep this at 6 or more.
define VN_LINE_SPACING = 8
define VN_VISIBLE_LINES = 2
define VN_BOX_W = 1019                  # BackSprite (img_talk_textbg) 1019x144, centred 13 px above the window origin
define VN_BOX_H = 144
define VN_BOX_CY = VN_WINDOW_CY - 13
define VN_NAME_PLATE_LEFT = 3           # TalkNameBack left edge (pivot Left at x = -509)
define VN_NAME_PLATE_TOP = 288 + 218 - 89.5 - 24   # TalkName centre y - half of the 48 px plate
define VN_NAME_PLATE_H = 48
define VN_NAME_TEXT_X = 512 - 483.5     # TalkNameRoot: 25.5 px inside the plate's left edge
define VN_NAME_BASE_W = 80              # talkNameBackBaseWidth: plate width = max(220, text width) + 80
define VN_NAME_DEFAULT_W = 220          # talkNameBackDefaultWidth
define VN_NEXTMARK_X = 512 + 467.5      # NextMark/Icon centre (img_arrow_under 36x48)
define VN_NEXTMARK_Y = VN_WINDOW_CY + 42.5
define VN_NEXTMARK_BOUNCE = 18          # TweenPosition y 18 -> 0, 0.3 s ping-pong
# Choice dialog (ScriptSelectDialog / BattleScriptScene.unity): SelectListPanel y +70, ListItemSeed y +134,
# items 100 px apart going down (95 for five options, 77 for six; the list is raised 22 px for five or six).
# The ScrollView's UIScrollView has contentPivot = Center, so the list is centred on the scroll panel
# (SelectListPanel y +70, ScrollView y +1): 71 px above the screen centre, 217 px from the top.
define VN_SELECT_CENTER_Y = 288 - 71
define VN_SELECT_COLOR = "#ffffff"      # SCRIPT_ACTION_SELECT_COLOR (confirmed by the user: white)
define VN_TALK_DIM = "#808080"          # UIStandFigureRender.SetTalkMask: material rgb *= 0.5 for non-talkers
define VN_COMM_NOISE = True             # approximate the communicationCharaEffect transmission noise (real prefab not exported)
define VN_COMM_NOISE_PERIOD = 2.0       # CommunicationCharaEffectComponent.Update: noiseEffect2 re-fires every 2.0 s
define VN_COMM_NOISE_BURST = 0.28       # length of each approximated static burst
define VN_COMM_NOISE_JITTER = 4         # max horizontal jitter (px, 1024x576 space) during a burst
define VN_FS_EDGE_OFFSET = 0            # subRender FS/FSSide horizontal edge offset (0 on a 16:9 surface; GetFSCommandOffsetX)
define VN_SUBBLUR_RADIUS = 6.0          # approximated frosted-glass blur radius for subBlur (real params not in decompile)
define VN_KEY_DELAY = 0.2               # ScriptMessageCommonManager.defaultKeyDelayTime: taps ignored 0.2 s after text ends
define VN_DEFAULT_FADE_TIME = 0.5       # ScriptManager.DEFAULT_FADE_TIME

# The reader drives everything from Python loops, so Ren'Py rollback (mouse wheel up) would replay
# whole phases; FGO has no rollback either (it has the backlog). Autosave is off for the same reason
# (the reader keeps its own persistent marker).

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

# Message text style. line_leading is patched at runtime so the line pitch matches FGO exactly
# (see vn_ensure_metrics).
# UILabel prefab: shadow effect (effectStyle 1) offset (1,1) black, vertical gradient white -> 0.7 grey.
style vn_msg_text:
    font "fonts/FGO-Main-Font.otf"
    size int(VN_FONT_SIZE * upscale_ratio)
    color "#ffffff"
    outlines [(0, "#000000", 1, 1)]
    textshader "vn_gradient"
    slow_abortable True
    line_spacing int(VN_LINE_SPACING * upscale_ratio)

style vn_name_text:
    font "fonts/FGO-Main-Font.otf"
    size int(VN_FONT_SIZE * upscale_ratio)
    color "#ffffff"
    outlines [(0, "#000000", 1, 1)]
    textshader "vn_gradient"

# NextMark: img_arrow_under bouncing 18 px (TweenPosition 0.3 s ping-pong) and pulsing pink -> dark red
# (TweenColor 0.5 s ping-pong), from the NextMark/Icon objects in BattleScriptScene.unity.
transform vn_next_mark:
    anchor (0.5, 0.5)
    pos (int(VN_NEXTMARK_X * upscale_ratio), int(VN_NEXTMARK_Y * upscale_ratio))
    parallel:
        yoffset -int(VN_NEXTMARK_BOUNCE * upscale_ratio)
        linear 0.3 yoffset 0
        linear 0.3 yoffset -int(VN_NEXTMARK_BOUNCE * upscale_ratio)
        repeat
    parallel:
        matrixcolor TintMatrix("#ffcaca")
        linear 0.5 matrixcolor TintMatrix("#a50d0d")
        linear 0.5 matrixcolor TintMatrix("#ffcaca")
        repeat

# ScriptSelectListViewItemDraw: unselected options TweenAlpha to 0 over 0.5 s; the chosen option waits
# 0.1 + 0.5 s then fades over 0.6 s, while a copy of its text in the select colour scales to 2x over
# 0.3 s (EffectScale) and fades out over 0.2 s starting at 0.1 s.
transform vn_choice_fade_out(delay, t):
    alpha 1.0
    pause delay
    linear t alpha 0.0

transform vn_choice_burst:
    xanchor 0.5
    yanchor 0.5
    xpos 0.5
    ypos 0.5
    zoom 1.0
    alpha 1.0
    parallel:
        linear 0.3 zoom 2.0
    parallel:
        pause 0.1
        linear 0.2 alpha 0.0


# Layout, in the reader's 1280x720 space.
define sel_top_bar_h     = 66      # black bar holding the caption and Settings
define sel_top           = 74      # y both columns start at (bar + 8px of air)
define sel_bottom_bar_h  = 66      # the reader's existing black FETCH/Quit bar
define sel_left_w        = 600
define sel_gutter        = 12
define sel_banner_w      = 500
# 500x139 keeps the 450x125 Atlas war banners at their own aspect ratio; the old
# 600x120 slot stretched them 1.33x wide and squashed them 0.96x tall.
define sel_banner_h      = 139
define sel_banner_x      = 40
define sel_banner_gap    = 26
define sel_scrollbar_x   = 560
define sel_scrollbar_w   = 16
define sel_chapter_h     = 124
define sel_chapter_pad   = 44
define sel_chapter_gap   = 16
# Space between the right edge of a chapter bar and its scrollbar.
define sel_chapter_bar_gap = 16

# The bars use FGO's own "Myroom menu" plate art. It is a light plate, so the
# text on top of it is dark.
define sel_chapter_plate      = "gui/settings_textbox.png"
define sel_chapter_text       = "#12213a"
define sel_chapter_text_hover = "#0a4f8a"
# Nudges the label down inside the plate; the art is not vertically symmetric,
# so dead-centre sits slightly high.
define sel_chapter_text_y     = 6

# Vertical scrollbars reuse the FGO slider handle, stretched down its length.
define fgo_scrollbar_thumb_w     = 16
define fgo_slider_thumb          = "gui/slider/scrollbar_bar.png"
define fgo_scrollbar_bg          = "gui/scrollbar/scrollbar_bg.png"
define fgo_scrollbar_track_w     = 16
# How far the groove stops short of each end of the bar. With the handle at
# either extreme its tip then overhangs the groove, so no groove peeks out from
# behind it.
define fgo_scrollbar_track_inset = 10

# Bar label text. A style rather than inline properties, so the hover colour
# follows the parent button's focus the way textbutton does it.
# Loose text sitting directly on the terminal art. That art is very pale, and
# style.default is white, so anything without an explicit colour vanishes into it.
define sel_pane_text = "#12213a"

style sel_pane_label is default:
    font "fonts/FGO-Main-Font.otf"
    color sel_pane_text
    outlines [(2, "#ffffffcc", 0, 0)]

style sel_chapter_label is default:
    font "fonts/FGO-Main-Font.otf"
    size 26
    text_align 0.5
    color sel_chapter_text
    hover_color sel_chapter_text_hover
    line_spacing int(8 * upscale_ratio)
    outlines [(2, "#ffffffcc", 0, 0)]


init -1 python:
    def vn_px(value):
        return int(round(value * upscale_ratio))
