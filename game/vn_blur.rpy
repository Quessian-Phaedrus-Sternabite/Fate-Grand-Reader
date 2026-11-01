# ---------------------------------------------------------------------------
#  [blur] / [blurOff] — ScriptActionRenderEffectController port
# ---------------------------------------------------------------------------
# FGO draws the main script scene (ActionCamera: backgrounds, characters, the back
# EffectBase; camera move and shake included) into a 1024x1024 RenderTexture and shows
# it on meshRenderBase. [blur] swaps that mesh's shader for a camera filter
# ("Custom/Sprite-ScriptActionRender" + GaussianBlur / MotionBlur / FrostedGlass) and
# tweens the material; the message window, fades, wipes, picture frames and sub-render
# quads are siblings outside that texture and are never blurred.
#
# Controller (ScriptActionRenderEffectController):
#   Start(sec): linear timer, rate = clamp(t/sec); every frame the Reflection lerps
#   the tweened material fields from their current material values (CreateByMaterialValue)
#   to the target, and _BlurFactor is rebuilt from the lerped sigma. sec <= 0 applies the
#   target at once. isEnable (busy) is true while the timer runs.
#
# ScriptCommandExecute (disassembled from libil2cpp.so):
#   [blur lens S R SIGMA]                       exactly 4 tokens
#   [blur motion S R SIGMA CX CY RANGE COEF]    exactly 8 tokens
#   [blur glass S R SIGMA TEXNAME TILING SCATTER]  exactly 7 tokens; blocks like [wait blur]
#   Any other token count is a "parameter error" and does nothing. If the controller is
#   busy, the [blur] line waits and re-runs once the running tween has finished.
#   [blurOff]            StopGaussianBlur(0, b__1): everything off immediately
#   [blurOff lens S]     sigma -> 0, then filter off
#   [blurOff motion S]   sigma -> 0.01, range -> 0, coefficient -> 0, then filter off
#   [blurOff glass S]    sigma -> 0, scatter -> 0, blurLv -> 1, then filter off
#   [blurOff <other>]    (e.g. [blurOff 1.0]) matches no mode: nothing happens
#   blurOff never blocks and replaces a running tween (the replaced stop callback is dropped).
#   Off play speed (skip) the new tween is skipped to its end (SkipBlur).
#   [wait blur] / [wait blurOff] hold while the controller is busy.

# Tap spacing is one pixel of the device screen (_ScreenParams), in the RenderTexture's UV
# space. None = use this window's drawable resolution, i.e. the reader window is the
# "device" (FGO renders at native resolution, optionally reduced to 350 dpi). Set to a
# (width, height) tuple to emulate a fixed device instead.
define VN_BLUR_SCREEN_PARAMS = None

