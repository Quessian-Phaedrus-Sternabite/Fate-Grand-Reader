# ---------------------------------------------------------------------------
#  [wipein] / [wipeout] / [wipeFilter] / [wipeOff] — ScriptManager wipe port
# ---------------------------------------------------------------------------
# FGO's wipe is MargePanel/WipeMesh in BattleScriptScene: an ExUIMeshRenderer quad, 1346x578 NGUI px,
# pivot centre, z -210, isRescale off, uv (0,0)-(1,1) with v up. Its material is WipeMaterial =
# Custom/WipeSheder (queue 3001, so it draws over FadeMesh/FlashMesh at 3000 despite their z). The real
# program is the APK's GLES3 blob (the export's shader text is a dummy), already registered as
# "fgo.fx_wipe" in fgo_fx_runtime.rpy:
#     d = _Volume - tex.r;  alpha = d > _Gradation ? 1 : (d > 0 ? d / _Gradation : 0);  rgb = _Color.rgb
# so the dark parts of a mask are covered first. _Color is black (EndLoadWipe SetTweenColor(0,0,0,1)).
# The mask is the Texture2D of the asset bundle Wipe/<NAME>, decoded into game/assets/wipe/ by
# tests/extract_wipe_masks.py (82 bundles; 512x512 grey, wormEaten 1024x1024).
#
# ScriptCommandExecute (disassembled from libil2cpp.so; wipein/wipeout share one branch):
#   [wipein|wipeout NAME T L]  NAME = ConvertWipeName (surrounding quotes dropped, "Wipe/" prefixed).
#       T is read only with >= 2 args, L only with >= 3; both default 0. Off normal play speed T = 0.
#       If a wipe (not a filter) is running, the line holds the script until it has ended, then runs again.
#   [wipeFilter NAME V L]  same tokens and defaults, no play-speed test.
#   [wipeOff (anything)]  InitWipe: tween stopped, volume 0, image cleared, mesh hidden.
#   [wait wipe]  holds while a wipe runs; a filter never holds it.
# EndLoadWipe, with g = max(L, 0) and top = g + 1.0039:
#   _Gradation = g only when L >= 0 (else the material keeps its last value; the material is shared).
#   filter: _Volume = top * V at once, no tween, and the mesh stays up until [wipeOff] or a new wipe.
#   wipeout: _Volume 0 -> top; wipein: top -> 0; a linear TweenRenderVolume over T (T <= 0: the end value
#   at once). At the end EndExecuteWipe hides the mesh if _Volume is 0, so a finished wipeout stays black.
# A name with no bundle (AssetManager lookups are exact, so wrong case counts) loads nothing: the mesh
# keeps whatever it was showing and the command is otherwise ignored.
#
# Not ported: wipeinEx / wipeoutEx / wipeFilterEx (the cutVer five-split ExWipeMesh; unused in the corpus).

