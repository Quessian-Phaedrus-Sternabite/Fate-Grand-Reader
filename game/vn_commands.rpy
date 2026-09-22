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
            "face_x": 0, "face_y": 0, "face_size": 256, "offset_x": 0, "offset_y": 0,
            "sub_group": None,          # charaLayer: None = main camera, else "#A".. sub-render group
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

    def vn_slot_image(slot):
        """Render the body/face as FGO's separate ring mesh and face quad."""
        data = renpy.store.current_chara_defs.get(slot) or {}
        path = data.get("path")
        if data.get("kind") == "image":
            return path
        face = str(data.get("face") or "0")
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

    def vn_slot_transform(slot, capture=False):
        """Per-frame transform: alpha fade, position/scale tweens, talk dim and filter tint.
        Reads the slot's plain-data state so it is always in sync with the script."""
        def fn(trans, st, at):
            data = renpy.store.current_chara_defs.get(slot)
            if data is None or not data.get("path"):
                trans.alpha = 0.0
                return None
            now = vn_now()
            running = False
            if data.get("alpha_anim"):
                alpha, r = vn_anim_value(data["alpha_anim"], now)
            else:
                alpha, r = data.get("alpha", 0.0), False
            running = running or r
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

            # Image slots use the Back texture as a single sprite, without face tiles or
            # svtScript offsets. Their independent Scale/Depth/slot hierarchy remains.
            if data.get("kind") == "image":
                scale = float(scale)
                width, height = data.get("image_size", (1024, 626))
                trans.matrixcolor = TintMatrix("#ffffff")
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

            # Colour: material _Color multiply. Non-talkers 0.5 grey; silhouette filter = black.
            if data.get("filter") == "silhouette":
                tint = "#000000"
            elif data.get("talk_mask") and not data.get("communication_chara"):
                tint = VN_TALK_DIM
            else:
                tint = "#ffffff"
            trans.matrixcolor = TintMatrix(tint)

            # ScriptPosition is relative to CharaBase (world y=288). The mesh and
            # svtScript offsets scale below that parent; x is centered on the root.
            root_x = (VN_SUB_CAPTURE / 2.0 if capture else 512.0) + float(pos[0])
            root_y = (VN_SUB_CAPTURE / 2.0 if capture else 288.0) - float(pos[1])          # NGUI +y is up
            ox = float(data.get("offset_x") or 0)
            oy = float(data.get("offset_y") or 0)
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

            # sceneSet image slots use non-uniform xzoom/yzoom. Ren'Py transfers transform
            # state between replacements at the same screen-language add site, which is
            # especially easy to hit while skip mode churns through visible slots. Reset
            # those axes before applying the figure's uniform scale or the inherited image
            # zoom multiplies this sprite and shunts its visible pixels down and right.
            trans.xzoom = 1.0
            trans.yzoom = 1.0
            trans.zoom = scale
            trans.xpos = int(round(root_x + scale * (ox - 512.0) + noise_dx))
            # CharaBase is at world y=288 ABOVE the per-character Scale node.
            # Scale only the mesh/metadata offset, never this parent translation.
            trans.ypos = int(round(root_y - 288.0 - scale * oy))
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
                data["talk_mask"] = (slot != renpy.store.vn_talk_index)
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

    def vn_start_wipe(is_in, duration):
        """Cover/reveal the stage while script state changes behind a wipe.

        The original named strip shader is unavailable in Ren'Py; the opaque
        cover preserves its execution and visibility semantics.
        """
        duration = max(0.0, vn_float(duration, 0.0))
        renpy.store.vn_wipe = vn_anim(1.0, 0.0, duration) if is_in else vn_anim(0.0, 1.0, duration)
        vn_mark_busy("wipe", duration)

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

    def vn_camera_transform():
        """Stage pan/zoom: NGUI camera node at (x, y) (+y up) with scale s about the screen centre."""
        def fn(trans, st, at):
            cam = renpy.store.vn_camera
            trans.xanchor = 0.5
            trans.yanchor = 0.5
            trans.xpos = 0.5
            trans.ypos = 0.5
            if not cam:
                trans.zoom = 1.0
                trans.xoffset = 0
                trans.yoffset = 0
                return None
            pos, r1 = vn_anim_value(cam.get("pos_anim"))
            scale, r2 = vn_anim_value(cam.get("scale_anim"))
            trans.zoom = float(scale)
            trans.xoffset = int(round(float(pos[0]) * upscale_ratio))
            trans.yoffset = int(round(-float(pos[1]) * upscale_ratio))
            return 0 if (r1 or r2) else None
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

    def vn_group_fs_pos(lname, tok):
        """Parse an x,y position and add the FS/FSSide horizontal edge offset (0 on a 16:9 surface)."""
        x, y = vn_parse_position(tok, (0.0, 0.0))
        l = lname.lower()
        if l.endswith("fssider") or l.endswith("fsr"):
            x += VN_FS_EDGE_OFFSET
        elif l.endswith("fssidel") or l.endswith("fsl"):
            x -= VN_FS_EDGE_OFFSET
        return (x, y)

    def vn_group_members(gkey):
        defs = renpy.store.current_chara_defs
        slots = [s for s, d in defs.items() if d.get("sub_group") == gkey and vn_slot_is_drawn(d)]
        return sorted(slots, key=lambda s: (vn_effective_depth(defs[s]), s))

    def vn_active_groups():
        subs = renpy.store.vn_sublayers or {}
        return [g for g in subs if subs[g].get("active") and vn_group_members(g)]

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
        children = [At(vn_slot_image(slot), vn_slot_transform(slot, capture=True)) for slot in vn_group_members(gkey)]
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
    # Music
    # ------------------------------------------------------------------

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

    def vn_apply_music_flag(flag):
        if not flag:
            return
        if flag.get("action") == "play" and flag.get("path"):
            renpy.music.play(flag.get("path"), fadein=flag.get("fade", 0.2))
        elif flag.get("action") == "stop":
            renpy.music.stop(fadeout=flag.get("fade", 0.2))

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

        return False

    def vn_execute_command(node, api):
        name = str(node.get("name") or "")
        lname = name.lower()
        args = list(node.get("args") or [])
        defs = renpy.store.current_chara_defs

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
            if tag == "time":
                vn_wait_time(args[1] if len(args) >= 2 else 0)
            elif tag.startswith("sub") or tag in ("fade", "scene", "mask", "wipe", "charaFade", "charaMove", "charaScale", "camera", "charaSpecialEffect"):
                vn_wait_for(tag, args[1] if len(args) > 1 else None)
            # flash/se/effect waits: those effects are not replicated, nothing to wait for.
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
            # Grammar: wipe{out,in} EFFECT TIME [PARAM]. Keep the effect name
            # for diagnostics even though the port currently uses an opaque cover.
            duration = args[1] if len(args) >= 2 else VN_DEFAULT_FADE_TIME
            vn_start_wipe(lname == "wipein", duration)
            if renpy.store.vn_wipe is not None:
                renpy.store.vn_wipe["effect"] = args[0] if args else ""
                renpy.store.vn_wipe["raw_args"] = list(args)
            return True
        if lname == "charaput" and len(args) >= 2 and args[0] in defs:
            data = defs[args[0]]
            data["pos"] = vn_parse_position(args[1], data.get("pos", (0.0, 0.0)))
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
            # MoveReturnPosition: TweenPosition to base + offset over TIME/2, then back to base over TIME/2.
            data = defs[args[0]]
            offset = vn_parse_position(args[1], (0.0, 0.0))
            duration = vn_float(args[2], 0.0) if len(args) >= 3 else 0.0
            base = tuple(vn_anim_current(data.get("pos_anim"), data.get("pos", (0.0, 0.0))))
            out = (base[0] + offset[0], base[1] + offset[1])
            now = vn_now()
            data["pos_anim"] = vn_anim_keys([(now, base), (now + duration / 2.0, out), (now + duration, base)])
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
        if lname in ("charaeffect", "charaeffectstop"):
            renpy.store.vn_unrendered_wait = True
            return True                                  # visual effects not replicated; swallow silently
        if lname == "charaspecialeffect" and len(args) >= 2 and args[0] in defs:
            effect = args[1].lower()
            data = defs[args[0]]
            duration = vn_float(args[3], 0.0) if len(args) >= 4 else 0.0
            current = vn_slot_alpha(data)
            if effect in ("appearance", "appearancereverse", "erasurereverse"):
                data["alpha_anim"] = vn_anim(current, 1.0, duration)
                data["alpha"] = 1.0
            elif effect in ("erasure", "enemyerasure"):
                data["alpha_anim"] = vn_anim(current, 0.0, duration)
                data["alpha"] = 0.0
            vn_mark_busy("charaSpecialEffect", duration)
            return True

        if lname == "crimovie":
            # The next scene is the movie's static fallback. Do not retain the movie's 10–20 second
            # playback timing when no frames or audio are being presented.
            renpy.store.vn_unrendered_movie = True
            renpy.store.vn_unrendered_wait = True
            return True

        if (lname in ("effect", "backeffect", "overlayfadein", "overlayfadeout", "shake", "shakestop",
                      "charashake", "flashin", "flashout", "blur", "bluroff") or
                lname.startswith(("se", "cuese", "backeffect", "overlay"))):
            renpy.store.vn_unrendered_wait = True
            return True

        # Not replicated (other effects, SE, flow control): ignored.
        return False

    VN_INLINE_COMMANDS = ("charaface", "charamove", "charamovereturn", "charamovereturnease", "charamoveease",
                          "charamovescale", "charascale", "charaput", "charadepth", "cameramove", "cameramoveease")

    def vn_apply_inline_tags(node, api):
        """Commands embedded in a message line ([charaFace X N], [charaMoveReturn S 0,10 0.2], ...) are run when
        the line starts. FGO runs them when the typewriter reaches them; the parser drops that position."""
        for key, token in (node.get("tags") or {}).items():
            parts = str(token or key).split()
            if not parts:
                continue
            name = parts[0]
            if name.lower() == "charaface" and len(parts) >= 3 and parts[1] in renpy.store.current_chara_defs:
                renpy.store.current_chara_defs[parts[1]]["face"] = parts[2]
            elif name.lower() in VN_INLINE_COMMANDS or name.lower().startswith(("subrender", "subcamera", "subblur", "substretch")):
                vn_execute_command({"name": name, "args": parts[1:]}, api)
