# ---------------------------------------------------------------------------
#  [charaFadeTime] / [charaShadow] / [charaFaceFade] / [imageChange]
# ---------------------------------------------------------------------------
# Ground truth: ScriptManager.ScriptCommandExecute disassembled from libil2cpp.so (string
# literals resolved through global-metadata.dat), plus UIScriptChara / UIScriptFigure /
# UIScriptImage / UIStandFigureRender / UIStandFigureM pseudocode and the decoded GLES3
# "Custom/Sprite-ScriptActionFigureCrossFade" fragment shader.
#
# The executor tokenises with the spaces kept, so "exactly N tokens" below means N args here.
#
#   [charaFadeTime S T A]      exactly 3 args, else "parameter error" (nothing happens).
#       ScriptCharaData.MoveAlpha(T, A, isWaitTalk=false). T is 0 unless play speed is NORMAL.
#       UIStandFigureRender.MoveAlpha = TweenRendererColor from the current colour to
#       (same rgb, A): a linear alpha tween. Never blocks. [wait charaFade] polls it
#       (IsMoveAlpha); the next message does NOT wait for it (IsMoveAlphaWaitTalk needs
#       isWaitTalk). On a figure mid-crossfade, MoveAlpha first force-ends the crossfade
#       (EndMoveAlphaForce -> EndChange, which leaves the figure at alpha 1).
#
#   [charaShadow S true|false] >= 2 args; the flag is args[1] == "true" (case-sensitive).
#       SetShadow only acts when the flag changes. The renderer's tint becomes rgb 0.1, times
#       0.5 while talk-masked (SetBaseColor), alpha kept; instant. Image prefabs use the same
#       0.1 but never talk-mask. While shadowed and displayed, FGO also spawns the
#       "Talk/bit_talk_11" particle prefab (shadowservant, 5 emitters) on the figure; this
#       reader does not render particle prefabs, so only the tint is reproduced.
#
#   [charaFaceFade S FACE T]   exactly 3 args, else "parameter error".
#       ChangeCharacter("crossFade", T (0 on fast play), current image, FACE): the other
#       figure of the pair loads the new face, and UIScriptFigure.UpdateChange shows the
#       dedicated crossFadeStandFigure instead of both figures. Its material samples the OLD
#       figure (tex set 1) and the NEW one (set 2) and blends them per pixel:
#           c  = cutoff(composite) : rgb zeroed where alpha < _CrossFadeCutOff (0.01)
#           out = lerp(c_old, c_new, _FadeFactor) * _Color
#       _Color is the old figure's mColor (talk dim / shadow tint and alpha) frozen at the
#       start; _FadeFactor is tweened 0 -> 1 linearly over T (UIStandFigureM.SetTweenColor
#       routes the tween's alpha into _FadeFactor). The filter colour is not in that shader,
#       so a silhouette is not applied during the blend. EndChange then shows the new figure
#       at alpha 1 (SetAlpha(1.0)), whatever the old alpha was.
#       The new figure keeps the shadow flag (ChangeCharacter re-applies SetShadow). Its
#       talk-mask flag is whatever SetTalkMask last gave it: SetTalkMask only reaches the
#       second figure once backIndex >= 0, i.e. after the slot's first change, so the FIRST
#       change after charaSet comes out without the talk dim until the next SetTalkIndex.
#       Not blocking at normal speed ([wait charaCrossFade]/[wait charaChange] poll IsChange).
#
#   [charaCrossFade S ID FACE T] / [charaChange S ID FACE KIND T]
#       ChangeCharacter("crossFade" | KIND, T, ID, FACE): the slot's other figure loads svt
#       image ID (its own sheet and svtScript offsets) at FACE. Arg order is from the corpus
#       (1,409 / 841 uses, always 4 / 5 args). UIScriptFigure.UpdateChange by kind:
#         crossFade (4): the charaFaceFade blend above, old figure -> new figure. FGO only
#                 refuses it for wide/high mixes (IsEnableCrossFade), which this reader lacks.
#         fade (2):  old MoveAlpha(T, 0) under new MoveAlpha(T, 1) (the slot's `back` record,
#                 drawn with the old sheet and offsets); [wait charaFade] covers it.
#         blink (3): FGO flickers between the two; treated as normal here (7 corpus uses).
#         else (1):  EndChange at once. Kind strings are case-sensitive: "nomal"/"Fade" too.
#       EndChange leaves the new figure at alpha 1 with the shadow flag kept, and the
#       first change after charaSet drops the talk mask exactly as charaFaceFade does.
#
#   [imageChange S IMAGE KIND T (SCALETYPE)]  >= 4 args, else "parameter error".
#       Normal speed: ChangeCharacter(KIND, T, IMAGE, 0). Other speeds: SetCharacter (instant).
#       Afterwards the script WAITs "charaSet", which only holds while the asset loads.
#       UIScriptImage.UpdateChange by kind ("fade" 2, "blink" 3, "crossFade" 4, else normal 1):
#         normal: EndChange at once -> new image alpha 1, old image alpha 0 (T is ignored).
#         fade:   old image MoveAlpha(T, 0) and new image MoveAlpha(T, 1) together, new on
#                 top (depth 0.1 vs 0); EndChange -> new 1, old released.
#       The corpus only uses normal (332) and fade (11); blink/crossFade are treated as normal
#       (UIScriptImage has no crossFade branch at all, so FGO would never finish that change).
#       The new image keeps the shadow flag. The fade's main tween is an IsMoveAlpha tween, so
#       [wait charaFade] covers it.

