# ---------------------------------------------------------------------------
#  [flashin] / [flashout] / [flashOff] — ScriptManager flash port
# ---------------------------------------------------------------------------
# FGO's flash is MargePanel/FlashMesh in BattleScriptScene: a full-screen white sprite (Sprites-Default,
# plain alpha blending) whose colour is tweened. It is a sibling of the camera zoom/move/roll nodes and not
# under Shake, so camera moves and [shake] never move it. MargePanel z order (lower draws on top): RenderMesh
# 0 (the stage RT) < CaptureMesh/PictureFrame -10 < forward effects -100 < FlashMesh -200 < wipes -210 <
# FadeMesh -220; the message window (CommonUICamera) and the [maskin] mask (SystemUICamera) are above all of
# it. Here: screen vn_flash_layer, zorder 47 (over vn_stage and vn_pictureframe_layer 45, under
# vn_fade_layer 50, vn_wipe_layer 60, the message box and the mask).
#
# ScriptCommandExecute (disassembled from libil2cpp.so):
#   [flashin MODE D1 D2 C1 C2]  exactly 5 tokens, else "parameter error" (nothing happens). If a flash is
#       running it first calls EndFlash(0) and holds the script (wait type "flash") until that flash has
#       ended, then runs the line again. Off normal speed a "once" flash is StartFlash(0, 0): dropped.
#       Only "once" and "pingpong" are special; every other word (loop, blink, ...) loops.
#   [flashout (T)]  EndFlash(T): only if a flash is running; T is 0 off normal speed. Does not wait.
#   [flashOff (anything)]  InitFlashCommand: colour (1,1,1,0) at once, flash stopped. Args are ignored.
#   [wait flash]  holds while isExecuteFlash.
# ConvertColor: 8 hex digits RRGGBBAA; 6 digits RRGGBB with alpha 1; any other length is opaque black.
#
# OnFlashRequest runs at every leg boundary; each leg is a linear TweenRendererColor from the *current*
# colour (straight RGBA lerp, all four channels), and a zero duration sets the colour and re-enters after a
# 0.001 s Invoke:
#   end requested -> T3 > 0: tween to (0,0,0,0) over T3, then end; else end now.
#   leg A: -> C1 over D1 (once with D1 = D2 = 0: end at once).
#   leg B: pingpong: set C2, -> C1 over D2, repeat B forever.
#          otherwise -> C2 over D2; once ends after it (snap to (0,0,0,0), end requests ignored),
#          anything else goes back to leg A.
# So a loop's [flashout T] takes effect at the next leg boundary, and [flashout] during a once flash's leg B
# does nothing. EndExecuteFlash leaves the colour (0,0,0,0), so the next flash lerps from transparent black;
# the first flash of a script, or the first after [flashOff], lerps from transparent white.
#
# Not ported: flashinDep / flashOffDep / flashoutDep (the per-character flash mesh; unused in the corpus),
# and InitFlashCommand from a [fadein clear]/[maskout clear] (2 uses).