init -40 python:
    import json

    VN_WIPE_DIR = "assets/wipe/"
    VN_WIPE_TOP_BIAS = 1.0039                        # EndLoadWipe: volume end = gradation + 1.0039
    VN_WIPE_MESH_SIZE = (1346.0, 578.0)              # serialized WipeMesh mWidth / mHeight (NGUI px)
    _vn_wipe_names = None

    def vn_wipe_mask_path(token):
        """ConvertWipeName + the bundle lookup: the mask path, or None when FGO has no such asset."""
        global _vn_wipe_names
        if _vn_wipe_names is None:
            # The extractor's list keeps FGO's exact names (the file system here ignores case).
            with renpy.open_file(VN_WIPE_DIR + "inventory.json", encoding="utf-8") as f:
                _vn_wipe_names = set(e["name"] for e in json.load(f))
        s = str(token)
        if len(s) >= 3 and s[0] == '"' and s[-1] == '"':
            s = s[1:-1]
        return VN_WIPE_DIR + s + ".png" if s in _vn_wipe_names else None

    def vn_wipe_running():
        """isExecuteWipe && !isWipeFilter: a wipein/wipeout tween that has not ended yet."""
        wipe = renpy.store.vn_wipe
        if not wipe or wipe.get("filter"):
            return False
        end = renpy.store.vn_busy.get("wipe")
        return end is not None and end > vn_now()

    def vn_wipe_start(name, is_in, amount, level, is_filter):
        """StartWipe / SetWipeFilter + EndLoadWipe. `amount` is T for a wipe and V for a filter."""
        wipe = renpy.store.vn_wipe
        path = vn_wipe_mask_path(name)
        if path is None:
            if wipe:
                wipe["filter"] = is_filter
            return
        if wipe:
            # [wipeOff]-free hand-over: the earlier tween's displayable stops drawing.
            wipe["disp"].wipe = None
        if level >= 0.0:
            renpy.store.vn_wipe_gradation = level
        top = max(level, 0.0) + VN_WIPE_TOP_BIAS
        if is_filter:
            vol = vn_anim(top * amount, top * amount, 0.0)
            renpy.store.vn_busy.pop("wipe", None)
        else:
            vol = vn_anim(top, 0.0, amount) if is_in else vn_anim(0.0, top, amount)
            vn_mark_busy("wipe", amount)
        wipe = {"mask": path, "vol": vol, "is_in": is_in, "filter": is_filter, "effect": str(name),
                "gradation": renpy.store.vn_wipe_gradation}
        # A new displayable per wipe: Ren'Py keeps a finished displayable's last render.
        wipe["disp"] = VNWipeMesh(wipe)
        renpy.store.vn_wipe = wipe

    def vn_wipe_reset():
        """InitWipe ([wipeOff], script start). The material's _Gradation is not reset."""
        renpy.store.vn_wipe = None
        renpy.store.vn_busy.pop("wipe", None)

    def vn_wipe_command(lname, args):
        if lname == "wipeoff":
            vn_wipe_reset()
            return True
        if not args:
            return True                                   # pd[0] missing: nothing happens
        amount = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
        level = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
        if vn_wipe_running():
            vn_wait_for("wipe")
        if lname == "wipefilter":
            vn_wipe_start(args[0], True, amount, level, True)
        else:
            if vn_skipping():
                amount = 0.0                              # !IsNormalPlaySpeed
            vn_wipe_start(args[0], lname == "wipein", amount, level, False)
        if renpy.store.vn_wipe:
            renpy.store.vn_wipe["raw_args"] = list(args)
        return True

    class VNWipeMesh(renpy.Displayable):
        """The WipeMesh quad, centred on the screen, drawn with the game's WipeSheder."""

        def __init__(self, wipe):
            super(VNWipeMesh, self).__init__()
            self.wipe = wipe

        def render(self, width, height, st, at):
            rv = renpy.Render(width, height)
            wipe = self.wipe
            if not wipe:
                return rv
            vol, running = vn_anim_value(wipe["vol"])
            if vol > 0.0:
                mw = VN_WIPE_MESH_SIZE[0] * upscale_ratio
                mh = VN_WIPE_MESH_SIZE[1] * upscale_ratio
                # The quad is 1682.5 x 722.5 here and Model sizes are whole pixels, so it is built at the
                # mask's own size and zoomed; the blit keeps the -1.25 px edge (steep strip masks show it).
                tw, th = renpy.image_size(wipe["mask"])
                model = (Model(size=(tw, th)).texture(wipe["mask"])
                         .shader("renpy.texture").shader("fgo.fx_wipe")
                         .uniform("u_fgo_wipe", (0.0, 0.0, 0.0, float(wipe["gradation"])))
                         .uniform("u_fgo_vol", float(vol)))
                quad = Transform(model, xzoom=mw / tw, yzoom=mh / th)
                rv.subpixel_blit(renpy.render(quad, width, height, st, at), ((width - mw) / 2.0, (height - mh) / 2.0))
            if running:
                renpy.redraw(self, 0)
            return rv
