# War selection support, persistence helpers, and Ren'Py flow labels.
init -10 python:
    import json
    import urllib.parse

    # Source geometry of gui/settings_textbox.png (FGO's "Myroom menu" plate).
    #
    # The file is 450x115, but the tag itself is only x 4..450, y 13..99:
    #   y  0..12   transparent padding
    #   y 13..98   the tag
    #   y 99..108  a baked-in black drop shadow (alpha fading 132 -> 4)
    #   y 109..114 transparent padding
    # The crop must stop at y 99. Including the shadow rows and stretching them
    # to button height turns a few pixels of shadow into a dark band along the
    # bottom of every bar.
    FGO_PLATE_SRC = "gui/settings_textbox.png"
    FGO_PLATE_X = 4             # left edge of the tag in the source
    FGO_PLATE_Y = 13            # top edge of the tag in the source
    FGO_PLATE_W = 446           # tag width  (x 4..450)
    FGO_PLATE_H = 86            # tag height (y 13..99), shadow excluded
    FGO_PLATE_LCAP = 58         # left cap, wide enough to keep the notch
    FGO_PLATE_RCAP = 20         # right cap, just the rounded gold edge
    FGO_PLATE_CLEAN_X = 34      # a vertical slice with no baked-in wording
    FGO_PLATE_CLEAN_W = 18

    _fgo_plate_cache = {}

    def vn_load_war_promos():
        """Load manually maintained per-war YouTube links."""
        try:
            with renpy.open_file("war_promos.json") as promo_file:
                data = json.load(promo_file)
        except Exception:
            return {}
        if isinstance(data, dict) and isinstance(data.get("promos"), dict):
            data = data["promos"]
        return data if isinstance(data, dict) else {}

    _vn_war_promos = vn_load_war_promos()

    def vn_get_war_promo(war_id):
        """Return a safe promo record for a war, or None when none is configured."""
        entry = _vn_war_promos.get(str(war_id))
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict):
            return None
        url = str(entry.get("url") or "").strip()
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
            return None
        return {
            "title": str(entry.get("title") or "Watch Event Promo"),
            "url": url,
        }

    def fgo_plate(width, height, src=None):
        """The FGO menu plate, rebuilt at an arbitrary size.

        The source art has the words "Myroom menu" baked into a band across its
        middle, so simply stretching it leaves that text showing behind our own
        label. Instead the plate is reassembled from its left cap, its right cap,
        and a clean text-free slice stretched to fill the gap.
        """
        src = src or FGO_PLATE_SRC
        width, height = int(width), int(height)
        key = (src, width, height)
        if key in _fgo_plate_cache:
            return _fgo_plate_cache[key]

        lcap = min(FGO_PLATE_LCAP, width // 2)
        rcap = min(FGO_PLATE_RCAP, width // 2)

        y, ch = FGO_PLATE_Y, FGO_PLATE_H
        right_x = FGO_PLATE_X + FGO_PLATE_W - FGO_PLATE_RCAP

        # The middle fills only the gap *between* the caps, never the whole
        # width. Stretching it edge to edge puts an opaque slice behind the left
        # cap, and the cap's notch -- which is meant to be see-through -- ends up
        # showing that off-white instead of the background.
        middle = Transform(
            im.Crop(src, (FGO_PLATE_CLEAN_X, y, FGO_PLATE_CLEAN_W, ch)),
            xysize=(max(0, width - lcap - rcap), height),
            xpos=lcap, ypos=0)
        left = Transform(
            im.Crop(src, (FGO_PLATE_X, y, FGO_PLATE_LCAP, ch)),
            xysize=(lcap, height), xpos=0, ypos=0)
        right = Transform(
            im.Crop(src, (right_x, y, FGO_PLATE_RCAP, ch)),
            xysize=(rcap, height), xpos=width - rcap, ypos=0)

        d = Fixed(middle, left, right, xysize=(width, height), fit_first=False)
        _fgo_plate_cache[key] = d
        return d

    def fgo_scrollbar_groove(height):
        """The groove behind a scrollbar, drawn as its own layer.

        A Ren'Py bar never draws a continuous track: it renders the track as two
        pieces, one either side of the handle, and blits the handle into the gap
        between them -- so nothing is drawn behind the handle at all. Drawing the
        groove separately underneath, with the bar's own track set transparent,
        gives one unbroken groove the handle sits on top of.
        """
        return Transform(Frame(fgo_scrollbar_bg, Borders(4, 3, 4, 3)),
                         xysize=(fgo_scrollbar_track_w, int(height)))

    def fgo_scrollbar_track():
        """The bar's own track: deliberately invisible.

        The visible groove is fgo_scrollbar_groove(), drawn behind the bar.
        """
        return Solid("#00000000")

    # Only these wars are reachable from the two select screens: the four group wars
    # and the main-story wars behind them. Fetching art for all 196 would pull ~12 MB
    # for banners that are never drawn; these 35 cost ~2.2 MB.
    def vn_war_is_displayed(war_id):
        war_id = war_id or 0
        return (war_id // 1000 >= 11 and war_id % 1000 == 0) or (1 <= war_id // 100 <= 4)

    # The Events section is a fifth entry after the four group wars. Its id is synthetic: it is
    # not an Atlas war, only the list's last "part" (see war_select / war_selectMainStory).
    VN_EVENTS_PART_ID = 15000
    VN_EVENTS_PART_TARGET = 5

    def vn_mark_event_rows(api, rows, export):
        """Flag rows of readable event wars: row["event"] = rank by (eventId, war id), row
        ["eventName"] and row["bannerUrls"]. Reruns (AtlasAPI.group_event_reruns) keep only the
        first run's row, but their banners follow it as fallbacks: many first runs have no banner
        on Atlas's CDN while their revival does."""
        by_id = {row["id"]: row for row in rows}
        rank = 0
        for runs in api.group_event_reruns(export):
            row = by_id.get(runs[0].get("id"))
            if row is None:
                continue
            rank += 1
            row["event"] = rank
            row["eventName"] = (runs[0].get("eventName") or "").strip()
            urls = []
            for war in runs:
                url = war.get("banner") or ""
                if url and url not in urls:
                    urls.append(url)
            row["bannerUrls"] = urls
        return rank

    def vn_event_rows():
        return sorted((w for w in war_list if w.get("event")), key=lambda w: w["event"])

    def vn_resolve_banners(rows):
        """Fill each row's cached banner path from its url.

        Cache-only and cheap, so it runs on every load rather than being baked into the
        saved list: a stored path goes stale the moment the asset cache is cleared, and
        a path stored as "" would never recover once the art did arrive.
        """
        cache = get_api().cache
        for row in rows:
            row["banner"] = ""
            # Event rows list their reruns' banners after their own; the first one cached wins.
            for url in row.get("bannerUrls") or [row.get("bannerUrl") or ""]:
                path = cache.get_asset_path(url) if url else None
                if path:
                    row["banner"] = path.replace(chr(92), "/")
                    break
        return rows

    def vn_download_banners(rows):
        """Fetch the banners the select screens will actually show.

        cache.get_asset_path never downloads, so on a first run nothing is cached and
        every war would fall back to a name plate. Offline this is a no-op: get_banner_path
        returns None and the rows keep their plate fallback.
        """
        api = get_api()
        fetched = 0
        for row in rows:
            if not (vn_war_is_displayed(row.get("id")) or row.get("event")):
                continue
            # In order, stopping at the first that exists: a rerun's banner is only fetched
            # when the first run's 404s.
            for url in row.get("bannerUrls") or [row.get("bannerUrl") or ""]:
                if not url:
                    continue
                if api.cache.get_asset_path(url):
                    break
                if api.get_banner_path(url):
                    fetched += 1
                    break
        return fetched

    def vn_forget_war_list():
        """Throw away the saved war list so the next entry rebuilds it from the network."""
        persistent.wars_fetched_once = False
        persistent.war_list_cache = None
        persistent.war_list_cache_region = None
        persistent.war_list_cache_version = None
        renpy.save_persistent()

    def vn_clear_chapter_select():
        """Blank the chapter column, so a stale war's chapters never linger."""
        store.chapter_war_id = None
        store.chapter_war_name = ""
        store.chapter_rows = []
        store.chapter_error = ""

    def vn_select_chapter_war(war_id, war_name):
        """Fill the chapter column for a war.

        Called from the war button's action rather than from the screen body: the
        first look at a war can hit the network for its detail JSON, and screen
        bodies are re-evaluated on every interaction. AtlasAPI memoises the
        result, so coming back to a war later is free.
        """
        vn_clear_chapter_select()
        store.chapter_war_id = war_id
        store.chapter_war_name = war_name

        api = get_api()
        rows = api.get_war_chapters(war_id)
        if rows is None:
            store.chapter_error = api.last_error or "Could not load this war's chapters."
        else:
            store.chapter_rows = rows

    def vn_war_banner(war):
        """The cached banner path for a war row, or None to fall back to a plate."""
        banner = war.get("banner")
        if banner and renpy.loadable(banner):
            return banner
        return None

    def vn_war_title(war):
        if war.get("id") == VN_EVENTS_PART_ID:
            return "Events"
        if war.get("event"):
            return war.get("eventName") or war.get("longName") or war.get("name") or "Unnamed Event"
        return war.get("longName") or war.get("name") or "Unnamed War"

    def save_current_marker():
        if renpy.store.current_reader_marker:
            persistent.reader_marker = dict(renpy.store.current_reader_marker)
            renpy.save_persistent()
            renpy.store.menu_notice = "Saved reading marker."

    def vn_menu_war_name():
        """The title the war-select screens gave the war being read."""
        war_id = renpy.store.selected_war_id
        for row in renpy.store.war_list or ():
            if row.get("id") == war_id:
                return vn_war_title(row)
        war = (renpy.store.active_war or {}).get("war") or {}
        return war.get("longName") or war.get("name") or ""

    def vn_menu_position():
        """(quest_idx, phase_idx) of the phase being read; (0, 0) before the first one."""
        marker = renpy.store.current_reader_marker or {}
        return marker.get("quest_idx", 0), marker.get("phase_idx", 0)

    def vn_menu_row_layout(count, current):
        """Button width for a row of `count` numbered buttons, squeezed horizontally
        to fit the row; past menu_row_btn_min the row scrolls instead. Returns
        (width, scrolls, xinitial), xinitial keeping `current` in view."""
        gap = menu_row_btn_gap
        fit = (menu_row_w - gap * max(0, count - 1)) // max(1, count)
        width = max(menu_row_btn_min, min(menu_row_btn_w, fit))
        total = count * width + gap * max(0, count - 1)
        if total <= menu_row_w:
            return width, False, 0.0
        centre = current * (width + gap) + width / 2.0
        span = float(total - menu_row_w)
        return width, True, max(0.0, min(1.0, (centre - menu_row_w / 2.0) / span))

    def vn_menu_wrap_layout(count, current):
        """Chapter Select layout: one row while buttons stay at least menu_wrap_btn_w
        wide, otherwise wrapped rows. Returns (width, per_row, rows, visible_rows,
        yinitial), yinitial keeping `current`'s row in view when the block scrolls."""
        gap = menu_row_btn_gap
        count = max(1, count)
        fit = (menu_row_w - gap * (count - 1)) // count
        if fit >= menu_wrap_btn_w:
            return min(menu_row_btn_w, fit), count, 1, 1, 0.0
        per = max(1, (menu_row_w + gap) // (menu_wrap_btn_w + gap))
        width = (menu_row_w - gap * (per - 1)) // per
        rows = (count + per - 1) // per
        visible = min(rows, menu_wrap_rows)
        if rows <= visible:
            return width, per, rows, visible, 0.0
        # Whole rows only: the scroll range is exactly (rows - visible) row pitches.
        top = max(0, min(rows - visible, current // per - (visible - 1) // 2))
        return width, per, rows, visible, top / float(rows - visible)

    def vn_menu_groove(st, at, adj, height):
        """The MENU backlog's scrollbar groove, only while the backlog can scroll."""
        return (fgo_scrollbar_groove(height) if adj.range > 0 else Null()), None

    def vn_menu_info_size(name):
        """Long war/chapter names step down so the info column stays above the rows."""
        n = len(name or "")
        return 21 if n <= 22 else (18 if n <= 44 else 16)


# Bumped when the saved war-list schema changes, so an older cache is rebuilt rather
# than half-read. v2 stores bannerUrl and resolves the cached path on every load.
# v4 groups event reruns and gives event rows bannerUrls (first run's, then reruns').
define VN_WAR_LIST_CACHE_VERSION = 4

label start:
    $ menu_notice = ""
    call screen title_menu
    jump start

label reader_jump_section:
    # The MENU's Section buttons: restart playback at resume_marker within the war being
    # read. play_war resets the stage, effects and backlog itself; the message, voices and
    # music belong to the section being left, so clear them first.
    $ vn_message_off()
    $ renpy.music.stop(fadeout=0.5)
    $ vn_voice_stop()
    $ api = get_api()
    jump play_war

label war_list_flow:
    # Reached from the in-reader "War List" button too: the reader screens are shown with `show screen`
    # and survive the jump, so hide them (and clear the message) before the war-select screen appears.
    hide screen reader_nav
    hide screen vn_message
    hide screen vn_mask_layer
    hide screen vn_forward_effects
    $ vn_fx_reset()
    hide screen vn_pictureframe_top_layer
    hide screen vn_pictureframe_layer
    hide screen vn_fade_layer
    hide screen vn_flash_layer
    hide screen vn_wipe_layer
    hide screen vn_stage
    $ vn_message_off()
    scene black
    with fade
    $ resume_marker = None
    $ vn_clear_chapter_select()
    # This label always ends in the user picking a war, so never inherit the previous
    # one. A stale id here is what used to send "FETCH NEW WARS" back into the last war
    # that had been read instead of returning to the list.
    $ selected_war_id = 0

label war_list_refresh:
    if not war_list:
        python:
            # A previous launch already built this list. It is ~30 KB, where rebuilding it
            # means re-parsing the 41 MB export just to keep six fields per war -- so reuse
            # it and skip both loading screens entirely. Keyed on the API region rather
            # than the store's `lang`, which the settings screen does not always update.
            _saved = getattr(persistent, "war_list_cache", None)
            if (_saved
                    and getattr(persistent, "war_list_cache_region", None) == get_api().region
                    and getattr(persistent, "war_list_cache_version", None) == VN_WAR_LIST_CACHE_VERSION):
                # Copy the rows before touching them; the originals belong to persistent.
                war_list = vn_resolve_banners([dict(row) for row in _saved])
                war_load_error = None

    if not war_list:
        show screen loading_screen("Loading war list...")
        $ renpy.pause(0.1, hard=True)
        python:
            api = get_api()
            war_list_data = None
            if not persistent.wars_fetched_once:
                # First-ever boot: fetch from network
                war_list_data = api.get_war_list(force_refresh=True)
                if war_list_data is not None:
                    persistent.wars_fetched_once = True
                    renpy.save_persistent()
            else:
                # Subsequent boots: load from cache only (no network fetch)
                war_list_data = api.get_war_list()
        hide screen loading_screen

        if war_list_data is None:
            # Surface it on war_select, which renders the message with a Retry button,
            # rather than bouncing the user back out to the title menu.
            $ war_load_error = api.last_error or "Unable to fetch the war list. Check your internet connection."

        if war_list_data:
            show screen loading_screen("Downloading war banners...")
            $ renpy.pause(0.1, hard=True)
            python:
                war_list = sorted(war_list_data, key=lambda item: item.get("id", 0))
                war_list = [
                    {
                        "id": item.get("id", 0),
                        "name": item.get("name", ""),
                        "longName": item.get("longName", ""),
                        "scriptId": item.get("scriptId", ""),
                        "script": item.get("script", ""),
                        # The url, not a resolved path: vn_resolve_banners turns it into a
                        # cached path on every load, so a cleared asset cache self-heals.
                        "bannerUrl": item.get("banner") or "",
                    }
                    for item in war_list
                ]
                # A first run has no banner art cached, and nothing else in the reader ever
                # downloads it -- without this every war renders as a bare name plate.
                # Event stories: rows are flagged with their chronological rank (1-based) so the
                # Events section can list them; revivals that replay an earlier event's scripts
                # are dropped, and wars with nothing readable never get a row in the section.
                vn_mark_event_rows(api, war_list, war_list_data)
                vn_download_banners(war_list)
                vn_resolve_banners(war_list)
                # Keep it for the next launch so this whole block is skipped.
                persistent.war_list_cache = war_list
                persistent.war_list_cache_region = api.region
                persistent.war_list_cache_version = VN_WAR_LIST_CACHE_VERSION
                renpy.save_persistent()
            hide screen loading_screen
            $ war_load_error = None

    call screen war_select

    if _return == "refetch":
        jump war_list_refresh

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

label play_war:
    scene black
    with fade
    $ vn_reset_stage()
    $ backlog = []
    show screen vn_stage
    show screen vn_flash_layer
    show screen vn_fade_layer
    show screen vn_pictureframe_layer
    show screen vn_wipe_layer
    show screen vn_message
    show screen vn_forward_effects
    show screen vn_pictureframe_top_layer
    show screen vn_mask_layer
    show screen reader_nav

    if active_war.get("story_quests"):
        # Modern wars are split into quest phases, so load each phase script as the reader reaches it.
        $ quest_idx = resume_marker.get("quest_idx", 0) if resume_marker else 0
        $ resume_phase_idx = resume_marker.get("phase_idx", 0) if resume_marker else 0
        $ _skip_first_card = True
        while quest_idx < len(active_war["story_quests"]):
            $ quest = active_war["story_quests"][quest_idx]
            if not _skip_first_card:
                call screen quest_title_card(quest)
            $ _skip_first_card = False

            $ phase_idx = resume_phase_idx if resume_marker and quest_idx == resume_marker.get("quest_idx", 0) else 0
            while phase_idx < len(quest["phase_scripts"]):
                $ phase_script = quest["phase_scripts"][phase_idx]
                $ current_reader_marker = {"war_id": selected_war_id, "quest_idx": quest_idx, "phase_idx": phase_idx, "script_id": phase_script.get("scriptId"), "script": phase_script.get("script")}
                # The MENU's backlog covers only the section being read.
                $ backlog = []
                show screen loading_screen("Loading phase [phase_script.get('phase')]...")
                $ renpy.pause(0.1, hard=True)
                python:
                    phase_nodes = api.load_script_nodes(phase_script.get("script"))
                hide screen loading_screen

                if phase_nodes is None:
                    $ war_load_error = api.last_error or "Failed to load a phase script."
                    call screen message_screen(war_load_error)
                else:
                    $ vn_begin_phase(phase_nodes)
                    $ vn_play_nodes(phase_nodes, api)
                    $ vn_message_off()

                $ phase_idx += 1
                $ resume_marker = None

            $ quest_idx += 1

        n "End of story."
        hide screen reader_nav
        hide screen vn_mask_layer
        hide screen vn_forward_effects
        $ vn_fx_reset()
        hide screen vn_pictureframe_top_layer
        hide screen vn_message
        hide screen vn_pictureframe_layer
        hide screen vn_fade_layer
        hide screen vn_flash_layer
        hide screen vn_wipe_layer
        hide screen vn_stage
        jump start

    if not active_war["script_nodes"]:
        n "No parsed script content was found for this war."
        if active_war.get("script_url"):
            n "Atlas listed [active_war['script_url']], but it did not contain readable dialogue."
        n "Returning to the war list."
        hide screen reader_nav
        hide screen vn_mask_layer
        hide screen vn_forward_effects
        $ vn_fx_reset()
        hide screen vn_pictureframe_top_layer
        hide screen vn_message
        hide screen vn_pictureframe_layer
        hide screen vn_fade_layer
        hide screen vn_flash_layer
        hide screen vn_wipe_layer
        hide screen vn_stage
        jump start

    $ vn_begin_phase(active_war["script_nodes"])
    $ vn_play_nodes(active_war["script_nodes"], api)
    $ vn_message_off()

    n "End of script."
    hide screen reader_nav
    hide screen vn_mask_layer
    hide screen vn_forward_effects
    $ vn_fx_reset()
    hide screen vn_pictureframe_top_layer
    hide screen vn_message
    hide screen vn_pictureframe_layer
    hide screen vn_fade_layer
    hide screen vn_flash_layer
    hide screen vn_wipe_layer
    hide screen vn_stage