init -40 python:

    VN_FLASH_IDLE_COLOR = (1.0, 1.0, 1.0, 0.0)       # InitFlashCommand
    VN_FLASH_CLEAR = (0.0, 0.0, 0.0, 0.0)            # EndExecuteFlash
    VN_FLASH_INVOKE = 0.001                          # Invoke("OnFlashRequest", 0.001)

    def vn_flash_convert_color(s):
        """ScriptManager.ConvertColor. Raises ValueError for non-hex text (Convert.ToInt32 throws)."""
        s = str(s).strip().strip('"')
        v = int(s, 16)
        if len(s) == 8:
            return ((v >> 24 & 255) / 255.0, (v >> 16 & 255) / 255.0, (v >> 8 & 255) / 255.0, (v & 255) / 255.0)
        if len(s) == 6:
            return ((v >> 16 & 255) / 255.0, (v >> 8 & 255) / 255.0, (v & 255) / 255.0, 1.0)
        return (0.0, 0.0, 0.0, 1.0)

    def _vn_flash_lerp(a, b, f):
        return tuple(a[i] + (b[i] - a[i]) * f for i in range(4))

    def _vn_flash_legs(fl):
        """Yields (start, dur, from, to, instant) for each leg, in order, and finally (end_time, None, ...)."""
        t = fl["t0"]
        col = fl["c0"]
        name, d1, d2, c1, c2 = fl["name"], fl["d1"], fl["d2"], fl["c1"], fl["c2"]
        end_t, t3 = fl.get("end_t"), fl.get("t3", 0.0)

        def ending(tb, col):
            # OnFlashRequest with isEndRequestFlash set.
            if t3 > 0.0:
                return [(tb, t3, col, VN_FLASH_CLEAR, False), (tb + t3, None, None, None, None)]
            return [(tb, None, None, None, None)]

        def leg(to, dur):
            if dur > 0.0:
                return (dur, to, False)
            return (VN_FLASH_INVOKE, to, True)

        # Leg A.
        if d1 <= 0.0 and name == "once" and d2 <= 0.0:
            yield (t, None, None, None, None)
            return
        dur, to, inst = leg(c1, d1)
        yield (t, dur, col, to, inst)
        t += dur
        col = to
        cycle_start = None
        cycle_col = None
        while True:
            if end_t is not None and end_t <= t:
                for e in ending(t, col):
                    yield e
                return
            if name == "pingpong":
                col = c2                                     # SetColor(C2) before the tween
                dur, to, inst = leg(c1, d2)
                yield (t, dur, col, to, inst)
                t += dur
                col = to
            else:
                dur, to, inst = leg(c2, d2)
                yield (t, dur, col, to, inst)
                t += dur
                col = to
                if name == "once":
                    yield (t, None, None, None, None)      # EndExecuteFlash: end requests are ignored
                    return
                if end_t is not None and end_t <= t:
                    for e in ending(t, col):
                        yield e
                    return
                dur, to, inst = leg(c1, d1)
                yield (t, dur, col, to, inst)
                t += dur
                col = to
            # Every cycle after the first ends on the same colour, so whole cycles can be jumped.
            if cycle_start is None:
                cycle_start, cycle_col = t, col
            elif cycle_col == col:
                period = t - cycle_start
                limit = fl.get("_now", t)
                if end_t is not None:
                    limit = min(limit, end_t)
                if period > 0.0 and limit - t > 2.0 * period:
                    t += math.floor((limit - t) / period - 1.0) * period
                cycle_start = t

    def vn_flash_eval(fl, now=None):
        """Pure: (colour, busy, end_time) of the flash at `now`. end_time is None while it can still run
        forever (a loop without a flashout)."""
        if not fl:
            return VN_FLASH_IDLE_COLOR, False, None
        if now is None:
            now = vn_now()
        prev = fl.get("prev")
        if prev is not None and now < fl["t0"]:
            # An inline [flashin] issued over a running flash starts once that one has ended.
            col, _, _ = vn_flash_eval(prev, now)
            return col, True, None
        fl = dict(fl)
        fl["_now"] = now
        for start, dur, a, b, inst in _vn_flash_legs(fl):
            if dur is None:
                if now >= start:
                    return VN_FLASH_CLEAR, False, start
                break
            if now < start + dur:
                if inst:
                    return b, True, None
                return _vn_flash_lerp(a, b, max(0.0, (now - start) / dur)), True, None
        return VN_FLASH_CLEAR, False, None

    def vn_flash_end_time(fl, now=None):
        """When the flash will have ended (EndExecuteFlash), or None for a loop that runs forever."""
        if not fl:
            return None
        if now is None:
            now = vn_now()
        if fl.get("prev") is not None and now < fl["t0"]:
            now = fl["t0"]
        if fl.get("end_t") is None and fl["name"] != "once":
            return None                                      # loop / pingpong: runs until a flashout
        fl = dict(fl)
        fl["_now"] = max(now, fl.get("end_t") or now)
        for start, dur, a, b, inst in _vn_flash_legs(fl):
            if dur is None:
                return start
        return None

    def vn_flash_busy(now=None):
        return vn_flash_eval(renpy.store.vn_flash, now)[1]

    def vn_flash_reset():
        """InitFlashCommand: colour (1,1,1,0), flash stopped (script start, [flashOff], skipping)."""
        renpy.store.vn_flash = None

    def vn_flash_request_end(duration, now=None):
        """EndFlash(duration): only while a flash runs; takes effect at its next leg boundary."""
        fl = renpy.store.vn_flash
        if now is None:
            now = vn_now()
        if not fl or not vn_flash_eval(fl, now)[1]:
            return
        fl = dict(fl)
        if fl.get("prev") is not None and now < fl["t0"]:
            now = fl["t0"]
        fl["end_t"] = now
        fl["t3"] = max(0.0, float(duration))
        renpy.store.vn_flash = fl

    def vn_flash_wait():
        """[wait flash] / a [flashin] over a running flash: hold until isExecuteFlash is false. Skipping
        runs InitFlashCommand (ScriptManager's WAIT_SKIP state) and releases the wait."""
        while True:
            fl = renpy.store.vn_flash
            if not fl or not vn_flash_eval(fl)[1]:
                return
            if vn_skipping():
                vn_flash_reset()
                return
            end = vn_flash_end_time(fl)
            remaining = (end - vn_now()) if end is not None else 0.1
            renpy.store.vn_wait_until = vn_now() + max(0.0, remaining)
            renpy.pause(max(0.01, min(remaining, 0.1)) if end is None else max(0.01, remaining), hard=True)

    def vn_flash_start(name, d1, d2, c1, c2, now=None):
        if now is None:
            now = vn_now()
        old = renpy.store.vn_flash
        col, busy, _ = vn_flash_eval(old, now)
        fl = {"name": name, "d1": float(d1), "d2": float(d2), "c1": tuple(c1), "c2": tuple(c2),
              "c0": col, "t0": now, "end_t": None, "t3": 0.0}
        if busy:
            # Inline only (a whole-line flashin waits first): the new flash starts where the old one ends,
            # from (0,0,0,0).
            end = vn_flash_end_time(old, now)
            if end is not None and end > now:
                fl["t0"] = end
                fl["c0"] = VN_FLASH_CLEAR
                fl["prev"] = old
        renpy.store.vn_flash = fl

    def vn_flash_command(token, blocking=True):
        """One [flashin ...] / [flashout ...] / [flashOff ...] token (string or split list). `blocking` is
        False for commands fired inline by the typewriter, which cannot hold the script."""
        parts = token.split() if isinstance(token, str) else list(token)
        if not parts:
            return False
        lname = parts[0].lower()
        args = parts[1:]
        if lname == "flashoff":
            vn_flash_reset()
            return True
        if lname == "flashout":
            if len(args) > 1:
                return True                                  # "parameter error"
            dur = 0.0
            if args and not vn_skipping():
                try:
                    dur = float(args[0])
                except Exception:
                    return True
            vn_flash_request_end(dur)
            return True
        if lname != "flashin":
            return False
        if vn_flash_busy():
            vn_flash_request_end(0.0)
            if blocking:
                vn_flash_wait()
        if len(args) != 5:
            return True                                      # "parameter error"
        mode = args[0].strip('"')
        try:
            c1 = vn_flash_convert_color(args[3])
            c2 = vn_flash_convert_color(args[4])
            d1 = float(args[1])
            d2 = float(args[2])
        except Exception:
            return True
        if vn_skipping() and mode == "once":
            d1 = d2 = 0.0                                    # IsFastPlaySpeed: StartFlash(mode, 0, 0, ...)
        vn_flash_start(mode, d1, d2, c1, c2)
        return True

    def vn_flash_transform():
        def fn(trans, st, at):
            col, busy, _ = vn_flash_eval(renpy.store.vn_flash)
            r, g, b, a = col
            trans.alpha = max(0.0, min(1.0, a))
            trans.matrixcolor = TintMatrix(Color(rgb=(max(0.0, min(1.0, r)), max(0.0, min(1.0, g)),
                                                      max(0.0, min(1.0, b)))))
            return 0 if busy else None
        return Transform(function=fn)

screen vn_flash_layer():
    zorder 47
    if vn_flash:
        add Solid("#ffffff") xysize (config.screen_width, config.screen_height) at vn_flash_transform()