init -40 python:

    # The 16:9 scene occupies this rectangle of the 1024x1024 RT (Unity UV, v up). The
    # filter shaders clamp every tap to it; T1 (a narrower-than-16:9 correction) is 1 here.
    VN_BLUR_RT_U0 = 0.119850188
    VN_BLUR_RT_UW = 0.760299653    # 0.880149841 - 0.119850188
    VN_BLUR_RT_V0 = 0.287640452
    VN_BLUR_RT_VH = 0.424719096    # 0.712359548 - 0.287640452

    # GaussianBlurParam.GetBlurFactors: _BlurFactor rings in shader order as (d^2, tap count).
    # A 2D diamond |dx|+|dy| <= sampleRange; entries 5 and 6 are out of d^2 order, as in FGO.
    VN_BLUR_GAUSS_RINGS = ((0, 1), (1, 4), (2, 4), (4, 4), (5, 8), (9, 4), (8, 4), (10, 8), (16, 4))
    VN_BLUR_GAUSS_USED = {1: 2, 2: 4, 3: 6, 4: 9}     # sampleRange -> rings read by SAMPLERANGE_n

    # Fields each Reflection lerps; everything else snaps when the command runs.
    VN_BLUR_TWEEN_FIELDS = {
        "lens": ("sigma",),
        "motion": ("sigma", "cx", "cy", "range", "coef"),
        "glass": ("sigma", "tiling", "scatter", "blurlv"),
    }

    def vn_blur_gauss_factors(sample_range, sigma):
        w = [0.0] * 9
        n = VN_BLUR_GAUSS_USED.get(sample_range)
        if sigma == 0.0 or n is None:
            w[0] = 1.0
            return w
        s2 = 2.0 * sigma * sigma
        g = [math.exp(-d2 / s2) for d2, _ in VN_BLUR_GAUSS_RINGS[:n]]
        total = sum(g[i] * VN_BLUR_GAUSS_RINGS[i][1] for i in range(n))
        for i in range(n):
            w[i] = g[i] / total
        return w

    def vn_blur_motion_factors(sample_range, sigma):
        """MotionBlurParam.GetBlurFactors: 1D kernel at steps 0, +-1..+-sampleRange."""
        w = [0.0] * 5
        if sigma == 0.0 or sample_range not in (1, 2, 3, 4):
            w[0] = 1.0
            return w
        s2 = 2.0 * sigma * sigma
        g = [math.exp(-(k * k) / s2) for k in range(sample_range + 1)]
        total = g[0] + 2.0 * sum(g[1:])
        for k in range(sample_range + 1):
            w[k] = g[k] / total
        return w

    def vn_blur_default():
        # The material's values before any filter command (motion defaults are the shader's).
        return {"filter": None, "tween": None, "cam": None, "cam_params": [0.0] * 16,
                "mat": {"sr": 0, "sigma": 0.0, "cx": 0.5, "cy": 0.5, "range": 0.0, "coef": 100.0,
                        "tiling": 0.0, "scatter": 0.0, "blurlv": 1.0}}

    def vn_blur_state():
        if not renpy.store.vn_blur:
            renpy.store.vn_blur = vn_blur_default()
        return renpy.store.vn_blur

    def vn_blur_eval(state, now=None, finish=False):
        """Pure: the (filter, material, running) the controller shows at `now`."""
        if not state:
            return None, None, False
        filt = state.get("filter")
        mat = dict(state["mat"])
        tw = state.get("tween")
        if not tw:
            return filt, mat, False
        if now is None:
            now = vn_now()
        f = 1.0 if finish or tw["dur"] <= 0.0 else (now - tw["start"]) / tw["dur"]
        if f < 1.0:
            f = max(0.0, f)
            for k in tw["fields"]:
                mat[k] = tw["src"][k] + (tw["dst"][k] - tw["src"][k]) * f
            return filt, mat, True
        for k in tw["fields"]:
            mat[k] = tw["dst"][k]
        stop = tw.get("stop")
        if stop:
            # Stop callbacks b__1 / b__16 / b__17: SetGaussianBlur/SetMotionBlur(0, 0) clear the
            # SAMPLERANGE keyword; b__18: SetFrostedGlass(1, 0, 0, 0). All end in SetCameraFilter(null).
            if stop == "glass":
                mat["tiling"] = 0.0; mat["scatter"] = 0.0; mat["blurlv"] = 1.0
            else:
                mat["sr"] = 0
            filt = None
        return filt, mat, False

    def vn_blur_settle(finish=False):
        """Commit a finished (or, with finish=True, skipped) tween into the stored state."""
        state = renpy.store.vn_blur
        if not state or not state.get("tween"):
            return
        filt, mat, running = vn_blur_eval(state, finish=finish)
        if not running:
            state["filter"] = filt
            state["mat"] = mat
            state["tween"] = None

    def vn_blur_busy_end():
        state = renpy.store.vn_blur
        tw = state.get("tween") if state else None
        if not tw or not vn_blur_eval(state)[2]:
            return None
        return tw["start"] + tw["dur"]

    def vn_blur_wait():
        """[wait blur]: hold while the controller is busy (hard, like the other effect waits)."""
        end = vn_blur_busy_end()
        if end is not None:
            if vn_skipping():
                vn_blur_settle(finish=True)
                return
            remaining = end - vn_now()
            if remaining > 0.005:
                renpy.store.vn_wait_until = end
                renpy.pause(remaining, hard=True)
        vn_blur_settle(finish=True)

    def vn_blur_start(kind, sec, dst, stop=None):
        """Start/Stop*: tween `kind`'s fields from the material's current values to `dst`."""
        state = vn_blur_state()
        now = vn_now()
        # A replaced tween leaves the material where its last frame put it.
        filt, mat, _ = vn_blur_eval(state, now)
        state["mat"] = mat
        fields = VN_BLUR_TWEEN_FIELDS[kind]
        src = dict((k, mat[k]) for k in fields)
        target = dict(src)
        target.update(dst)
        dur = 0.0 if vn_skipping() else max(0.0, sec)
        state["tween"] = {"fields": fields, "src": src, "dst": target, "start": now, "dur": dur, "stop": stop}
        if dur <= 0.0:
            vn_blur_settle()

    def vn_blur_command(lname, args):
        if lname == "blur":
            if not args:
                return True                                      # "parameter error"
            mode = args[0]
            wanted = {"lens": 4, "motion": 8, "glass": 7}.get(mode)
            if wanted is None or len(args) != wanted:
                return True                                      # unknown mode / "parameter error"
            try:
                sec = float(args[1])
                sample_range = int(float(args[2]))
                sigma = float(args[3])
                if mode == "motion":
                    cx, cy, rng, coef = (float(a) for a in args[4:8])
                elif mode == "glass":
                    tiling, scatter = float(args[5]), float(args[6])
            except Exception:
                return True
            # A busy controller makes the [blur] line wait and run again afterwards.
            if vn_blur_busy_end() is not None:
                vn_blur_wait()
            state = vn_blur_state()
            state["filter"] = mode                               # SetCameraFilter(...)
            state["cam"] = None                                  # replaces a [cameraFilter] shader
            mat = state["mat"]
            if mode == "glass":
                # SetFrostedGlass(1, sigma, tiling, 0) runs before Start and seeds its start values.
                mat["tiling"] = tiling; mat["scatter"] = 0.0; mat["blurlv"] = 1.0
                vn_blur_start("glass", sec, {"sigma": sigma, "tiling": tiling, "scatter": scatter,
                                              "blurlv": float(sample_range)})
                # The frosted_glass bump texture load holds the script like [wait blur].
                vn_blur_wait()
                return True
            mat["sr"] = sample_range                             # SAMPLERANGE_n keyword snaps
            if mode == "lens":
                vn_blur_start("lens", sec, {"sigma": sigma})
            else:
                vn_blur_start("motion", sec, {"sigma": sigma, "cx": cx, "cy": cy, "range": rng, "coef": coef})
            return True

        # blurOff
        if not args:
            vn_blur_start("lens", 0.0, {"sigma": 0.0}, stop="all")
            return True
        mode = args[0]
        if mode not in VN_BLUR_TWEEN_FIELDS:
            return True                                          # e.g. [blurOff 1.0]: nothing stops
        sec = vn_float(args[1], 0.0) if len(args) > 1 else 0.0
        if mode == "lens":
            vn_blur_start("lens", sec, {"sigma": 0.0}, stop="lens")
        elif mode == "motion":
            vn_blur_start("motion", sec, {"sigma": 0.01, "range": 0.0, "coef": 0.0}, stop="motion")
        else:
            vn_blur_start("glass", sec, {"sigma": 0.0, "scatter": 0.0, "blurlv": 1.0}, stop="glass")
        return True

    # ------------------------------------------------------------------
    # [cameraFilter NAME (P0 P1 ...)] — SetCameraFilter on the same material
    # ------------------------------------------------------------------
    # ScriptCommandExecute: no token = SetCameraFilter(null); NAME only = SetCameraFilter(NAME, null)
    # (_FilterParam left as it was); NAME + more tokens = up to 16 Single.Parse'd floats (row-major into the
    # _FilterParam matrix; a bad float throws and nothing happens). Then, on every path,
    # ScriptActionRenderEffectController.SetDisable(): a running blur tween dies without its stop callback.
    # Instant, no wait, no parameter error. null / "" / "normal" = the plain RenderMesh shader; any other
    # name is "Custom/Sprite-ScriptActionRender" + Capitalised(NAME). The filter, [blur] and distortion all
    # set the shader of this one material, so the last command wins: [blur] replaces gray, and when that
    # blur's [blurOff] ends the stage is back to normal, not gray. It lasts until then or the script ends
    # (RenderCameraInit). Scope = the main scene RT only, like the blur.
    VN_CAMERA_FILTERS = ("gray", "inversion", "darkred", "aberration")

    def vn_camera_filter_command(args):
        state = vn_blur_state()
        params = None
        if len(args) >= 2:
            try:
                params = [float(a) for a in args[1:17]]
            except Exception:
                return True
            params += [0.0] * (16 - len(params))
        name = args[0].strip('"').lower() if args else ""
        # SetDisable(): freeze a running blur tween where it is, drop its stop callback.
        if state.get("tween"):
            _, mat, _ = vn_blur_eval(state)
            state["mat"] = mat
            state["tween"] = None
        state["filter"] = None
        # "through"/"summon" keep the RT alpha but are otherwise plain; an unknown name has no shader
        # (Shader.Find returns null; unused in the corpus) and is shown as normal.
        state["cam"] = name if name in VN_CAMERA_FILTERS else None
        if params is not None:
            state["cam_params"] = params
        return True

    # [backCameraColor (RRGGBB|false)]: MargeCamera clearFlags SolidColor with that colour (default 000000,
    # alpha 1) under everything in MargePanel; "false" restores depth-only. Exactly 6 hex digits, else a
    # parameter error. Restored at script end. The opaque RenderMesh covers a 16:9 screen, so this only
    # shows where the stage does not reach.
    def vn_back_camera_color_command(args):
        s = args[0] if args else "000000"
        if s.lower() == "false":
            renpy.store.vn_back_camera_color = None
            return True
        if len(s) != 6:
            return True
        try:
            int(s, 16)
        except Exception:
            return True
        renpy.store.vn_back_camera_color = "#" + s
        return True

    # ------------------------------------------------------------------
    # Shaders (decoded from the GLES3 programs of the Custom/Sprite-ScriptActionRender* filters)
    # ------------------------------------------------------------------
    # Everything is done in the RT's UV space and mapped back to this texture (Ren'Py UV, v down).
    # One tap step is 1/_ScreenParams in RT UV, i.e. (1/(Sw*UW), 1/(Sh*VH)) here.

    _VN_BLUR_VARIABLES = """
        uniform sampler2D tex0;
        uniform vec2 res0;
        attribute vec2 a_tex_coord;
        varying vec2 v_tex_coord;
        uniform vec4 u_fgo_blur_wa;
        uniform vec4 u_fgo_blur_wb;
        uniform float u_fgo_blur_wc;
        uniform float u_fgo_blur_n;
        uniform vec2 u_fgo_blur_screen;
        uniform vec2 u_fgo_blur_center;
        uniform vec2 u_fgo_blur_rc;
    """

    _VN_BLUR_FUNCTIONS = """
        vec3 fgo_blur_tap(sampler2D t, vec2 uv) {
            return texture2D(t, clamp(uv, 0.0, 1.0)).rgb;
        }
    """

    # Ren'Py binds only uniforms the main shader body mentions, so the step is inlined there.
    _VN_BLUR_PX = ("vec2 fgo_px = 1.0 / ((u_fgo_blur_screen.x > 0.0 ? u_fgo_blur_screen : res0)"
                   " * vec2(%r, %r));\n" % (VN_BLUR_RT_UW, VN_BLUR_RT_VH))

    def _vn_blur_gauss_fragment():
        weights = ("u_fgo_blur_wa.x", "u_fgo_blur_wa.y", "u_fgo_blur_wa.z", "u_fgo_blur_wa.w",
                   "u_fgo_blur_wb.x", "u_fgo_blur_wb.y", "u_fgo_blur_wb.z", "u_fgo_blur_wb.w",
                   "u_fgo_blur_wc")
        ring_of = dict((d2, i) for i, (d2, _) in enumerate(VN_BLUR_GAUSS_RINGS))
        rings = [[] for _ in VN_BLUR_GAUSS_RINGS]
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                if 0 < abs(dx) + abs(dy) <= 4:
                    rings[ring_of[dx * dx + dy * dy]].append((dx, dy))
        def ring(i):
            taps = " + ".join("fgo_blur_tap(tex0, fgo_uv + fgo_px * vec2(%d.0, %d.0))" % t for t in rings[i])
            return "fgo_rgb += %s * (%s);\n" % (weights[i], taps)
        src = _VN_BLUR_PX + ("vec2 fgo_uv = v_tex_coord.xy;\n"
               "vec4 fgo_c = texture2D(tex0, fgo_uv);\n"
               "vec3 fgo_rgb = u_fgo_blur_wa.x * fgo_c.rgb;\n")
        src += ring(1)
        # SAMPLERANGE_2 / _3 / _4 add rings; the shader never reads rings past its keyword.
        src += "if (u_fgo_blur_n > 1.5) {\n" + ring(2) + ring(3) + "}\n"
        src += "if (u_fgo_blur_n > 2.5) {\n" + ring(4) + ring(5) + "}\n"
        src += "if (u_fgo_blur_n > 3.5) {\n" + ring(6) + ring(7) + ring(8) + "}\n"
        # rgb is blurred; alpha is the centre texel's.
        src += "gl_FragColor = vec4(fgo_rgb, fgo_c.a);\n"
        return src

    def _vn_blur_motion_fragment():
        src = _VN_BLUR_PX + """
            vec2 fgo_uv = v_tex_coord.xy;
            vec4 fgo_c = texture2D(tex0, fgo_uv);
            vec3 fgo_rgb = fgo_c.rgb;
            vec2 fgo_p = vec2(%(u0)r + fgo_uv.x * %(uw)r, %(v0)r + (1.0 - fgo_uv.y) * %(vh)r);
            vec2 fgo_v = fgo_p - u_fgo_blur_center;
            float fgo_d = length(fgo_v);
            if (fgo_d > 0.0) {
                vec2 fgo_dir = fgo_v / fgo_d;
                vec2 fgo_o = fgo_px * vec2(fgo_dir.x, -fgo_dir.y);
                vec3 fgo_b = u_fgo_blur_wa.x * fgo_c.rgb
                    + u_fgo_blur_wa.y * (fgo_blur_tap(tex0, fgo_uv + fgo_o) + fgo_blur_tap(tex0, fgo_uv - fgo_o));
                if (u_fgo_blur_n > 1.5) {
                    fgo_b += u_fgo_blur_wa.z * (fgo_blur_tap(tex0, fgo_uv + 2.0 * fgo_o) + fgo_blur_tap(tex0, fgo_uv - 2.0 * fgo_o));
                }
                if (u_fgo_blur_n > 2.5) {
                    fgo_b += u_fgo_blur_wa.w * (fgo_blur_tap(tex0, fgo_uv + 3.0 * fgo_o) + fgo_blur_tap(tex0, fgo_uv - 3.0 * fgo_o));
                }
                if (u_fgo_blur_n > 3.5) {
                    fgo_b += u_fgo_blur_wc * (fgo_blur_tap(tex0, fgo_uv + 4.0 * fgo_o) + fgo_blur_tap(tex0, fgo_uv - 4.0 * fgo_o));
                }
                // Clear ring at distance _Range from the centre, blur growing with _Coefficient.
                float fgo_m = clamp(u_fgo_blur_rc.y * (fgo_d - u_fgo_blur_rc.x) * (fgo_d - u_fgo_blur_rc.x), 0.0, 1.0);
                fgo_rgb = mix(fgo_c.rgb, fgo_b, fgo_m);
            }
            gl_FragColor = vec4(fgo_rgb, fgo_c.a);
        """ % {"u0": VN_BLUR_RT_U0, "uw": VN_BLUR_RT_UW, "v0": VN_BLUR_RT_V0, "vh": VN_BLUR_RT_VH}
        return src

    # Custom/Sprite-ScriptActionRender{Gray,Inversion,Darkred,Aberration}: rgb from the RT, alpha = the
    # vertex colour's (1), so the stage quad is opaque and empty RT areas show black (Ren'Py's texture is
    # premultiplied, which is that same composite over black). Luma weights and 0.545099974 (139/255) are
    # the shader's own constants. Aberration: channel i samples uv - (P[2i]+1, P[2i+1]+1) * 0.000744047633
    # (1/1344) in the 1024x1024 RT's UV (v up) = u_fgo_cf_ab offsets here, pre-scaled to stage UV (v down).
    renpy.register_shader("fgo.camera_filter", variables="""
            uniform sampler2D tex0;
            attribute vec2 a_tex_coord;
            varying vec2 v_tex_coord;
            uniform float u_fgo_cf_mode;
            uniform vec2 u_fgo_cf_r;
            uniform vec2 u_fgo_cf_g;
            uniform vec2 u_fgo_cf_b;
        """,
        vertex_200="v_tex_coord = a_tex_coord;",
        fragment_300="""
            vec2 fgo_uv = v_tex_coord.xy;
            vec3 fgo_rgb = texture2D(tex0, fgo_uv).rgb;
            if (u_fgo_cf_mode > 3.5) {
                fgo_rgb = vec3(texture2D(tex0, clamp(fgo_uv + u_fgo_cf_r, 0.0, 1.0)).r,
                               texture2D(tex0, clamp(fgo_uv + u_fgo_cf_g, 0.0, 1.0)).g,
                               texture2D(tex0, clamp(fgo_uv + u_fgo_cf_b, 0.0, 1.0)).b);
            } else if (u_fgo_cf_mode > 2.5) {
                float fgo_l = dot(fgo_rgb, vec3(0.300000012, 0.600000024, 0.100000001));
                fgo_rgb = vec3(fgo_l * 0.545099974, 0.0, 0.0);
            } else if (u_fgo_cf_mode > 1.5) {
                fgo_rgb = 1.0 - fgo_rgb;
            } else if (u_fgo_cf_mode > 0.5) {
                float fgo_l = dot(fgo_rgb, vec3(0.300000012, 0.600000024, 0.100000001));
                fgo_rgb = vec3(fgo_l);
            }
            gl_FragColor = vec4(fgo_rgb, 1.0);
        """)

    def vn_camera_filter_offsets(p):
        k = 0.000744047633
        return tuple((-(p[2 * i] + 1.0) * k / VN_BLUR_RT_UW, (p[2 * i + 1] + 1.0) * k / VN_BLUR_RT_VH)
                     for i in range(3))

    renpy.register_shader("fgo.blur_gauss", variables=_VN_BLUR_VARIABLES,
                          fragment_functions=_VN_BLUR_FUNCTIONS,
                          vertex_200="v_tex_coord = a_tex_coord;",
                          fragment_300=_vn_blur_gauss_fragment())
    renpy.register_shader("fgo.blur_motion", variables=_VN_BLUR_VARIABLES,
                          fragment_functions=_VN_BLUR_FUNCTIONS,
                          vertex_200="v_tex_coord = a_tex_coord;",
                          fragment_300=_vn_blur_motion_fragment())

    def vn_blur_transform():
        """Camera filter on the main scene quad (meshRenderBase). Off, it is a plain pass-through."""
        def fn(trans, st, at):
            filt, mat, running = vn_blur_eval(renpy.store.vn_blur)
            shader = None
            if filt == "motion":
                w = vn_blur_motion_factors(int(mat["sr"]), mat["sigma"])
                if w[0] < 1.0:
                    shader = "fgo.blur_motion"
                    trans.u_fgo_blur_wa = (w[0], w[1], w[2], w[3])
                    trans.u_fgo_blur_wc = w[4]
                    # Unity UV (v up), same space as the fragment's fgo_p.
                    trans.u_fgo_blur_center = (mat["cx"], mat["cy"])
                    trans.u_fgo_blur_rc = (mat["range"], mat["coef"])
                    trans.u_fgo_blur_n = float(mat["sr"])
            elif filt in ("lens", "glass"):
                # FrostedGlass runs the same diamond kernel at radius (int)_BlurLv. Its _BumpMap
                # scatter is not reproduced: the frosted_glass texture is not available.
                sample_range = int(mat["blurlv"]) if filt == "glass" else int(mat["sr"])
                w = vn_blur_gauss_factors(sample_range, mat["sigma"])
                if w[0] < 1.0:
                    shader = "fgo.blur_gauss"
                    trans.u_fgo_blur_wa = tuple(w[0:4])
                    trans.u_fgo_blur_wb = tuple(w[4:8])
                    trans.u_fgo_blur_wc = w[8]
                    trans.u_fgo_blur_n = float(sample_range)
            # [cameraFilter]: only when no blur shader holds the material.
            cam = (renpy.store.vn_blur or {}).get("cam")
            if not shader and filt is None and cam in VN_CAMERA_FILTERS:
                shader = "fgo.camera_filter"
                trans.u_fgo_cf_mode = float(VN_CAMERA_FILTERS.index(cam) + 1)
                off = vn_camera_filter_offsets(renpy.store.vn_blur.get("cam_params") or [0.0] * 16)
                trans.u_fgo_cf_r, trans.u_fgo_cf_g, trans.u_fgo_cf_b = off
            if shader == "fgo.camera_filter":
                trans.mesh = True
                trans.shader = shader
            elif shader:
                trans.u_fgo_blur_screen = tuple(VN_BLUR_SCREEN_PARAMS) if VN_BLUR_SCREEN_PARAMS else (0.0, 0.0)
                trans.mesh = True
                trans.shader = shader
            else:
                trans.mesh = False
                trans.shader = None
            return 0 if running else None
        return Transform(function=fn)