init -35 python:

    VN_SHADOW_RGB = 0.1          # UIStandFigureRender/UIImageRender.SetBaseColor: isShadow
    VN_TALK_MASK_RGB = 0.5       # ... * 0.5 while isTalkMask
    VN_CROSSFADE_CUTOFF = 0.01   # _CrossFadeCutOff default

    renpy.register_shader("fgo.figure_crossfade", variables="""
        uniform float u_lod_bias;
        uniform sampler2D tex0;
        uniform sampler2D tex1;
        uniform float u_fgo_fade;
        uniform float u_fgo_cutoff;
        attribute vec2 a_tex_coord;
        varying vec2 v_tex_coord;
    """, vertex_200="""
        v_tex_coord = a_tex_coord;
    """, fragment_200="""
        // Ren'Py textures are premultiplied; FGO's figure textures are straight alpha
        // (_MainTex rgb + _SubTex alpha), so un-premultiply before the cutoff and lerp.
        vec4 fgo_c0 = texture2D(tex0, v_tex_coord.st, u_lod_bias);
        vec4 fgo_c1 = texture2D(tex1, v_tex_coord.st, u_lod_bias);
        vec3 fgo_rgb0 = fgo_c0.a > 0.0 ? fgo_c0.rgb / fgo_c0.a : vec3(0.0);
        vec3 fgo_rgb1 = fgo_c1.a > 0.0 ? fgo_c1.rgb / fgo_c1.a : vec3(0.0);
        fgo_rgb0 *= step(u_fgo_cutoff, fgo_c0.a);
        fgo_rgb1 *= step(u_fgo_cutoff, fgo_c1.a);
        vec3 fgo_rgb = mix(fgo_rgb0, fgo_rgb1, u_fgo_fade);
        float fgo_a = mix(fgo_c0.a, fgo_c1.a, u_fgo_fade);
        gl_FragColor = vec4(fgo_rgb * fgo_a, fgo_a);
    """)

    class VNFigureCrossFade(renpy.Displayable):
        """crossFadeStandFigure: both composites blended by the FGO crossfade shader.

        The slot transform supplies the frozen _Color (tint and alpha). Once the tween has
        finished this draws the slot's live image, as the real figure is shown again."""

        def __init__(self, slot, old, new, start, dur, **kwargs):
            super(VNFigureCrossFade, self).__init__(**kwargs)
            self.slot = slot
            self.old = old
            self.new = new
            self.start = start
            self.dur = dur

        def render(self, width, height, st, at):
            f = 1.0 if self.dur <= 0.0 else (vn_now() - self.start) / self.dur
            if f >= 1.0 or not renpy.display.render.models:
                return renpy.render(vn_slot_image(self.slot), width, height, st, at)
            f = max(0.0, f)
            bottom = renpy.render(self.old, width, height, st, at)
            top = renpy.render(self.new, width, height, st, at)
            w = max(top.width, bottom.width)
            h = max(top.height, bottom.height)
            if top.get_size() != (w, h):
                top = top.subsurface((0, 0, w, h))
            if bottom.get_size() != (w, h):
                bottom = bottom.subsurface((0, 0, w, h))
            rv = renpy.Render(w, h)
            rv.mesh = True
            rv.add_shader("fgo.figure_crossfade")
            rv.add_uniform("u_fgo_fade", f)
            rv.add_uniform("u_fgo_cutoff", VN_CROSSFADE_CUTOFF)
            rv.blit(bottom, (0, 0), focus=False, main=False)
            rv.blit(top, (0, 0), focus=True, main=True)
            renpy.redraw(self, 0)
            return rv

        def visit(self):
            return [self.old, self.new]

    _vn_crossfade_cache = {}

    def vn_chara_crossfade(data, now=None):
        """The running crossfade record of a figure slot, or None once it has ended."""
        xf = data.get("xfade")
        if not xf:
            return None
        if now is None:
            now = vn_now()
        if now >= xf["start"] + xf["dur"]:
            return None
        return xf

    def vn_chara_crossfade_image(slot, data):
        xf = vn_chara_crossfade(data)
        if xf is None:
            return None
        # charaChange/charaCrossFade blend from the previous figure's own sheet and face layout.
        old = xf.get("from_figure") or data
        key = (slot, xf["start"], xf["from_face"], xf["to_face"], old.get("path"), data.get("path"))
        disp = _vn_crossfade_cache.get(key)
        if disp is None:
            _vn_crossfade_cache.clear()     # only ever a handful alive; drop finished ones
            disp = VNFigureCrossFade(slot,
                                     vn_chara_composite(old, xf["from_face"]),
                                     vn_chara_composite(data, xf["to_face"]),
                                     xf["start"], xf["dur"])
            _vn_crossfade_cache[key] = disp
        return disp

    def vn_chara_end_crossfade(data):
        """UIScriptFigure.MoveAlpha on a CROSS_FADE change: EndMoveAlphaForce -> EndChange."""
        if data.get("xfade"):
            data["xfade"] = None
            data["change_end"] = min(float(data.get("change_end") or 0.0), vn_now())

    def vn_chara_tint_value(data):
        """SetBaseColor's rgb multiplier for the slot's current flags."""
        v = VN_SHADOW_RGB if data.get("shadow") else 1.0
        # FGO's image-set subclasses inherit UIScriptChara's empty SetTalkMask.
        if (data.get("talk_mask") and not data.get("communication_chara")
                and data.get("kind") not in ("script_image", "image")):
            v *= VN_TALK_MASK_RGB
        return v

    def vn_tint_matrix(v):
        if v >= 1.0:
            return TintMatrix("#ffffff")
        if v == VN_TALK_MASK_RGB:
            return TintMatrix(VN_TALK_DIM)     # the reader's established talk-dim colour
        return TintMatrix(Color(rgb=(v, v, v)))

    def vn_mark_busy_max(tag, duration):
        """Like vn_mark_busy, but a shorter tween never cuts an earlier, longer one's wait."""
        duration = 0.0 if vn_skipping() else max(0.0, vn_float(duration, 0.0))
        end = vn_now() + duration
        renpy.store.vn_busy[tag] = max(end, renpy.store.vn_busy.get(tag) or 0.0)

    def vn_chara_slot(defs, slot):
        """ConvertCharaIndex + charaList lookup: script slots only, never comm figures."""
        data = defs.get(slot)
        if data is None or data.get("communication_chara"):
            return None
        return data

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def vn_chara_fade_time(args, defs):
        if len(args) != 3:
            return True                              # parameter error
        data = vn_chara_slot(defs, args[0])
        if data is None:
            return True
        duration = vn_float(args[1], 0.0)            # vn_anim zeroes it while skipping
        target = vn_float(args[2], 0.0)
        vn_chara_end_crossfade(data)
        data["alpha_anim"] = vn_anim(vn_slot_alpha(data), target, duration)
        data["alpha"] = target
        # isWaitTalk=false: [wait charaFade] waits, the next message does not.
        vn_mark_busy_max("charaFadeNoTalk", duration)
        return True

    def vn_chara_shadow(args, defs):
        if len(args) < 2:
            return True                              # parameter error
        data = vn_chara_slot(defs, args[0])
        if data is None:
            return True
        data["shadow"] = (args[1] == "true")         # String.op_Equality: exact match
        return True

    def vn_chara_face_fade(args, defs):
        if len(args) != 3:
            return True                              # parameter error
        data = vn_chara_slot(defs, args[0])
        if data is None or data.get("kind") != "chara":
            # UIScriptImage has no CROSS_FADE branch; only standing figures crossfade.
            return True
        face = str(args[1])
        try:
            face = str(int(face))                    # Int32.Parse
        except Exception:
            return True
        duration = 0.0 if vn_skipping() else max(0.0, vn_float(args[2], 0.0))
        now = vn_now()
        old_face = str(data.get("face") or "0")
        if duration > 0.0 and data.get("path"):
            data["xfade"] = {"from_face": old_face, "to_face": face,
                             "start": now, "dur": duration,
                             # _Color = outFigure.mColor at the moment the blend starts.
                             "tint": vn_chara_tint_value(data),
                             "alpha": max(0.0, min(1.0, float(vn_slot_alpha(data, now))))}
        else:
            data["xfade"] = None
        data["change_end"] = now + duration
        # State after EndChange: the new figure, at alpha 1.
        data["face"] = face
        data["alpha_anim"] = None
        data["alpha"] = 1.0
        if not data.get("changed"):
            # backIndex was -1 until now, so SetTalkMask never reached this figure.
            data["talk_mask"] = False
        data["changed"] = True
        return True

    def vn_image_change(args, defs, api):
        if len(args) < 4:
            return True                              # parameter error
        data = vn_chara_slot(defs, args[0])
        if data is None or data.get("kind") != "script_image":
            return True
        image_name = vn_convert_name(args[1])        # ConvertNameString
        path = api.get_script_image_path(image_name)
        if not path and image_name != "00001":
            path = api.get_script_image_path("00001")   # UIImageRender.GetAssetNameList fallback
        variant = data.get("image_variant", "image")
        second_path = api.get_script_image_path(image_name + "a") if variant != "image" else None
        kind = args[2]
        duration = vn_float(args[3], 0.0)
        now = vn_now()
        fade = (kind == "fade" and duration > 0.0 and not vn_skipping())
        if fade:
            # The old image keeps its own tween (MoveAlpha(T, 0)) under the new one.
            data["back"] = {"path": data.get("path"), "second_path": data.get("second_path"),
                            "image_variant": variant,
                            "alpha_anim": vn_anim(vn_slot_alpha(data, now), 0.0, duration)}
            data["alpha_anim"] = vn_anim(0.0, 1.0, duration)
            data["change_end"] = now + duration
            vn_mark_busy_max("charaFadeNoTalk", duration)
        else:
            data["back"] = None
            data["alpha_anim"] = None
            data["change_end"] = now
        data["id"] = image_name
        data["path"] = path.replace("\\", "/") if path else None
        data["second_path"] = second_path.replace("\\", "/") if second_path else None
        data["alpha"] = 1.0
        data["changed"] = True
        return True

    VN_FIGURE_SHEET_KEYS = ("id", "path", "face", "face_x", "face_y", "face_size",
                            "offset_x", "offset_y")

    def vn_chara_figure_change(args, defs, api, kind):
        """[charaChange S ID FACE KIND T] / [charaCrossFade S ID FACE T]: swap a standing
        figure for another svt image, ChangeCharacter(KIND, T, ID, FACE)."""
        if len(args) < 4:
            return True                              # parameter error
        data = vn_chara_slot(defs, args[0])
        if data is None or data.get("kind") not in (None, "chara"):
            return True
        chara_id = str(args[1])
        try:
            face = str(int(args[2]))                 # Int32.Parse
        except Exception:
            return True
        duration = 0.0 if vn_skipping() else max(0.0, vn_float(args[3], 0.0))
        now = vn_now()
        old = dict((k, data.get(k)) for k in VN_FIGURE_SHEET_KEYS)
        old["face"] = str(old.get("face") or "0")
        old_alpha = max(0.0, min(1.0, float(vn_slot_alpha(data, now))))

        char_path = api.get_character_path(chara_id)
        offsets = api.get_chara_script_offsets(chara_id) or {}
        data.update({
            "id": chara_id,
            "path": char_path.replace("\\", "/") if char_path else None,
            "face_x": offsets.get("face_x") or 0,
            "face_y": offsets.get("face_y") or 0,
            "face_size": offsets.get("face_size") or 256,
            "offset_x": offsets.get("offset_x") or 0,
            "offset_y": offsets.get("offset_y") or 0,
        })

        data["xfade"] = None
        data["back"] = None
        if duration > 0.0 and old.get("path") and data.get("path") and kind == "crossFade":
            # IsEnableCrossFade only refuses wide/high mixes; this reader has neither.
            data["xfade"] = {"from_face": old["face"], "to_face": face, "from_figure": old,
                             "start": now, "dur": duration,
                             "tint": vn_chara_tint_value(data), "alpha": old_alpha}
            data["alpha_anim"] = None
        elif duration > 0.0 and old.get("path") and kind == "fade":
            # Old figure MoveAlpha(T, 0) under the new one's MoveAlpha(T, 1) (depth 0 vs 0.1).
            old["kind"] = "chara"
            old["alpha_anim"] = vn_anim(old_alpha, 0.0, duration)
            data["back"] = old
            data["alpha_anim"] = vn_anim(0.0, 1.0, duration)
            vn_mark_busy_max("charaFadeNoTalk", duration)
        else:
            # "normal" and anything unrecognised (kind strings are case-sensitive, so the
            # corpus's "nomal"/"Fade" land here) end at once. "blink" is also instant here.
            duration = 0.0
            data["alpha_anim"] = None
        data["change_end"] = now + duration
        # State after EndChange: the new figure at alpha 1, shadow flag kept.
        data["face"] = face
        data["alpha"] = 1.0
        if not data.get("changed"):
            data["talk_mask"] = False                # backIndex was -1 (see charaFaceFade)
        data["changed"] = True
        return True

    def vn_wait_chara_change(slot=None):
        """[wait charaChange|charaCrossFade (SLOT)]: hold while IsChange (changeKind != 0)."""
        defs = renpy.store.current_chara_defs
        if slot is not None and slot in defs:
            ends = [defs[slot].get("change_end") or 0.0]
        else:
            ends = [d.get("change_end") or 0.0 for d in defs.values()]
        end = max(ends) if ends else 0.0
        if vn_skipping():
            return
        remaining = end - vn_now()
        if remaining > 0.005:
            renpy.store.vn_wait_until = end
            renpy.pause(remaining, hard=True)

    # ------------------------------------------------------------------
    # imageChange fade: the outgoing image, drawn under the slot's main image
    # ------------------------------------------------------------------

    def vn_slot_back_visible(slot, now=None):
        data = renpy.store.current_chara_defs.get(slot) or {}
        back = data.get("back")
        if not back or not back.get("path"):
            return False
        value, running = vn_anim_value(back.get("alpha_anim"), now)
        return running or (value or 0.0) > 0.0

    def vn_slot_back_image(slot):
        data = renpy.store.current_chara_defs.get(slot) or {}
        back = data.get("back") or {}
        if back.get("kind") == "chara":              # outgoing figure of a charaChange fade
            return vn_chara_composite(back, back.get("face"))
        return vn_script_image_disp(back.get("path"), back.get("second_path"),
                                    back.get("image_variant", "image"))
