# ---------------------------------------------------------------------------
#  SE channels
# ---------------------------------------------------------------------------
# FGO plays several sound effects at once and stops them by name, so each
# concurrent effect needs its own channel.
init -100 python:
    for _i in range(6):
        renpy.music.register_channel("se%d" % _i, mixer="sfx", loop=False)
    # [voice]: SeManager plays each voice on its own pooled SePlayer, so different cues overlap.
    VN_VOICE_CHANNELS = 4
    for _i in range(VN_VOICE_CHANNELS):
        renpy.music.register_channel("voice%d" % _i, mixer="voice", loop=False)

# Stage animation, character state, command dispatch, and effects.
init -40 python:
    # ------------------------------------------------------------------
    # Geometry helpers (all in the original 1024x576 NGUI space unless noted)
    # ------------------------------------------------------------------

    # ScriptPosition.positionList: index -> (x, y) with origin at screen centre, +y up.
    # Out-of-range indices clamp to entry 0 (FGO GetPosition), not to centre.
    CHARA_POSITION_X_COORD = {
        0: -256,  # Left
        1:    0,  # Center
        2:  256,  # Right
        3: -438,  # Far left
        4: -512,  # Furthest left
        5:  438,  # Far right
        6:  512,  # Furthest right
    }

    def vn_parse_position(value, default=(0.0, 0.0)):
        """Position argument of charaSet/charaFadein/charaPut/charaMove: a slot index 0..6 or 'x,y'."""
        if value is None:
            return default
        text = str(value).strip()
        if "," in text:
            try:
                x, y = text.split(",", 1)
                return (float(x), float(y))
            except Exception:
                return default
        try:
            idx = int(float(text))
        except Exception:
            return default
        if idx not in CHARA_POSITION_X_COORD:
            idx = 0
        return (float(CHARA_POSITION_X_COORD[idx]), 0.0)

    def vn_float(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return default

    def vn_now():
        return time.time()

    def vn_skipping():
        """True while the custom skip mode is active (Tab toggle)."""
        return bool(renpy.store.vn_skip_mode and renpy.store.vn_skip_allowed)

    def vn_anim(from_value, to_value, duration, ease=None):
        # FGO passes its skip state into stage actions so tweens reach their final state immediately.
        # Letting wall-clock tweens continue while the script raced ahead was what left old figures,
        # backgrounds and camera transforms visible after a skipped transition.
        dur = 0.0 if vn_skipping() else max(0.0, vn_float(duration, 0.0))
        return {"from": from_value, "to": to_value, "start": vn_now(), "dur": dur, "ease": ease}

    def vn_ease(t, name):
        # iTween easing names found in the script corpus. Existing non-sub tweens stay linear.
        name = str(name or "linear").lower()
        if name == "linear":
            return t
        mode = "inout" if "inout" in name else ("out" if "out" in name else "in")
        prefix = "ease" + mode
        curve = name[len(prefix):] if name.startswith(prefix) else name
        def inward(x):
            powers = {"quad": 2, "cubic": 3, "quart": 4, "quint": 5}
            if curve in powers:
                return x ** powers[curve]
            if curve == "sine":
                return 1.0 - math.cos(x * math.pi / 2.0)
            if curve == "expo":
                return 0.0 if x == 0.0 else 2.0 ** (10.0 * (x - 1.0))
            if curve == "circ":
                return 1.0 - math.sqrt(max(0.0, 1.0 - x * x))
            if curve == "back":
                overshoot = 1.70158 * (1.525 if mode == "inout" else 1.0)
                return x * x * ((overshoot + 1.0) * x - overshoot)
            return x
        if mode == "out":
            return 1.0 - inward(1.0 - t)
        if mode == "inout":
            return inward(t * 2.0) / 2.0 if t < .5 else 1.0 - inward(2.0 - t * 2.0) / 2.0
        return inward(t)

    def vn_lerp(a, b, f):
        if isinstance(a, (tuple, list)):
            return tuple(a[i] + (b[i] - a[i]) * f for i in range(len(a)))
        return a + (b - a) * f

    def vn_anim_keys(keys):
        """Piecewise-linear keyframes: [(wall_time, value), ...] in time order."""
        return {"keys": [(float(t), v) for t, v in keys]}

    def vn_anim_value(anim, now=None):
        """Linear NGUI tween evaluation. Returns (value, still_running)."""
        if anim is None:
            return None, False
        if now is None:
            now = vn_now()
        keys = anim.get("keys")
        if keys:
            if now <= keys[0][0]:
                return keys[0][1], True
            for i in range(1, len(keys)):
                t0, v0 = keys[i - 1]
                t1, v1 = keys[i]
                if now < t1:
                    f = (now - t0) / (t1 - t0) if t1 > t0 else 1.0
                    return vn_lerp(v0, v1, f), True
            return keys[-1][1], False
        dur = anim.get("dur", 0.0)
        a, b = anim["from"], anim["to"]
        if dur <= 0.0:
            return b, False
        f = (now - anim["start"]) / dur
        if f >= 1.0:
            return b, False
        if f <= 0.0:
            return a, True
        f = vn_ease(f, anim.get("ease"))
        if isinstance(a, (tuple, list)):
            return tuple(a[i] + (b[i] - a[i]) * f for i in range(len(a))), True
        return a + (b - a) * f, True

    def vn_anim_current(anim, default):
        if anim is None:
            return default
        value, _ = vn_anim_value(anim)
        return value

    def vn_mark_busy(tag, duration):
        duration = 0.0 if vn_skipping() else max(0.0, vn_float(duration, 0.0))
        renpy.store.vn_busy[tag] = vn_now() + duration

    def vn_wait_for(tag, group=None):
        """[wait TAG]: block (hard, like FGO) until the tagged command finished."""
        end = vn_sub_wait_end(tag, group) if tag.lower().startswith("sub") else renpy.store.vn_busy.get(tag)
        if end is None:
            return
        if vn_skipping():
            return
        remaining = end - vn_now()
        if remaining > 0.005:
            renpy.store.vn_wait_until = end
            renpy.pause(remaining, hard=True)

    def vn_wait_time(seconds):
        """[wt N]: an un-skippable timed wait (ProcessScript 'wait time' has no tap test)."""
        seconds = vn_float(seconds, 0.0)
        if seconds > 0.0:
            if vn_skipping():
                return
            renpy.store.vn_wait_until = vn_now() + seconds
            renpy.pause(seconds, hard=True)

    # ------------------------------------------------------------------
    # Character slots
    # ------------------------------------------------------------------

    def vn_slot_default():
        return {
            "id": None, "name": "", "path": None, "face": "0", "kind": "chara",
            "pos": (0.0, 0.0), "pos_anim": None,
            "scale": 1.0, "scale_anim": None,
            "alpha": 0.0, "alpha_anim": None,
            "depth": 0, "talk_mask": False, "talk_depth": False,
            "filter": None, "communication_chara": False,
            "shake": None,
            # UIScriptChara Roll1/Roll2 (charaRoll family): Roll1 orientation quaternion (w,x,y,z),
            # the pivot centerOffset (Roll1 at -c, Roll2 at +c), a running tween, and the
            # StartRollAxis loop fields that survive between commands (isRollWait is never reset).
            "roll_q": (1.0, 0.0, 0.0, 0.0), "roll_c": (0.0, 0.0), "roll_anim": None,
            "roll_loop": None, "roll_wait": False, "roll_stop_q": (1.0, 0.0, 0.0, 0.0),
            # RelativePos node (charaRelativeLoopMove family): absolute local offset + segment loop.
            "rel": (0.0, 0.0), "rel_move": None,
            "face_x": 0, "face_y": 0, "face_size": 256, "offset_x": 0, "offset_y": 0,
            "sub_group": None,          # charaLayer: None = main camera, else "#A".. sub-render group
            # vn_chara_change.rpy: charaShadow flag, the running charaFaceFade blend, the outgoing
            # image of an imageChange fade, when the ChangeCharacter ends (IsChange), and whether
            # the slot has changed since charaSet (backIndex >= 0).
            "shadow": False, "xfade": None, "back": None, "change_end": 0.0, "changed": False,
        }

    def vn_slot_alpha(data, now=None):
        if data.get("alpha_anim"):
            return vn_anim_value(data["alpha_anim"], now)[0]
        return data.get("alpha", 0.0)

    def vn_slot_is_drawn(data):
        if not data.get("path"):
            return False
        # Effect/dummy slots (charaEffect, bit_talk_10, etc.) are visual-effect prefabs, not characters.
        slot_name = (data.get("name") or "").strip().lower()
        if slot_name in ("effect", "effect dummy") or "dummy" in slot_name:
            return False
        if data.get("back") and data["back"].get("path"):
            value, running = vn_anim_value(data["back"].get("alpha_anim"))
            if running or (value or 0.0) > 0.0:
                return True
        anim = data.get("alpha_anim")
        if anim:
            value, running = vn_anim_value(anim)
            return running or value > 0.0
        return data.get("alpha", 0.0) > 0.0

    def vn_effective_depth(data):
        # SetTalkDepth: the talker is raised to depth 9 while isTalkDepth is on.
        if data.get("talk_depth") and renpy.store.vn_talk_depth_enabled:
            return 9
        return data.get("depth", 0)

    def vn_draw_order():
        defs = renpy.store.current_chara_defs
        slots = [s for s, d in defs.items() if vn_slot_is_drawn(d)]
        return sorted(slots, key=lambda s: (vn_effective_depth(defs[s]), s))

    def chara_face_crop(data, face):
        try:
            face_index = max(1, int(face))
        except Exception:
            face_index = 1
        # Expression cells come from one or more 1024x1024 atlas pages. Standard 256px cells follow
        # the 1024x768 body directly; large-cell figures keep the body's full 1024px source page.
        # faceSize is data-driven: Limbo uses 336 and Caenis 3041001 uses 320.
        try:
            face_size = max(1, int(data.get("face_size") or 256))
        except Exception:
            face_size = 256
        columns = max(1, 1024 // face_size)
        zero_idx = face_index - 1
        cells_per_page = columns * columns
        page = zero_idx // cells_per_page
        cell = zero_idx % cells_per_page
        col = cell % columns
        row = cell // columns
        body_page_h = 768 if face_size == 256 else 1024
        return (col * face_size,
                body_page_h + page * 1024 + row * face_size,
                face_size, face_size)

    _vn_image_cache = {}
    _vn_face_registration_cache = {}
    # Cached displayables can be serialized in a Ren'Py save. Bump this whenever face geometry,
    # atlas addressing, or registration changes so an old hole-only composite cannot be reused.
    VN_FACE_RENDER_CACHE_VERSION = 2
    # The crop drops the atlas cell's first texel, so its destination starts one 1024-space
    # pixel down/right of the svtScript anchor. At a 2256px window this is the measured ~2px.
    VN_FACE_RASTER_OFFSET = (1, 1)

    def vn_face_position(data):
        return int(data.get("face_x") or 0), int(data.get("face_y") or 0)

    def vn_registered_face_position(data, path):
        """Re-register Atlas's merged face cells when their baked border moved during export.

        Native FGO can trust svtScript because it renders the original texture list. Atlas Academy's
        flattened _merged.png occasionally relocates a complete opaque face cell (Aphrodite is a
        large example). Compare the invariant outer ring of face 1 with the body once per sheet; a
        transparent ordinary portrait has too few samples and simply keeps the native coordinates.
        """
        fx, fy = vn_face_position(data)
        size = max(1, int(data.get("face_size") or 256))
        key = (VN_FACE_RENDER_CACHE_VERSION, path, fx, fy, size)
        cached = _vn_face_registration_cache.get(key)
        if cached is not None:
            return cached
        result = (fx, fy)
        try:
            surface = renpy.load_surface(path)
            sw, sh = surface.get_size()
            cx, cy, cw, ch = chara_face_crop(data, 1)
            if cx + cw <= sw and cy + ch <= sh and cw <= 512 and ch <= 512:
                # Samples on several rings avoid expression-only pixels in the middle. Requiring
                # opaque pixels on both images makes this an alignment test, not a transparency fit.
                points = set()
                stride = max(6, size // 40)
                for inset in (2, 8, 16):
                    if inset * 2 >= size:
                        continue
                    for p in range(inset, size - inset, stride):
                        points.add((p, inset)); points.add((p, size - 1 - inset))
                        points.add((inset, p)); points.add((size - 1 - inset, p))

                def score(x, y, sample_stride=1):
                    total = 0.0
                    count = 0
                    for i, (px, py) in enumerate(points):
                        if i % sample_stride:
                            continue
                        a = surface.get_at((cx + px, cy + py))
                        b = surface.get_at((x + px, y + py))
                        if a[3] > 32 and b[3] > 32:
                            total += abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])
                            count += 1
                    return (total / (count * 3.0), count) if count else (9999.0, 0)

                native_score, native_count = score(fx, fy)
                if native_count >= 48 and native_score > 2.0:
                    radius = min(64, max(32, size // 5))
                    best = (native_score, fx, fy, native_count)
                    # Coarse search, then a full-sample refinement around the winner.
                    for y in range(max(0, fy - radius), min(768 - size, fy + radius) + 1, 2):
                        for x in range(max(0, fx - radius), min(1024 - size, fx + radius) + 1, 2):
                            value, count = score(x, y, 2)
                            if count >= 24 and value < best[0]:
                                best = (value, x, y, count)
                    coarse_x, coarse_y = best[1], best[2]
                    for y in range(max(0, coarse_y - 3), min(768 - size, coarse_y + 3) + 1):
                        for x in range(max(0, coarse_x - 3), min(1024 - size, coarse_x + 3) + 1):
                            value, count = score(x, y)
                            if count >= 48 and value < best[0]:
                                best = (value, x, y, count)
                    if best[0] < 20.0 and best[0] < native_score * 0.55:
                        result = (best[1], best[2])
        except Exception:
            pass
        _vn_face_registration_cache[key] = result
        return result

    def vn_rendered_face_position(data, path):
        """Return the merged-atlas face origin in Ren'Py's integer raster coordinates."""
        fx, fy = vn_registered_face_position(data, path)
        return (fx + VN_FACE_RASTER_OFFSET[0], fy + VN_FACE_RASTER_OFFSET[1])

    def vn_body_with_face_hole(path, fx, fy, fw, fh):
        """FGO's 8-triangle body mesh: the body surrounds, but never draws beneath, the face quad."""
        x0 = max(0, min(1024, int(fx)))
        y0 = max(0, min(768, int(fy)))
        x1 = max(x0, min(1024, int(fx + fw)))
        y1 = max(y0, min(768, int(fy + fh)))
        pieces = []
        if y0:
            pieces.extend(((0, 0), Transform(path, crop=(0, 0, 1024, y0))))
        if y1 < 768:
            pieces.extend(((0, y1), Transform(path, crop=(0, y1, 1024, 768 - y1))))
        if x0 and y1 > y0:
            pieces.extend(((0, y0), Transform(path, crop=(0, y0, x0, y1 - y0))))
        if x1 < 1024 and y1 > y0:
            pieces.extend(((x1, y0), Transform(path, crop=(x1, y0, 1024 - x1, y1 - y0))))
        return pieces

    def vn_script_image_disp(path, second_path, variant):
        # Atlas's Image PNG retains the square source texture, including
        # unused atlas space. FGO's three renderers expose a 1024x1024,
        # 2048x1024, or 1024x2048 first quad respectively.
        width = 2048 if variant == "horizontal" else 1024
        height = 2048 if variant == "vertical" else 1024
        first = Transform(path, crop=(0, 0, width, height))
        if second_path and variant == "horizontal":
            return Composite((4096, 1024), (0, 0), first,
                             (2048, 0), Transform(second_path, crop=(0, 0, width, height)))
        if second_path and variant == "vertical":
            return Composite((1024, 4096), (0, 0), first,
                             (0, 2048), Transform(second_path, crop=(0, 0, width, height)))
        return first

    def vn_slot_image(slot):
        """Render the body/face as FGO's separate ring mesh and face quad."""
        data = renpy.store.current_chara_defs.get(slot) or {}
        path = data.get("path")
        if data.get("kind") == "script_image":
            return vn_script_image_disp(path, data.get("second_path"), data.get("image_variant", "image"))
        if data.get("kind") == "image":
            return path
        # [charaFaceFade]: the crossFadeStandFigure replaces both figures while it runs.
        crossfade = vn_chara_crossfade_image(slot, data)
        child = crossfade if crossfade is not None else vn_chara_composite(data, data.get("face"))
        return VNFigureSpecial(slot, child) if vn_special_active(data) else child

    def vn_chara_composite(data, face):
        """One standing figure (body ring mesh + face quad) showing the given expression."""
        path = data.get("path")
        face = str(face or "0")
        fx, fy = vn_rendered_face_position(data, path)
        face_size = int(data.get("face_size") or 256)
        key = (VN_FACE_RENDER_CACHE_VERSION, path, face, fx, fy, face_size)
        disp = _vn_image_cache.get(key)
        if disp is None:
            if face not in ("0", ""):
                cx, cy, cw, ch = chara_face_crop(data, face)
                # UIStandFigureRender uses a (size-2)x(size-3) destination quad, samples the atlas
                # from 1.5px inside the cell, and leaves that rectangle out of the body mesh. This
                # prevents the base face/ears from showing through transparent expression pixels.
                fw, fh = max(1, cw - 2), max(1, ch - 3)
                # Ren'Py treats a crop tuple containing floats as relative coordinates. Use the
                # enclosing integer texels for FGO's half-texel sample bounds; this yields the same
                # destination dimensions without resampling the independently compressed patch.
                face_tile = Transform(path, crop=(cx + 1, cy + 1, fw, fh))
                pieces = vn_body_with_face_hole(path, fx, fy, fw, fh)
                pieces.extend(((fx, fy), face_tile))
                disp = Composite((1024, 768), *pieces)
            else:
                disp = Transform(path, crop=(0, 0, 1024, 768))
            _vn_image_cache[key] = disp
        return disp

    # ------------------------------------------------------------------
    # charaRoll family (UIScriptChara Roll1/Roll2) and charaRelativeLoopMove (RelativePos)
    # ------------------------------------------------------------------
    # ScriptFigurePrefab: root -> RelativePos -> Depth -> Shake -> Scale -> Roll1 -> Roll2 -> figure.
    # Only Roll1 rotates. Roll1 sits at -c and Roll2 at +c, so the figure turns about the Scale-frame
    # point -c. The camera is orthographic, so a 3D orientation of Roll1 shows as the upper-left 2x2
    # of its rotation matrix: z is a plain 2D turn, x/y are a vertical/horizontal squash by cos
    # (a mirror past 90 degrees). Unity quaternions are stored here as (w, x, y, z).
    VN_ROLL_IDENTITY = (1.0, 0.0, 0.0, 0.0)
    # RollAxis re-pivots on transform.parent.localPosition, i.e. CharaBase (0, 288): the screen's
    # vertical centre at the figure's x.
    VN_CHARA_BASE_POS = (0.0, 288.0)
    VN_ROLL_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}

    def vn_quat_mul(a, b):
        aw, ax, ay, az = a
        bw, bx, by, bz = b
        return (aw * bw - ax * bx - ay * by - az * bz,
                aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw)

    def vn_quat_axis(angle, axis):
        """Quaternion.AngleAxis(angle degrees, axis). An unknown axis string is Vector3.zero in
        FGO, which AngleAxis turns into the identity."""
        if axis is None or axis == (0.0, 0.0, 0.0):
            return VN_ROLL_IDENTITY
        h = math.radians(float(angle)) / 2.0
        s = math.sin(h)
        return (math.cos(h), axis[0] * s, axis[1] * s, axis[2] * s)

    def vn_quat_euler(e):
        """Quaternion.Euler(x, y, z): Unity applies z, then x, then y."""
        return vn_quat_mul(vn_quat_axis(e[1], (0.0, 1.0, 0.0)),
                           vn_quat_mul(vn_quat_axis(e[0], (1.0, 0.0, 0.0)),
                                       vn_quat_axis(e[2], (0.0, 0.0, 1.0))))

    def vn_quat_matrix(q):
        w, x, y, z = q
        n = w * w + x * x + y * y + z * z
        s = 2.0 / n if n > 0.0 else 0.0
        return ((1.0 - s * (y * y + z * z), s * (x * y - w * z), s * (x * z + w * y)),
                (s * (x * y + w * z), 1.0 - s * (x * x + z * z), s * (y * z - w * x)),
                (s * (x * z - w * y), s * (y * z + w * x), 1.0 - s * (x * x + y * y)))

    def vn_make_positive(e):
        """Quaternion.Internal_MakePositive: euler components in [0, 360)."""
        return tuple(v % 360.0 for v in e)

    def vn_quat_to_euler(q):
        """Transform.localEulerAngles (ZXY order, R = Ry*Rx*Rz), made positive."""
        m = vn_quat_matrix(q)
        sx = max(-1.0, min(1.0, -m[1][2]))
        x = math.degrees(math.asin(sx))
        if abs(sx) < 0.9999999:
            y = math.degrees(math.atan2(m[0][2], m[2][2]))
            z = math.degrees(math.atan2(m[1][0], m[1][1]))
        else:
            y = math.degrees(math.atan2(-m[2][0], m[0][0]))
            z = 0.0
        return vn_make_positive((x, y, z))

    def vn_roll_value(data, now=None):
        """Roll1.localRotation at `now` -> (quaternion, isRoll)."""
        anim = data.get("roll_anim")
        if not anim:
            return data.get("roll_q") or VN_ROLL_IDENTITY, False
        if now is None:
            now = vn_now()
        elapsed = max(0.0, now - anim["start"])
        if anim["kind"] == "euler":
            # NGUI TweenRotation (quaternionLerp off): a linear lerp of the euler components.
            if anim["dur"] <= 0.0 or elapsed >= anim["dur"]:
                return vn_quat_euler(anim["to"]), False
            return vn_quat_euler(vn_lerp(anim["from"], anim["to"], elapsed / anim["dur"])), True
        # <RollAxis>d__121: Roll1.Rotate(axis, angle/duration * dt, Space.World) every frame. In
        # loop mode its timer never advances; otherwise it snaps to stopRotation at the end.
        if not anim["loop"]:
            if elapsed >= anim["dur"]:
                return anim["stop_q"], False
        return vn_quat_mul(vn_quat_axis(anim["omega"] * elapsed, anim["axis"]), anim["q0"]), True

    def vn_roll_freeze(data):
        """StopRollAxisTween / disabling the TweenRotation: stop where it is, no snap."""
        data["roll_q"] = vn_roll_value(data)[0]
        data["roll_anim"] = None

    def vn_roll_remaining(data, now=None):
        """Seconds until IsRoll() goes false; None for an endless charaRollAxis loop."""
        anim = data.get("roll_anim")
        if not anim:
            return 0.0
        if anim.get("loop"):
            return None
        if now is None:
            now = vn_now()
        return max(0.0, anim["start"] + anim["dur"] - now)

    def vn_roll_is_identity(q):
        return abs(abs(q[0]) - 1.0) < 1e-9

    def vn_chara_set_roll(data, angle, center):
        """UIScriptChara.SetRoll: absolute Z angle and pivot, instantly."""
        vn_roll_freeze(data)
        data["roll_q"] = vn_quat_euler((0.0, 0.0, float(angle)))
        data["roll_c"] = tuple(center)

    def vn_chara_start_roll(data, duration, angle, center=None):
        """StartRoll (center None: keeps the pivot, target euler made positive, so -10 from 0
        spins 350 degrees) and StartRollEx (sets the pivot, raw target: 0 -> -10 is the short way)."""
        vn_roll_freeze(data)
        start_e = vn_quat_to_euler(data["roll_q"])
        if center is None:
            target_e = vn_quat_to_euler(vn_quat_euler((0.0, 0.0, float(angle))))
        else:
            target_e = (0.0, 0.0, float(angle))
            data["roll_c"] = tuple(center)
        dur = 0.0 if vn_skipping() else max(0.0, float(duration))
        if dur <= 0.0:
            data["roll_q"] = vn_quat_euler(target_e)
            return 0.0
        data["roll_anim"] = {"kind": "euler", "from": start_e, "to": target_e,
                             "start": vn_now(), "dur": dur}
        data["roll_q"] = vn_quat_euler(target_e)
        return dur

    def vn_chara_roll_axis(data, axis_name, angle, duration):
        """<RollAxis>d__121: re-pivot on CharaBase, then turn `angle` degrees about a local axis."""
        vn_roll_freeze(data)
        data["roll_c"] = VN_CHARA_BASE_POS
        axis = VN_ROLL_AXES.get(axis_name)
        q0 = data["roll_q"]
        stop_q = vn_quat_mul(q0, vn_quat_axis(angle, axis))
        loop = bool(data.get("roll_looping"))
        if duration <= 0.0:
            # isRoll is never raised on this path; a stale isRollWait selects the loop-end pose.
            data["roll_q"] = data.get("roll_stop_q", VN_ROLL_IDENTITY) if data.get("roll_wait") else stop_q
            return
        data["roll_anim"] = {"kind": "axis", "q0": q0, "axis": axis or (0.0, 0.0, 0.0),
                             "omega": float(angle) / duration, "start": vn_now(), "dur": duration,
                             "loop": loop, "stop_q": stop_q}
        data["roll_q"] = stop_q

    def vn_chara_start_roll_axis(data, axis_name, angle, duration, is_loop, is_wait, stop_angle):
        """UIScriptChara.StartRollAxis. The loop fields are only written for a loop."""
        vn_roll_freeze(data)
        data["roll_looping"] = bool(is_loop)                      # isRollLoop
        data["roll_init"] = vn_quat_to_euler(data["roll_q"])      # initAngle, every call
        if is_loop:
            # rollAxis / rollAngle / rollDuration / isRollWait / stopLocalRotationLoopEnd.
            # The stop pose is absolute (AngleAxis from identity), not relative to initAngle.
            data["roll_loop"] = {"axis": axis_name, "angle": float(angle), "dur": float(duration)}
            data["roll_wait"] = bool(is_wait)
            data["roll_stop_q"] = vn_quat_axis(stop_angle, VN_ROLL_AXES.get(axis_name))
        dur = float(duration)
        if not is_loop and vn_skipping():
            dur = 0.0
        vn_chara_roll_axis(data, axis_name, angle, dur)

    def vn_converted_axis_angle(axis_name, e, base):
        """GetConvertedLocalRotationEulerAnglesToAngle."""
        if axis_name == "x":
            if (e[1] - base[1]) ** 2 + (e[2] - base[2]) ** 2 >= 1e-10:
                return (540.0 if e[0] > 90.0 else 180.0) - e[0]
            return e[0]
        if axis_name == "y":
            return e[1]
        if axis_name == "z":
            return e[2]
        return 0.0

    def vn_chara_stop_roll_axis_loop(data):
        """UIScriptChara.StopRollAxisLoop: without isRollWait the figure freezes where it is;
        with it, it keeps turning the same way at the same speed onto the stop angle."""
        vn_roll_freeze(data)
        data["roll_looping"] = False
        loop = data.get("roll_loop")
        if not data.get("roll_wait") or not loop:
            return
        axis_name = loop["axis"]
        init = data.get("roll_init") or (0.0, 0.0, 0.0)
        cur = vn_converted_axis_angle(axis_name, vn_quat_to_euler(data["roll_q"]), init)
        tgt = vn_converted_axis_angle(axis_name, vn_quat_to_euler(data["roll_stop_q"]), init)
        if loop["angle"] >= 0.0:
            if tgt < cur:
                tgt += 360.0
        elif tgt >= cur:
            tgt -= 360.0
        delta = tgt - cur
        speed = loop["angle"] / loop["dur"] if loop["dur"] else 0.0
        if vn_skipping() or not speed:
            dur = 0.0
        else:
            dur = abs(delta / speed)
        vn_chara_roll_axis(data, axis_name, delta, dur)

    def vn_rel_value(data, now=None):
        """RelativePos.localPosition at `now` -> (offset, isMoveRelativePosition)."""
        move = data.get("rel_move")
        if not move:
            return data.get("rel") or (0.0, 0.0), False
        if now is None:
            now = vn_now()
        points, durs = move["points"], move["durs"]
        period = sum(durs)
        passes = move["passes"]
        elapsed = max(0.0, now - move["start"])
        if passes is None and period <= 0.0:
            return points[-1], False     # FGO recurses forever here; settle instead of hanging
        if passes is not None and elapsed >= period * passes:
            return points[-1], False     # StopMoveRelativePositionLoop snaps to the last point
        k = int(elapsed // period) if period > 0.0 else 0
        t = elapsed - k * period
        prev = move["from"] if k == 0 else points[-1]
        for i, d in enumerate(durs):
            if t < d:
                f = vn_ease(t / d, move["ease"]) if move["ease"] else t / d
                return vn_lerp(prev, points[i], f), True
            t -= d
            prev = points[i]
        return points[-1], True

    def vn_rel_remaining(data, now=None):
        """Seconds a [wait charaRelativeLoopMove] blocks: 0 for an endless loop (IsMoveRelativePositionLoop)."""
        move = data.get("rel_move")
        if not move or move["passes"] is None:
            return 0.0
        if now is None:
            now = vn_now()
        return max(0.0, move["start"] + sum(move["durs"]) * move["passes"] - now)

    def vn_chara_rel_stop(data):
        """StopMoveRelativePositionLoop: snap to the last listed point and clear the loop."""
        move = data.get("rel_move")
        if move:
            data["rel"] = move["points"][-1]
        elif data.get("rel_points"):
            data["rel"] = data["rel_points"][-1]
        data["rel_move"] = None

    def vn_chara_rel_loop(data, points, durs, loop_num, ease):
        """MoveRelativePositionLoop / MoveRelativePositionEaseLoop. loopNum is the number of passes;
        0 loops forever and a negative count plays once. Positions are absolute RelativePos offsets
        (NGUI units, +y up), tweened from wherever the node currently is."""
        current = vn_rel_value(data)[0]
        data["rel_move"] = None
        data["rel"] = tuple(current)
        data["rel_points"] = list(points)
        if vn_skipping():
            data["rel"] = points[-1]
            return
        passes = None if loop_num == 0 else max(1, loop_num)
        data["rel_move"] = {"points": list(points), "durs": [max(0.0, d) for d in durs],
                            "passes": passes, "from": tuple(current), "ease": ease,
                            "start": vn_now()}
        data["rel"] = points[-1]

    def vn_chara_roll_command(lname, args, defs):
        """[charaRoll] / [charaRollMove] / [charaRollMoveEx] / [charaRollAxis] /
        [charaRollAxisLoopStop] / [charaRelativeLoopMove(Ease)] / [charaRelativeLoopMoveStop].
        ScriptCommandExecute has no decompiled body; argument order follows the script corpus."""
        if not args or args[0] not in defs:
            return True
        data = defs[args[0]]
        a = list(args[1:])

        def center(tokens):
            # "x,y" or two separate tokens ([charaRoll Z 358 0 0]); absent means Vector3.zero.
            if not tokens:
                return (0.0, 0.0)
            if "," in tokens[0]:
                return vn_parse_position(tokens[0], (0.0, 0.0))
            if len(tokens) >= 2:
                return (vn_float(tokens[0]), vn_float(tokens[1]))
            return (vn_float(tokens[0]), 0.0)

        def flag(i):
            if len(a) <= i:
                return False
            return a[i].lower() == "true" or vn_float(a[i], 0.0) != 0.0

        if lname == "charaRoll".lower():
            vn_chara_set_roll(data, vn_float(a[0], 0.0) if a else 0.0, center(a[1:]))
            return True
        if lname == "charaRollMove".lower():
            # StartRoll has no pivot argument; the one corpus line with a trailing 0,0 is ignored.
            vn_chara_start_roll(data, vn_float(a[0], 0.0) if a else 0.0,
                                vn_float(a[1], 0.0) if len(a) > 1 else 0.0)
            return True
        if lname == "charaRollMoveEx".lower():
            vn_chara_start_roll(data, vn_float(a[0], 0.0) if a else 0.0,
                                vn_float(a[1], 0.0) if len(a) > 1 else 0.0, center(a[2:]))
            return True
        if lname == "charaRollAxis".lower():
            axis_name = a[0] if a else ""
            vn_chara_start_roll_axis(data, axis_name,
                                     vn_float(a[1], 0.0) if len(a) > 1 else 0.0,
                                     vn_float(a[2], 0.0) if len(a) > 2 else 0.0,
                                     flag(3), flag(4),
                                     vn_float(a[5], 0.0) if len(a) > 5 else 0.0)
            return True
        if lname == "charaRollAxisLoopStop".lower():
            vn_chara_stop_roll_axis_loop(data)
            return True
        if lname == "charaRelativeLoopMoveStop".lower():
            vn_chara_rel_stop(data)
            return True
        if lname in ("charaRelativeLoopMove".lower(), "charaRelativeLoopMoveEase".lower()):
            # S N x1,y1 .. xN,yN d1 .. dN LOOPNUM [EASETYPE]
            try:
                n = int(float(a[0]))
            except Exception:
                return True
            need = 1 + 2 * n + 1
            if n <= 0 or len(a) < need:
                return True
            points = [vn_parse_position(tok, (0.0, 0.0)) for tok in a[1:1 + n]]
            durs = [vn_float(tok, 0.0) for tok in a[1 + n:1 + 2 * n]]
            loop_num = int(vn_float(a[1 + 2 * n], 0.0))
            ease = None
            if lname.endswith("ease"):
                ease = a[need] if len(a) > need else "linear"
            vn_chara_rel_loop(data, points, durs, loop_num, ease)
            return True
        return False

    VN_ROLL_COMMANDS = ("charaRoll".lower(), "charaRollMove".lower(), "charaRollMoveEx".lower(),
                        "charaRollAxis".lower(), "charaRollAxisLoopStop".lower(),
                        "charaRelativeLoopMove".lower(), "charaRelativeLoopMoveEase".lower(),
                        "charaRelativeLoopMoveStop".lower())

    def vn_wait_chara_roll(tag, slot):
        """[wait charaRollMove S] etc. polls IsRoll(); [wait charaRelativeLoopMove S] polls
        IsMoveRelativePosition() unless the loop is endless. No slot means every figure."""
        if vn_skipping():
            return
        defs = renpy.store.current_chara_defs
        slots = [slot] if slot in defs else ([] if slot else list(defs))
        is_rel = tag.startswith("charaRelative")
        now = vn_now()
        longest = 0.0
        for s in slots:
            remaining = vn_rel_remaining(defs[s], now) if is_rel else vn_roll_remaining(defs[s], now)
            # An endless charaRollAxis loop would hold FGO's script forever; don't hang the reader.
            if remaining is not None:
                longest = max(longest, remaining)
        if longest > 0.005:
            renpy.store.vn_wait_until = now + longest
            renpy.pause(longest, hard=True)

    def vn_slot_transform(slot, capture=False, back=False):
        """Per-frame transform: alpha fade, position/scale tweens, talk dim and filter tint.
        Reads the slot's plain-data state so it is always in sync with the script.
        back=True draws the outgoing image of an [imageChange] fade (same root, own alpha)."""
        def fn(trans, st, at):
            data = renpy.store.current_chara_defs.get(slot)
            if data is None or not data.get("path"):
                trans.alpha = 0.0
                return None
            now = vn_now()
            running = False
            src = data
            if back:
                src = data.get("back") or {}
                alpha, r = vn_anim_value(src.get("alpha_anim"), now) if src.get("alpha_anim") else (0.0, False)
                alpha = alpha or 0.0
            elif data.get("alpha_anim"):
                alpha, r = vn_anim_value(data["alpha_anim"], now)
            else:
                alpha, r = data.get("alpha", 0.0), False
            running = running or r
            # [charaFaceFade]: the crossfade figure's _Color is the old figure's colour, frozen.
            xfade = None if back else vn_chara_crossfade(data, now)
            if xfade is not None:
                alpha = xfade["alpha"]
                running = True
            if data.get("pos_anim"):
                pos, r = vn_anim_value(data["pos_anim"], now)
            else:
                pos, r = data.get("pos", (0.0, 0.0)), False
            running = running or r
            if data.get("scale_anim"):
                scale, r = vn_anim_value(data["scale_anim"], now)
            else:
                scale, r = data.get("scale", 1.0), False
            running = running or r
            # Only ScriptFigurePrefab has the RelativePos/Roll rig handled here; image prefabs never
            # carry a rotation, so clear any matrix a replaced figure transform left behind.
            trans.matrixtransform = None

            if data.get("kind") == "script_image":
                # The Image/VerticalImage/HorizontalImage prefabs use an
                # origin at the upper-left of the first quad. Decompiled mesh
                # constants: left=-510, top=511; main quad is 1022x1022 or
                # 1982x1022 / 1022x1982 for horizontal / vertical images.
                # Their mesh coordinates, not the PNG's padded dimensions,
                # determine placement under ScriptPosition and charaScale.
                variant = src.get("image_variant", "image")
                source_width = 2048.0 if variant == "horizontal" else 1024.0
                source_height = 2048.0 if variant == "vertical" else 1024.0
                mesh_width = 1982.0 if variant == "horizontal" else 1022.0
                mesh_height = 1982.0 if variant == "vertical" else 1022.0
                # UIScriptImage / Horizontal / Vertical inherit UIScriptChara's
                # empty SetTalkMask. Only UIScriptFigure dims non-speakers; charaShadow
                # still darkens images (UIImageRender.SetShadow).
                if data.get("filter") == "silhouette":
                    trans.matrixcolor = TintMatrix("#000000")
                else:
                    trans.matrixcolor = vn_tint_matrix(vn_chara_tint_value(data))
                trans.zoom = 1.0
                trans.xzoom = float(scale) * mesh_width / source_width
                trans.yzoom = float(scale) * mesh_height / source_height
                trans.xpos = int(round((VN_SUB_CAPTURE / 2.0 if capture else 512.0)
                                        + float(pos[0]) - 510.0 * float(scale)))
                trans.ypos = int(round((VN_SUB_CAPTURE / 2.0 if capture else 288.0)
                                        - float(pos[1]) - 511.0 * float(scale)))
                trans.alpha = max(0.0, min(1.0, float(alpha)))
                return 0 if running else None

            # Image slots use the Back texture as a single sprite, without face tiles or
            # svtScript offsets. Their independent Scale/Depth/slot hierarchy remains.
            if data.get("kind") == "image":
                scale = float(scale)
                width, height = data.get("image_size", (1024, 626))
                trans.matrixcolor = vn_tint_matrix(vn_chara_tint_value(data))
                # Screen replacement can transfer the previous TransformState into this
                # transform.  Define all three zoom properties so a standing figure's
                # uniform zoom cannot be multiplied into this non-uniform image scale.
                trans.zoom = 1.0
                # UIImageRender uses a fixed square body mesh rather than texture-sized
                # geometry; model its 1024 logical height here. The image prefab's
                # Image(+511) and Depth(-288) nodes establish its vertical origin.
                trans.xpos = int(round((VN_SUB_CAPTURE / 2.0 if capture else 512.0)
                                        + float(pos[0]) - scale * width * (1344.0 / 1024.0) / 2.0))
                trans.ypos = int(round((VN_SUB_CAPTURE / 2.0 if capture else 288.0)
                                        - float(pos[1]) + 288.0 - scale * (511.0 + 512.0)))
                trans.xzoom = scale * (1344.0 / 1024.0)
                trans.yzoom = scale * (1024.0 / float(height))
                trans.alpha = max(0.0, min(1.0, float(alpha)))
                return 0 if running else None

            # Colour: material _Color multiply. Non-talkers 0.5 grey, charaShadow 0.1 (0.05 while
            # talk-masked); silhouette filter = black. The crossfade shader has no filter colour.
            if xfade is not None:
                trans.matrixcolor = vn_tint_matrix(xfade["tint"])
            elif data.get("filter") == "silhouette" and not vn_special_active(data):
                trans.matrixcolor = TintMatrix("#000000")
            else:
                trans.matrixcolor = vn_tint_matrix(vn_chara_tint_value(data))

            # ScriptPosition is relative to CharaBase (world y=288). The mesh and
            # svtScript offsets scale below that parent; x is centered on the root.
            # RelativePos (charaRelativeLoopMove) is the root's first child, above Scale: unscaled.
            rel, r = vn_rel_value(data, now)
            running = running or r
            root_x = (VN_SUB_CAPTURE / 2.0 if capture else 512.0) + float(pos[0]) + float(rel[0])
            root_y = (VN_SUB_CAPTURE / 2.0 if capture else 288.0) - float(pos[1]) - float(rel[1])  # NGUI +y is up
            # A charaChange fade's outgoing figure keeps its own svtScript offsets.
            ox = float(src.get("offset_x", data.get("offset_x")) or 0)
            oy = float(src.get("offset_y", data.get("offset_y")) or 0)
            scale = float(scale)

            # communicationCharaEffect noise: a brief static burst on a 2.0 s heartbeat (approximation; the
            # real noise prefab is not exported). Comm figure only, and only while VN_COMM_NOISE is on.
            noise_dx = 0.0
            if data.get("communication_chara") and store.VN_COMM_NOISE:
                phase = (now - float(data.get("comm_start") or now)) % VN_COMM_NOISE_PERIOD
                if phase < VN_COMM_NOISE_BURST:
                    noise_dx = math.sin(now * 91.0) * VN_COMM_NOISE_JITTER      # fast horizontal jitter
                    alpha = alpha * (0.72 + 0.28 * abs(math.sin(now * 47.0)))   # flicker
                running = True   # keep redrawing so the heartbeat keeps ticking

            shake_dx = shake_dy = 0.0
            sh = data.get("shake")
            if sh and now < sh["end"]:
                tick = int(max(0.0, now - sh["start"]) / sh["cycle"])
                import random
                rng = random.Random(str(sh["start"]) + ":" + str(tick))
                shake_dx = rng.uniform(-sh["x"], sh["x"])
                shake_dy = rng.uniform(-sh["y"], sh["y"])
                running = True

            # sceneSet image slots use non-uniform xzoom/yzoom. Ren'Py transfers transform
            # state between replacements at the same screen-language add site, which is
            # especially easy to hit while skip mode churns through visible slots. Reset
            # those axes before applying the figure's uniform scale or the inherited image
            # zoom multiplies this sprite and shunts its visible pixels down and right.
            trans.xzoom = 1.0
            trans.yzoom = 1.0
            trans.zoom = scale
            trans.xpos = int(round(root_x + scale * (ox - 512.0) + noise_dx + shake_dx))
            # CharaBase is at world y=288 ABOVE the per-character Scale node.
            # Scale only the mesh/metadata offset, never this parent translation.
            trans.ypos = int(round(root_y - 288.0 - scale * oy - shake_dy))

            # Roll1 turns everything under it about the Scale-frame point -c. The 1024x768 mesh's
            # top-left is at Scale-frame (ox - 512, -oy), so as a fraction of the (uniformly
            # zoomed) image the pivot is independent of scale. Orthographic camera: draw the
            # 2x2 upper-left of Roll1's rotation, with y flipped for Ren'Py's downward y.
            q, r = vn_roll_value(data, now)
            running = running or r
            if not vn_roll_is_identity(q):
                cx, cy = data.get("roll_c") or (0.0, 0.0)
                m = vn_quat_matrix(q)
                trans.matrixanchor = ((512.0 - cx - ox) / 1024.0, (cy + oy) / 768.0)
                trans.matrixtransform = Matrix([m[0][0], -m[0][1], 0.0, 0.0,
                                                -m[1][0], m[1][1], 0.0, 0.0,
                                                0.0, 0.0, 1.0, 0.0,
                                                0.0, 0.0, 0.0, 1.0])
            trans.alpha = max(0.0, min(1.0, float(alpha)))
            return 0 if running else None
        return Transform(function=fn)

    # ------------------------------------------------------------------
    # Talk highlight (ScriptManager.SetTalkIndex / SetTalkName)
    # ------------------------------------------------------------------

    def vn_set_talk_index(idx):
        defs = renpy.store.current_chara_defs
        renpy.store.vn_talk_index = idx if idx in defs else None
        for slot, data in defs.items():
            if renpy.store.vn_talk_index is None:
                data["talk_mask"] = False
                data["talk_depth"] = False
            else:
                # FGO's image-set subclasses do not implement SetTalkMask.
                data["talk_mask"] = (slot != renpy.store.vn_talk_index and
                                     data.get("kind") != "script_image")
                data["talk_depth"] = (slot == renpy.store.vn_talk_index)

    def vn_convert_name(name):
        name = str(name or "").strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "\"'“”":
            name = name[1:-1]
        return name.strip()

    def vn_find_slot_by_name(name):
        if not name:
            return None
        for slot, data in sorted(renpy.store.current_chara_defs.items()):
            if vn_convert_name(data.get("name")) == name:
                return slot
        return None

    def vn_set_talk_name(name):
        """SetTalkName: keywords on/off/depthOn/depthOff, otherwise remember the name and match it
        against the charaSet display names (only while isTalkMask is on)."""
        s = vn_convert_name(name)
        if s == "on":
            renpy.store.vn_talk_mask_enabled = True
            s = renpy.store.vn_talk_mask_name
        elif s == "off":
            renpy.store.vn_talk_mask_enabled = False
        elif s == "depthOn":
            renpy.store.vn_talk_depth_enabled = True
            s = renpy.store.vn_talk_mask_name
        elif s == "depthOff":
            renpy.store.vn_talk_depth_enabled = False
            s = renpy.store.vn_talk_mask_name
        else:
            renpy.store.vn_talk_mask_name = s
        idx = None
        if renpy.store.vn_talk_mask_enabled:
            idx = vn_find_slot_by_name(s)
        vn_set_talk_index(idx)

    def vn_apply_talk_line(name, slot):
        """A ＠ line: slot letter wins (even with isTalkMask off); otherwise name matching."""
        if slot and slot in renpy.store.current_chara_defs:
            vn_set_talk_index(slot)
        elif slot:
            vn_set_talk_index(None)
        else:
            vn_set_talk_name(name)

    def vn_chara_talk(arg):
        """[charaTalk X]: keyword, or name match then slot letter (ConvertCharaIndexTalk)."""
        s = vn_convert_name(arg)
        if s in ("on", "off", "depthOn", "depthOff"):
            vn_set_talk_name(s)
            return
        idx = vn_find_slot_by_name(s)
        if idx is None and s in renpy.store.current_chara_defs:
            idx = s
        vn_set_talk_index(idx)

    def vn_is_equal_talk_name(name, slot):
        """IsEqualTalkName: same speaker as the text currently in the window?"""
        last_name = renpy.store.vn_msg_last_name
        last_slot = renpy.store.vn_msg_last_slot
        if name and name == last_name:
            return True
        if slot is not None and last_slot is not None and slot == last_slot:
            return True
        if slot is not None and last_name:
            data = renpy.store.current_chara_defs.get(slot)
            if data and vn_convert_name(data.get("name")) == last_name:
                return True
        return False

    # ------------------------------------------------------------------
    # Screen fades / masks / backgrounds
    # ------------------------------------------------------------------

    def vn_fade_color(name):
        name = str(name or "black").strip().lower()
        if name == "black":
            return "#000000"
        if name == "white":
            return "#ffffff"
        if name.startswith("#"):
            return name
        if len(name) in (6, 8):
            return "#" + name
        return "#000000"

    def vn_start_fade(name, is_in, duration):
        """[fadein]/[fadeout]: meshFadeBase quad in the stage layer (below the message window)."""
        anim = vn_anim(1.0, 0.0, duration) if is_in else vn_anim(0.0, 1.0, duration)
        anim["color"] = vn_fade_color(name)
        renpy.store.vn_fade = anim
        renpy.store.vn_phase_fade_seen = True
        vn_mark_busy("fade", duration)

    def vn_start_mask(name, is_in, duration):
        """[maskin]/[maskout]: CommonUI MaskFade, covers everything including the window."""
        anim = vn_anim(0.0, 1.0, duration) if is_in else vn_anim(1.0, 0.0, duration)
        anim["color"] = vn_fade_color(name)
        renpy.store.vn_mask = anim
        vn_mark_busy("mask", duration)

    # Wipes ([wipein]/[wipeout]/[wipeFilter]/[wipeOff]): vn_wipe.rpy.

    def vn_overlay_transform(key):
        def fn(trans, st, at):
            anim = getattr(renpy.store, key)
            if not anim:
                trans.alpha = 0.0
                return None
            value, running = vn_anim_value(anim)
            trans.alpha = max(0.0, min(1.0, float(value)))
            return 0 if running else None
        return Transform(function=fn)

    def vn_overlay_color(key):
        anim = getattr(renpy.store, key)
        return (anim or {}).get("color", "#000000")

    def vn_scene_change(api, scene_id, duration=0.0):
        """[scene ID TIME]: new background cross-fades over the old one; the window is untouched."""
        path = api.get_background_path(scene_id)
        path = path.replace("\\", "/") if path else None
        prev = renpy.store.current_background_path
        renpy.store.current_scene_id = scene_id
        renpy.store.current_background_path = path
        duration = vn_float(duration, 0.0)
        renpy.store.vn_bg = {"prev": prev if duration > 0 else None, "anim": vn_anim(0.0, 1.0, duration)}
        vn_mark_busy("scene", duration)

    def vn_bg_transform():
        def fn(trans, st, at):
            state = renpy.store.vn_bg or {}
            anim = state.get("anim")
            if not anim:
                trans.alpha = 1.0
                return None
            value, running = vn_anim_value(anim)
            trans.alpha = max(0.0, min(1.0, float(value)))
            return 0 if running else None
        return Transform(function=fn)

    def vn_camera_move(args, ease=False):
        """[cameraMove TIME X,Y SCALE]: the stage's cameraPosition node moves to (x, y) and the cameraScale
        node scales by SCALE (about the screen centre) over TIME (TweenPosition/TweenScale, linear)."""
        duration = vn_float(args[0], 0.0) if args else 0.0
        target = vn_parse_position(args[1], (0.0, 0.0)) if len(args) >= 2 else (0.0, 0.0)
        scale = vn_float(args[2], 1.0) if len(args) >= 3 else 1.0
        cam = renpy.store.vn_camera or {}
        cur_pos = vn_anim_current(cam.get("pos_anim"), (0.0, 0.0))
        cur_scale = vn_anim_current(cam.get("scale_anim"), 1.0)
        renpy.store.vn_camera = {
            "pos_anim": vn_anim(tuple(cur_pos), tuple(target), duration),
            "scale_anim": vn_anim(float(cur_scale), float(scale), duration),
        }
        vn_mark_busy("camera", duration)

    def vn_camera_transform(include_shake=True):
        """Stage pan/zoom: NGUI camera node at (x, y) (+y up) with scale s about the screen centre."""
        def fn(trans, st, at):
            cam = renpy.store.vn_camera
            trans.xanchor = 0.5
            trans.yanchor = 0.5
            trans.xpos = 0.5
            trans.ypos = 0.5
            running = False
            cam_xoff = 0
            cam_yoff = 0
            cam_zoom = 1.0
            if cam:
                pos, r1 = vn_anim_value(cam.get("pos_anim"))
                scale, r2 = vn_anim_value(cam.get("scale_anim"))
                cam_zoom = float(scale)
                cam_xoff = int(round(float(pos[0]) * upscale_ratio))
                cam_yoff = int(round(-float(pos[1]) * upscale_ratio))
                running = r1 or r2
            shake_dx = shake_dy = 0.0
            sh = renpy.store.vn_shake if include_shake else None
            if sh:
                now = vn_now()
                if now < sh["end"]:
                    tick = int(max(0.0, now - sh["start"]) / sh["cycle"])
                    import random
                    rng = random.Random(str(sh["start"]) + ":" + str(tick))
                    shake_dx = rng.uniform(-sh["x"], sh["x"])
                    shake_dy = rng.uniform(-sh["y"], sh["y"])
                    running = True
                else:
                    renpy.store.vn_shake = None
            trans.zoom = cam_zoom
            trans.xoffset = cam_xoff + int(round(shake_dx * upscale_ratio))
            trans.yoffset = cam_yoff + int(round(shake_dy * upscale_ratio))
            return 0 if running else None
        return Transform(function=fn)

    # ------------------------------------------------------------------
    # [messageShake CYCLE X Y DURATION] / [messageShakeStop]
    # ------------------------------------------------------------------
    # ScriptMessageCommonManager.Shake(duration, cycle, x, y) stores shakeTime = now + duration (0 = no end),
    # shakeCycle, shakeX, shakeY and calls OnShake. OnShake, while shakeCycle > 0 and (shakeTime == 0 or
    # now < shakeTime), sets MessageShake.localPosition = (Random.Range(-x, x), Random.Range(-y, y), 0) and
    # re-Invokes itself after `cycle`; the first call at or past shakeTime zeroes the position and shakeCycle.
    # So the last offset is held until the first tick at/after the end, not cut off exactly at shakeTime.
    # MessageShake (BattleScriptScene) parents the whole window: MessageWindow (back sprite, TalkName plate,
    # MessagePanel text) and NextMark. messageShakeStop is Shake(0, 0, 0, 0); the script-end reset
    # (ProcessScript / ExitExecuteScript) does the same.

    def vn_msg_shake_start(duration, cycle, x, y, now=None):
        if now is None:
            now = vn_now()
        if cycle <= 0.0:
            renpy.store.vn_msg_shake = None
            return
        renpy.store.vn_msg_shake = {"x": float(x), "y": float(y), "cycle": float(cycle), "start": now,
                                    "end": now + duration if duration > 0.0 else float("inf")}

    def vn_msg_shake_command(token, now=None):
        """Runs one [messageShake ...] / [messageShakeStop] token (a string or a split list). Also the
        dispatcher for the other positioned inline commands (fgo_parser.POSITIONED_TAGS): voices, flashes and
        cameraFilter typed mid-line run when the typewriter reaches them, and never block."""
        parts = token.split() if isinstance(token, str) else list(token)
        if not parts:
            return False
        lname = parts[0].lower()
        args = parts[1:]
        if vn_fx_command_kind(lname):
            return vn_fx_command(lname, args, now)
        if lname in ("voice", "voicestop"):
            return vn_voice_command(lname, args, get_api())
        if lname in ("flashin", "flashout", "flashoff"):
            return vn_flash_command(parts, blocking=False)
        if parts[0] == "cameraFilter":
            return vn_camera_filter_command(args)
        import fgo_parser
        if lname in fgo_parser.POSITIONED_CHARA_TAGS:
            # Same handlers as the whole-line command; none of them block. A figure that starts fading in
            # mid-line is not in the stage screen yet, so the screen is re-evaluated.
            node = fgo_parser._parse_command_node(" ".join(parts))
            done = bool(node) and apply_reader_node(node, get_api())
            renpy.restart_interaction()
            return done
        if lname == "messageshake":
            cycle = vn_float(args[0], 0.0) if len(args) >= 1 else 0.0
            ax = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
            ay = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            duration = vn_float(args[3], 0.0) if len(args) >= 4 else 0.0
            vn_msg_shake_start(duration, cycle, ax, ay, now)
            return True
        if lname == "messageshakestop":
            vn_msg_shake_start(0.0, 0.0, 0.0, 0.0, now)
            return True
        return False

    def vn_msg_typewriter_time():
        """Typewriter time of the current message (the same clock VNTextBody feeds the text), or None once
        it has been completed (tap-to-finish or natural end)."""
        if renpy.store.vn_msg_forced or renpy.store.vn_text_done_at is not None:
            return None
        body = renpy.store.vn_msg_body
        if body is not None and body.gated and not renpy.store.vn_auto_mode:
            return vn_gate_eff_and_hold(body.steps)[0]
        return vn_now() - float(renpy.store.vn_msg_start_wall)

    def vn_msg_shake_promote(flush=False):
        """Runs the inline shakes the typewriter has reached. AnalysText executes a line's text chunks and
        commands in order, so a command runs once the text before it is typed; a tap that completes the
        message runs the rest at once. Idempotent: entries are consumed as they run, and each start time is
        derived from the typewriter clock rather than from when this happens to be called."""
        pending = renpy.store.vn_msg_shake_pending
        if not pending:
            return
        now = vn_now()
        eff = None if flush else vn_msg_typewriter_time()
        while pending:
            t, token = pending[0]
            if eff is None:
                start = now
            elif eff >= t:
                start = now - (eff - t)
            else:
                break
            pending.pop(0)
            vn_msg_shake_command(token, start)

    def vn_msg_shake_offset(now=None):
        """(dx, dy, running) of the MessageShake node in 1024x576 NGUI units (+y up)."""
        sh = renpy.store.vn_msg_shake
        if not sh:
            return 0.0, 0.0, False
        if now is None:
            now = vn_now()
        tick = int(max(0.0, now - sh["start"]) / sh["cycle"])
        if sh["start"] + tick * sh["cycle"] >= sh["end"]:
            renpy.store.vn_msg_shake = None
            return 0.0, 0.0, False
        import random
        rng = random.Random(str(sh["start"]) + ":" + str(tick))
        return rng.uniform(-sh["x"], sh["x"]), rng.uniform(-sh["y"], sh["y"]), True

    def vn_msg_shake_transform():
        def fn(trans, st, at):
            vn_msg_shake_promote()
            dx, dy, running = vn_msg_shake_offset()
            trans.xoffset = int(round(dx * upscale_ratio))
            trans.yoffset = int(round(-dy * upscale_ratio))
            return 0 if (running or renpy.store.vn_msg_shake_pending) else None
        return Transform(function=fn)

    def vn_bg_prev_visible():
        state = renpy.store.vn_bg or {}
        if not state.get("prev"):
            return False
        return vn_anim_value(state.get("anim"))[1]

    # ------------------------------------------------------------------
    # Sub-render / cut-in layers (ScriptSubLayer / ScriptSubLayerManager)
    #
    # Capture camera: ortho 2.3333 world units, UI scale 1/288 => ~1344 units.
    # RT resolution is 1024 square; prefab display quad is 1345 square (one-unit overscan).
    # CameraScale/Position/Roll are PARENTS of renderRoot, not a camera inside the mask.
    VN_SUB_CAPTURE = 1344.0
    VN_SUB_TEXTURE = 1024
    VN_SUB_QUAD = 1345

    def vn_sub_busy(g, kind, duration, command=None):
        # Track independent animations per group; a short move must not truncate a longer fade.
        g.setdefault("busy", {})[kind] = (vn_now() + max(0.0, duration), (command or "").lower())

    def vn_sub_wait_end(tag, group=None):
        tag = tag.lower()
        ends = []
        for key, g in renpy.store.vn_sublayers.items():
            if group and key != vn_group_key(group):
                continue
            for kind, (end, command) in g.get("busy", {}).items():
                if tag in ("subrender", "subcamera") or tag == command or (tag == "substretch" and kind == "stretch"):
                    ends.append(end)
        return max(ends) if ends else None

    def vn_group_key(tok):
        s = str(tok or "").strip()
        if not s:
            return "#A"                              # DEFAULT_SUB_LAYER_GROUP_NAME
        return s if s.startswith("#") else ("#" + s)

    def vn_sublayer_default():
        return {
            "active": True,
            "pos": (0.0, 0.0), "pos_anim": None,        # subRenderMove/Fadein (RT quad, NGUI +y up)
            "scale": 1.0, "scale_anim": None,           # subRenderScale
            "alpha": 0.0, "alpha_anim": None,           # subRenderFadein/out
            "depth": 1,                                 # subRenderDepth (meshRender z = -depth)
            "cam_pos": (0.0, 0.0), "cam_pos_anim": None,   # subCameraMove pan
            "cam_scale": 1.0, "cam_scale_anim": None,      # subCameraMove zoom
            "roll": 0.0, "roll_anim": None, "roll_pivot": (0.0, 0.0),
            "stretch": 1.0, "stretch_anim": None, "busy": {},
            "filter": None, "mask_name": None, "mask_path": None,  # subCameraFilter
            "blur": 0.0, "blur_anim": None, "mask_group": None,
            "blur2": 0.0, "blur2_anim": None,
            "shake": None,                              # subRenderShake {"x","y","end"}
        }

    def vn_sublayer(gkey, create=True):
        subs = renpy.store.vn_sublayers
        g = subs.get(gkey)
        if g is None and create:
            g = vn_sublayer_default()
            subs[gkey] = g
        return g

    def vn_fs_command_offset_x(lname):
        """ScriptManager.GetFSCommandOffsetX: +edge offset for the FSR/FSRNotNotch/FSSideR variants, - for the
        L ones, 0 for the plain command (and on a 16:9 surface, where FSUtility.IsUnderVista holds)."""
        l = lname.lower()
        if l.endswith(("fssider", "fsr", "fsrnotnotch")):
            return VN_FS_EDGE_OFFSET
        if l.endswith(("fssidel", "fsl", "fslnotnotch")):
            return -VN_FS_EDGE_OFFSET
        return 0

    # BattleScriptScene MargePanel/PictureFrame: an ExUISpriteRenderer widget, 627 high, pivot centre. Its anchors
    # have no target, so NGUI never resizes it; a 16:9 screen shows the middle 576 rows. Its width is set by
    # ScriptManager: PICTURE_FRAME_SPRITE_WIDTH_16_9 while CommonUI ForceObi_16_9 is on, the 21:9 width when a
    # script contains [enableFullScreen] (EndFirstLoadAsset) or a frame asks for forceFullScreenSize.
    VN_PICTURE_FRAME_HEIGHT = 627
    VN_PICTURE_FRAME_WIDTH_16_9 = 1025
    VN_PICTURE_FRAME_WIDTH_21_9 = 1346

    def vn_picture_frame_script_width():
        return VN_PICTURE_FRAME_WIDTH_21_9 if renpy.store.vn_script_full_screen else VN_PICTURE_FRAME_WIDTH_16_9

    def vn_nodes_enable_full_screen(nodes):
        """EndFirstLoadAsset: ForceObi_16_9 = !scriptData.Contains("[enableFullScreen]") (anywhere, branches too)."""
        for node in nodes or []:
            if node.get("name") == "enableFullScreen":
                return True
            if any(str(v).split()[:1] == ["enableFullScreen"] for v in (node.get("tags") or {}).values()):
                return True
            for choice in node.get("choices") or []:
                if vn_nodes_enable_full_screen(choice.get("nodes")):
                    return True
        return False

    # Custom/Sprite-ScriptActionPictureFrame (GLES3 vertex program): uv.y * 0.6113 + 0.3887, so only the top
    # 61.13% of the texture is sampled (cut063_cinema: rows 0..626 of 1024) and stretched over the whole widget.
    VN_PICTURE_FRAME_V_SPAN = 0.6113

    def vn_picture_frame_crop(path):
        try:
            w, h = renpy.image_size(path)
        except Exception:
            return None
        return (0, 0, w, int(round(h * VN_PICTURE_FRAME_V_SPAN)))

    def vn_picture_frame_displayable(frame):
        child = frame["path"]
        if frame.get("crop"):
            child = Crop(frame["crop"], child)
        return Transform(child, xysize=(int(round(renpy.store.vn_pictureframe_width * upscale_ratio)),
                                        int(round(VN_PICTURE_FRAME_HEIGHT * upscale_ratio))),
                         xanchor=0.5, xpos=0.5, yanchor=0.5, ypos=0.5)

    def vn_group_fs_pos(lname, tok):
        """Parse an x,y position and add the FS/FSSide horizontal edge offset (0 on a 16:9 surface)."""
        x, y = vn_parse_position(tok, (0.0, 0.0))
        return (x + vn_fs_command_offset_x(lname), y)

    def vn_group_members(gkey):
        defs = renpy.store.current_chara_defs
        slots = [s for s, d in defs.items() if d.get("sub_group") == gkey and vn_slot_is_drawn(d)]
        return sorted(slots, key=lambda s: (vn_effective_depth(defs[s]), s))

    def vn_active_groups():
        subs = renpy.store.vn_sublayers or {}
        return [g for g in subs if subs[g].get("active") and vn_fx_stage_items(g)]

    def vn_stage_items():
        """The main scene is one RT at depth zero; sublayer quads are its siblings."""
        items = [(0, "main", "")]
        for gkey in vn_active_groups():
            g = renpy.store.vn_sublayers.get(gkey) or {}
            items.append((int(g.get("depth", 1)), "grp", gkey))
        items.sort(key=lambda t: (t[0], t[2]))
        return [(kind, key) for _, kind, key in items]

    def vn_group_geometry(g, now=None):
        """Prefab affine hierarchy in NGUI coordinates, evaluated without mutating scene state."""
        if now is None:
            now = vn_now()
        running = False
        def value(name, default):
            nonlocal running
            if g.get(name + "_anim"):
                v, r = vn_anim_value(g[name + "_anim"], now)
                running = running or r
                return v
            return g.get(name, default)
        p = value("pos", (0., 0.))
        scale = value("scale", 1.)
        cp = value("cam_pos", (0., 0.))
        cs = value("cam_scale", 1.)
        roll = value("roll", 0.)
        alpha = value("alpha", 0.)
        sx = sy = 0.
        sh = g.get("shake")
        if sh and now < sh["end"]:
            # One repeatable random sample per cycle, independent of redraw frequency.
            tick = int(max(0., now - sh["start"]) / sh["cycle"])
            import random
            rng = random.Random(str(sh["start"]) + ":" + str(tick))
            sx = rng.uniform(-sh["x"], sh["x"]); sy = rng.uniform(-sh["y"], sh["y"])
            running = True
        # Mesh local offset(-.5,-.5) is scaled by RenderRoot, then rotated about the rig pivot.
        px, py = g.get("roll_pivot", (0., 0.))
        x = p[0] + scale * (sx - .5)
        y = p[1] + scale * (sy - .5)
        angle = math.radians(roll)
        co, si = math.cos(angle), math.sin(angle)
        x, y = co * (x + px) - si * (y + py) - px, si * (x + px) + co * (y + py) - py
        return (cs * (x + cp[0]), cs * (y + cp[1]), scale * cs, roll, alpha, running)

    def vn_group_transform(gkey):
        """Transform the masked quad through the independent renderRoot and camera rig."""
        def fn(trans, st, at):
            g = renpy.store.vn_sublayers.get(gkey) or {}
            x, y, scale, roll, alpha, running = vn_group_geometry(g)
            trans.xanchor = .5; trans.yanchor = .5
            trans.xpos = 512; trans.ypos = 288
            trans.zoom = float(scale)
            trans.rotate = -float(roll)  # Unity +Z counterclockwise; screen y points down.
            trans.xoffset = x; trans.yoffset = -y
            trans.subpixel = True
            trans.alpha = max(0., min(1., alpha))
            return 0 if running else None
        return Transform(function=fn)

    def vn_group_capture_transform(gkey):
        def fn(trans, st, at):
            g = renpy.store.vn_sublayers.get(gkey) or {}
            stretch, r1 = (vn_anim_value(g["stretch_anim"]) if g.get("stretch_anim") else (g.get("stretch", 1.), False))
            blur, r2 = (vn_anim_value(g["blur_anim"]) if g.get("blur_anim") else (g.get("blur", 0.), False))
            blur2, r3 = (vn_anim_value(g["blur2_anim"]) if g.get("blur2_anim") else (g.get("blur2", 0.), False))
            trans.xanchor = .5; trans.yanchor = .5; trans.xpos = .5; trans.ypos = .5
            trans.zoom = float(stretch) if renpy.store.vn_substretch_enabled else 1.0
            # Approximation of the unavailable lens shader, applied BEFORE the mask/quad.
            trans.blur = math.hypot(max(0., blur), max(0., blur2))
            return 0 if r1 or r2 or r3 else None
        return Transform(function=fn)

    _vn_edge_cache = {}

    def vn_mask_edge_image(mask_path, raw_args):
        """Approximate an inner mask-space edge; the exported FGO shader is a stub.

        raw_args are retained on the group. Width is treated as mask texels here,
        not as a verified interpretation of the native _FilterParam matrix.
        """
        if len(raw_args) < 3:
            return None
        try:
            radius = max(1, min(64, int(round(float(raw_args[1])))))
            rgba = tuple(max(0, min(255, int(x))) for x in raw_args[2].split(","))
            if len(rgba) != 4 or rgba[3] == 0:
                return None
            key = (mask_path, radius, rgba)
            if key in _vn_edge_cache:
                return _vn_edge_cache[key]
            source = renpy.load_surface(mask_path)
            width, height = source.get_size()
            # The mask is opaque RGBA; its red channel holds the cut-in silhouette.
            red = bytearray(width * height)
            for y in range(height):
                row = y * width
                for x in range(width):
                    red[row + x] = source.get_at((x, y))[0]
            pixels = bytearray(width * height * 4)
            for y in range(height):
                row = y * width
                up = max(0, y - radius) * width
                down = min(height - 1, y + radius) * width
                for x in range(width):
                    i = row + x
                    center = red[i]
                    if not center:
                        continue
                    low = min(red[row + max(0, x - radius)],
                              red[row + min(width - 1, x + radius)],
                              red[up + x], red[down + x])
                    a = int((center - low) * rgba[3] / 255.0)
                    if a > 0:
                        j = i * 4
                        pixels[j:j + 4] = bytes((rgba[0], rgba[1], rgba[2], a))
            import hashlib, struct, zlib
            fingerprint = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:24]
            directory = os.path.join(renpy.config.gamedir, "cache", "derived_masks")
            os.makedirs(directory, exist_ok=True)
            output = os.path.join(directory, fingerprint + ".png")
            if not os.path.exists(output):
                def chunk(kind, value):
                    body = kind + value
                    return struct.pack(">I", len(value)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
                scanlines = b"".join(b"\0" + pixels[y * width * 4:(y + 1) * width * 4] for y in range(height))
                png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                       + chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))
                with open(output, "wb") as fp:
                    fp.write(png)
            result = output.replace("\\", "/")
            _vn_edge_cache[key] = result
            return result
        except Exception:
            return None

    def vn_group_texture(gkey, ancestors=()):
        """Capture member sprites once in world units, resample to RT, filter/mask in UV space."""
        if gkey in ancestors:  # malformed cyclic group masks cannot recurse forever
            return Solid("#0000", xysize=(VN_SUB_TEXTURE, VN_SUB_TEXTURE))
        g = renpy.store.vn_sublayers.get(gkey)
        if not g:
            return Solid("#0000", xysize=(VN_SUB_TEXTURE, VN_SUB_TEXTURE))
        children = []
        for kind, slot in vn_fx_stage_items(gkey):
            if kind == "fx":
                children.append(slot["display"])
                continue
            if vn_slot_back_visible(slot):
                children.append(At(vn_slot_back_image(slot), vn_slot_transform(slot, capture=True, back=True)))
            children.append(At(vn_slot_image(slot), vn_slot_transform(slot, capture=True)))
        content = Fixed(*children, xysize=(int(VN_SUB_CAPTURE), int(VN_SUB_CAPTURE)), fit_first=False)
        # A containing Fixed resolves the stretch's center anchor before the crop; directly
        # wrapping a zoomed child in a crop would sample its upper-left instead of its center.
        content = Fixed(At(content, vn_group_capture_transform(gkey)),
                        xysize=(int(VN_SUB_CAPTURE), int(VN_SUB_CAPTURE)), fit_first=False)
        # Fixed does not clip overflowing children. Explicit crop models the finite camera target.
        content = Transform(content, crop=(0, 0, int(VN_SUB_CAPTURE), int(VN_SUB_CAPTURE)), xysize=(VN_SUB_TEXTURE, VN_SUB_TEXTURE))
        filt = (g.get("filter") or "normal").lower()
        color_matrix = TintMatrix("#ffffff")
        if "gray" in filt:
            color_matrix = SaturationMatrix(0.0)
        elif filt == "inversion":
            color_matrix = InvertMatrix(1.0)
        # mask&darkred retains its mask, but its unavailable shader tint is not guessed.
        # maskEdge's raw edge/color/flag arguments remain in filter_args.
        # Keep this transform present even for through/normal: Ren'Py transfers transform
        # state across screen replacements, so omitting it can leave an old gray filter active.
        content = Transform(content, matrixcolor=color_matrix)
        if g.get("mask_path"):
            # Source images have opaque alpha and encode visibility in red (not alpha).
            mask_image = im.MatrixColor(g["mask_path"], im.matrix(
                1, 0, 0, 0, 0,
                0, 1, 0, 0, 0,
                0, 0, 1, 0, 0,
                1, 0, 0, 0, 0,
                0, 0, 0, 0, 1))
            mask = Transform(mask_image, xysize=(VN_SUB_TEXTURE, VN_SUB_TEXTURE))
            content = AlphaMask(content, mask)
            if "maskedge" in filt:
                edge_path = vn_mask_edge_image(g["mask_path"], g.get("filter_args") or [])
                if edge_path:
                    content = Fixed(content, edge_path, xysize=(VN_SUB_TEXTURE, VN_SUB_TEXTURE), fit_first=False)
        elif g.get("mask_group"):
            mask = vn_group_texture(g["mask_group"], ancestors + (gkey,))
            content = AlphaMask(content, mask)
        return content

    def vn_group_image(gkey):
        return Transform(vn_group_texture(gkey), xysize=(VN_SUB_QUAD, VN_SUB_QUAD))

    # ------------------------------------------------------------------
    # Sound effects
    # ------------------------------------------------------------------

    _vn_se_channels = {}

    def vn_se_play(name, api, loop=False, sheet=None):
        """[se <name>] / [seLoop <name>] / [cueSe <sheet> <cue>]"""
        path = api.get_se_path(name, sheet=sheet)
        if not path or not renpy.loadable(path):
            return
        chan = _vn_se_channels.get(name)
        if chan is None:
            used = set(_vn_se_channels.values())
            free = [c for c in range(6) if "se%d" % c not in used]
            chan = "se%d" % (free[0] if free else 0)
            _vn_se_channels[name] = chan
        renpy.music.play(path, channel=chan, loop=loop)

    def vn_se_stop(name=None, fadeout=0.0):
        """[seStop <name> <t>] / [cueSeStop <name> <t>]"""
        if name is None:
            vn_se_stop_all(fadeout)
            return
        chan = _vn_se_channels.pop(name, None)
        if chan:
            renpy.music.stop(channel=chan, fadeout=float(fadeout))

    def vn_se_stop_all(fadeout=0.0):
        for chan in list(_vn_se_channels.values()):
            renpy.music.stop(channel=chan, fadeout=float(fadeout))
        _vn_se_channels.clear()

    def vn_sound_stop_all(fadeout=0.0):
        """[soundStopAll] / [soundStopAllFade <t>] / [soundStopAllEnd]: BGM and SE. Voices keep playing
        (SoundStopAll is BgmManager.StopAll + stopSe; it never touches the voice players)."""
        vn_se_stop_all(fadeout)
        renpy.music.stop(channel="music", fadeout=float(fadeout))
        renpy.store.vn_bgm_name = None

    # ------------------------------------------------------------------
    # Voices ([voice NAME (VOL)], [voiceStop (NAME (T))], [wait voice NAME])
    # ------------------------------------------------------------------
    # ScriptCommandExecute "voice": skipped entirely unless IsNormalPlaySpeed (nothing plays while
    # skipping); SoundManager.playCharaVoice(ConvertNameString(NAME)[, VOL]). The 2nd token is a volume,
    # not a fade. SeManager restarts a player only when the same sheet+cue is already playing; any other
    # voice overlaps. No BGM ducking. "voiceStop": no token = stopVoiceAll(0); NAME = stopVoice(NAME, 0);
    # NAME T = linear fade over T (T <= 0 is instant). Advancing past a message's [k] (tap or auto)
    # runs stopVoiceAll(0), so a voice never outlives its line; auto mode holds until the voice ends.

    _vn_voice_channels = {}                          # channel -> (sheet, cue)

    def vn_voice_key(name):
        return AtlasAPI.voice_cue(name)

    def vn_voice_play(name, api, volume=None):
        if vn_skipping():
            return
        key = vn_voice_key(name)
        if not key:
            return
        path = api.get_voice_path(name)
        if not path or not renpy.loadable(path):
            return
        chans = ["voice%d" % i for i in range(VN_VOICE_CHANNELS)]
        chan = next((c for c in chans if _vn_voice_channels.get(c) == key), None)   # same cue restarts
        if chan is None:
            chan = next((c for c in chans if renpy.music.get_playing(channel=c) is None), None)
        if chan is None:
            chan = chans[0]
        _vn_voice_channels[chan] = key
        kwargs = {}
        if volume is not None:
            kwargs["relative_volume"] = max(0.0, min(1.0, float(volume)))
        renpy.music.play(path, channel=chan, loop=False, **kwargs)

    def vn_voice_channels_for(name=None):
        if name is None:
            return list(_vn_voice_channels)
        key = vn_voice_key(name)
        return [c for c, k in _vn_voice_channels.items() if k == key]

    def vn_voice_stop(name=None, fadeout=0.0):
        fade = max(0.0, float(fadeout or 0.0))
        for chan in vn_voice_channels_for(name):
            renpy.music.stop(channel=chan, fadeout=fade)
            _vn_voice_channels.pop(chan, None)

    def vn_voice_busy(name=None):
        """isBusyVoice(NAME): is that cue (or, with no name, any voice) still playing?"""
        busy = False
        for chan in vn_voice_channels_for(name):
            if renpy.music.get_playing(channel=chan) is not None:
                busy = True
            else:
                _vn_voice_channels.pop(chan, None)
        return busy

    def vn_voice_wait(name=None):
        """[wait voice NAME]: hold while the cue plays (a voice that never started does not wait)."""
        while vn_voice_busy(name):
            if vn_skipping():
                vn_voice_stop()
                return
            renpy.pause(0.05, hard=True)

    def vn_voice_command(lname, args, api):
        if lname == "voice":
            if args:
                vol = None
                if len(args) >= 2:
                    try:
                        vol = float(args[1])
                    except Exception:
                        return True                      # float.Parse throws: the command is dropped
                vn_voice_play(args[0], api, vol)
            return True
        # voicestop
        if not args:
            vn_voice_stop()
        else:
            vn_voice_stop(args[0], vn_float(args[1], 0.0) if len(args) >= 2 else 0.0)
        return True

    # ------------------------------------------------------------------
    # Music
    # ------------------------------------------------------------------

    def vn_bgm_play_flag(parts, api):
        """[bgm NAME (FADE (VOLUME))]: e.g. [bgm X 0.8 0.5] ... [bgm X 0.8 1.0] dips the playing track to
        half volume and back over 0.8 s (a re-issue of the playing BGM never restarts it, see
        vn_apply_music_flag)."""
        path = api.get_bgm_path(parts[1])
        if not path:
            return None
        try:
            fadein = float(parts[2]) if len(parts) >= 3 else 0.2
        except Exception:
            fadein = 0.2
        try:
            volume = float(parts[3]) if len(parts) >= 4 else 1.0
        except Exception:
            volume = 1.0
        return {"action": "play", "path": path.replace("\\", "/"), "fade": fadein, "volume": volume, "name": parts[1]}

    def get_scene_music_flag(node, api):
        if node.get("type") == "bgm":
            parts = (node.get("tags") or {}).get("bgm", "").split()
            if len(parts) >= 2:
                return vn_bgm_play_flag(parts, api)
        if node.get("type") == "audio_stop":
            token = next(iter((node.get("tags") or {}).values()), "")
            parts = token.split()
            command = parts[0].lower() if parts else ""
            if command in ("bgmstop", "bgmstopend"):
                return vn_bgm_stop_flag(parts)
            try:
                fadeout = float(parts[-1]) if len(parts) >= 2 else 0.2
            except Exception:
                fadeout = 0.2
            flag = {"action": "stop", "fade": fadeout}
            if command == "soundstopall":
                flag["stop_se"] = True
            return flag

        # Music flags are embedded in scene text, so resolve them before the line is shown.
        for key, token in (node.get("tags") or {}).items():
            parts = (token or key).split()
            command = parts[0].lower() if parts else ""
            if command == "bgm" and len(parts) >= 2:
                flag = vn_bgm_play_flag(parts, api)
                if flag:
                    return flag
            elif command in ("bgmstop", "bgmstopend"):
                return vn_bgm_stop_flag(parts)
            elif command == "soundstopall":
                try:
                    fadeout = float(parts[2]) if len(parts) >= 3 else 0.2
                except Exception:
                    fadeout = 0.2
                flag = {"action": "stop", "fade": fadeout}
                if command == "soundstopall":
                    flag["stop_se"] = True
                return flag
        return None

    def vn_bgm_stop_flag(parts):
        """[bgmStop (NAME (T))] / [bgmStopEnd NAME T]: one ScriptCommandExecute branch. bgmStopEnd is bgmStop
        unless the quest phase has nonStopEndBgm (a server script param Atlas does not expose; the default,
        false, is used). BgmManager.StopBgm(NAME, T) acts only on a player that is playing NAME (no NAME =
        all), fading linearly over T (default 0), starting at once; the script does not wait for it."""
        name = parts[1] if len(parts) >= 2 else None
        try:
            fadeout = float(parts[2]) if len(parts) >= 3 else 0.0
        except Exception:
            return None                                      # float.Parse throws: nothing stops
        return {"action": "stop", "fade": fadeout, "name": name}

    def vn_apply_music_flag(flag):
        if not flag:
            return
        if flag.get("action") == "play" and flag.get("path"):
            volume = max(0.0, min(1.0, float(flag.get("volume", 1.0))))
            fade = max(0.0, float(flag.get("fade", 0.2)))
            playing = renpy.music.get_playing(channel="music") is not None
            if playing and flag.get("name") == renpy.store.vn_bgm_name:
                # BgmPlayerBase.PlayBgm: the playing BGM keeps its position; only ChangeVolume(volume, fade).
                renpy.music.set_volume(volume, delay=fade, channel="music")
                return
            renpy.music.set_volume(volume, delay=0, channel="music")
            renpy.music.play(flag.get("path"), fadein=fade)
            renpy.store.vn_bgm_name = flag.get("name")
        elif flag.get("action") == "stop":
            fade = flag.get("fade", 0.2)
            name = flag.get("name")
            if name and name != renpy.store.vn_bgm_name:
                return                                       # StopBgm: no player is playing NAME
            if flag.get("stop_se"):
                vn_se_stop_all(fade)
            renpy.music.stop(fadeout=fade)
            renpy.store.vn_bgm_name = None

    # ------------------------------------------------------------------
    # Script command execution
    # ------------------------------------------------------------------

    def vn_slot_args(node, key):
        """Raw args of the command that produced a chara_* node (the parser keeps the token)."""
        token = (node.get("tags") or {}).get(key, "")
        parts = token.split()
        return parts[1:]

    def apply_reader_node(node, api):
        node_type = node.get("type")
        defs = renpy.store.current_chara_defs

        if node_type == "scene":
            # A criMovie is replaced by the following still scene in this port. Once that scene is
            # available, ordinary scripted pacing applies again.
            renpy.store.vn_unrendered_movie = False
            renpy.store.vn_unrendered_wait = False
            parts = (node.get("tags") or {}).get("scene", "").split()
            vn_scene_change(api, node.get("scene_id"), parts[2] if len(parts) >= 3 else 0.0)
            return True

        if node_type == "chara_set":
            slot = node.get("slot")
            if slot:
                char_path = api.get_character_path(node.get("chara_id"))
                offsets = api.get_chara_script_offsets(node.get("chara_id")) or {}
                data = vn_slot_default()
                data.update({
                    "id": node.get("chara_id"),
                    "name": node.get("name"),
                    "form": node.get("form"),           # FORM/limitCount (kept for future form-aware sheet selection)
                    # No pos here: charaSet defines the slot; charaFadein/charaPut place it. (The old code read a
                    # "position" that was really FORM, so charaSet with FORM 2 wrongly parked the sprite right-of-centre.)
                    "path": char_path.replace("\\", "/") if char_path else None,
                    "face_x": offsets.get("face_x") or 0,
                    "face_y": offsets.get("face_y") or 0,
                    "face_size": offsets.get("face_size") or 256,
                    "offset_x": offsets.get("offset_x") or 0,
                    "offset_y": offsets.get("offset_y") or 0,
                    "scale": offsets.get("scale") or 1.0,
                })
                # SetCharacter resets the talk mask: a freshly set character is bright until the next ＠.
                defs[slot] = data
            return True

        if node_type == "chara_face":
            slot = node.get("slot")
            if slot and slot in defs:
                defs[slot]["face"] = node.get("face") or "0"     # instant UV swap
            return True

        if node_type == "chara_show":
            slot = node.get("slot")
            if slot and slot in defs:
                args = vn_slot_args(node, "charaFadein")
                duration = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
                data = defs[slot]
                if len(args) >= 3:
                    data["pos"] = vn_parse_position(args[2], data.get("pos", (0.0, 0.0)))
                    data["pos_anim"] = None
                vn_chara_end_crossfade(data)     # UIScriptFigure.MoveAlpha ends a charaFaceFade
                current = vn_slot_alpha(data)
                data["alpha_anim"] = vn_anim(current, 1.0, duration)
                data["alpha"] = 1.0
                vn_mark_busy("charaFade", duration)
            return True

        if node_type == "chara_hide":
            slot = node.get("slot")
            if slot and slot in defs:
                args = vn_slot_args(node, "charaFadeout")
                duration = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
                data = defs[slot]
                vn_chara_end_crossfade(data)     # UIScriptFigure.MoveAlpha ends a charaFaceFade
                current = vn_slot_alpha(data)
                data["alpha_anim"] = vn_anim(current, 0.0, duration)
                data["alpha"] = 0.0
                vn_mark_busy("charaFade", duration)
            return True

        if node_type == "chara_filter":
            slot = node.get("slot")
            chara_filter = str(node.get("filter") or "normal")
            if slot and slot in defs:
                defs[slot]["filter"] = chara_filter if chara_filter in ("silhouette",) else None
            return True

        if node_type in ("communication_chara", "communication_charaloop"):
            chara_id = node.get("chara_id")
            # In FGO, comm figures are separate prefabs (CommunicationCharaEffectComponent), NOT in
            # charaList — so they never share a slot with a standing figure.  Always use a private slot
            # to avoid hijacking an existing charaSet slot's alpha/position/face.
            slot = "comm:" + str(chara_id)
            if slot not in defs:
                char_path = api.get_character_path(chara_id)
                offsets = api.get_chara_script_offsets(chara_id) or {}
                data = vn_slot_default()
                data.update({
                    "id": chara_id, "name": "",
                    "path": char_path.replace("\\", "/") if char_path else None,
                    "face_x": offsets.get("face_x") or 0, "face_y": offsets.get("face_y") or 0,
                    "face_size": offsets.get("face_size") or 256,
                    "offset_x": offsets.get("offset_x") or 0, "offset_y": 0,
                    "scale": offsets.get("scale") or 1.0,
                })
                defs[slot] = data
            data = defs[slot]
            data["communication_chara"] = True
            # communicationCharaLoop -> isStartLoop (base anim loops); one-shot otherwise. Recorded for parity.
            data["comm_loop"] = (node_type == "communication_charaloop")
            data["comm_noise_kind"] = node.get("noisekind")
            data["pos"] = vn_parse_position(node.get("position"), (0.0, 0.0))
            data["pos_anim"] = None
            # DEPTH arg: z = -d*10 in FGO (higher DEPTH drawn in front). Comm figures are never talk-dimmed.
            try:
                data["depth"] = int(float(node.get("depth")))
            except Exception:
                data["depth"] = data.get("depth", 0)
            data["face"] = node.get("face") or "0"
            data["offset_y"] = 0
            data["alpha"] = 1.0
            data["alpha_anim"] = None
            data["comm_start"] = vn_now()               # anchors the 2.0 s noise heartbeat
            return True

        if node_type == "communication_charaface":
            for slot_temp, data in sorted(defs.items()):
                if data.get("communication_chara"):
                    data["face"] = node.get("face") or "0"
                    data["alpha"] = 1.0
                    data["alpha_anim"] = None
            return True

        if node_type == "communication_characlear":
            for slot_temp, data in sorted(defs.items()):
                if data.get("communication_chara"):
                    data["alpha"] = 0.0
                    data["alpha_anim"] = None
            return True

        if node_type == "command":
            return vn_execute_command(node, api)

        if node_type == "metadata" and node.get("inline_cmds"):
            # A tag-only line ([image x][messageShake ...][se y]) has no text to type, so it runs now.
            for _, token in node["inline_cmds"]:
                parts = token.split()
                if parts[0].lower() == "wt":
                    vn_execute_command({"name": "wt", "args": parts[1:]}, api)   # in order, a plain wait
                else:
                    vn_msg_shake_command(token)
            return True

        return False

    def vn_execute_command(node, api):
        name = str(node.get("name") or "")
        lname = name.lower()
        args = list(node.get("args") or [])
        defs = renpy.store.current_chara_defs

        if vn_fx_command_kind(lname):
            return vn_fx_command(lname, args)

        if vn_special_command(lname, args):
            return True

        if lname in ("imageset", "horizontalimageset", "verticalimageset") and len(args) >= 2:
            slot, image_name = args[:2]
            # ScriptManager makes a new ScriptCharaData for this letter. Like
            # charaSet, the image starts hidden until charaFadein (or another
            # alpha command) shows it. Reusing the slot must discard old state.
            path = api.get_script_image_path(image_name)
            if not path and image_name != "00001":
                # UIImageRender.GetAssetNameList substitutes Image/00001 when
                # the requested asset is absent from AssetStorage.
                path = api.get_script_image_path("00001")
            variant = ("horizontal" if lname == "horizontalimageset" else
                       "vertical" if lname == "verticalimageset" else "image")
            # The second quad, when present, samples the optional <name>a
            # texture. Atlas does not provide one for many scripts, so None
            # leaves that quad transparent as FGO's renderer does.
            second_path = api.get_script_image_path(image_name + "a") if variant != "image" else None
            data = vn_slot_default()
            data.update({"kind": "script_image", "id": image_name, "name": "",
                         "path": path.replace("\\", "/") if path else None,
                         "second_path": second_path.replace("\\", "/") if second_path else None,
                         "image_variant": variant,
                         "pos": vn_parse_position(args[2], (0.0, 0.0)) if len(args) >= 3 else (0.0, 0.0)})
            defs[slot] = data
            return True

        if lname == "sceneset" and len(args) >= 2:
            slot, image_id = args[:2]
            # sceneSet's numeric ID is converted to Back/back<ID> by ScriptManager.
            path = api.get_background_path(image_id)
            data = vn_slot_default()
            data.update({"kind": "image", "id": image_id, "name": "",
                         "path": path.replace("\\", "/") if path else None,
                         "scene_args": list(args[2:])})
            if path:
                try:
                    data["image_size"] = renpy.load_surface(data["path"]).get_size()
                except Exception:
                    data["image_size"] = (1024, 626)
            defs[slot] = data
            return True

        if lname == "charatalk":
            vn_chara_talk(args[0] if args else "")
            return True
        # vn_chara_change.rpy (names are case-sensitive in ScriptCommandExecute's switch).
        if name == "charaFadeTime":
            return vn_chara_fade_time(args, defs)
        if name == "charaShadow":
            return vn_chara_shadow(args, defs)
        if name == "charaFaceFade":
            return vn_chara_face_fade(args, defs)
        if name == "imageChange":
            return vn_image_change(args, defs, api)
        if name == "charaChange":
            return vn_chara_figure_change(args[:2] + args[2:3] + args[4:5], defs, api,
                                          args[3] if len(args) >= 5 else "")
        if name == "charaCrossFade":
            return vn_chara_figure_change(args, defs, api, "crossFade")
        if lname == "messageoff":
            vn_message_off()
            return True
        if lname == "messageon":
            renpy.store.vn_msg_visible = True
            return True
        if lname == "skip":
            # Script-level skip gates protect cinematics and stateful transitions. This is permission
            # to fast-forward, not a request to toggle the user's Tab setting.
            renpy.store.vn_skip_allowed = not args or str(args[0]).lower() not in ("false", "0", "off")
            return True
        if lname == "wt":
            # Native scripts often use wt solely to let an SE, shake, effect, or movie finish. A
            # literal pause becomes dead air when that command has no renderer in this port.
            if renpy.store.vn_unrendered_movie or renpy.store.vn_unrendered_wait:
                renpy.store.vn_unrendered_wait = False
            else:
                vn_wait_time(args[0] if args else 0)
            return True
        if lname == "wait":
            tag = args[0] if args else ""
            if vn_special_wait(tag, args[1:]):
                return True
            if vn_fx_wait(tag, args[1:]):
                return True
            if tag == "time":
                vn_wait_time(args[1] if len(args) >= 2 else 0)
            elif tag.startswith("sub") or tag in ("fade", "scene", "mask", "wipe", "charaFade", "charaMove", "charaScale", "camera", "charaSpecialEffect"):
                vn_wait_for(tag, args[1] if len(args) > 1 else None)
                if tag == "charaFade":
                    # IsMoveAlpha also covers charaFadeTime / imageChange fades (isWaitTalk=false).
                    vn_wait_for("charaFadeNoTalk")
            elif tag in ("charaChange", "charaCrossFade"):
                vn_wait_chara_change(args[1] if len(args) > 1 else None)
            elif tag in ("blur", "blurOff"):
                vn_blur_wait()
            elif tag == "flash":
                vn_flash_wait()
            elif tag == "voice":
                vn_voice_wait(args[1] if len(args) > 1 else None)
            elif tag in ("charaRollAxis", "charaRollAxisLoopStop", "charaRollMove", "charaRollMoveEx",
                         "charaRelativeLoopMove", "charaRelativeLoopMoveEase"):
                vn_wait_chara_roll(tag, args[1] if len(args) > 1 else None)
            # effect waits not replicated; se waits skipped (audio finishes on its own).
            return True
        if lname == "fadein":
            vn_start_fade(args[0] if args else "black", True, args[1] if len(args) >= 2 else VN_DEFAULT_FADE_TIME)
            return True
        if lname == "fadeout":
            vn_start_fade(args[0] if args else "black", False, args[1] if len(args) >= 2 else VN_DEFAULT_FADE_TIME)
            return True
        if lname == "maskin":
            vn_start_mask(args[0] if args else "black", True, args[1] if len(args) >= 2 else VN_DEFAULT_FADE_TIME)
            return True
        if lname == "maskout":
            vn_start_mask(args[0] if args else "black", False, args[1] if len(args) >= 2 else VN_DEFAULT_FADE_TIME)
            return True
        if lname in ("wipeout", "wipein"):
            return vn_wipe_command(lname, args)        # vn_wipe.rpy
        if lname in VN_ROLL_COMMANDS:
            return vn_chara_roll_command(lname, args, defs)
        if lname in VN_CHARA_PUT_COMMANDS and len(args) >= 2 and args[0] in defs:
            # charaPut{,FSL,FSR,FSLNotNotch,FSRNotNotch,FSSideL,FSSideR} share one ScriptCommandExecute case:
            # 'x,y' or a slot index, plus GetFSCommandOffsetX(tag, "charaPut") on x, then
            # ScriptCharaData.SetPosition. The FS offset is 0 on a 16:9 surface.
            data = defs[args[0]]
            x, y = vn_parse_position(args[1], data.get("pos", (0.0, 0.0)))
            data["pos"] = (x + vn_fs_command_offset_x(lname), y)
            data["pos_anim"] = None
            # Alpha is intentionally untouched: charaPut is instant repositioning.
            # Characters not yet shown (via charaFadein / charaSpecialEffect) stay
            # hidden, preventing ghost sprites during unrendered effect sequences.
            return True
        if lname == "charamove" and len(args) >= 2 and args[0] in defs:
            data = defs[args[0]]
            target = vn_parse_position(args[1], data.get("pos", (0.0, 0.0)))
            duration = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            current = vn_anim_current(data.get("pos_anim"), data.get("pos", (0.0, 0.0)))
            data["pos_anim"] = vn_anim(tuple(current), tuple(target), duration)
            data["pos"] = tuple(target)
            vn_mark_busy("charaMove", duration)
            return True
        if lname in ("charamovereturn", "charamovereturnease") and len(args) >= 2 and args[0] in defs:
            # MoveReturnPosition: TweenPosition to absolute target over TIME/2, then back to base over TIME/2.
            data = defs[args[0]]
            target = vn_parse_position(args[1], data.get("pos", (0.0, 0.0)))
            duration = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            base = tuple(vn_anim_current(data.get("pos_anim"), data.get("pos", (0.0, 0.0))))
            now = vn_now()
            data["pos_anim"] = vn_anim_keys([(now, base), (now + duration / 2.0, tuple(target)), (now + duration, base)])
            data["pos"] = base
            vn_mark_busy("charaMove", duration)
            return True
        if lname == "charamoveease" and len(args) >= 2 and args[0] in defs:
            # iTween move with an easetype; approximated as linear (charaMove).
            return vn_execute_command({"name": "charaMove", "args": args[:3]}, api)
        if lname == "camerahome":
            vn_camera_move([args[0] if args else 0.0, "0,0", "1.0"])
            return True
        if lname in ("cameramove", "cameramoveease"):
            vn_camera_move(args)
            return True
        if lname == "charascale" and len(args) >= 2 and args[0] in defs:
            data = defs[args[0]]
            data["scale"] = vn_float(args[1], 1.0)
            data["scale_anim"] = None
            return True
        if lname == "charamovescale" and len(args) >= 2 and args[0] in defs:
            data = defs[args[0]]
            target = vn_float(args[1], 1.0)
            duration = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            current = vn_anim_current(data.get("scale_anim"), data.get("scale", 1.0))
            data["scale_anim"] = vn_anim(float(current), target, duration)
            data["scale"] = target
            vn_mark_busy("charaScale", duration)
            return True
        if lname == "charadepth" and len(args) >= 2 and args[0] in defs:
            try:
                defs[args[0]]["depth"] = int(float(args[1]))
            except Exception:
                pass
            return True
        if lname == "characlear" and args and args[0] in defs:
            del defs[args[0]]
            return True
        if lname in ("charafadeinfsl", "charafadeinfsr") and args and args[0] in defs:
            data = defs[args[0]]
            duration = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
            if len(args) >= 3:
                data["pos"] = vn_parse_position(args[2], data.get("pos", (0.0, 0.0)))
                data["pos_anim"] = None
            vn_chara_end_crossfade(data)
            data["alpha_anim"] = vn_anim(vn_slot_alpha(data), 1.0, duration)
            data["alpha"] = 1.0
            vn_mark_busy("charaFade", duration)
            return True

        # Sub-camera allocation and assignment are separate from quad visibility.
        if lname == "charalayer":
            if args and args[0] in defs:
                if len(args) > 1 and args[1].lower() == "sub":
                    key = vn_group_key(args[2] if len(args) > 2 else "#A")
                    defs[args[0]]["sub_group"] = key
                    vn_sublayer(key)
                else:
                    defs[args[0]]["sub_group"] = None
            return True
        if lname == "subcameraon":
            if not isinstance(renpy.store.vn_sublayers, dict):
                renpy.store.vn_sublayers = {}
            return True
        if lname == "subcameraoff":
            for d in defs.values():
                if d.get("sub_group") is not None:
                    # Group members are capture sources, not independently visible
                    # stage figures. ClearSubLayer must not expose an opaque source
                    # after its quad has faded out.
                    d["alpha"] = 0.0
                    d["alpha_anim"] = None
                    d["sub_group"] = None
            renpy.store.vn_sublayers = {}
            return True
        if lname == "substretch":
            renpy.store.vn_substretch_enabled = bool(args and args[0].lower() == "on")
            return True
        if lname.startswith(("subrender", "subcamera", "subblur", "substretch")):
            # The group is optional in scripts; only an explicit #name consumes it.
            a = list(args)
            key = vn_group_key(a.pop(0)) if a and str(a[0]).startswith("#") else "#A"
            g = vn_sublayer(key)
            def busy(kind, duration):
                vn_sub_busy(g, kind, duration, lname)
            def tween(field, target, duration, ease=None):
                current = vn_anim_current(g.get(field + "_anim"), g.get(field, target))
                g[field + "_anim"] = vn_anim(current, target, duration, ease)
                g[field] = target
            if lname == "subcamerafilter":
                mode = a[0].lower() if a else "normal"
                g["filter"] = mode
                g["mask_name"] = None; g["mask_path"] = None; g["mask_group"] = None
                g["filter_args"] = list(a[1:])
                if mode.startswith("mask") and len(a) > 1:
                    g["mask_name"] = a[1]
                    if str(a[1]).startswith("#"):
                        g["mask_group"] = vn_group_key(a[1])
                    else:
                        try:
                            p = api.get_script_image_path(a[1])
                            g["mask_path"] = p.replace("\\", "/") if p else None
                        except Exception:
                            g["mask_path"] = None
                return True
            if lname.startswith("subrenderfadein") or lname == "subrenderfadeout":
                duration = vn_float(a[0], 0.0) if a else 0.0
                # Optional position is an immediate placement, independent of alpha tween.
                if len(a) > 1:
                    g["pos"] = vn_group_fs_pos(lname, a[1]); g["pos_anim"] = None
                tween("alpha", 0.0 if lname == "subrenderfadeout" else 1.0, duration)
                busy("fade", duration)
                return True
            if lname.startswith("subrendermovescale"):
                target = vn_float(a[0], 1.0) if a else 1.0
                duration = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                tween("scale", target, duration, a[2] if "ease" in lname and len(a) > 2 else None)
                busy("scale", duration)
                return True
            if lname.startswith("subrendermove"):
                target = vn_group_fs_pos(lname, a[0]) if a else g.get("pos", (0.0, 0.0))
                duration = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                tween("pos", tuple(target), duration, a[2] if "ease" in lname and len(a) > 2 else None)
                busy("move", duration)
                return True
            if lname == "subrenderscale":
                g["scale"] = vn_float(a[0], 1.0) if a else 1.0; g["scale_anim"] = None
                busy("scale", 0.0)
                return True
            if lname == "subrenderdepth":
                if a:
                    g["depth"] = int(vn_float(a[0], 1.0))
                return True
            if lname in ("subcamerahome", "subcameramove", "subcameramoveease"):
                ease = None
                if lname == "subcamerahome":
                    duration = vn_float(a[0], 0.0) if a else 0.0
                    pos = (0.0, 0.0); scale = 1.0
                elif lname == "subcameramove":
                    duration = vn_float(a[0], 0.0) if a else 0.0
                    pos = vn_parse_position(a[1], (0.0, 0.0)) if len(a) > 1 else g.get("cam_pos", (0.0, 0.0))
                    scale = vn_float(a[2], 1.0) if len(a) > 2 else vn_anim_current(g.get("cam_scale_anim"), g.get("cam_scale", 1.0))
                else:
                    pos = vn_parse_position(a[0], (0.0, 0.0)) if a else g.get("cam_pos", (0.0, 0.0))
                    duration = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                    ease = a[2] if len(a) > 2 else None
                    scale = vn_float(a[3], 1.0) if len(a) > 3 else vn_anim_current(g.get("cam_scale_anim"), g.get("cam_scale", 1.0))
                tween("cam_pos", tuple(pos), duration, ease)
                tween("cam_scale", scale, duration, ease)
                busy("camera", duration)
                return True
            if lname == "subcameraroll":
                g["roll"] = vn_float(a[0], 0.0) if a else 0.0; g["roll_anim"] = None
                if len(a) > 1:
                    g["roll_pivot"] = vn_parse_position(a[1], (0.0, 0.0))
                busy("roll", 0.0)
                return True
            if lname == "subcamerarollmove":
                duration = vn_float(a[0], 0.0) if a else 0.0
                tween("roll", vn_float(a[1], 0.0) if len(a) > 1 else 0.0, duration)
                busy("roll", duration)
                return True
            if lname in ("subblur", "subblur2", "subbluroff", "subblur2off"):
                # Lens shader parameters are retained; Gaussian radius remains an approximation.
                duration = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                field = "blur2" if lname.startswith("subblur2") else "blur"
                g[field + "_args"] = list(a)
                tween(field, 0.0 if lname.endswith("off") else VN_SUBBLUR_RADIUS, duration)
                busy(field, duration)
                return True
            if lname == "subrendershake":
                cycle = max(0.001, vn_float(a[0], 0.05)) if a else 0.05
                ax = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                ay = vn_float(a[2], 0.0) if len(a) > 2 else 0.0
                duration = vn_float(a[3], 0.0) if len(a) > 3 else 0.0
                now = vn_now()
                g["shake"] = {"x": ax, "y": ay, "cycle": cycle, "start": now,
                              "end": now + duration if duration > 0.0 else float("inf")}
                busy("shake", duration)
                return True
            if lname == "subrendershakestop":
                g["shake"] = None; busy("shake", 0.0)
                return True
            if lname in ("substretchout", "substretchin"):
                # Full-mode stretch lives on a separate transform, not the quad scale.
                # Other directional modes are not used by the audited script corpus.
                mode = a[0].lower() if a else "full"
                duration = vn_float(a[1], 0.0) if len(a) > 1 else 0.0
                target = 1.0 if lname == "substretchin" else (vn_float(a[2], 1.0) if len(a) > 2 else 1.0)
                g["stretch_mode"] = mode
                if mode == "full":
                    if g.get("stretch_anim") and vn_anim_value(g["stretch_anim"])[1]:
                        return True  # ScriptSubLayer.IsExecuteStretch rejects overlapping starts.
                    tween("stretch", target, duration)
                    busy("stretch", duration)
                return True
            return False
        if lname == "enablefullscreen":
            return True
        if lname == "charashake" and len(args) >= 1 and args[0] in defs:
            data = defs[args[0]]
            cycle = max(0.001, vn_float(args[1], 0.05)) if len(args) >= 2 else 0.05
            ax = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            ay = vn_float(args[3], 0.0) if len(args) >= 4 else 0.0
            duration = vn_float(args[4], 0.0) if len(args) >= 5 else 0.0
            now = vn_now()
            data["shake"] = {"x": ax, "y": ay, "cycle": cycle, "start": now,
                             "end": now + duration if duration > 0.0 else float("inf")}
            vn_mark_busy("charaMove", duration)
            return True
        if lname == "charashakestop" and len(args) >= 1 and args[0] in defs:
            defs[args[0]]["shake"] = None
            return True

        if lname == "crimovie":
            # The next scene is the movie's static fallback. Do not retain the movie's 10–20 second
            # playback timing when no frames or audio are being presented.
            renpy.store.vn_unrendered_movie = True
            renpy.store.vn_unrendered_wait = True
            return True

        if lname == "shake":
            cycle = max(0.001, vn_float(args[0], 0.05)) if args else 0.05
            ax = vn_float(args[1], 0.0) if len(args) >= 2 else 0.0
            ay = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            duration = vn_float(args[3], 0.0) if len(args) >= 4 else 0.0
            now = vn_now()
            renpy.store.vn_shake = {"x": ax, "y": ay, "cycle": cycle, "start": now,
                                    "end": now + duration if duration > 0.0 else float("inf")}
            vn_mark_busy("shake", duration)
            return True
        if lname == "shakestop":
            renpy.store.vn_shake = None
            return True

        if lname in ("messageshake", "messageshakestop"):
            return vn_msg_shake_command([name] + args)

        if name in ("pictureFrame", "pictureFrameTop"):
            # One ScriptCommandExecute case. With ForceObi_16_9 (no [enableFullScreen] in the script) a
            # forceFullScreenSize token widens the sprite to the 21:9 width once (isPictureFrameForceChangeSize).
            # No name: undo that widening, then ClearPictureFrameImage. Else SetPictureFrameImage(name,
            # isTop = "pictureFrameTop"): both load into the single PictureFrame sprite, isTop only moves it to
            # PICTURE_FRAME_Z_POS_TOP, so the last one wins.
            store = renpy.store
            if (not store.vn_script_full_screen and "forceFullScreenSize" in args
                    and not store.vn_pictureframe_force_size):
                store.vn_pictureframe_width = VN_PICTURE_FRAME_WIDTH_21_9
                store.vn_pictureframe_force_size = True
            if not args:
                if store.vn_pictureframe_force_size:
                    store.vn_pictureframe_width = vn_picture_frame_script_width()
                    store.vn_pictureframe_force_size = False
                store.vn_pictureframe = None
                return True
            path = api.get_script_image_path(args[0])
            if path:
                path = path.replace("\\", "/")
                store.vn_pictureframe = {"path": path, "name": args[0], "top": name == "pictureFrameTop",
                                         "crop": vn_picture_frame_crop(path)}
            else:
                store.vn_pictureframe = None     # the sprite is activated but its texture never arrives
            return True

        if lname in ("wipefilter", "wipeoff"):
            return vn_wipe_command(lname, args)        # vn_wipe.rpy

        if lname in ("se", "seloop"):
            se = args[0] if args else None
            if se:
                vn_se_play(se, api, loop=(lname == "seloop"))
            return True
        if lname == "sestop":
            vn_se_stop(args[0] if args else None, vn_float(args[1], 0.0) if len(args) >= 2 else 0.0)
            return True
        if lname in ("cuese", "cueseloop"):
            sheet, cue = (args[0] if args else None), (args[1] if len(args) >= 2 else None)
            if sheet and cue:
                vn_se_play(cue, api, loop=(lname == "cueseloop"), sheet=sheet)
            return True
        if lname == "cuesestop":
            vn_se_stop(args[0] if args else None, vn_float(args[1], 0.0) if len(args) >= 2 else 0.0)
            return True
        if lname in ("secontinue", "cuesecontinue"):
            sheet = args[0] if args else None
            cue = args[1] if len(args) >= 2 else None
            if lname == "secontinue":
                if args:
                    vn_se_play(args[0], api)
            elif sheet and cue:
                vn_se_play(cue, api, sheet=sheet)
            return True
        if lname == "cuesecontinuestop":
            vn_se_stop(args[0] if args else None, vn_float(args[1], 0.0) if len(args) >= 2 else 0.0)
            return True
        if lname == "soundstopallfade":
            vn_sound_stop_all(vn_float(args[0], 0.0) if args else 0.0)
            return True
        if lname == "soundstopallend":
            # SoundStopAllEnd(GetNonStopEndBgm()): with nonStopEndBgm false (see vn_bgm_stop_flag) this is
            # SoundStopAll: BGM and SE cut at once, voices untouched.
            vn_sound_stop_all(0.0)
            return True
        if lname in ("voice", "voicestop"):
            return vn_voice_command(lname, args, api)
        if lname in ("sevolume", "cuesevolume"):
            return True

        if lname in ("blur", "bluroff"):
            return vn_blur_command(lname, args)          # vn_blur.rpy (camera filter on the main scene)
        if name == "cameraFilter":
            return vn_camera_filter_command(args)        # vn_blur.rpy (same material as the blur)
        if name == "backCameraColor":
            return vn_back_camera_color_command(args)
        if name in ("flashin", "flashout", "flashOff"):
            return vn_flash_command([name] + args)       # vn_flash.rpy

        if lname.startswith("overlay"):
            renpy.store.vn_unrendered_wait = True
            return True

        # Not replicated (other effects, flow control): ignored.
        return False

    VN_CHARA_PUT_COMMANDS = ("charaput", "charaputfsl", "charaputfsr", "charaputfslnotnotch", "charaputfsrnotnotch",
                             "charaputfssidel", "charaputfssider")

    # Inline figure commands (fgo_parser.POSITIONED_CHARA_TAGS: charaFace/charaPut*/charaFadein/charaMove ...) are
    # positioned and run by vn_msg_shake_command as the typewriter reaches them. These still run at line start.
    VN_INLINE_COMMANDS = ("cameramove", "cameramoveease") + VN_ROLL_COMMANDS

    def vn_apply_inline_tags(node, api):
        """Unpositioned commands embedded in a message line ([cameraMove ...], [charaRollMove ...], subRender*)
        are run when the line starts. FGO runs them when the typewriter reaches them; the parser drops that
        position for these."""
        for key, token in (node.get("tags") or {}).items():
            parts = str(token or key).split()
            if not parts:
                continue
            name = parts[0]
            if name.lower() in VN_INLINE_COMMANDS or name.lower().startswith(("subrender", "subcamera", "subblur", "substretch")):
                vn_execute_command({"name": name, "args": parts[1:]}, api)
