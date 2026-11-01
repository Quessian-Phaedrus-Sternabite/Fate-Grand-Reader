# Converted effects: ScriptManager / ScriptCharaData / UIScriptChara integration.
# Renderer and definitions come from the converter workspace; see docs/converted-effects.md.
default vn_effects = []
default vn_fx_serial = 0

init 10 python:
    import copy
    VN_FX_ANCHORS = ("charabackeffect", "charaeffect", "backeffect", "fowardeffect", "effect")
    VN_FX_IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    VN_FX_POSE_FIELDS = ("pos", "pos_anim", "scale", "scale_anim", "rel", "rel_move", "roll_q",
                         "roll_c", "roll_anim", "roll_loop", "roll_wait", "roll_stop_q", "kind", "shake")

    def vn_fx_pose(data):
        return copy.deepcopy({k: data.get(k) for k in VN_FX_POSE_FIELDS if k in data})

    def vn_fx_checkpoint():
        # Record command-time rig changes, so new camera renders cannot move already-born
        # world particles to a later charaPut/charaMove/charaScale pose.
        now = vn_now()
        for rec in store.vn_effects:
            if rec["slot"] is None or not vn_fx_alive(rec, now):
                continue
            pose = vn_fx_pose(rec["owner"])
            history = rec["poses"]
            if pose != history[-1][1]:
                history.append((now, pose))
            cutoff = now - rec["display"].max_life - 1.0
            while len(history) > 1 and history[1][0] < cutoff:
                history.pop(0)

    def vn_fx_command_kind(name):
        name = name.lower().replace("forwardeffect", "fowardeffect")
        for anchor in VN_FX_ANCHORS:
            if name.startswith(anchor):
                op = name[len(anchor):]
                if op in ("", "pause", "start", "stop", "destroy", "forcestop"):
                    return anchor, op
        return None

    def vn_fx_name(name):
        name = vn_convert_name(name)
        return name[5:] if name.lower().startswith("talk/") else name

    def vn_fx_elapsed(rec, now=None):
        return 0.0 if rec["paused"] else max(0.0, (vn_now() if now is None else now) - rec["start"])

    def vn_fx_alive(rec, now=None):
        if rec["slot"] is not None:
            # A replacement charaSet/imageSet destroys the old rig and its children.
            if store.current_chara_defs.get(rec["slot"]) is not rec["owner"]:
                return False
        if rec["paused"]:
            return True
        end = _fgo_fx_done_time(rec.get("spec") or fgo_fx_defs[rec["name"]], rec["stop"])
        return end is None or vn_fx_elapsed(rec, now) < end

    def vn_fx_prune():
        store.vn_effects = [r for r in store.vn_effects if vn_fx_alive(r)]

    def vn_fx_reset():
        store.vn_effects = []
        store.vn_fx_serial = 0

    def vn_fx_geometry(rec, now=None, historical=False):
        """EffectBase in NGUI world units; independent of sprite alpha/face/offset metadata."""
        now = vn_now() if now is None else now
        x, y = rec["offset"]
        if rec.get("special"):
            sx, sy = vn_special_child_offset(rec, now)
            x, y = x + sx, y + sy
        scale = 1.0
        R = VN_FX_IDENTITY
        if rec["slot"] is not None:
            data = rec["owner"]
            if historical:
                data = rec["poses"][0][1]
                for time, pose in rec["poses"]:
                    if time > now:
                        break
                    data = pose
            pos = vn_anim_value(data["pos_anim"], now)[0] if data.get("pos_anim") else data.get("pos", (0, 0))
            scale = vn_anim_value(data["scale_anim"], now)[0] if data.get("scale_anim") else data.get("scale", 1.0)
            rel, _ = vn_rel_value(data, now)
            q, _ = vn_roll_value(data, now)
            R = vn_quat_matrix(q)
            c = data.get("roll_c") or (0.0, 0.0)
            # EndSet overwrites prefab EffectBase.xy with GetCenterOffset (0,-288).
            # Image Depth additionally cancels CharaBase's +288 before Scale.
            image = data.get("kind") in ("image", "script_image")
            by = -288.0
            p = _fgo_fx_mv(R, (x + c[0], y + by + c[1], 0.0))
            x = pos[0] + rel[0] + scale * (p[0] - c[0])
            y = pos[1] + rel[1] + (0.0 if image else 288.0) + scale * (p[1] - c[1])
            sh = data.get("shake")
            if sh and now < sh["end"]:
                tick = int(max(0.0, now - sh["start"]) / sh["cycle"])
                rng = random.Random(str(sh["start"]) + ":" + str(tick))
                x += rng.uniform(-sh["x"], sh["x"])
                y += rng.uniform(-sh["y"], sh["y"])
        flip = rec["flip"]
        F = ((-1.0 if flip in ("H", "F") else 1.0, 0.0, 0.0),
             (0.0, -1.0 if flip in ("V", "F") else 1.0, 0.0),
             (0.0, 0.0, -1.0 if flip in ("H", "V") else 1.0))
        return x, y, float(scale), _fgo_fx_m3mul(R, F)

    def vn_fx_depth(rec):
        if rec["slot"] is not None:
            dz = -2.0 if rec["anchor"] == "charabackeffect" else (1.0 if rec["anchor"] == "specialeffect" else 2.0)
            return 20.0 + 10.0 * vn_effective_depth(rec["owner"]) + dz
        return 12.0 if rec["anchor"] == "backeffect" else 100.0

    def vn_fx_stage_items(group=None):
        """One depth-sorted list of figures and effect rigs, including invisible dummy slots."""
        result = []
        for slot, data in store.current_chara_defs.items():
            if data.get("sub_group") == group and vn_slot_is_drawn(data):
                result.append((20.0 + 10.0 * vn_effective_depth(data), 0, slot, "slot", slot))
        for rec in store.vn_effects:
            if rec["anchor"] == "fowardeffect" or rec["paused"] or not vn_fx_alive(rec):
                continue
            g = rec["owner"].get("sub_group") if rec["slot"] is not None else None
            if g == group:
                result.append((vn_fx_depth(rec), 1, rec["serial"], "fx", rec))
        result.extend(vn_special_stage_items(group))
        result.sort(key=lambda item: item[:3])
        return [(item[3], item[4]) for item in result]

    def vn_fx_forward():
        return [r for r in store.vn_effects if r["anchor"] == "fowardeffect" and not r["paused"] and vn_fx_alive(r)]

    def vn_fx_select(anchor, args):
        slot = args[0] if anchor.startswith("chara") and args else None
        name_args = args[1:] if anchor.startswith("chara") else args
        name = vn_fx_name(name_args[0]) if name_args else None
        return [r for r in store.vn_effects if r["anchor"] == anchor and
                (slot is None or r["slot"] == slot) and (name is None or r["name"] == name)]

    _vn_fx_art_cache = {}

    def vn_fx_art_present(name, spec):
        """False when a texture the converted effect draws is not in the game folder (effect is skipped)."""
        ok = _vn_fx_art_cache.get(name)
        if ok is None:
            ok = True
            stack = [spec]
            while stack and ok:
                v = stack.pop()
                if isinstance(v, str):
                    if v.startswith("gen/") and not (renpy.loadable("assets/fx/%s.png" % v) or renpy.loadable("assets/fx/%s.rgba.z" % v)):
                        ok = False
                elif isinstance(v, dict):
                    stack.extend(v.values())
                elif isinstance(v, (list, tuple)):
                    stack.extend(v)
            if not ok:
                renpy.log("vn_fx: textures missing for %s; effect skipped" % name)
            _vn_fx_art_cache[name] = ok
        return ok

    def vn_fx_command(name, args, now=None):
        kind = vn_fx_command_kind(name)
        if kind is None:
            return False
        anchor, op = kind
        vn_fx_prune()
        now = vn_now() if now is None else now
        if op in ("", "pause"):
            chara = anchor.startswith("chara")
            if len(args) < (2 if chara else 1):
                return True
            slot = args[0] if chara else None
            owner = store.current_chara_defs.get(slot) if chara else None
            if chara and owner is None:
                return True
            a = args[1:] if chara else args
            effect = vn_fx_name(a[0])
            if effect not in fgo_fx_defs or not vn_fx_art_present(effect, fgo_fx_defs[effect]):
                store.vn_unrendered_wait = True
                return True
            spec = _fgo_fx_delay_spec(effect, fgo_fx_defs[effect])
            if (chara and spec.get("cams")) or (anchor == "fowardeffect" and any(e.get("zwrite") for e in spec.get("emitters", ()))):
                renpy.log("vn_fx: unsupported anchor %s for %s (donor rendering restriction)" % (anchor, effect))
                store.vn_unrendered_wait = True
                return True
            if vn_skipping() and not spec["loop"] and op != "pause":
                return True
            # GetCharaOffset(index) is ZERO for all three entries, unlike GetPosition.
            offset = (0.0, 0.0)
            if len(a) > 1:
                offset = vn_parse_position(a[1]) if not chara or "," in a[1] else offset
            flip = a[2] if len(a) > 2 and a[2] in ("H", "V", "F") else ""
            store.vn_fx_serial += 1
            rec = dict(name=effect, anchor=anchor, slot=slot, owner=owner, offset=offset,
                       flip=flip, paused=op == "pause", start=now, stop=None, serial=store.vn_fx_serial)
            rec["spec"] = spec
            rec["poses"] = [(now, vn_fx_pose(owner))] if owner is not None else []
            if vn_skipping() and spec["loop"] and op != "pause":
                rec["start"] -= spec["clips"].get("start") or 0.0
            rec["display"] = VNConvertedEffect(rec)
            store.vn_effects.append(rec)
            store.vn_unrendered_wait = False
            return True
        chosen = vn_fx_select(anchor, args)
        for rec in chosen:
            if op == "start":
                if rec["paused"]:
                    spec = fgo_fx_defs[rec["name"]]
                    if vn_skipping() and not spec["loop"]:
                        store.vn_effects.remove(rec)
                        continue
                    rec["paused"] = False
                    rec["start"] = now
                    if vn_skipping() and spec["loop"]:
                        rec["start"] -= spec["clips"].get("start") or 0.0
            elif op == "destroy" or vn_skipping():
                store.vn_effects.remove(rec)
            elif op in ("stop", "forcestop"):
                if rec["paused"]:
                    store.vn_effects.remove(rec)
                elif rec["stop"] is None:
                    rec["stop"] = vn_fx_elapsed(rec, now)
                if op == "forcestop" and rec in store.vn_effects:
                    # CommonEffectComponent.ForceStop halts the current animation; the next
                    # update enters _end immediately instead of finishing the current loop.
                    original = rec.get("spec") or fgo_fx_defs[rec["name"]]
                    if original["has_anim"]:
                        spec = dict(original)
                        spec["clips"] = dict(original["clips"])
                        force_at = vn_fx_elapsed(rec, now)
                        spec["vn_force_t7"] = force_at
                        if (spec["clips"].get("start") or 0.0) > force_at:
                            spec["clips"]["start"] = force_at
                        rec["spec"] = spec
        return True

    def vn_fx_wait(tag, args):
        kind = vn_fx_command_kind(tag)
        if kind is None:
            return False
        anchor, op = kind
        if vn_skipping() or op == "start":
            # CommonEffectManager.IsStart tests asset/component initialization, NOT the
            # end of the _start clip. Bundled definitions initialize synchronously, even
            # when created by effectPause. ScriptManager waits while IsStart is FALSE.
            return True
        # IsBusy waits through destruction, with an optional name and/or character slot.
        while True:
            now = vn_now()
            pending = []
            for rec in vn_fx_select(anchor, args):
                if not vn_fx_alive(rec, now):
                    continue
                pending.append(rec)
            if not pending or vn_skipping():
                break
            renpy.pause(0.03, hard=True)
        vn_fx_prune()
        return True

    # Off-screen cull. Unity never draws an off-screen rig, but here every live particle costs
    # Python each 1/30 s frame, and scripts park rigs off-screen with a looping effect still on
    # them (charaPut R 2000,2000, often with a misspelled charaEffectDestroy that FGO ignores too).
    # A looping effect in its steady state (vn_fx_cull_steady) is skipped only when its root lies
    # outside the camera view plus a half-canvas margin AND everything it drew over at least one
    # particle life of full frames (VN_FX_CULL_WATCH at most), taken relative to the root, did too
    # (a frame that drew nothing never culls). A rig or camera change draws it again at once and
    # restarts the watch. Each skip lasts half the time the drawn box's fastest edge (measured
    # between two full frames) needs to reach the view, at most VN_FX_CULL_RECHECK seconds.
    # The particle clock is wall time, so skipped frames change nothing that is drawn later.
    # Validated by a sweep of all 895 effects parked just past each edge of the view.
    VN_FX_CULL_RECHECK = 2.0
    VN_FX_CULL_WATCH = 3.0

    def vn_fx_draw_box(r):
        """Conservative (x0, y0, x1, y1) of everything a Render tree draws, in its own coordinates
        (a child blitted at o draws its point p at o + r.reverse * p). None = unknown, assume visible;
        an empty tree gives an empty box (inf, inf, -inf, -inf)."""
        if not isinstance(r, renpy.display.render.Render):
            size = getattr(r, "get_size", None)
            if size is None:
                return None
            w, h = size()
            return (0.0, 0.0, float(w), float(h))
        M = r.reverse
        if r.mesh:
            # A mesh Render draws only its geometry; its children are the textures it samples.
            if r.mesh is True:
                pts = [(0.0, 0.0), (float(r.width), float(r.height))]
            else:
                # Mesh2 (big quads, clip polygons): its points, in the Render's coordinates.
                get_points = getattr(r.mesh, "get_points", None)
                raw = get_points() if get_points is not None else None
                if not raw or any(p[3] <= 0.0 for p in raw):
                    return None
                pts = [(p[0] / p[3], p[1] / p[3]) for p in raw]
            x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
            x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
            if M is not None:
                pts = [M.transform(px, py) for px, py in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
                x0, y0 = min(p[0] for p in pts), min(p[1] for p in pts)
                x1, y1 = max(p[0] for p in pts), max(p[1] for p in pts)
            return (x0, y0, x1, y1)
        # A plain container draws only its children, never its own (often canvas-sized) area.
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for child in r.children:
            b = vn_fx_draw_box(child[0])
            if b is None:
                return None
            if b[0] > b[2]:
                continue
            if M is not None:
                pts = [M.transform(px, py) for px, py in ((b[0], b[1]), (b[2], b[1]), (b[0], b[3]), (b[2], b[3]))]
                b = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
            x0 = min(x0, b[0] + child[1])
            y0 = min(y0, b[1] + child[2])
            x1 = max(x1, b[2] + child[1])
            y1 = max(y1, b[3] + child[2])
        return (x0, y0, x1, y1)

    def vn_fx_cull_steady(spec, rec, elapsed, max_life):
        """Only a looping effect in its steady state may be culled: past its start clip plus one full
        particle life, with no loop-clip or autoplay animation and no stop pending. Earlier, what a
        frame draws does not bound later frames (intros that grow onto the screen, late flashes)."""
        if not spec.get("loop") or rec["stop"] is not None or spec.get("autos"):
            return False
        clips = spec.get("clips") or {}
        if clips.get("loop"):
            return False
        return elapsed > (clips.get("start") or 0.0) + max_life + 1.0

    def vn_fx_view_rect(width, height):
        """The canvas area the stage camera can show, widened by half a canvas; None = do not cull."""
        cam = store.vn_camera
        px = py = 0.0
        zoom = 1.0
        if cam:
            pos = vn_anim_value(cam.get("pos_anim"))[0]
            scale = vn_anim_value(cam.get("scale_anim"))[0]
            if pos is not None:
                px, py = float(pos[0]), float(pos[1])
            if scale is not None:
                zoom = float(scale)
        if zoom <= 0.0:
            return None
        # Canvases are in native NGUI px (FGOFXCloud: up = width / native_width = 1).
        hx = width * 0.5 / min(zoom, 1.0) + width * 0.5
        hy = height * 0.5 / min(zoom, 1.0) + height * 0.5
        cx = width * 0.5 + px
        cy = height * 0.5 - py
        return (cx - hx, cy - hy, cx + hx, cy + hy), (px, py, zoom)

    class VNConvertedEffect(FGOFXCloud):
        def __init__(self, rec):
            super(VNConvertedEffect, self).__init__(rec["name"], layer13=rec["anchor"] != "fowardeffect",
                anchor="forwardeffect" if rec["anchor"] == "fowardeffect" else rec["anchor"])
            self.record = rec
            self.native_width = 1024.0
            self.root_matrix = VN_FX_IDENTITY
            self.birth_roots = {}
            self.cull = None
            self.cull_seen = None
            self.cull_reach = None

        def render(self, width, height, st, at):
            rec = self.record
            if rec["paused"] or not vn_fx_alive(rec) or not vn_special_child_visible(rec):
                return renpy.Render(width, height)
            x, y, scale, R = vn_fx_geometry(rec)
            self.x, self.y, self.scale = x, -y, scale
            self.root_matrix = R
            self.spec = rec.get("spec") or fgo_fx_defs[rec["name"]]
            self.stop_at = rec["stop"]
            # Stage/sub-camera canvases are in native NGUI units, including the wider square RT.
            self.native_width = float(width)
            self.frame_width = 1024.0
            elapsed = vn_fx_elapsed(rec)
            cutoff = elapsed - self.max_life - 1.0
            self.birth_roots = {k: v for k, v in self.birth_roots.items() if k[1] >= cutoff}
            view = None
            if vn_fx_cull_steady(self.spec, rec, elapsed, self.max_life) and (
                    rec["slot"] is None or rec["owner"].get("sub_group") is None):
                # Sub-camera groups draw into their own canvases; only the main stage is culled.
                view = vn_fx_view_rect(width, height)
            if view is not None:
                (vx0, vy0, vx1, vy1), cam_key = view
                rx, ry = width * 0.5 + x, height * 0.5 - y
                if vx0 <= rx <= vx1 and vy0 <= ry <= vy1:
                    view = None
            if view is None:
                self.cull = self.cull_seen = self.cull_reach = None
                return super(VNConvertedEffect, self).render(width, height, elapsed, at)
            key = (x, y, scale, R, rec["flip"], cam_key, width, height)
            if self.cull is not None and self.cull[0] == key and elapsed < self.cull[1]:
                renpy.redraw(self, 1.0 / 30.0)
                return renpy.Render(width, height)
            rv = super(VNConvertedEffect, self).render(width, height, elapsed, at)
            self.cull = None
            seen, self.cull_seen = self.cull_seen, None
            box = None
            for child in rv.children:
                b = vn_fx_draw_box(child[0])
                if b is None:
                    return rv
                if b[0] > b[2]:
                    continue
                b = (b[0] + child[1], b[1] + child[2], b[2] + child[1], b[3] + child[2])
                box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
            if box is None:
                return rv
            # Reach: everything drawn since the rig/camera last changed, relative to the root, so
            # sporadic content (random bolts, bursts) counts once it has been seen.
            rel = (box[0] - rx, box[1] - ry, box[2] - rx, box[3] - ry)
            reach = self.cull_reach
            if reach is None or reach[0] != key:
                reach = (key, elapsed, rel)
            else:
                r_ = reach[2]
                reach = (key, reach[1], (min(r_[0], rel[0]), min(r_[1], rel[1]), max(r_[2], rel[2]), max(r_[3], rel[3])))
            self.cull_reach = reach
            env = (reach[2][0] + rx, reach[2][1] + ry, reach[2][2] + rx, reach[2][3] + ry)
            # Separation from the view along the best axis (> 0: off-screen).
            gap = max(vx0 - env[2], env[0] - vx1, vy0 - env[3], env[1] - vy1)
            if gap <= 0.0:
                return rv
            self.cull_seen = (key, elapsed, box)
            # Watch one particle life (at most VN_FX_CULL_WATCH s) of full frames before skipping any.
            if elapsed - reach[1] < min(self.max_life, VN_FX_CULL_WATCH):
                renpy.redraw(self, 1.0 / 30.0)
                return rv
            if seen is not None and seen[0] == key and elapsed > seen[1]:
                # Skip for half the time the fastest-moving box edge (since the previous full frame,
                # at least 50 px/s) needs to cross the gap; the first off-screen frame only measures.
                speed = max(50.0, max(abs(a - b) for a, b in zip(box, seen[2])) / (elapsed - seen[1]))
                self.cull = (key, elapsed + min(VN_FX_CULL_RECHECK, 0.5 * gap / speed))
            renpy.redraw(self, 1.0 / 30.0)
            return rv

        def parent_particle(self, ei, e, birth, now):
            # World-space particles retain the root pose at birth. Cache on the displayable,
            # never in the store or on shared converter definitions.
            if e.get("sim_space") == "world":
                key = (ei, birth)
                if key not in self.birth_roots:
                    self.birth_roots[key] = vn_fx_geometry(self.record, self.record["start"] + birth, historical=True)
                return self.birth_roots[key]
            return self.x, -self.y, self.scale, self.root_matrix

init 20 python:
    def vn_fx_track_command(fn):
        def execute(*args, **kwargs):
            try:
                vn_special_tick()
                return fn(*args, **kwargs)
            finally:
                # Finished effects already draw nothing and stop redrawing; dropping their
                # records here also frees their caches instead of keeping them until the
                # next effect command.
                vn_fx_prune()
                vn_fx_checkpoint()
        return execute

    vn_execute_command = vn_fx_track_command(vn_execute_command)
    apply_reader_node = vn_fx_track_command(apply_reader_node)

screen vn_forward_effects():
    # MargePanel: PictureFrame(-10), forward EffectBase(-100), FlashMesh(-200).
    zorder 46
    fixed at vn_camera_transform(include_shake=False):
        xysize (config.screen_width, config.screen_height)
        fixed at Transform(zoom=upscale_ratio):
            xysize (original_screen_width, original_screen_height)
            yalign 1.0
            for fx in vn_fx_forward():
                add fx["display"]
