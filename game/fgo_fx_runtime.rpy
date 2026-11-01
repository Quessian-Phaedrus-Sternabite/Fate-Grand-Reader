# ============================================================================
#  fgo_fx_runtime.rpy -- ONE generic player for every converted particle effect.
#
#  tools/fgo_fx_convert.py turns a Talk-effect prefab into a data-only file
#  under game/fx_generated/ (fgo_fx_register(name, spec)); this file plays any
#  of them. It is the per-effect Cloud classes in fgo_core.rpy
#  (FGOImpactLandingCloud, FGOBitTalk14Cloud, FGOBitTalk41Cloud, ...) merged
#  into one, keeping each of their Unity-verified conventions:
#
#    * fgo_px_per_unit=288 flat conversion for size/velocity (Shape/Local
#      scalingMode), hierarchy-scaled for scalingMode=Hierarchy (bit_talk_41_fs)
#    * the `spread` (shape offset, carries charaScale) / `unit` (size and
#      velocity, exempt) split
#    * min/maxParticleSize clamp against the layer-13 1344-FGO-px reference or
#      the live screen width for fowardEffect
#    * Stretched Billboard: clamp on the base width only, length unclamped
#      (bit_talk_14 hard-edge capture); signed length = size*lengthScale +
#      speed*velocityScale: positive trails BEHIND the particle, negative
#      extends AHEAD, texture u=0 at the particle (ProbeOpen.cs Stretch);
#      the lengthScale term is foreshortened when the velocity has a depth
#      component (bit_talk_34 Unity renders)
#    * prewarm = steady state, every earlier cycle counts (ProbeOpen.cs Prewarm)
#    * blit from the render's own size (rotate_pad)
#    * [effectStop]: particles born after the stop never appear, particles
#      already in flight finish their lives
#    * looping emitters draw a fresh, deterministic population per cycle
#      (bit_talk_41's per-cycle skew fix), seeded with crc32, never hash()
#
#  and adding what the converter now extracts that those classes didn't:
#  m_IsActive animation windows per emitter, emitter X offsets, UV flipbook
#  curves + cycles, sort mode, rotation ranges, velocityOverLifetime curves
#  integrated over age (not curve*age), and -- since 2026-09-24 -- REAL 3D
#  geometry: each shape is sampled in its local 3D frame, mapped by the
#  emitter's world matrix (any rotation, not just 90-degree turns), and
#  projected by dropping world Z. The hand ports' 2D samplers in fgo_core.rpy
#  (_fgo_spawn_offset / _fgo_spawn_dir) are no longer used here.
#
#  Since 2026-09-25 also NGUI widgets (UISprite / UITexture quads, see
#  "NGUI widgets" below), animated transforms (per-phase tracks from
#  tools/fgo_fx_anim.py, composed per frame by _fgo_fx_nodes), World-space
#  particle emitters that move (spawned through the transform at their
#  birth, _fgo_fx_emitter_frame), a unified draw order spec["order"] for
#  prefabs with NGUI content, and widget destruction at
#  T7 + max(end clip, losttime) (_fgo_fx_destroy_time).
#
#  Reader integration: vn_effects.rpy owns commands, clocks, anchors, and rigs.
#  Demo show/stop/hide functions are omitted. Generated definitions are unchanged.
# ============================================================================

init -5 python:

    import math

    # --- the frame length of a converted effect (prompts/EFFECTS_30FPS.md) ----------------
    # The GAME runs a story scene at Application.targetFrameRate = 30 with Fixed Timestep and
    # Maximum Particle Timestep 1/30 (notes/prewarm_unify.md Phase 1), so after t = 0 every
    # particle system advances in steps of 1/30 s. `_fgo_fx_frame_hz` is the ONE switch (the
    # user's decision 2026-10-01): 30 = the game's frame (default), 60 = the older 1/60 s grid
    # (kept for comparisons: with 60 the runtime reproduces commit 0225fbf exactly).
    # `_FGO_FX_HZ` / `_FGO_FX_FRAME` are derived from it by _fgo_fx_set_frame_hz; every rule
    # that is written "per frame" reads them, nothing else hard-codes a frame length.
    # Not derived from it, on purpose:
    #   * _FGO_FX_PW_FIXED / _FGO_FX_PW_MAXPART: the game's own time settings (the prewarm),
    #     independent of the display's rate;
    #   * _FGO_FX_CLAMP_DT: the resolution of the age tables the stepped tracks are resampled to
    #     (linear between samples), not a frame. It scales with the frame rate (hz / 7200: 1/120 s
    #     at 60, 1/240 s at 30): the chord error at a frame kink of a mid-frame-born particle is
    #     dv x resolution / 4 and dv grows with the frame length, so a coarser frame needs a finer
    #     table to keep the error at the level the 1/60 s runtime had (tools/fx_verify/fps30_rt.py);
    #   * _FGO_FX_DAMPEN_HZ: Limit Velocity's dampen is normalised to 30 Hz by Unity
    #     (k = 1 - (1 - dampen)^(30 h), a per-second form).
    _fgo_fx_frame_hz = 30.0
    # Display (prompts/EFFECTS_30FPS.md Step 4): True = a converted effect shows the state of the latest
    # frame of its own clock (k = floor(t x hz)) and redraws at the next frame boundary, like the game
    # (Application.targetFrameRate 30); text, UI and transitions keep the display's rate. False = the
    # frame-grid simulation drawn at continuous time (smooth, at the display's own rate).
    _fgo_fx_display_30 = True
    # Which state the k-th displayed frame (k = 0 the creation frame) shows: Unity instantiates the effect
    # and plays its start clip in ONE frame, evaluating a Legacy clip at 0 while the particle systems
    # ALREADY advance one step (ps.time = dt at the end of the creation frame), so frame k shows the
    # particle systems after k + 1 steps and Legacy-clip values at k dt [Unity-verified: notes/modules.md
    # "the clip is evaluated at 0 ... while the particle system already advances dt", mesh_objects.md
    # "the creation frame already shows dt", the rigs' one-frame lead (xform.md, stretchscale.md,
    # ngui_clip.md); here F3GridRunner: ps.time = dt after frame 0]. True = particle clocks run one
    # frame ahead of the clip clock (only with _fgo_fx_display_30); False = both at k dt.
    _fgo_fx_display_lead = True
    # Unity-verified 2026-10-02 (SFExtraRunner / clk_rate7_life, 0_real_fit.py):
    # no Birth children during the frame that removes their parent particle.
    _fgo_fx_sub_death_frame = True
    _fgo_fx_sub_onset = True    # delayed time-0 child bursts fire at their frame START (small_rules_2)
    # A system a clip switches on (m_IsActive) starts at the first FRAME whose clip time reaches the
    # key, and its particle clock at the start of that frame: the activation time is rounded UP to the
    # frame grid (an exact key keeps its frame). Without it a key at 0.167 s (an odd multiple of the
    # 1/60 s the clips were authored at) activated a particle system 1/60 s ... 1/30 s too early.
    # [Unity: Legacy Animation evaluates the clip at k dt in frame k; F3 probes, see notes] True = on;
    # False = the exact key time (the runtime before the 30 fps change).
    _fgo_fx_clip_frames = True
    _FGO_FX_HZ = 30.0
    _FGO_FX_FRAME = 1.0 / 30.0
    _FGO_FX_CLAMP_DT = 30.0 / 7200.0
    _FGO_FX_DAMPEN_HZ = 30.0

    def _fgo_fx_set_frame_hz(hz):
        """Set the frame rate every per-frame rule runs at (also done lazily by
        FGOFXCloud.render when `_fgo_fx_frame_hz` was assigned directly). Every cache
        that holds frame-dependent results is dropped."""
        global _FGO_FX_HZ, _FGO_FX_FRAME, _FGO_FX_CLAMP_DT, _fgo_fx_frame_hz
        hz = float(hz)
        _fgo_fx_frame_hz = hz
        _FGO_FX_HZ = hz
        _FGO_FX_FRAME = 1.0 / hz
        _FGO_FX_CLAMP_DT = hz / 7200.0
        for name_ in ("_fgo_fx_chunk_cache", "_fgo_fx_rate_tabs", "_fgo_fx_rate_cache",
                      "_fgo_fx_oneshot_cache", "_fgo_fx_loopoff_cache", "_fgo_fx_dist_cache",
                      "_fgo_fx_cap_state", "_fgo_fx_wrot_cache", "_fgo_fx_cycle_cache", "_fgo_fx_burst_cache"):
            d_ = globals().get(name_)
            if d_ is not None:
                d_.clear()
        t_ = globals().get("_fgo_fx_tint_clock")
        if t_ is not None:
            del t_[1:]

    def _fgo_fx_sync_frame_hz():
        if float(_fgo_fx_frame_hz) != _FGO_FX_HZ:
            _fgo_fx_set_frame_hz(_fgo_fx_frame_hz)

    fgo_fx_defs = {}

    # (runtime_performance 2026-10-23) Performance only, the same numbers: Ren'Py compiles every
    # list / dict / set literal, list(), dict(), range() and sorted() in .rpy code into its
    # rollback-tracked Revertable* types, whose item access is a Python-level call and whose every
    # append logs the object for rollback. The effect data and the render path need no rollback, so
    # True = specs are stored as plain list / dict / set (fgo_fx_register, before anything keys on
    # their id()) and the runtime's functions use the plain types (_fgo_fx_plain_patch at the end
    # of this file); the public show / stop / hide API keeps Ren'Py's types. An OFF comparison sets
    # it between this block and the specs' init -1 (an init -2 python block). [regress IDENTICAL, notes/runtime_performance.md]
    _fgo_fx_plain = True

    # the base of the runtime's helper classes: Ren'Py's store `object` logs every attribute write
    # for rollback (RevertableObject); with _fgo_fx_plain the plain built-in (read at init 0)
    _FGO_FX_OBJECT = object

    def _fgo_fx_plain_data(x, memo):
        """A deep copy of registered spec data with plain containers (shared objects stay shared)."""
        t = type(x)
        if t is int or t is float or t is str or t is bool or x is None:
            return x
        got = memo.get(id(x))
        if got is not None:
            return got
        if isinstance(x, python_list):
            rv = python_list()
            memo[id(x)] = rv
            rv.extend(_fgo_fx_plain_data(v, memo) for v in x)
        elif isinstance(x, python_dict):
            rv = python_dict()
            memo[id(x)] = rv
            for k, v in x.items():
                rv[k] = _fgo_fx_plain_data(v, memo)
        elif t is tuple:
            rv = tuple(_fgo_fx_plain_data(v, memo) for v in x)
            memo[id(x)] = rv
        elif isinstance(x, (python_set, frozenset)):
            rv = (python_set if isinstance(x, python_set) else frozenset)(x)
            memo[id(x)] = rv
        else:
            return x
        return rv

    # id() of every Birth sub-emitter parent (filled by fgo_fx_register; _fgo_fx_fclock_scope)
    _fgo_fx_sub_parents = set()

    def fgo_fx_register(name, spec):
        if _fgo_fx_plain:
            spec = _fgo_fx_plain_data(spec, {})
        fgo_fx_defs[name] = spec
        for e_ in spec["emitters"]:
            if "sub" in e_:
                # a Birth sub-emitter's parent keeps the lattice (its verified child rules)
                _fgo_fx_sub_parents.add(id(spec["emitters"][e_["sub"]["p"]]))
        spec["duration"] = _fgo_fx_done_time(spec, None)

    # --- effect lifecycle (CommonEffectComponent) ------------------------------
    # Decompiled from CommonEffectComponent.NextPlayAnimation / Update /
    # StopInternal (FGO assets/pseudocode/codes/CommonEffectComponent.cpp):
    #   * "_start" (or a clip named baseName, or a lone clip) plays first;
    #     entering it resets endtime to 0. With no Animation at all, the start
    #     status instead waits for `endtime`.
    #   * then, only if the component's `loop` flag is set and a "_loop" clip
    #     exists, "_loop" repeats until [effectStop];
    #   * stop clears `loop` and sets the _loop state's wrapMode to Once: the
    #     cycle in progress finishes, then "_end" plays. A stop during _start
    #     goes straight to _end after _start (no loop cycle).
    #   * entering _end (status 7) calls ParticleSystem.Stop() on every
    #     particlelist entry -- emission stops, live particles finish. Emitters
    #     NOT in a particlelist keep going while their GameObject is active.
    # "T7" below = the time the owner enters status 7.

    # An Animation with NO start clip (neither <root>_start nor <root>) but a found
    # <root>_loop on a loop=1 root (spec "start_absent", converter): Init's
    # NextPlayAnimation(3) falls through to the loop branch and enters status 4 at
    # once (CommonEffectComponent.cpp NextPlayAnimation ~1499-1557); Update waits
    # `endtime` only in status 3 (~3326-3331), so the loop starts at t = 0. endtime
    # is NOT zeroed (only a found start clip does that, ~1524) and is kept for the
    # Destroy check. [Decompile + EPVoiceLifecycle / D2 lifecycle replicas on the
    # real voice01 / noise_gr bundles, notes/diag2.md Part 1; the user's decision
    # 2026-10-07.] True = Unity's rule; False = the older runtime, which waited
    # `endtime` as if it were the start clip's length.
    _fgo_fx_absent_start = True

    # (diag2 Part 2, 2026-10-07) Plain particle systems at one sortingOrder + queue that mix alpha
    # and additive blends draw in Unity's equal-queue order: far first by the float32 bounds-centre
    # depth (+ sortingFudge), EXACT ties by the global renderer list (registration: frame-0
    # Instantiate rank, else the switch-on's clip binding order, children first) -- spec "dorder",
    # converter _plain_mixed_decide, only where that differs from the older emitter-z / alpha-first
    # order. [Unity-decoded real bit_talk_20_remake: same-salt solo layers composite to the full
    # frame within 2-4 / 255 only in this order; notes/diag2.md Part 2.] False = the older order.
    _fgo_fx_mixed_list = True

    # (diag2 Part 3, 2026-10-07) Clip HANDOFFS on the frame grid. CommonEffectComponent asks for the
    # next clip only in the Update AFTER the current one stopped playing (Update: isPlaying ->
    # return; NextPlayAnimation; Play), and a Legacy Once clip played in frame p is evaluated at
    # (k - p) dt in frame k and stops after its LAST evaluation, the first frame k >= p + 1 whose
    # clip time reaches its length (sampled at the length). So: the loop's time 0 is in frame
    # last(start) + 1; a stop (wrapMode Once) during the FIRST loop cycle lets that cycle finish,
    # a stop at or after the first cycle's end cuts the loop in the stop frame (the state's time is
    # not wrapped); either way the loop's last frame shows its END value and _end starts one frame
    # later; a stop during the start clip: _end one frame after the start's last evaluation.
    # [Unity-verified 2026-10-07, D2ClipRunner: exact CommonEffectComponent replica, synthetic
    # clips at 1/30, 9 cases (start 0 / 0.7 / 0.75 / 1, loop 0 / 0.55 / 1 / 10.666667, stops in
    # the start, the first cycle, a later cycle, on a cycle end): every transition frame and
    # sampled value; real bit_talk_10_violet's end at frame 343, lens_flare's loop at frame 31;
    # notes/diag2.md Part 3.] False = the older analytic boundaries (start length, whole cycles).
    _fgo_fx_clip_handoff = True

    # (diag2 Part 3) A Legacy clip's m_IsActive key at k/30 s exactly (the clips are authored on
    # the 1/60 s grid: 1/6 = 0.16666667) takes effect IN frame k, a key 0.001 frames later in frame
    # k + 1 [Unity-verified 2026-10-07, D2KeyRunner: 36 on / off keys, k/30 vs 0.0667 / 0.1667 /
    # 0.167 / 0.2334 ...]. Specs store event times to 4 decimals, so a key k/30 whose rounding goes
    # UP (0.0667, 0.1667, 0.2667 ...) used to act one frame late; True = spec keys within 6e-5 s of
    # a frame boundary (the rounding of k/30 is <= 3.4e-5) count as that boundary (real
    # bit_talk_10_violet: lightlong_add off at end-clip frame 5 in all 4 Unity salts).
    _fgo_fx_key_snap = True

    _fgo_fx_clip_last_cache = {}

    def _fgo_fx_clip_last(L):
        """Frames from a Legacy Once clip's Play to its LAST evaluation: the first frame k >= 1
        whose clip time k x float32(1 / 30) (in double precision) is STRICTLY greater than the
        clip's float32 length [D2ClipRunner: 1.0 -> 30, 0.7 -> 21, 0.75 -> 23, 0.55 -> 17, 0 -> 1,
        float32(0.1666667) -> 6, Loop states switched to Once alike; real bit_talk_06: _start (last
        key 0.033333335 = float32(1/30), equal at frame 1) -> 2, _loop (0.33333334) -> 10].
        Spec lengths are rounded to 4 decimals; the clips are authored on the 1/60 s grid, so a
        length within 6e-5 s of k / 60 is taken as float32(k / 60) (10.6667 -> 320 frames)."""
        r = _fgo_fx_clip_last_cache.get((L, _FGO_FX_HZ))
        if r is not None:
            return r
        import struct
        def f(x):
            return struct.unpack("f", struct.pack("f", x))[0]
        if L <= 0.0:
            r = 1
        else:
            k60 = round(L * 60.0)
            Lf = f(k60 / 60.0) if abs(L - k60 / 60.0) < 6e-5 else f(L)
            dt, r = f(_FGO_FX_FRAME), 0
            while True:
                r += 1
                if r * dt > Lf:
                    break
        _fgo_fx_clip_last_cache[(L, _FGO_FX_HZ)] = r
        return r

    def _fgo_fx_loop_period(spec, L):
        """Seconds of one root loop cycle: L for a Loop-wrap _loop clip; for a _loop clip without
        Loop wrap (spec "loop_once", _fgo_fx_clip_handoff) the component re-Plays it the frame
        after each pass: last evaluation + 1 frames."""
        if _fgo_fx_clip_handoff and spec.get("loop_once") and L is not None:
            return (_fgo_fx_clip_last(L) + 1) * _FGO_FX_FRAME
        return L

    def _fgo_fx_start_len(spec, o):
        """Length of the root's start status: the start clip's length (with
        _fgo_fx_clip_handoff: up to the frame the next clip starts in), else 0 for
        the absent-start shape (_fgo_fx_absent_start), else the owner's endtime."""
        S = spec["clips"]["start"]
        if S is not None:
            if _fgo_fx_clip_handoff and spec["has_anim"]:
                return (_fgo_fx_clip_last(S) + 1) * _FGO_FX_FRAME
            return S
        if _fgo_fx_absent_start and spec.get("start_absent"):
            return 0.0
        return o["endtime"]

    def _fgo_fx_t7(spec, owner, stop):
        o = spec["owners"][owner]
        if o["root"] and spec.get("vn_force_t7") is not None:
            return spec["vn_force_t7"]
        if not o["root"]:
            if o.get("t7") is not None:
                # An Animation none of whose clips the component finds by
                # name: status 7 at once (converter _gate_animations).
                return o["t7"]
            return None if o["loop"] else o["endtime"]
        if not spec["has_anim"]:
            if not o["loop"]:
                return o["endtime"]
            return None if stop is None else max(stop, o["endtime"])
        cl = spec["clips"]
        S = _fgo_fx_start_len(spec, o)
        L = cl["loop"]
        if not (o["loop"] and L is not None):
            return S
        if stop is None:
            return None
        if _fgo_fx_clip_handoff:
            # frame-grid handoffs (see _fgo_fx_clip_handoff): the stop's frame f, the loop's
            # first frame k1 (S = k1 dt)
            f = int(math.floor(stop * _FGO_FX_HZ + 1e-6))
            k1 = int(round(S * _FGO_FX_HZ))
            if f <= k1:
                return S                       # stopped before the loop's first Update: _end then
            last = _fgo_fx_clip_last(L)
            if spec.get("loop_once"):
                # a _loop clip without Loop wrap: the component re-Plays it the frame after each
                # pass (cycle = last + 1 frames); a stop lets the pass in progress finish, a stop
                # in a pass's first frame ends it at once [D2ClipRunner once cases; real bit_talk_06]
                P = last + 1
                j = f - k1
                return (f if j % P == 0 else k1 + (j // P + 1) * P) * _FGO_FX_FRAME
            # Loop wrap: the state's time is not wrapped, so a stop (Once) finishes the first
            # pass, and stops at once after it; _end one frame after the last evaluation
            return (max(f, k1 + last) + 1) * _FGO_FX_FRAME
        if stop <= S:
            return S
        if L <= 0:
            # A zero-length _loop clip (bit_talk_20re): the component sets the
            # _loop state's wrapMode to Loop and only moves on when the
            # animation stops playing, which a looping state never does -- so
            # the loop phase holds until [effectStop], then ends at once.
            # [From the decompile (set_wrapMode(_loop, 2) + isPlaying checks);
            # CommonEffectComponent is a stub in the export, so not runnable.]
            return stop
        return S + (int((stop - S) // L) + 1) * L

    def _fgo_fx_final(ph):
        return ph[1][-1][1] if ph[1] else ph[0]

    def _fgo_fx_window(e, spec, T7, st):
        """_fgo_fx_window_raw with the activation time rounded up to the frame grid
        (_fgo_fx_clip_frames)."""
        w = _fgo_fx_window_raw(e, spec, T7, st)
        if w is not None and _fgo_fx_clip_frames and w[0] > 1e-9:
            return (math.ceil(w[0] * _FGO_FX_HZ - (2e-3 if _fgo_fx_key_snap else 1e-6)) / _FGO_FX_HZ, w[1])
        return w

    def _fgo_fx_window_raw(e, spec, T7, st):
        """(on_time, key) of the activation window emitter `e` is in at st, or
        None if it's inactive. key is a stable id for that activation (seeds
        its particles). Walks back across clip phases while the emitter stays
        continuously active, since Unity only restarts a ParticleSystem when
        its GameObject is re-activated."""
        act = e["act"]
        if not spec["has_anim"]:
            s, ev = act["rest"]
            if not ev:
                return (0.0, "r") if s else None
            # An Animator's m_IsActive curves (converter _phase_states /
            # _ABS_ACT): activity over absolute effect time.
            on_at = 0.0 if s else None
            for t, v in ev:
                if t > st + (6e-5 if _fgo_fx_key_snap else 0.0):
                    break
                if v and not s:
                    on_at = t
                s = v
            if not s:
                return None
            return (on_at, "r@%.4f" % on_at)
        cl = spec["clips"]
        o = spec["owners"][0]
        S = _fgo_fx_start_len(spec, o)
        L = cl["loop"] if (o["loop"] and cl["loop"] is not None) else None
        E = cl["end"] or 0.0
        looped = L is not None and T7 is not None and T7 > S
        loop_end = T7 if T7 is not None else float("inf")
        Lp = _fgo_fx_loop_period(spec, L)

        def phase(seg):
            k = seg[0]
            if k == "S":
                return act["start"] or act["rest"]
            if k == "L":
                return act["loop"]
            if k == "E":
                return act["end_l" if looped else "end_s"]
            return act["post_l" if looped else "post_s"]

        def start_of(seg):
            k = seg[0]
            if k == "S":
                return 0.0
            if k == "L":
                return S + seg[1] * Lp
            if k == "E":
                return T7
            return T7 + (E if act["end_s"] is not None else 0.0)

        def prev(seg):
            k = seg[0]
            if k == "S":
                return None
            if k == "L":
                return ("L", seg[1] - 1) if seg[1] > 0 else ("S",)
            if k == "P" and act["end_s"] is not None and E > 0:
                return ("E",)
            if looped:
                if _fgo_fx_clip_handoff:
                    # the cycle holding the loop's last frame (T7 - one frame)
                    return ("L", int(math.floor((T7 - _FGO_FX_FRAME - S) / Lp + 1e-6)) if Lp > 0 else 0)
                return ("L", (int(round((T7 - S) / L)) - 1) if L > 0 else 0)
            return ("S",)

        if st < S:
            seg = ("S",)
        elif L is not None and st < loop_end:
            if _fgo_fx_clip_handoff:
                seg = ("L", int(math.floor((st - S) / Lp + 1e-6)) if Lp > 0 else 0)
            else:
                seg = ("L", int((st - S) // L) if L > 0 else 0)
        elif T7 is not None and act["end_s"] is not None and st < T7 + E:
            seg = ("E",)
        else:
            seg = ("P",)

        ph = phase(seg)
        t0 = start_of(seg)
        u = st - t0
        s = ph[0]
        on_at = None
        for t, v in ph[1]:
            if t > u + (6e-5 if _fgo_fx_key_snap else 0.0):
                break
            if v and not s:
                on_at = t
            s = v
        if not s:
            return None
        if on_at is not None:
            return (t0 + on_at, "%s%s@%.4f" % (seg[0], seg[1] if len(seg) > 1 else "", on_at))
        # Active since this segment began: walk back while still active.
        while True:
            p = prev(seg)
            if p is None:
                return (t0, "%s%s@0" % (seg[0], seg[1] if len(seg) > 1 else ""))
            pph = phase(p)
            if p[0] == "L" and pph[0] and not pph[1]:
                # Every loop cycle is fully active: the activation reaches back
                # to the first cycle at least -- continue from there.
                seg, t0 = ("L", 0), S
                p = ("S",)
                pph = phase(p)
            if not _fgo_fx_final(pph):
                return (t0, "%s%s@0" % (seg[0], seg[1] if len(seg) > 1 else ""))
            s = pph[0]
            on_at = None
            for t, v in pph[1]:
                if v and not s:
                    on_at = t
                s = v
            pt0 = start_of(p)
            if on_at is not None:
                return (pt0 + on_at, "%s%s@%.4f" % (p[0], p[1] if len(p) > 1 else "", on_at))
            seg, t0 = p, pt0

    def _fgo_fx_phase(spec, T7, st):
        """(phase, u): which clip phase the root lifecycle is in at st and
        the time into it -- the phases animated tracks are keyed by
        (tools/fgo_fx_anim.py): start, loop (u = time into the current
        cycle), end_l / end_s (end clip after a loop / straight after start),
        post_l / post_s (after the end clip: everything holds). "rest" when
        the prefab has no Animation. Same segmentation as _fgo_fx_window."""
        if not spec["has_anim"]:
            return ("rest", 0.0)
        cl = spec["clips"]
        o = spec["owners"][0]
        S = _fgo_fx_start_len(spec, o)
        L = cl["loop"] if (o["loop"] and cl["loop"] is not None) else None
        E = cl["end"]
        looped = L is not None and T7 is not None and T7 > S
        if st < S:
            if _fgo_fx_clip_handoff and cl["start"] is not None:
                return ("start", min(st, cl["start"]))   # the Once clip holds its end value
            return ("start", st)
        if L is not None and (T7 is None or st < T7):
            if L <= 0:
                return ("loop", 0.0)
            if _fgo_fx_clip_handoff and T7 is not None and st >= T7 - _FGO_FX_FRAME - 1e-9:
                return ("loop", L)        # the loop's last frame: Once, sampled at its end
            if _fgo_fx_clip_handoff and spec.get("loop_once"):
                # re-Played passes: time into the pass, held at the clip's end in its last frame
                Lp = _fgo_fx_loop_period(spec, L)
                return ("loop", min(L, max(0.0, (st - S) - math.floor((st - S) / Lp + 1e-6) * Lp)))
            if _fgo_fx_clip_handoff:
                # a cycle end landing on a frame wraps to 0 (D2ClipRunner: 1.1 s of a 0.55 s loop)
                return ("loop", max(0.0, (st - S) - math.floor((st - S) / L + 1e-6) * L))
            return ("loop", (st - S) - math.floor((st - S) / L) * L)
        if T7 is not None and E is not None and st < T7 + E:
            return ("end_l" if looped else "end_s", st - T7)
        return ("post_l" if looped else "post_s", 0.0)

    def _fgo_fx_destroy_time(spec, owner, stop):
        """When a CommonEffectComponent Destroy()s its GameObject (and so
        every widget under it): Update() in status 7, once the end animation
        has stopped playing and totaltime > endtime + losttime, endtime
        having been raised to the time status 7 was entered (decompiled,
        CommonEffectComponent.Update; isDestroy defaults to 1 and
        SetEndlessEnable is never called). None = not before a stop.
        This analytic time drives emission/widgets. Particle drawing uses
        _fgo_fx_particle_destroy for the independently verified strict frame."""
        o = spec["owners"][owner]
        T7 = _fgo_fx_t7(spec, owner, stop if o["root"] else None)
        if T7 is None:
            return None
        if o.get("keep_endtime"):
            # No start clip was found by name, so endtime was never zeroed:
            # Update raises it to T7 at most (endtime = max(endtime,
            # totaltime) in status 7) -- bit_talk_valentinegift01/02 (an
            # autoplayed lone clip), bit_talk_10*'s bit_liaison01.
            return max(o["endtime"], T7) + o["losttime"]
        E = (spec["clips"]["end"] or 0.0) if (o["root"] and spec["has_anim"]) else 0.0
        if o["root"] and _fgo_fx_absent_start and spec.get("start_absent"):
            # Absent start clip: endtime keeps its serialized value; status 7
            # raises it to T7 at most, and Update destroys once the end clip
            # has stopped AND totaltime > endtime + losttime (decompile).
            return max(T7 + E, max(o["endtime"], T7) + o["losttime"])
        return T7 + max(E, o["losttime"])

    # Known approximation 16, the verified part (prompts/OPUS_DIAG_3.md Part 2): the root's Destroy()
    # removes the particles too. With an end clip (E > 0) and losttime <= E, Update destroys in the
    # frame AFTER the end clip stopped: the end clip stops in the first frame k >= 1 after status 7
    # with k x f32(1/30) > f32(E) (the clip handoff rule), so frame T7 + k + 1. [Unity-verified
    # 2026-10-08, probe tool, real bit_talk_06 stopped at 0.8 s: status 7 in frame 25, end clip
    # 0.3333 stopped in frame 35 with 22 rays alive, all gone in frame 36; look tool: Unity blank
    # from 1.267 s. tools/fx_verify/diag3/probes/p2_bt06.json.] Other owner shapes (no end clip,
    # losttime > E, absent start / kept endtime, nested owners) keep approximation 16. True = no
    # particle is drawn from that frame. Additional verified root shapes use
    # _fgo_fx_destroy_cut_all below; both switches OFF retain finite tails.
    _fgo_fx_destroy_cut = True

    # Descendant destruction (2026-10-10): source Update accumulates a float32
    # total from frame zero, records endtime in the status-7 Update, and checks
    # total > float32(endtime + losttime) only after the end clip stops.
    # Root Destroy removes ALL descendants, including nested owners/unlisted PS.
    # Verified against OWNER/FRAMEPS logs; notes/destroy_descendants.md. Exact
    # serialized times are supplied only for the supported additional shapes.
    # Absent-start/autoplay and kept-endtime owners retain approximation 16.
    _fgo_fx_destroy_cut_all = True
    _fgo_fx_destroy_totals = {}

    def _fgo_fx_owner_total(frame):
        """CommonEffect Update's float32 total AFTER frame's Update (frame0=dt)."""
        clock = _fgo_fx_destroy_totals.setdefault(_FGO_FX_HZ, [])
        dt = _fgo_fx_f32(_FGO_FX_FRAME)
        while len(clock) <= frame:
            clock.append(_fgo_fx_f32((clock[-1] if clock else 0.0) + dt))
        return clock[frame]

    def _fgo_fx_particle_destroy_all(spec, stop):
        """Strict root Destroy for the additional source-proven owner shapes."""
        times = spec.get("destroy_clock")
        if not _fgo_fx_destroy_cut_all or times is None:
            return None
        o = spec["owners"][0]
        if not o["root"] or o.get("keep_endtime") or spec.get("start_absent"):
            return None
        f32 = _fgo_fx_f32
        total = _fgo_fx_owner_total
        if spec["has_anim"]:
            if not _fgo_fx_clip_handoff or spec["clips"]["start"] is None:
                return None
            t7 = _fgo_fx_t7(spec, 0, stop)
            if t7 is None:
                return None
            k7 = int(round(t7 * _FGO_FX_HZ))
            endtime = total(k7)  # found start cleared serialized endtime
            E = spec["clips"]["end"]
        else:
            # Init's empty request is consumed in frame0; status3 is first
            # advanced in frame1 when total >= serialized endtime.
            k7 = 1
            while total(k7) < f32(times[0]):
                k7 += 1
            if o["loop"]:
                if stop is None:
                    return None
                k7 = max(k7, int(math.floor(stop * _FGO_FX_HZ + 1e-6)))
            endtime = max(f32(times[0]), total(k7))
            E = None
        threshold = f32(endtime + f32(times[1]))
        k = k7 + (_fgo_fx_clip_last(E) + 1 if E is not None else 1)
        while total(k) <= threshold:
            k += 1
        return k * _FGO_FX_FRAME

    def _fgo_fx_particle_destroy(spec, stop):
        """Effect time from which the root's Destroy() has removed every particle, or None
        (not destroyed, or outside both verified switches' scopes)."""
        extra = _fgo_fx_particle_destroy_all(spec, stop)
        if extra is not None:
            return extra
        if not _fgo_fx_destroy_cut:
            return None
        o = spec["owners"][0]
        if not o["root"] or o.get("keep_endtime") or (_fgo_fx_absent_start and spec.get("start_absent")):
            return None
        T7 = _fgo_fx_t7(spec, 0, stop)
        if T7 is None or not spec["has_anim"]:
            return None
        E = spec["clips"].get("end") or 0.0
        if E <= 0.0 or o["losttime"] > E:
            return None
        f32 = _fgo_fx_f32
        dt = f32(_FGO_FX_FRAME)
        k = 1
        while f32(k * dt) <= f32(E):
            k += 1
        return T7 + (k + 1) * _FGO_FX_FRAME

    def _fgo_fx_done_time(spec, stop):
        """When the effect has nothing left to draw, or None if it runs until
        stopped (or forever: a looping emitter nobody ever stops)."""
        T7 = _fgo_fx_t7(spec, 0, stop)
        has = spec["has_anim"]
        if T7 is None and has:
            return None   # clip loop running: emitters may re-activate every cycle
        E = (spec["clips"]["end"] or 0.0) if has else 0.0
        end = (T7 + E) if T7 is not None else 0.0
        done = end
        D = _fgo_fx_destroy_time(spec, 0, stop)
        for w in list(spec.get("widgets", ())) + list(spec.get("mobjs", ())):
            ds = [_fgo_fx_destroy_time(spec, i, stop) for i in w.get("owners", (0,))]
            ds = [d for d in ds if d is not None]
            if not ds:
                return None
            done = max(done, min(ds))
        for e in spec["emitters"]:
            if "sub" in e:
                continue          # sub-emitter children: below, after their parent
            if e.get("life_inf"):
                if D is None:
                    return None
                done = max(done, D)
                continue
            ml = e["life"][1] if isinstance(e["life"], (tuple, list)) else e["life"]
            # Activations that can still be running once the clips are over:
            # the window open at the end of the timeline, plus the ones open
            # just before the end clip and just before T7.
            ons = []
            probes = [end + 1e-4]
            if T7 is not None:
                probes += [T7 - 1e-4, end - 1e-4]
            for t in probes:
                w = _fgo_fx_window(e, spec, T7, max(0.0, t))
                if w is not None:
                    ons.append(w[0])
            if not ons:
                continue
            on = max(ons)
            if e["loop"]:
                owner_stop = None
                if "plist" in e:
                    o = spec["owners"][e["plist"]]
                    owner_stop = _fgo_fx_t7(spec, e["plist"], stop if o["root"] else None)
                if D is not None:
                    # The root Destroy()s the whole effect: nothing emits
                    # after that (see render: emission cut).
                    owner_stop = D if owner_stop is None else min(owner_stop, D)
                w_end = _fgo_fx_window(e, spec, T7, end + 1e-4) if T7 is not None or not has else None
                if owner_stop is None:
                    if w_end is not None:
                        return None      # emits for as long as it stays shown
                    continue
                done = max(done, max(owner_stop, on) + ml)
            else:
                done = max(done, on + e.get("delay", 0.0) + e["length"] + ml)
        subs = [e for e in spec["emitters"] if "sub" in e]
        if subs:
            # a child emits while its parent's particles live: the parent's
            # last particle death (done already covers it) + the child's life
            done = done + max((max(e["life"]) if isinstance(e["life"], (tuple, list)) else e["life"]) for e in subs)
        return round(done, 3)

init python:

    if _fgo_fx_plain:
        _FGO_FX_OBJECT = python_object

    # Legacy Shaders/Particles/Alpha Blended: col = clamp(2 * vcol * _TintColor
    # * tex). u_fgo_mul carries 2*_TintColor*particle colour, which can exceed
    # 1, so it can't go through TintMatrix; clamp before re-premultiplying
    # (an LDR target clamps the fragment before blending).
    renpy.register_shader(
        "fgo.fx_matmul",
        variables="""
            uniform vec4 u_fgo_mul;
        """,
        fragment_300="""
            vec4 c = gl_FragColor;
            vec3 rgb = c.a > 0.0 ? c.rgb / c.a : vec3(0.0);
            float a = clamp(c.a * u_fgo_mul.a, 0.0, 1.0);
            gl_FragColor = vec4(clamp(rgb * u_fgo_mul.rgb, 0.0, 1.0) * a, a);
        """)

    # Mesh particles (_fgo_fx_mesh_draw): one Mesh2 per emitter per frame,
    # every particle's triangles in Unity's order. a_fgo_color = particle
    # colour x mesh vertex colour (straight, not premultiplied; alpha may
    # exceed 1 with _oneValue > 1), interpolated across each triangle as the
    # GPU does in Unity; u_fgo_mul = the material multiplier (mat_mul, or 1).
    # The texture sample arrives premultiplied; the product is clamped like
    # an LDR target, then premultiplied again for Ren'Py's blend functions.
    renpy.register_shader(
        "fgo.fx_meshcol",
        variables="""
            uniform vec4 u_fgo_mul;
            attribute vec4 a_fgo_color;
            varying vec4 v_fgo_color;
        """,
        vertex_300="""
            v_fgo_color = a_fgo_color;
        """,
        fragment_300="""
            vec4 c = gl_FragColor;
            vec3 rgb = c.a > 0.0 ? c.rgb / c.a : vec3(0.0);
            float a = clamp(c.a * v_fgo_color.a * u_fgo_mul.a, 0.0, 1.0);
            gl_FragColor = vec4(clamp(rgb * v_fgo_color.rgb * u_fgo_mul.rgb, 0.0, 1.0) * a, a);
        """)

    # Literal Custom/WipeSheder, game GLES3 blob d415a5c8 (SpriteRenderer mesh objects, spec "wipe"):
    #   d = _Volume - tex.r; alpha = d > _Gradation ? 1 : (d > 0 ? d / _Gradation : 0); rgb = _Color.rgb
    # Texture colour, vertex colour and the SpriteRenderer colour are unused. The texel arrives
    # premultiplied; the masks are opaque greys, so r is un-premultiplied only for safety.
    # u_fgo_wipe = (_Color.rgb, _Gradation), u_fgo_vol = _Volume.
    renpy.register_shader(
        "fgo.fx_wipe",
        variables="""
            uniform vec4 u_fgo_wipe;
            uniform float u_fgo_vol;
        """,
        fragment_300="""
            vec4 c = gl_FragColor;
            float r = c.a > 0.0 ? c.r / c.a : 0.0;
            float d = u_fgo_vol - r;
            float a = d > u_fgo_wipe.a ? 1.0 : (d > 0.0 ? d / u_fgo_wipe.a : 0.0);
            gl_FragColor = vec4(u_fgo_wipe.rgb * a, a);
        """)

    # Literal Custom/SpriteWithMask, game Metal 1c7e6d15c5; independent
    # samples preserve filtering across alpha boundaries (SHRestGrid).
    renpy.register_shader("fgo.fx_pair", variables="""
        uniform sampler2D tex0;
        uniform sampler2D tex1;
        varying vec2 v_tex_coord;
        uniform vec4 u_fgo_crop;
        uniform vec4 u_fgo_pair_mul;
    """, fragment_300="""
        vec2 uv = v_tex_coord * u_fgo_crop.zw + u_fgo_crop.xy;
        vec3 rgb = texture2D(tex0, uv).rgb;
        float a = clamp(texture2D(tex1, uv).r * u_fgo_pair_mul.a, 0.0, 1.0);
        gl_FragColor = vec4(clamp(rgb * u_fgo_pair_mul.rgb, 0.0, 1.0) * a, a);
    """)

    def _fgo_fx_pair_crop(asset, mask, crop, mul=(1.0, 1.0, 1.0, 1.0), wrap=(1, 1)):
        path = "assets/fx/%s.png" % asset
        sw, sh = renpy.image_size(path)
        x, y, w, h = crop
        glwrap = tuple(_FGO_FX_GL_WRAP[k] for k in wrap)
        return (Model(size=(w, h)).texture(path, texture_wrap=glwrap)
                .texture("assets/fx/%s.png" % mask, texture_wrap=glwrap)
                .shader("renpy.texture").shader("fgo.fx_pair")
                .uniform("u_fgo_crop", (x / float(sw), y / float(sh), w / float(sw), h / float(sh)))
                .uniform("u_fgo_pair_mul", mul))

    # Multi-texture effect materials (shaders_distortion 2026-10-23, spec "mtex"): literal game Metal
    # programs, one family per u_mt_f.x. UVs arrive RAW (Unity uv0, v up, sheet coordinates; uv1 the
    # second channel); ST, scroll and offsets are applied here exactly where the program applies them,
    # then sampled at (u, 1 - v) with each texture's own wrap. tex0 = _MainTex, tex1 = map / second
    # texture, tex2 = _ThirdTex. Maps and masks are exported opaque (straight channels); tex0 arrives
    # premultiplied and is un-premultiplied like fgo.fx_matmul. Output clamped like the UNORM target.
    #   u_mt_f = (family, flags, 0, 0): 0 Gimmick/Distortion (flags 1 VERTEX_COLOR, 2 ALPHA_MASK,
    #            4 ALPHA_MASK_DISTORTION), 1 TALK tex0_distortion_Mapping, 2 CircleMapping,
    #            3 tex0_mul_tex1, 4 EF withMask
    #   u_mt_p = Gimmick (_twoValue0, _twoValue1, _threeValue0, _threeValue1); TALK (_ScrollX, _ScrollY, 0, 0)
    #   u_mt_q = (_Time.y, strength _InvFade / _Distortion, _InvFade2, _oneValue)
    renpy.register_shader("fgo.fx_mt", variables="""
        uniform sampler2D tex0;
        uniform sampler2D tex1;
        uniform sampler2D tex2;
        attribute vec2 a_tex_coord;
        attribute vec2 a_fgo_uv1;
        attribute vec4 a_fgo_color;
        varying vec2 v_mt_uv0;
        varying vec2 v_mt_uv1;
        varying vec4 v_mt_col;
        uniform vec4 u_mt_f;
        uniform vec4 u_mt_p;
        uniform vec4 u_mt_q;
        uniform vec4 u_mt_st0;
        uniform vec4 u_mt_st1;
        uniform vec4 u_mt_st2;
    """, fragment_functions="""
        vec4 fgo_mt_tex(sampler2D s, vec2 uv) { return texture2D(s, vec2(uv.x, 1.0 - uv.y)); }
        vec4 fgo_mt_main(vec2 uv) {
            vec4 c = fgo_mt_tex(tex0, uv);
            return vec4(c.a > 0.0 ? c.rgb / c.a : vec3(0.0), c.a);
        }
    """, vertex_300="""
        v_mt_uv0 = a_tex_coord;
        v_mt_uv1 = a_fgo_uv1;
        v_mt_col = a_fgo_color;
    """, fragment_300="""
        vec4 o;
        float fam = u_mt_f.x;
        float fl = u_mt_f.y;
        if (fam < 0.5) {
            float tx = u_mt_q.x / 20.0;
            vec2 base = v_mt_uv0 - tx * u_mt_p.zw;
            vec2 muv = (v_mt_uv0 - tx * u_mt_p.xy) * u_mt_st1.xy + u_mt_st1.zw;
            float d = fgo_mt_tex(tex1, muv).g - 0.5;
            o = fgo_mt_main((base + d * u_mt_q.y) * u_mt_st0.xy + u_mt_st0.zw);
            if (mod(fl, 8.0) >= 4.0) {
                o.a *= fgo_mt_tex(tex2, (base + d * u_mt_q.z) * u_mt_st2.xy + u_mt_st2.zw).r;
            } else if (mod(fl, 4.0) >= 2.0) {
                o.a *= fgo_mt_tex(tex2, v_mt_uv0 * u_mt_st2.xy + u_mt_st2.zw).r;
            }
            if (mod(fl, 2.0) >= 1.0) {
                o *= v_mt_col;
            } else {
                o.a *= v_mt_col.a;
            }
        } else if (fam < 1.5) {
            vec2 a = v_mt_uv0 * u_mt_st0.xy + u_mt_st0.zw;
            vec2 b = v_mt_uv1 * u_mt_st1.xy + u_mt_st1.zw;
            vec2 n = fgo_mt_tex(tex1, ((b - vec2(0.5)) * 2.0 + vec2(1.0)) * 0.5 + u_mt_p.xy * u_mt_q.x).gb;
            o = fgo_mt_main(a + 2.0 * u_mt_q.y * (n - vec2(0.5)));
            o.a *= u_mt_q.w;
        } else if (fam < 2.5) {
            // CircleMapping: the compiled polar lookup, literally (atan polynomial + quadrant selects)
            vec2 a = v_mt_uv0 * u_mt_st0.xy + u_mt_st0.zw;
            vec2 b = v_mt_uv1 * u_mt_st1.xy + u_mt_st1.zw;
            vec2 p = (b.yx - vec2(0.5)) * 2.0;
            float q = (1.0 / max(abs(p.y), abs(p.x))) * min(abs(p.y), abs(p.x));
            float s2 = q * q;
            float poly = (((0.0208350997 * s2 - 0.0851330012) * s2 + 0.180141002) * s2 - 0.330299497) * s2 + 0.999866009;
            float ang = q * poly + (abs(p.y) < abs(p.x) ? (poly * q) * -2.0 + 1.57079637 : 0.0);
            ang += p.y < -p.y ? -3.14159274 : 0.0;
            float mn = min(p.y, p.x);
            float mx = max(p.y, p.x);
            ang = (mn < -mn && mx >= -mx) ? -ang : ang;
            vec2 c = (vec2(ang * 0.318309873, 2.0 * length(p)) + vec2(1.0, 2.0)) * 0.5 + u_mt_p.xy * u_mt_q.x;
            vec2 n = fgo_mt_tex(tex1, c).gb;
            o = fgo_mt_main(a + 2.0 * u_mt_q.y * (n - vec2(0.5)));
            o.a *= u_mt_q.w;
        } else if (fam < 3.5) {
            vec4 s = fgo_mt_tex(tex1, v_mt_uv1 * u_mt_st1.xy + u_mt_st1.zw);
            vec4 m = fgo_mt_main(v_mt_uv0 * u_mt_st0.xy + u_mt_st0.zw);
            o = m * vec4(s.a > 0.0 ? s.rgb / s.a : vec3(0.0), s.a);
            o.a *= u_mt_q.w;
        } else {
            vec2 a = v_mt_uv0 * u_mt_st0.xy + u_mt_st0.zw;
            o = vec4(fgo_mt_tex(tex0, a).rgb, fgo_mt_tex(tex1, a).r) * v_mt_col;
        }
        o = clamp(o, 0.0, 1.0);
        gl_FragColor = vec4(o.rgb * o.a, o.a);
    """)

    _fgo_fx_mt_layout = []
    _fgo_fx_mt_tex = {}
    _FGO_FX_MT_FAMILY = {"gim": 0.0, "map": 1.0, "circ": 2.0, "mul": 3.0, "mask": 4.0}
    # The shader clock _Time.y = the game clock at the play's start + the effect clock (Unity's
    # global time since scene load: the start phase is unknowable). A float pins it (0.0 = "local
    # zero"), a dict pins per effect, None draws a crc32 phase per (effect, play) like the WindZone
    # clock. Only effects whose mtex scrolls depend on it (converter DISTORTION_TIME, held).
    _fgo_fx_shader_t0 = 0.0
    _fgo_fx_shader_plays = {}

    def _fgo_fx_shader_start(name):
        if isinstance(_fgo_fx_shader_t0, dict):
            return float(_fgo_fx_shader_t0.get(name, 0.0))
        if _fgo_fx_shader_t0 is not None:
            return float(_fgo_fx_shader_t0)
        n = _fgo_fx_shader_plays.get(name, 0)
        _fgo_fx_shader_plays[name] = n + 1
        h = zlib.crc32(("%s|%d|shader" % (name, n)).encode("utf-8")) & 0xffffffff
        return (h / 4294967296.0) * 600.0

    def _fgo_fx_mt_key(mt, t, one):
        """The hashable per-draw material (cell key element 6): the spec's mtex, the shader clock
        _Time.y and the current _oneValue."""
        return (tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in mt.items())),
                round(float(t), 6), round(float(one), 6))

    def _fgo_fx_mt_texture(asset):
        d = _fgo_fx_mt_tex.get(asset)
        if d is None:
            d = _fgo_fx_mt_tex[asset] = renpy.display.im.UnoptimizedTexture("assets/fx/%s.png" % asset)
        return d

    def _fgo_fx_mt_render(main, mtk, w, h, geo, uv0, uv1, col, tri, blend, st, at):
        """One Render (w, h) drawing triangles tri over geo (screen px) with the mtex material mtk
        (_fgo_fx_mt_key); uv0 / uv1 raw Unity uvs per vertex, col the per-vertex RGBA (particle colour
        x material multiplier, or mesh colours) -- fgo.fx_mt."""
        import renpy.gl2.gl2mesh as gm
        import renpy.gl2.gl2mesh2 as gm2
        if not _fgo_fx_mt_layout:
            lay = gm.AttributeLayout()
            lay.add_attribute("a_tex_coord", 2)
            lay.add_attribute("a_fgo_uv1", 2)
            lay.add_attribute("a_fgo_color", 4)
            _fgo_fx_mt_layout.append(lay)
        items, t, one = mtk
        mt = dict(items)
        n = len(geo) // 2
        if not tri:
            return renpy.Render(w, h)
        att = []
        for i in range(n):
            att.extend((uv0[2 * i], uv0[2 * i + 1], uv1[2 * i], uv1[2 * i + 1]))
            att.extend(col[4 * i:4 * i + 4])
        mesh = gm2.Mesh2(_fgo_fx_mt_layout[0], n, len(tri) // 3)
        mesh.set_geometry_data(geo)
        mesh.set_attribute_data(att)
        mesh.set_triangle_data(list(tri))
        rv = renpy.Render(w, h)
        texs = [main, mt.get("t1"), mt.get("t2")]
        wraps = [mt["w0"], mt.get("w1", (0, 0)), mt.get("w2", (0, 0))]
        for i, a_ in enumerate(texs):
            if a_ is None:
                a_ = main       # an unused sampler still needs a texture
            rv.blit(renpy.display.im.render_for_texture(_fgo_fx_mt_texture(a_), w, h, st, at), (0, 0))
            rv.add_property("texture_wrap_tex%d" % i, (_FGO_FX_GL_WRAP[wraps[i][0]], _FGO_FX_GL_WRAP[wraps[i][1]]))
        rv.mesh = mesh
        rv.add_shader("fgo.fx_mt")
        p = mt.get("p", (0.0, 0.0, 0.0, 0.0))
        rv.add_uniform("u_mt_f", (_FGO_FX_MT_FAMILY[mt["k"]], float(mt.get("fl", 0)), 0.0, 0.0))
        rv.add_uniform("u_mt_p", tuple(float(v) for v in p))
        rv.add_uniform("u_mt_q", (t, float(mt.get("s1", 0.0)), float(mt.get("s2", 0.0)), one))
        rv.add_uniform("u_mt_st0", tuple(float(v) for v in mt.get("st0", (1.0, 1.0, 0.0, 0.0))))
        rv.add_uniform("u_mt_st1", tuple(float(v) for v in mt.get("st1", (1.0, 1.0, 0.0, 0.0))))
        rv.add_uniform("u_mt_st2", tuple(float(v) for v in mt.get("st2", (1.0, 1.0, 0.0, 0.0))))
        if blend is not None:
            rv.add_property("blend_func", config.gl_blend_func[_fgo_blend_names.get(blend, "normal")])
        return rv

    import bisect
    import collections
    import heapq
    import math
    import random
    import struct
    import zlib

    # Native flat Shape/Local billboard clipping, verified effect/backeffect/forwardeffect anchors.
    # Orthographic GPU reversed-depth MVP: zclip in [0,1], including both boundaries.
    # Exact float32 coefficients from the nearclip native projection-bit probes.
    _fgo_fx_near_clip = True

    def _fgo_fx_near_scope(e):
        return (e.get("scaling_mode") == 2 and e.get("sim_space") == "local"
                and not any(k in e for k in ("stretch", "align_m", "mesh", "pivot", "pivot3",
                    "pivot_x", "tilt", "anim_geo", "sub", "vel_align")))

    _FGO_FX_NEAR_CAMERAS = {
        "effect": (0.0009980039903894067, 0.9983505010604858),
        "backeffect": (0.0009980039903894067, 0.9980455636978149),
        "forwardeffect": (0.0009990009712055326, 0.9993479251861572),
    }

    def _fgo_fx_near_hidden(z_world, anchor="effect"):
        camera = _FGO_FX_NEAR_CAMERAS.get(anchor)
        if camera is None:
            return False
        z = _fgo_fx_f32(z_world)
        depth = _fgo_fx_f32(_fgo_fx_f32(-camera[0] * z) + camera[1])
        return depth < 0.0 or depth > 1.0

    # Partial near-plane clipping: static Shape/Local billboards on the three
    # measured orthographic anchors. Native interpolation is pinned; ON by user
    # acceptance (2026-10-13) of the persistent 1/255 footprint in
    # notes/near_clip_2.md as a rounding-level difference.
    _fgo_fx_near_clip_partial = True

    def _fgo_fx_near_partial_scope(e):
        return ("clip_z" in e and e.get("scaling_mode") == 2
                and e.get("sim_space") == "local" and ("stretch" in e or "tilt" in e)
                and not any(k in e for k in ("stretch_s", "align_m", "mesh", "pivot", "pivot3",
                    "pivot_x", "anim_geo", "sub", "vel_align", "vbb", "quad_scale")))

    def _fgo_fx_near_partial_poly(zs, anchor):
        """None = untouched; () = rejected; otherwise normalized XY/UV polygon.
        Float32 clip depths use the same admitted projection as flat rejection.
        Particle colour is constant across each quad; its multiplier stays outside.
        """
        camera = _FGO_FX_NEAR_CAMERAS.get(anchor)
        if camera is None:
            return None
        ds = [_fgo_fx_f32(_fgo_fx_f32(-camera[0] * _fgo_fx_f32(z)) + camera[1]) for z in zs]
        if all(d <= 1.0 for d in ds):
            return None
        if all(d > 1.0 for d in ds):
            return ()
        poly = [(u, v, d) for (u, v), d in zip(((0, 0), (1, 0), (1, 1), (0, 1)), ds)]
        out = []
        prev = poly[-1]
        for cur in poly:
            if (prev[2] <= 1.0) != (cur[2] <= 1.0):
                t = (1.0 - prev[2]) / (cur[2] - prev[2])
                out.append((prev[0] + t * (cur[0] - prev[0]), prev[1] + t * (cur[1] - prev[1])))
            if cur[2] <= 1.0:
                out.append(cur[:2])
            prev = cur
        # A vertex exactly on the plane can be emitted twice. Remove that zero edge.
        clean = []
        for p in out:
            if not clean or p != clean[-1]:
                clean.append(p)
        if len(clean) > 1 and clean[0] == clean[-1]:
            clean.pop()
        return tuple(clean) if len(clean) >= 3 else ()

    # Stretched-billboard width side (2026-10-12, notes/stretch_width.md). Unity's real render
    # takes the side from the camera POSITION -> particle ray even under the orthographic
    # camera: side = screen-flattened cross(D, A), D = particle world position - camera
    # position, A = the signed 3D long axis, image top on the + side. On the camera axis
    # this is the old perpendicular, but MIRRORED for the game camera (100 px in front of
    # the effect plane; the older probes' camera sat behind it).
    # Verified scope: plain stretched quads (no pivot / stretch_s / animated frame / moving
    # births / sub-emitter child / quad_scale) on effect / backeffect / forwardeffect at scale 1.
    _fgo_fx_stretch_side = True
    _FGO_FX_SIDE_CAM_Z = {"effect": 100.0 / 288.0, "backeffect": 12.0 / 288.0, "forwardeffect": 100.0 / 288.0}
    _fgo_fx_side_census = False  # True (harness only) -> ("SIDE", ...) rows in _fgo_fx_debug

    def _fgo_fx_ray_side(cvx, cvy, cvz, length, h, Dx, Dy, Dz):
        """Screen vector (texture image top -> bottom) of a stretched quad's width.
        (cvx, cvy, cvz) world velocity, screen-oriented (y down); length signed px;
        h width px; D = particle - camera, world units, y UP. None when degenerate."""
        Ax, Ay, Az = -length * cvx, length * cvy, -length * cvz      # world, y up
        Sx = Dy * Az - Dz * Ay
        Sy = Dz * Ax - Dx * Az
        n = math.hypot(Sx, Sy)
        if n < 1e-12:
            return None
        # texture v = 1 (image top) lies on the +cross(D, A) side [Unity real render,
        # half-white texture, stretchw step7]; screen y down
        return (-h * Sx / n, h * Sy / n)

    def _fgo_fx_side_gate(cloud, e, bxy, Js, lx, pivot, w, h):
        """None when the ray side applies to this stretched particle, else why not."""
        if getattr(cloud, "anchor", None) not in _FGO_FX_SIDE_CAM_Z:
            return "anchor"
        if cloud.scale != 1.0:
            return "scale"
        if bxy is not None:
            return "moving"
        if Js is not None or "anim_geo" in e:
            return "anim_geo"
        if lx and "pa" in lx:
            return "sub"
        if e.get("quad_scale") is not None:
            return "quad_scale"
        if max(w, h) > _FGO_MOTE_SAFE_PX:
            return "big"
        return None

    _fgo_fx_debug = None        # a list -> every drawn particle is appended (regression harness)
    _fgo_fx_only_mobjs = False  # True -> draw mesh objects only (tools/fx_verify/mo_pixels_cmp.py)
    _fgo_fx_cam_only = None     # camera index -> draw that picture camera's target alone (camwrap diagnostic)
    _fgo_fx_camera_depth_cap = True  # perspective stretch cap uses camera-space depth (camthunder)
    _fgo_fx_tint_frame_end = True      # startColor Gradient sampled at the frame END (NCGradRunner)
    # Order bundle (2026-10-02, notes/order_bundle.md A): equal-queue shared-material particles
    # sort by Unity's float32 bounds-centre depth (the converter's analytic offset "zo") and
    # break EXACT ties by the renderer list -- frame-0 Instantiate in GameObject pathID order
    # ("gr"), switch-ons in clip binding order, children first ("bi", "po") -- never by the
    # first particle. False = the older first-emission / renderer-pathID registration.
    _fgo_fx_order_list = True
    _fgo_fx_first_registration = True
    # Item D (order bundle finish 2026-10-03): an NGUI draw call and a plain particle at one
    # sortingOrder + queue sort by bounds-centre depth (draw call = midpoint of its widgets' planes,
    # "pz"; particle "queue_depth"), far first. Only specs the converter decided carry "pz".
    _fgo_fx_dc_depth = True
    _fgo_fx_mesh_shader_order = True  # Same-source shader ID before material at mesh/particle ties.
    _FGO_FX_GRAVITY = 9.81      # Physics.gravity magnitude; gravityModifier scales it
    # Hierarchy-scaled emitters: gravity in world units, not prefab px (lane
    # xform 2026-09-27, see _draw_emitter). False = the old (288 x too weak)
    # drawing, kept only for A/B comparisons.
    _fgo_fx_grav_units = True
    _FGO_FX_ROT_STEP = 3.0      # rotation bucket (degrees) for the Transform cache
    # Bucketed rotation as a whole-degree number (the old int() cast); steps below
    # one degree (_fgo_fx_rot_exact) keep tenths. 46.6 -> 45 at step 3, 46.6 at 0.1.
    def _fgo_fx_rb(a, step):
        v = round(a / step) * step
        if step >= 1.0:
            return int(v) % 360
        return round(v, 1 if step == 0.1 else 4) % 360
    # Rotation precision (rotation_precision 2026-10-18): every particle angle (random
    # start rotation, spin, rotation curve, stretch direction, tilt, matrix alignment)
    # is bucketed to the coarsest step whose half-step error, seen at the quad's corner
    # (reach = half the diagonal in screen px, after any matrix scale), stays under
    # 0.5 px -- 3 degrees for a 20-px particle, 0.01 degrees for slash_trace's 4687-px
    # streak. The bucket only exists so the Transform cache has hits; the colour in the
    # key varies anyway. False = the old fixed 3-degree bucket (constant rotation 0.1).
    _fgo_fx_rot_exact_all = True
    _FGO_FX_ROT_STEPS = (3.0, 1.0, 0.3, 0.1, 0.03, 0.01, 0.003, 0.001)
    def _fgo_fx_step_reach(reach):
        if not _fgo_fx_rot_exact_all:
            return _FGO_FX_ROT_STEP
        lim = math.degrees(1.0 / reach) if reach > 1e-6 else 3.0
        for s_ in _FGO_FX_ROT_STEPS:
            if s_ <= lim:
                return min(s_, _FGO_FX_ROT_STEP)
        return min(_FGO_FX_ROT_STEPS[-1], _FGO_FX_ROT_STEP)
    def _fgo_fx_step_for(w, h, qs=None, G=None):
        if not _fgo_fx_rot_exact_all:
            return _FGO_FX_ROT_STEP
        if G is not None:
            w *= math.hypot(G[0][0], G[1][0])
            h *= math.hypot(G[0][1], G[1][1])
        elif qs is not None:
            w *= abs(qs[0])
            h *= abs(qs[1])
        return _fgo_fx_step_reach(0.5 * math.hypot(w, h))
    # Sub-pixel placement and size (subpx 2026-10-18): Unity rasterises a particle's float
    # centre and float side lengths; the port used to truncate the top-left of the blit,
    # int(px - cw / 2), and the side lengths, int(raw) (up to 1 px each, on average half a
    # pixel up-left / smaller). ON: Render.subpixel_blit at the float offset, and the side
    # lengths to 1/_FGO_FX_SUBPX_Q px through a zoom of the integer-sized cell (the key of the
    # Transform cache then carries the quantised fraction). False = the old integer draw.
    _fgo_fx_subpx = True
    _FGO_FX_SUBPX_Q = 64
    def _fgo_fx_wh(wf, hf, w, h):
        """(w, h) to hand to a mote constructor: the exact sides (1/Q px) when ON."""
        if not _fgo_fx_subpx or wf is None:
            return w, h
        q = _FGO_FX_SUBPX_Q
        return max(float(w), round(wf * q) / q), max(float(h), round(hf * q) / q)
    def _fgo_fx_xy(w, h):
        """Transform kwargs sizing the cell to (w, h): integer xysize, the fraction as a zoom."""
        iw, ih = int(w), int(h)
        if iw == w and ih == h:
            return {"xysize": (iw, ih)}
        return {"xysize": (iw, ih), "xzoom": w / iw, "yzoom": h / ih}
    def _fgo_fx_blit(rv, cr, x, y):
        if _fgo_fx_subpx:
            rv.subpixel_blit(cr, (x, y))
        else:
            rv.blit(cr, (int(x), int(y)))
    def _fgo_fx_ipos(v):
        return v if _fgo_fx_subpx else int(v)
    # A particle whose rotation is constant for its whole life (equal rot_range ends,
    # no spin / spin curve / start-rotation curve) draws at its exact angle (0.1 degree
    # steps) instead of the 3-degree Transform-cache bucket: slash_trace's 4687-px
    # streak at 46.6 degrees drew at 45 and missed by 65 px at the ends (finish_up
    # target 3). False = the old bucket.
    _fgo_fx_rot_exact = True
    # Gravity integration. Unity integrates gravity per frame (1/30 s in the game) exactly
    # like Force (v += g dt, then x += v dt; a mid-frame birth takes a partial
    # first step d): x = g/2 (age^2 + d^2 + (age - d) dt) [Unity-verified
    # 2026-09-26, ProbeSmallGates.cs grav_only / force_grav: 1e-7]. The
    # runtime's historical closed form g age^2 / 2 is short by g/2 (d^2 +
    # (age - d) dt - ... ) ~ g dt age / 2: 0.037 units (~11 FGO px) at 1.5 s
    # for gravityModifier 0.3, ~70 px at 0.3 s for bit_talk_29's -10. True =
    # the Unity-exact rule, switched on 2026-09-26 by the user's decision
    # (AGENTS.md known approximation 24); False = the old closed form.
    _fgo_fx_grav_per_frame = True
    # Lane modules (2026-09-27), both Unity-verified, both change older
    # effects (notes/modules.md lists them) -- switches for the user /
    # integrator, like gravity above:
    #   * a one-shot system's final frame emits nothing (_fgo_fx_oneshot);
    #   * a prewarmed constant-rate loop's lattice starts at -maxLifetime.
    _fgo_fx_oneshot_final_frame = True
    _fgo_fx_prewarm_maxlife_lattice = True
    # Lane prewarm (notes/prewarm.md, phase 2 in small gates 2, 2026-09-27):
    # during a prewarm maxParticles never frees a dead particle's slot, so
    # only the FIRST maxParticles prewarm births exist; at t = 0 the dead
    # go; afterwards per frame the particles dying by the frame's end
    # are removed before the frame's births are admitted; a step's rate
    # births and a burst are each capped against the count at the step's
    # start (_fgo_fx_pw_admit). Unity PLAY mode, conclusive for maxLife <=
    # 8.5 s (the converter refuses the rest). True = that rule (default ON:
    # the user's standing rule, the Unity-accurate behaviour); False = the
    # older exact-death sequential cap through the prewarm too.
    _fgo_fx_prewarm_hold_slots = True
    # Small gates 2 item 5 (2026-09-27): during a PLAY-mode prewarm, gravity
    # and a constant Force move a particle by the EXACT constant-acceleration
    # formula (x = v t + a t^2 / 2 over its prewarm part); only the frames
    # after t = 0 use the per-frame rule (_fgo_fx_pw_k) [Unity-verified,
    # Assets/ProbeRuntime/SmallGates2/SG2PrewarmIntRunner.cs ->
    # probe_sg2_prewarmint/, tools/fx_verify/sg2_prewarmint_fit.py /
    # _fit2.py: 998 prewarmed particles per case group, lifetimes 0.73-4.6 s,
    # positions within 2e-4 units (= the probe's birth-time precision) vs
    # 0.13 for the frame rule from the birth; velocities 1e-5]. True = that
    # rule; False = the older frame rule from the birth for prewarmed
    # particles too.
    _fgo_fx_prewarm_exact_accel = True
    # Mesh objects pass (2026-09-27): FGO_param1anim copies one_value into
    # the material in Update, BEFORE the animation pass writes an animated
    # one_value, so the frame draws the previous frame's value (one frame late,
    # like UVScroll) [Unity-verified: exact replica, MOUvLagRunner.cs ->
    # probe_meshobj_uvlag/results.txt, 40/40 frames]. Applies to "one"
    # tracks the converter marks "one_lag" (animated FGO_param1anim alone).
    # True = that rule; False = no lag (the runtime before 2026-09-27).
    _fgo_fx_param1_lag = True
    # Loose ends (2026-09-28): a random velocityOverLifetime (TwoConstants /
    # TwoCurves) draws ONE mix PER AXIS [Unity-verified, play mode,
    # Assets/ProbeRuntime/Loose/LEPsAnimRunner.cs -> probe_loose_psanim/
    # wvol_*_two* / *_twoc_*, tools/fx_verify/le_psanim_fit.py: 36 particles,
    # TwoCurves and TwoConstants, rotating or still parent, clamped or free:
    # per-axis mixes fit every step within 2.5e-5, the three mixes of a
    # particle unrelated]. The runtime used one shared mix for curve axes
    # (every path) and for constant axes inside the clamp2 / orbit tracks (the
    # free constant path already drew per axis). True = per axis: the y / z
    # mixes come from a SEPARATE stream (the chunk seed ^ 0x9E3779B9), so no
    # other particle property changes; False = the shared mix.
    _fgo_fx_vol_mix_axes = True
    # Loose ends (2026-09-28): an unrotated billboard larger than the GPU's
    # texture limit. The shared mote path (fgo_core _fgo_mote_safe_size)
    # SHRINKS such a quad to 4000 px, which Unity never does: bit_talk_07's
    # end glow (40 units = 21600 screen px, "Back_White_01/Particle System 2")
    # was drawn ~5x too small, the white-out's edges 45 levels too dark
    # (tools/fx_verify/le_07_cmp.py: Unity corner 106 = the texture model's
    # 107, Ren'Py 61). True = draw only the part of the full-size quad inside
    # a window (<= 3800 px) around the render area, with the matching texture
    # sub-rectangle -- the same pixels on screen; False = the shrunken quad.
    # BigCrop2: only frame-covering, untilted matrix quads with plain cells.
    _fgo_fx_big_crop_general = True
    _fgo_fx_big_crop = True
    # (failing_layers 2026-10-17) Ren'Py cuts a relative crop to WHOLE texels of the cell, so
    # the plain crop window shifted / stretched the picture (runeslight_ppl flare_base: 64-px
    # cell, 4320 px quad -> texels 3..59 over 3800 px, +68 px right). True = while
    # _fgo_fx_big_mesh is on, those quads draw as BigMesh geometry (exact UVs) instead.
    _fgo_fx_crop_mesh = True
    # Billboards through a scaled general matrix G keep their sub-pixel pre-G size
    # (see the draw loop, gpx_); False = the older whole-pixel truncation.
    _fgo_fx_gpx_all = True
    # Noise (2026-09-28, notes/noise.md) -- APPROXIMATED, the one module the
    # port matches statistically instead of per particle (the user's
    # decision 2026-09-27): Unity's noise field is engine code with a per-
    # system random seed, so the game never draws the same noise twice.
    # [approximated, statistically matched 2026-09-28, NZFieldRunner.cs]
    # True = noise emitters wobble (e["noise"], _fgo_fx_nz_*); False = they
    # draw without noise (for comparing).
    _fgo_fx_noise_approx = True

    # Prewarmed orbital / radial emitters are approximated (the user's
    # decision 2026-09-29): 0.03 s steps ending at t = 0, ~0.037 FGO px off
    # Unity at worst (see _fgo_fx_orbit_track).  True = that grid; False =
    # prewarmed particles integrate on the frame grid from their birth.
    _fgo_fx_orbit_prewarm_approx = True

    # --- curve helpers ----------------------------------------------------------

    _fgo_fx_int_tables = {}

    def _fgo_fx_curve_int(keys, u, n=64):
        """Integral of a Unity curve over normalised age [0, u]. A
        velocityOverLifetime curve is a VELOCITY, so displacement is its
        integral -- the older runtimes used curve(u) * age, which is only
        right for a flat curve."""
        tab = _fgo_fx_int_tables.get(id(keys))
        if tab is None:
            tab = [0.0]
            prev = _fgo_curve_at(keys, 0.0)
            for i in range(1, n + 1):
                cur = _fgo_curve_at(keys, i / float(n))
                tab.append(tab[-1] + 0.5 * (prev + cur) / n)
                prev = cur
            _fgo_fx_int_tables[id(keys)] = tab
        x = max(0.0, min(1.0, u)) * n
        i = min(int(x), n - 1)
        return tab[i] + (tab[i + 1] - tab[i]) * (x - i)

    # velocityOverLifetime CURVES without Limit Velocity: Unity adds the curve per FRAME -- x += v(age at the
    # step's START) h, the first step partial -- over the analytic prewarm, exactly like the stepped
    # tracks (clamp2 / orbit). The runtime drew the exact integral of the curve instead, which differs by
    # ~ (frame / 2) x (change of v): [Unity-verified 2026-10-01, play mode, game timing (capture,
    # Fixed and Maximum Particle Timestep 1/30), PWSynRunner mod "vg" (VOL z = 2 x age / L + gravity),
    # 4380 particles over frames 0 .. 30: the left sum fits to 1e-4 (the probe's precision), the exact
    # integral is 1.5e-2 off, a midpoint sum equally far -- tools/fx_verify/fps30_rt.py]. The same
    # error existed at 1/60 s at half the size. True = the frame sum; False = the exact integral
    # (the runtime before the 30 fps change).
    _fgo_fx_vol_frames = True

    def _fgo_fx_vol_table(e, birth, life, vel_mix):
        """Per-particle table of the free VOL curves' frame sums: (d, ends, tabs, vals) with the
        particle ages at the step ends (0, d, d + dt, ...), per curve axis i the displacement at each
        end (tabs[i]) and the curve value the step AFTER it ran with (vals[i], at the step's START
        age: the 'velocity' of that step). A prewarmed particle (birth < 0) has the exact integral
        over its prewarm part (Unity's prewarm of a procedural system is analytic) as its first
        entry. Read by _fgo_fx_vol_at."""
        DT = _FGO_FX_FRAME
        axes = [(i, a) for i, a in enumerate(e.get("vol", ())) if a and a[0] == "curve"]

        def val(i, a, age):
            u = min(1.0, age / life)
            m_ = vel_mix[i] if type(vel_mix) is tuple else vel_mix
            lo = _fgo_curve_at(a[3], u)
            return a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * m_)
        q = birth / DT
        q -= math.floor(q)
        d = DT * (1.0 - q) if q > 1e-6 else DT
        ends = [0.0]
        tabs = {i: [0.0] for i, a in axes}
        vals = {i: [] for i, a in axes}
        age, h = 0.0, d
        if birth < -1e-9 and _fgo_fx_prewarm_unified:
            p_ = -birth
            for i, a in axes:
                m_ = vel_mix[i] if type(vel_mix) is tuple else vel_mix
                u_ = min(1.0, p_ / life)
                i_lo = _fgo_fx_curve_int(a[3], u_)
                tabs[i].append(a[1] * life * (i_lo + (_fgo_fx_curve_int(a[2], u_) - i_lo) * m_))
                vals[i].append(val(i, a, p_))
            ends.append(p_)
            age, h, d = p_, DT, p_
        while age < life + DT:
            for i, a in axes:
                v_ = val(i, a, age)
                tabs[i].append(tabs[i][-1] + v_ * h)
                vals[i].append(v_)
            age += h
            ends.append(age)
            h = DT
        return (d, ends, tabs, vals)

    def _fgo_fx_vol_at(vt, age):
        """([displacement per curve axis], [velocity per curve axis]) of a VOL table at `age`:
        linear inside a step; the velocity is the curve value of the step the age belongs to (a
        frame instant = the end of the step that just ran)."""
        d, ends, tabs, vals = vt
        i = bisect.bisect_left(ends, age - 1e-9)
        if i < 1:
            i = 1
        li = [0.0, 0.0, 0.0]
        lv = [0.0, 0.0, 0.0]
        if i >= len(ends):
            for ax, tb in tabs.items():
                li[ax] = tb[-1]
                lv[ax] = vals[ax][-1] if vals[ax] else 0.0
            return li, lv
        a0, a1 = ends[i - 1], ends[i]
        f = (age - a0) / (a1 - a0) if a1 > a0 else 1.0
        for ax, tb in tabs.items():
            li[ax] = tb[i - 1] + (tb[i] - tb[i - 1]) * f
            lv[ax] = vals[ax][i - 1]
        return li, lv

    def _fgo_fx_rnd(v, rng):
        if isinstance(v, (tuple, list)):
            return rng.uniform(v[0], v[1])
        return v

    def _fgo_fx_max_life(e):
        """The lifetime's TRUE maximum: TwoConstants may be stored inverted
        (min > max: bit_talk_snow's snow_1 / snow_2, (20, 18) / (15, 10));
        the prewarm lattice anchors at -max [lane prewarm rule 1: the true
        maximum for constant / TwoConstants / curve lifetimes; an inverted
        pair is not probed -- U(lo, hi) draws the same range either way]."""
        life = e["life"]
        if e.get("life_inf") and e.get("prewarm"):
            # An INFINITE lifetime prewarms for one emission cycle (the duration), not for
            # the (1e30 s) census lifetime: births on the -duration + m / rate lattice, the
            # bursts of that cycle, maxParticles filling from the first of them
            # [Unity-verified 2026-10-17, small_rules_2 target 9: real storm_wht / moya_white
            # plus 50 + 48 synthetic play-mode cases, notes/small_rules_2.md].
            return max(e["length"], 1e-6)
        if e.get("pw_legacy") and isinstance(life, (tuple, list)) and not _fgo_fx_prewarm_unified:
            # [legacy approximation, the user's decision 2026-09-27] the
            # anchor of the runtime before small gates 2 (2b70f3d): the
            # stored "max" slot, even when inverted (converter pw_legacy).
            return life[1]
        return max(life[0], life[1]) if isinstance(life, (tuple, list)) else life

    # --- spawning ---------------------------------------------------------------

    # --- 3D shape sampling ---------------------------------------------------
    # Every shape is sampled in its own local 3D frame, mapped to world by the
    # converter's matrix e["geo"]["A"] (rotation x the scale that applies), and
    # projected by dropping world Z -- the camera is orthographic. Formulas
    # are Unity-verified per particle against Assets/Editor/ProbeShape3D.cs
    # (2026-09-24; see tools/fgo_fx_convert.py _geometry for the case list).
    # Directions go through A too, re-normalised when g["norm"] (Shape
    # scalingMode under a non-unit scale). Shape formulas, local frame:
    #   cone        base disk, rho = r*sqrt(U(1-thick, 1)) (NOT r*(1-thick) --
    #               cones differ from circles here), fire
    #               normalize(x/r*sin(angle), y/r*sin(angle), cos(angle)). One
    #               formula gives angle 90 = radial in the base plane
    #               (bit_talk_14), angle 60 thick 0 = constant |v_xy| =
    #               speed*sin 60 (bit_talk_41) and the narrow up-cones.
    #   conevolume  base point + fire direction as cone, then moved U(0, length)
    #               ALONG that direction (reduces to normalize(pos) at r=0,
    #               which is what miasma's earlier check saw).
    #   sphere/hemi uniform direction (hemi: z >= 0), radius cube-root in
    #               [r(1-thick), r]; fire radially. Projection gives the slow
    #               polar particles verified on bit_talk_36.
    #   circle      disk in local XY, radius sqrt-uniform in [r(1-thick), r],
    #               angle U(0, arc); fire radially in the plane.
    #   box         uniform in size, fire local +Z.
    #   edge        local X in [-r, r], fire local +Y.
    #   point       origin, fire local +Z.
    # randomDirectionAmount: normalize(lerp(dir, random unit vector, amount)),
    # only emitted for sphere/box/circle/edge (converter gate).

    def _fgo_fx_unit3(rng):
        z = rng.uniform(-1.0, 1.0)
        a = rng.uniform(0.0, 2 * math.pi)
        s = math.sqrt(max(0.0, 1.0 - z * z))
        return (s * math.cos(a), s * math.sin(a), z)

    def _fgo_fx_shape_angle(g, angle):
        """Unity-verified GMClock 2026-10-02: Random arc spread rounds the
        sampled angle DOWN to a multiple of arc * spread (angle in radians)."""
        if g.get("arc_spread"):
            step = math.radians(g.get("arc",360.0)) * g["arc_spread"]
            return math.floor(angle / step) * step
        return angle

    def _fgo_fx_bsp_angle(g, bs):
        """BurstSpread angle (radians) of particle i of an n-particle group: i arc / (n - 1),
        i 360 / n on a full circle [SG2Arc.cs bsp_*]. g["bsp_spread"] (new specs only): then
        DOWN to a multiple of arc * spread, in degrees; circles and cones alike [stale gates
        2026-10-16, probe stale_t8: 754 / 754 particles, 124 groups, worst 4.7e-5 deg]."""
        arc_ = g.get("arc", 360.0)
        div_ = bs[1] if arc_ >= 360.0 - 1e-6 else max(1, bs[1] - 1)
        deg_ = bs[0] * arc_ / div_
        if g.get("bsp_spread"):
            q_ = arc_ * g["bsp_spread"]
            deg_ = math.floor(deg_ / q_) * q_
        return math.radians(deg_)

    _fgo_fx_edge_burst_spread = True
    # ShapeModule.alignToDirection on Mesh particles (spec "align_dir"):
    # False keeps the start rotation unchanged (small_rules).
    _fgo_fx_align_dir = True
    # startFrame over frameOverTime TwoCurves: frame = floor(frac(scalar *
    # lerp(min, max, r) + startFrame) * n), r the particle's own TwoCurves
    # fraction ("fr"), independent of the startFrame draw (small_rules_2).
    # False reads only the max curve (the old, unverified float branch).
    _fgo_fx_sf_two_curves = True

    def _fgo_fx_shape_local(g, rng, raw=None, bs=None):
        """(position, direction) in the shape's own local frame (Unity axes).
        raw: a list that receives a cone's unit-disk base point (c, s) (the
        randomDirection rule needs the RAW cone vector). bs: (i, n) = the
        particle's index in its n-particle burst, for a BurstSpread circle
        (g["bsp"])."""
        kind = g["kind"]
        if kind == "point":
            return (0.0, 0.0, 0.0), (0.0, 0.0, 1.0)
        if kind in ("sphere", "hemi"):
            n = _fgo_fx_unit3(rng)
            if kind == "hemi":
                n = (n[0], n[1], abs(n[2]))
            r, t = g["r"], g["thick"]
            if t <= 0.0:
                d = r
            elif t >= 1.0:
                d = r * (rng.random() ** (1.0 / 3.0))
            else:
                r_in3 = (r * (1.0 - t)) ** 3
                d = (r_in3 + rng.random() * (r ** 3 - r_in3)) ** (1.0 / 3.0)
            return (n[0] * d, n[1] * d, n[2] * d), n
        if kind == "circle":
            r, t = g["r"], g["thick"]
            a = math.radians(rng.uniform(0.0, g.get("arc", 360.0)))
            a = _fgo_fx_shape_angle(g, a)
            if bs is not None:
                # BurstSpread: particle i of an n-burst at i arc / (n - 1), or
                # i 360 / n on a full circle [Unity-verified 2026-09-27,
                # SG2Arc.cs bsp_*, sg2_arc_fit.py: every burst, 1e-5 deg]
                a = _fgo_fx_bsp_angle(g, bs)
            r_in = r * (1.0 - t)
            d = math.sqrt(rng.random() * (r * r - r_in * r_in) + r_in * r_in)
            c, s = math.cos(a), math.sin(a)
            return (c * d, s * d, 0.0), (c, s, 0.0)
        if kind in ("cone", "conevolume"):
            r, t = g["r"], g["thick"]
            # arc < 360 (Random mode): base-point angle U(0, arc), the rest
            # unchanged [Unity-verified 2026-09-27, SG2Arc.cs cone_*: KS 0.015
            # over 4000 particles, direction 1e-6]
            a = rng.uniform(0.0, math.radians(g["arc"]) if "arc" in g else 2 * math.pi)
            a = _fgo_fx_shape_angle(g, a)
            if bs is not None and g.get("bsp"):
                # BurstSpread cone (new specs only): the circle's base-point angle rule
                # [probe stale_t8 dust_*: cone groups match the circle formula exactly]
                a = _fgo_fx_bsp_angle(g, bs)
            rho = math.sqrt(rng.uniform(1.0 - t, 1.0))
            c, s = math.cos(a) * rho, math.sin(a) * rho
            if raw is not None:
                raw.extend((c, s))
            th = math.radians(g["angle"])
            dx, dy, dz = c * math.sin(th), s * math.sin(th), math.cos(th)
            m = math.sqrt(dx * dx + dy * dy + dz * dz)
            dvec = (dx / m, dy / m, dz / m)
            pos = (c * r, s * r, 0.0)
            if kind == "conevolume":
                l = rng.uniform(0.0, g["length"])
                pos = (pos[0] + dvec[0] * l, pos[1] + dvec[1] * l, dvec[2] * l)
            return pos, dvec
        if kind == "rectangle":
            # Unity-verified 2026-10-01 (SRRectangle.Run): uniform XY plane,
            # fire +Z; shared sm / A carry the shape and emitter transforms.
            return (rng.random() - 0.5, rng.random() - 0.5, 0.0), (0.0, 0.0, 1.0)
        if kind == "donut":
            # Unity-verified 2026-10-02 GMBundle: a linear tube-radius draw,
            # uniform ring and cross-section angles. A sqrt-area disk fails.
            a = math.radians(rng.uniform(0.0, g.get("arc",360.0)))
            b = rng.uniform(0.0, 2.0 * math.pi)
            rho = rng.uniform(g["tube"] * (1.0-g["thick"]), g["tube"])
            c, s, cb, sb_ = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
            ring = g["r"] + rho * cb
            return (c * ring, s * ring, rho * sb_), (c * cb, s * cb, sb_)
        if kind in ("boxshell", "boxedge"):
            axis = min(2, int(rng.random() * 3))
            p = tuple((0.5 if rng.random() < 0.5 else -0.5)
                      if ((i == axis) == (kind == "boxshell"))
                      else rng.random() - 0.5 for i in range(3))
            return p, (0.0, 0.0, 1.0)
        if kind == "box":
            sx, sy, sz = g["size"]
            return ((rng.random() - 0.5) * sx, (rng.random() - 0.5) * sy,
                    (rng.random() - 0.5) * sz), (0.0, 0.0, 1.0)
        if kind == "edge":
            x = rng.uniform(-g["r"], g["r"])
            if g.get("edge_bsp") and bs is not None and _fgo_fx_edge_burst_spread:
                # Preserve existing random draws. Native uses float32 normalized
                # endpoint spacing, then radius: n=6,r=800 has x3=160.000061.
                u = _fgo_fx_f32(float(bs[0]) / max(1, bs[1] - 1))
                x = _fgo_fx_f32(_fgo_fx_f32(u * _fgo_fx_f32(2.0 * g["r"])) - g["r"])
            return (x, 0.0, 0.0), (0.0, 1.0, 0.0)
        raise Exception("fgo_fx: shape kind %r" % kind)

    def _fgo_fx_mv(m, v):
        return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
                m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
                m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])

    def _fgo_fx_shape_scale_geo(e, mv, birth, g=None):
        """Unity-verified E1ScaleRunner (2026-10-03): all step births use
        Legacy frame START / Animator END; box size is a scaled unit cube.
        Includes exact zero axes, without dividing by the rest scale.
        """
        g = dict(e["geo"] if g is None else g)
        j = max(0, int(math.ceil(birth / _FGO_FX_FRAME - 1e-6)) - 1)
        tc = mv[2] + (j + (1 if e.get("anim_end") else 0)) * _FGO_FX_FRAME
        ph, u = _fgo_fx_phase(mv[0], mv[1], tc)
        au = _fgo_fx_autos(mv[0], mv[1], tc) if "autos" in mv[0] else None
        spec = e["sm_anim"]
        scales = tuple(_fgo_fx_track(t, ph, u, au) for t in spec["tracks"])
        g["sm"] = tuple(tuple(row[i] * scales[i] for i in range(3)) for row in spec["r"])
        return g

    def _fgo_fx_spawn(e, rng, g=None, bs=None, canonical=None):
        """(ox, oy, dx, dy, dz, oz): shape offset in shape units and fire
        direction, screen-oriented (x right, y DOWN); dz is the world depth
        component (invisible, but part of the speed ClampVelocity limits); oz
        the offset's world depth (for the NGUI shared-queue depth sort).
        g: a moving emitter's geo at the birth time (_fgo_fx_emitter_frame)."""
        if g is None:
            g = e["geo"]
        f = g.get("rand", 0.0)
        raw = [] if (f > 0.0 and g["kind"] == "cone") else None
        p, d = _fgo_fx_shape_local(g, rng, raw, bs)
        if canonical is not None:
            canonical.extend(p)
        if raw is not None:
            # Cone: lerp the RAW cone vectors (q sin t, cos t) toward a second
            # random cone direction, q1 uniform on the whole unit disk, then
            # normalise [Unity-verified per particle 2026-09-26,
            # ProbeSmallGates.cs rd_cone*; see the converter's _RANDDIR_OK].
            th = math.radians(g["angle"])
            st_, ct_ = math.sin(th), math.cos(th)
            q1 = math.sqrt(rng.random())
            ph = rng.uniform(0.0, 2 * math.pi)
            d = ((1.0 - f) * raw[0] * st_ + f * q1 * math.cos(ph) * st_,
                 (1.0 - f) * raw[1] * st_ + f * q1 * math.sin(ph) * st_,
                 ct_)
            m = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
            d = (d[0] / m, d[1] / m, d[2] / m)
        elif f > 0.0:
            u = _fgo_fx_unit3(rng)
            d = tuple(a * (1.0 - f) + b * f for a, b in zip(d, u))
            # Spherical direction blends the RAW lerp on these shapes.
            # Keep the historical normalization for every older spec.
            if not g.get("spherical"):
                m = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
                d = (d[0] / m, d[1] / m, d[2] / m)
        if g.get("spherical"):
            d = _fgo_fx_spherize(p, d, g["spherical"])
        sm = g.get("sm")
        if sm is not None:
            # ShapeModule scale / position (converter _shape_matrix_fields;
            # Unity-verified 2026-09-26, LaneShapeProbe.cs Shape()):
            # position sm * p + pos, direction normalize(sm * d), or the
            # emitter's local +Z (unrotated) when sm * d is exactly zero.
            # g["A"] is then the emitter part only.
            p = _fgo_fx_mv(sm, p)
            po = g.get("pos")
            if po is not None:
                p = (p[0] + po[0], p[1] + po[1], p[2] + po[2])
            d = _fgo_fx_mv(sm, d)
            m = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
            d = (d[0] / m, d[1] / m, d[2] / m) if m > 1e-12 else (0.0, 0.0, 1.0)
        wp = _fgo_fx_mv(g["A"], p)
        wd = _fgo_fx_mv(g["A"], d)
        if g.get("norm"):
            m = math.sqrt(wd[0] * wd[0] + wd[1] * wd[1] + wd[2] * wd[2]) or 1.0
            wd = (wd[0] / m, wd[1] / m, wd[2] / m)
        return wp[0], -wp[1], wd[0], -wd[1], wd[2], wp[2]

    def _fgo_fx_spherize(p, d, amount):
        """Unity-verified 2026-10-02, GMBundle / GMTransform: local radial
        direction from the canonical spawn point (before randomPosition,
        shape scale/rotation/offset), normalized blend after randomDirection.
        Cone passes its already-normalized cone-specific random blend."""
        m = math.sqrt(sum(v * v for v in p))
        radial = tuple(v / m for v in p) if m else (0.0, 0.0, 0.0)
        out = tuple((1.0 - amount) * a + amount * b for a, b in zip(d, radial))
        m = math.sqrt(sum(v * v for v in out)) or 1.0
        return tuple(v / m for v in out)

    _fgo_fx_rate_tabs = {}

    def _fgo_fx_first_cycle(e):
        """First emission cycle of an activation: 0, or for a prewarmed loop
        -ceil(maxLife / length), i.e. the steady state (Unity-verified
        2026-09-24, ProbeOpen.cs Prewarm + ProbeBatch2.cs Emission: prewarmed
        births sit on the same lattice started that many cycles early)."""
        if e.get("prewarm") and e["loop"]:
            L = max(e["length"], 1e-6)
            return -int(math.ceil(_fgo_fx_max_life(e) / L - 1e-9))
        return 0

    def _fgo_fx_rate_tab(e):
        """Cumulative rate-over-time for a rate CURVE across one cycle, as
        Unity accumulates it: once per frame (1/30 s, the game's), with the rate sampled at
        the END of the frame (right Riemann sum); births are interpolated
        inside the frame. On a rising curve that puts births ~half a frame
        early and leaves a fractional surplus that carries into the next
        cycle. [Unity-verified 2026-09-24, ProbeBatch2.cs RateCurve(): linear
        0->60 over 3 s, first births 0.308/0.439/0.539, cycle 2 starting at
        +0.215 s.] tab[i] = particles emitted by the end of frame i."""
        rc = e["rate_curve"]
        key = id(rc)
        tab = _fgo_fx_rate_tabs.get(key)
        if tab is None:
            L = max(e["length"], 1e-6)
            N = max(1, int(round(L * _FGO_FX_HZ)))
            tab = [0.0]
            for i in range(1, N + 1):
                tab.append(tab[-1] + rc[0] * _fgo_curve_at(rc[1], i / float(N)) * L / N)
            _fgo_fx_rate_tabs[key] = tab
        return tab

    _fgo_fx_dist_cache = {}

    # rateOverDistance in Unity's frames [Unity-verified 2026-10-09, notes/dist_emission.md,
    # tools/fx_verify/distemit/fit.py + fit2.py; probe runs distemit_syn1 / _syn2 / _real1]:
    # the step that ends at system time s measures the WORLD distance between the emitter's
    # positions in the previous and the current frame -- the particle clock leads the clip
    # clock by one frame (_fgo_fx_display_lead), so that is clip time on + s - 2 dt -> on + s - dt;
    # the first step after activation / startDelay adds nothing; the rate curve is sampled
    # at the step's END system time (wrapped for a loop, clamped for a one-shot); ONE float32
    # accumulator with a fractional carry holds rate x distance and a constant rateOverTime;
    # births in the final frame count; births spawn on the straight line between the two frame
    # positions. False = the old rule (distance of the NEXT frame: one frame early, -8 % on
    # atomization). Prewarmed systems keep the old rule (unprobed).
    _fgo_fx_dist_frames = True

    def _fgo_fx_dist_new(e):
        return _fgo_fx_dist_frames and not e.get("prewarm") and bool(e.get("anim_geo", {}).get("rate_dist"))

    def _fgo_fx_dist_comb(e, mv):
        """A constant rateOverTime that shares the distance accumulator (Unity: one
        accumulator, probe final_both / combo)."""
        if mv is None or not _fgo_fx_dist_new(e):
            return False
        rate = e.get("rate", 0) or 0
        if isinstance(rate, (tuple, list)) or rate <= 0:
            return False
        return not any(k_ in e for k_ in ("rate_curve", "rate_anim", "emis_anim", "loop_anim", "start_values", "start_anim"))

    # Moving World rate births use the same chord as distance births. Probe
    # movespawn_syn2: translation/parent rotation, curves, activation, delay,
    # loops and Update events. Single-cycle constant-count bursts also use
    # the verified burst scheduler (syn3); mixed/random bursts and prewarm remain outside this
    # switch's scope; no emitter-name exceptions or extra random draws.
    _fgo_fx_move_spawn = True

    def _fgo_fx_move_spawn_scope(e):
        ag = e.get("anim_geo")
        if not _fgo_fx_move_spawn or not ag or ag.get("local") or ag.get("rate_dist"):
            return False
        if e.get("prewarm") or e.get("anim_end"):
            return False
        delay_frames = e.get("delay", 0.0) * _FGO_FX_HZ
        if abs(delay_frames - round(delay_frames)) > 1e-6:
            return False       # measured placement fits; existing partial-step age does not
        if any(k_ in e for k_ in ("sub", "sim_speed", "rate_anim", "emis_anim", "burst_prob",
                                  "loop_anim", "start_values", "start_anim", "radius_anim", "sm_anim")):
            return False
        if id(e) in _fgo_fx_sub_parents:
            return False
        rate = e.get("rate", 0) or 0
        if e.get("bursts"):
            # The source must explicitly AlwaysSimulate. quick_slash's
            # PauseAndCatchUp slash_flash differs between numerical and
            # picture probes; its visibility-dependent clock is not verified.
            return e.get("move_burst", False) and rate == 0 and not e.get("rate_curve") and all(
                not isinstance(c_, (tuple, list)) for b_, c_ in e["bursts"])
        return not isinstance(rate, (tuple, list)) and (rate > 0 or bool(e.get("rate_curve")))

    def _fgo_fx_move_spawn_frame(spec, e, T7, on, t):
        # Preserve the actual emission frame when its float32 birth time is
        # on a boundary. The float remains the existing age/integration clock.
        if hasattr(t, "se"):
            frame = max(0, int(round(t.se / _FGO_FX_FRAME)) - 1)
        elif hasattr(t, "bk"):
            frame = max(0, int(round(t.bk / _FGO_FX_FRAME)) - 1)
        elif hasattr(t, "frame"):
            frame = t.frame
        else:
            frame = max(0, int(math.ceil(t / _FGO_FX_FRAME - 1e-9)) - 1)
        return _fgo_fx_dist_spawn(spec, e, T7, on, t, frame, False)

    def _fgo_fx_dist_spawn(spec, e, T7, on, t, frame=None, delay_reset=True):
        """(geo, origin) of a distance emitter's birth at emitter-local time t: the
        step's frame A, origin on the line between the previous and current frame
        positions (the activation frame: no previous position, the current one)."""
        dt = _FGO_FX_FRAME
        f = max(0, int(math.ceil(t / dt - 1e-9)) - 1) if frame is None else frame
        frac = min(1.0, max(0.0, t / dt - f))
        g1, p1 = _fgo_fx_emitter_frame(spec, e, T7, on + f * dt)
        te0 = f * dt - e.get("delay", 0.0)
        if f == 0 or (delay_reset and te0 <= 1e-9):
            return g1, p1
        g0, p0 = _fgo_fx_emitter_frame(spec, e, T7, on + (f - 1) * dt)
        return g1, tuple(a + (b - a) * frac for a, b in zip(p0, p1))

    def _fgo_fx_distance_births2(e, mv, t_hi):
        """_fgo_fx_dist_frames' births (emitter-local times) up to t_hi."""
        spec, T7, on = mv
        key = (id(e), T7, on, 2)
        got = _fgo_fx_dist_cache.get(key)
        if got is not None and got[0] >= t_hi:
            return got[1]
        if len(_fgo_fx_dist_cache) > 200:
            _fgo_fx_dist_cache.clear()
        scal, rkeys = e["anim_geo"]["rate_dist"]
        L = max(e["length"], 1e-6)
        delay = e.get("delay", 0.0)
        node = e["anim_geo"]["node"]
        dt = _FGO_FX_FRAME
        f32 = _fgo_fx_f32
        rate0 = (e.get("rate", 0) or 0) if _fgo_fx_dist_comb(e, mv) else 0.0
        add_r = f32(f32(rate0) * f32(dt)) if rate0 else 0.0

        def pos(t):
            ph, u = _fgo_fx_phase(spec, T7, on + t)
            au = _fgo_fx_autos(spec, T7, on + t) if "autos" in spec else None
            return _fgo_fx_nodes(spec, ph, u, au)[node][1]
        out = []
        acc = 0.0
        prev = None
        f = 0
        while f * dt < t_hi:
            te = (f + 1) * dt - delay
            if te <= 1e-9:
                prev = None          # still in startDelay
                f += 1
                continue
            cur = pos(f * dt)        # this step's frame (clip time on + f dt)
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, prev))) / fgo_px_per_unit if prev is not None else 0.0
            prev = cur
            u = ((te % L) / L) if e["loop"] else min(te / L, 1.0)
            r = scal * (_fgo_curve_at(rkeys, u) if rkeys else 1.0)
            add = f32(f32(f32(r) * f32(d)) + add_r)
            if add > 0:
                s_ = f32(acc + add)
                n = int(math.floor(s_))
                for i in range(1, n + 1):
                    out.append((f + min(1.0, max(0.0, (i - acc) / add))) * dt)
                acc = f32(s_ - n)
            f += 1
        _fgo_fx_dist_cache[key] = (f * dt, out)
        return out

    def _fgo_fx_distance_births(e, mv, t_hi):
        """rateOverDistance births (emitter-local times) of a moving emitter,
        up to t_hi: Unity adds rate x (world distance moved) per frame; the
        n-th particle is born where that running total reaches n (the same
        lattice rule as rateOverTime), interpolated inside the frame.
        World distance = prefab px / 288 (UIRoot scale 2/576). charaScale is
        not applied. [Checked against Unity particle counts on
        bit_talk_atomization, RenderNGUIRig.cs.]"""
        if _fgo_fx_dist_new(e):
            return _fgo_fx_distance_births2(e, mv, t_hi)
        spec, T7, on = mv
        key = (id(e), T7, on)
        got = _fgo_fx_dist_cache.get(key)
        if got is not None and got[0] >= t_hi:
            return got[1]
        if len(_fgo_fx_dist_cache) > 200:
            _fgo_fx_dist_cache.clear()
        scal, rkeys = e["anim_geo"]["rate_dist"]
        L = max(e["length"], 1e-6)
        delay = e.get("delay", 0.0)
        node = e["anim_geo"]["node"]
        dt = _FGO_FX_FRAME

        def pos(t):
            ph, u = _fgo_fx_phase(spec, T7, on + t)
            au = _fgo_fx_autos(spec, T7, on + t) if "autos" in spec else None
            return _fgo_fx_nodes(spec, ph, u, au)[node][1]
        out = []
        acc = 0.0
        prev = pos(0.0)
        f = 0
        while f * dt < t_hi:
            cur = pos((f + 1) * dt)
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, prev))) / fgo_px_per_unit
            # rate curve over the system's cycle, sampled at the frame's
            # end (as the rateOverTime curve path, ProbeBatch2.cs RateCurve)
            te = (f + 1) * dt - delay
            r = scal * (_fgo_curve_at(rkeys, (te % L) / L) if rkeys else 1.0) if te >= 0 else 0.0
            add = r * d
            n = int(math.floor(acc)) + 1
            while add > 0 and n <= acc + add + 1e-9:
                out.append((f + (n - acc) / add) * dt)
                n += 1
            acc += add
            prev = cur
            f += 1
        _fgo_fx_dist_cache[key] = (f * dt, out)
        return out

    _fgo_fx_rate_cache = {}
    # A rate-clip key authored on the 1/30 s grid is stored to 6 decimals (0.266667 for 8/30), so the clip time of
    # frame k (k/30) fell a hair BELOW it: the step to the next value came one frame late (bit_talk_sunlight's end
    # ramp 6 -> 0 at 8/30 s: one extra glow birth, frame 69 vs Unity's last at 64). True = the clip time within a
    # millionth of a frame grid point reaches the key [Unity-verified 2026-10-20, real bit_talk_sunlight stops 1.6 /
    # 2.0 / 2.3, sprites_anim target 3]. False = the older strict comparison.
    _fgo_fx_rate_key_tie = True

    def _fgo_fx_rate_births(e, mv, t_hi):
        """Births (emitter-local times) up to t_hi of a system whose
        rateOverTime multiplier a clip animates (e["rate_anim"], converter
        _rate_anim_fields). mv = (spec, T7, on). System frame j covers
        (j dt, (j+1) dt] from the activation (startDelay included); its rate
        is the track at effect time on + j dt -- the clip value at the frame's
        START: the clip is evaluated at 0 in the frame the effect is created
        while the system already advances dt -- times a curve-mode rate at
        the frame's END (cycle phase, as _fgo_fx_rate_tab). Accumulated once
        per frame, the n-th birth where the total reaches n, interpolated
        inside the frame. [Unity-verified 2026-09-26, play mode,
        LaneModulesRateAnimRunner.cs, lane_modules_rateanim_check.py: 8
        cases, every birth within 5e-6 s.]"""
        spec, T7, on = mv
        key = (id(e), T7, on)
        st = _fgo_fx_rate_cache.get(key)
        if st is not None and st[0] * _FGO_FX_FRAME >= t_hi:
            return st[2]
        if st is None:
            if len(_fgo_fx_rate_cache) > 200:
                _fgo_fx_rate_cache.clear()
            acc0 = 0.0
            if _fgo_fx_prewarm_unified and e.get("prewarm") and e["loop"]:
                # the accumulator carries the prewarm's fraction (rest rate x maxLife)
                r0, on0 = _fgo_fx_pw_rest(e)
                if on0 and r0 > 0:
                    acc0 = _fgo_fx_max_life(e) * r0
                    acc0 -= math.floor(acc0 + 1e-9)
            st = [0, acc0, []]
            _fgo_fx_rate_cache[key] = st
        dt = _FGO_FX_FRAME
        L = max(e["length"], 1e-6)
        delay = e.get("delay", 0.0)
        rc = e.get("rate_curve")
        j, acc, out = st
        # (mesh objects extra phase 2026-09-28) "anim_end": an Animator drives
        # the curves -- the step (j dt, (j+1) dt] runs with its value at the
        # step's END; "emis_anim": EmissionModule.enabled gates the step
        # (converter _emis_radius_fields, probe cited there).
        tofs = dt if e.get("anim_end") else 0.0
        emis = e.get("emis_anim")
        while j * dt < t_hi:
            te = (j + 1) * dt - delay
            if te > 1e-9:
                # the frame the delay ends in advances the system clock only by its remainder
                # (Unity's float countdown, _fgo_fx_oneshot); every other frame by dt
                step = dt if te >= dt - 1e-9 else te
                ph, u = _fgo_fx_phase(spec, T7, on + j * dt + tofs)
                if _fgo_fx_rate_key_tie and abs(u * _FGO_FX_HZ - round(u * _FGO_FX_HZ)) < 1e-3:
                    u += 1e-6       # a step key authored at k/30 (stored to 6 decimals) is reached AT frame k
                au = _fgo_fx_autos(spec, T7, on + j * dt + tofs) if "autos" in spec else None
                r = _fgo_fx_track(e["rate_anim"], ph, u, au) if "rate_anim" in e else (e.get("rate", 0) or 0)
                if emis is not None and _fgo_fx_track(emis, ph, u, au) < 0.5:
                    r = 0.0
                if rc is not None:
                    cyc = math.ceil(te / L - 1e-9) - 1
                    r *= _fgo_curve_at(rc[1], min(1.0, max(0.0, te / L - cyc)))
                add = max(0.0, r) * step
                n = int(math.floor(acc + 1e-9)) + 1
                while add > 0 and n <= acc + add + 1e-9:
                    if step == dt:
                        out.append((j + (n - acc) / add) * dt)
                    else:
                        out.append((j + 1) * dt - step + (n - acc) / add * step)
                    if e.get("start_anim") or e.get("start_values"):
                        b_ = _FGOFXStartBirth(out[-1])
                        b_.frame = j
                        b_.phase = _fgo_fx_f32(_fgo_fx_sv_clock(e, j)[0] / _fgo_fx_f32(e["length"]))
                        out[-1] = b_
                    n += 1
                acc += add
            j += 1
        st[0], st[1] = j, acc
        return out

    def _fgo_fx_pw_rest(e):
        """(rest rate, rest enabled) of a clip-driven emitter: what its prewarm runs with
        -- the prewarm happens before the clip's first write [Unity-verified 2026-10-01,
        play mode, PWSynRunner clip suite, A and B settings: a Legacy clip 3 -> 20 /s on a
        rest rate of 10 prewarms at 10 /s; a clip disabling emission at 0 still gets the
        full prewarm; a rest-disabled system prewarms nothing; from frame 0 the clip
        drives the accumulator (_fgo_fx_rate_births)]."""
        r0 = e.get("rate", 0) or 0
        if "rate_anim" in e:
            r0 = _fgo_fx_track_rest(e["rate_anim"])
        on0 = True
        if "emis_anim" in e:
            on0 = _fgo_fx_track_rest(e["emis_anim"]) >= 0.5
        return r0, on0

    def _fgo_fx_f32(x):
        """x rounded to float32 (Unity's particle clock and accumulator are
        float32; a single + - * / of float32 values done in double and
        rounded is the float32 result)."""
        return struct.unpack("f", struct.pack("f", x))[0]

    _fgo_fx_oneshot_cache = {}

    def _fgo_fx_oneshot(e):
        """A NON-looping system's emission as Unity runs it, frame by frame
        in float32 [Unity-verified 2026-09-26, play mode, Assets/ProbeRuntime/
        Lane_modules/LaneModulesGridRunner.cs -> probe_modules_grid/, fitted
        by tools/fx_verify/lane_modules_grid_fit.py: ps.time bit-exact in
        208/208 systems, births per frame exact in 204/208 (rates 7.5-120,
        delays 0-0.6 on and off the frame grid, durations 0.1-5 incl. 0.13 /
        0.27 / 0.55 / 1.01, loops and one-shots, curve rates; the 4 others
        are one knife-edge birth of a linear rate CURVE)]:
          * startDelay is a float32 countdown (delay -= dt each frame); the
            frame it goes below 0 advances ps.time by -delay;
          * then ps.time += dt; the frame where it reaches the duration ends
            the system and EMITS NOTHING -- neither rate births nor a burst
            inside that frame (bursts at 0.0834 / 0.09 / 0.1 of a 0.1 s
            system never fire, one at 0.0833333 does);
          * acc += rate * (the frame's ps.time advance), n = floor(acc)
            births, acc -= n (rate curve at the frame's end).
        Returns {"n": rate births emitted, "fr": emission frame of each
        (0-based from the activation: frame j ends at (j+1) dt), "last":
        ps.time (float32) at the end of the last emitting frame}. Birth
        TIMES stay the exact lattice (Unity reports an on-grid birth emitted
        a frame late with age dt, i.e. at the grid time)."""
        key = id(e)
        if key in _fgo_fx_oneshot_cache:
            return _fgo_fx_oneshot_cache[key]
        f32 = _fgo_fx_f32
        dt = f32(_FGO_FX_FRAME)
        dur = f32(max(e["length"], 1e-6))
        delay = f32(e.get("delay", 0.0))
        rc = e.get("rate_curve")
        rate = e.get("rate", 0) or 0
        if isinstance(rate, (tuple, list)):
            _fgo_fx_oneshot_cache[key] = None
            return None        # a random rate: not modelled here
        rate = 0.0 if rc is not None else float(rate)
        pst = 0.0
        acc = 0.0
        started = False
        frames = []
        last = 0.0
        f = 0
        while True:
            if not started:
                delay = f32(delay - dt)
                if -delay > 0.0:
                    started = True
                    step = f32(-delay)
                    pst = step
                else:
                    step = 0.0
            else:
                new = f32(pst + dt)
                if new >= dur:
                    break
                step = f32(new - pst)
                pst = new
            if step > 0.0:
                r = rate if rc is None else f32(rc[0] * _fgo_curve_at(rc[1], min(1.0, pst / dur)))
                acc = f32(acc + f32(r * step))
                n = int(math.floor(acc))
                acc = f32(acc - n)
                frames.extend([f] * n)
                last = pst
            f += 1
            if f > 100000:
                break
        got = {"n": len(frames), "fr": frames, "last": last}
        _fgo_fx_oneshot_cache[key] = got
        return got

    # A rate CURVE whose cycle (or startDelay) is not a whole number of frames: the frames run on
    # across the cycle boundary, so the per-cycle table of _fgo_fx_rate_tab (N = round(L x fps)
    # frames per cycle) is not the frame grid any more. At 1/30 s eight converted specs are in
    # this case (tornado02, blood_splash / _fs, blackfire_all / _fs, rainbow_star, aerial_wave,
    # warp_failed); at 1/60 s two (aerial_wave storm1, blackfire_all part_aura_line2). True = the
    # float32 clock of _fgo_fx_oneshot run frame by frame, a loop wrapping with the two pieces
    # accumulated separately (tools/fx_verify/lane_modules_grid_fit.py: ps.time bit-exact in
    # 208/208 systems and births per frame in 204/208, at 1/60 s; re-recorded at 1/30 s in
    # notes/effects_30fps.md Step 3); False = the table for every curve (the runtime before the
    # 30 fps change: used to prove that a 60 fps run reproduces commit 0225fbf).
    _fgo_fx_rate_frames_general = True
    _fgo_fx_cycle_cache = {}

    def _fgo_fx_rc_aligned(e):
        """Does the table of _fgo_fx_rate_tab describe the real frames of this rate-curve
        emitter: a whole number of frames per cycle and a delay on the frame grid?"""
        if not _fgo_fx_rate_frames_general or (e.get("prewarm") and e["loop"]):
            return True
        L = max(e["length"], 1e-6)
        d = e.get("delay", 0.0)
        return (abs(L * _FGO_FX_HZ - round(L * _FGO_FX_HZ)) < 1e-6
                and abs(d * _FGO_FX_HZ - round(d * _FGO_FX_HZ)) < 1e-6)

    def _fgo_fx_rc_births(e, t_hi):
        """Birth times (emitter-local, from the activation, startDelay included) of a rate-curve
        system up to t_hi, frame by frame as Unity's float32 clock does it (see
        _fgo_fx_oneshot): the rate sampled at the system time at the frame's END, a loop that
        wraps inside a frame accumulates the two pieces separately (the first at the end of
        the cycle, the second at the new cycle's start), a one-shot's final frame emits
        nothing; births are interpolated inside their piece of the frame."""
        st = _fgo_fx_cycle_cache.get(id(e))
        f32 = _fgo_fx_f32
        if st is None:
            if len(_fgo_fx_cycle_cache) > 200:
                _fgo_fx_cycle_cache.clear()
            st = {"j": 0, "pst": 0.0, "acc": 0.0, "delay": f32(e.get("delay", 0.0)), "started": False,
                  "stopped": False, "out": []}
            _fgo_fx_cycle_cache[id(e)] = st
        F = _FGO_FX_FRAME
        dt = f32(F)
        dur = f32(max(e["length"], 1e-6))
        rc = e["rate_curve"]
        loop = bool(e["loop"])
        out = st["out"]
        while st["j"] * F < t_hi and not st["stopped"]:
            j = st["j"]
            tl0 = j * F
            pieces = []
            if not st["started"]:
                st["delay"] = f32(st["delay"] - dt)
                if -st["delay"] > 0.0:
                    st["started"] = True
                    s_ = f32(-st["delay"])
                    st["pst"] = s_
                    pieces = [(0.0, s_, (j + 1) * F - s_)]
            else:
                pst = st["pst"]
                new = f32(pst + dt)
                if new >= dur:
                    if loop:
                        pieces = [(pst, dur, tl0), (0.0, f32(new - dur), tl0 + float(f32(dur - pst)))]
                        st["pst"] = f32(new - dur)
                    else:
                        st["stopped"] = True
                else:
                    pieces = [(pst, new, tl0)]
                    st["pst"] = new
            acc0 = st["acc"]
            adds = []
            for a, b, tls in pieces:
                step = f32(b - a)
                if step <= 0.0:
                    continue
                r = f32(rc[0] * _fgo_curve_at(rc[1], min(1.0, b / dur)))
                adds.append((tls, step, f32(r * step)))
            if len(adds) == 1:
                tls, step, add = adds[0]
                acc = f32(acc0 + add)
            elif adds:
                acc = f32(acc0 + f32(adds[0][2] + adds[1][2]))
                add = acc - acc0
            else:
                acc = acc0
            n = int(math.floor(acc))
            for m in range(1, n + 1):
                if len(adds) > 1:
                    # a frame that wraps: the births are spread over the WHOLE frame by the total the
                    # accumulator gains (not per piece) [Unity-verified 2026-10-01, F3GridRunner: 13
                    # looping curve systems, every birth within 2.4e-6 s; per piece 9.8e-3 s off]
                    out.append(tl0 + ((m - acc0) / add if add > 0.0 else 0.0) * F)
                elif adds:
                    out.append(adds[0][0] + ((m - acc0) / add if add > 0.0 else 0.0) * adds[0][1])
                if (e.get("start_values") or e.get("cap_batch")) and adds:
                    b_ = _FGOFXStartBirth(out[-1])
                    b_.frame = j
                    b_.phase = f32(st["pst"] / dur)
                    out[-1] = b_
            st["acc"] = f32(acc - n)
            st["j"] = j + 1
        return out

    # --- Unity's float32 particle clock (prompts/OPUS_DIAG_3.md Part 1, notes/diag3.md) ---
    # Unity keeps a particle's age as a float32 PERCENT P (aliveTimePercent): each step
    # P += f32(f32(dt x 100) x inv) with inv = f32(1 / startLifetime), and removes the
    # particle in the step where P > 100 (strictly: P == 100 is alive and drawn, the
    # "remaining lifetime exactly 0" particles); remainingLifetime = L x (1 - P x 0.01).
    # A rate birth starts at fr = max(f32((acc + i) x f32(1 / add)), f32(1e-6)) of its
    # frame (acc = the float32 accumulator after the frame, add = the frame's rate x
    # system-time pieces, i = 0 for the newest), P = f32(f32(f32(fr x dt) x 100) x inv).
    # A prewarmed loop's clock runs from duration - P on the prewarm steps, so ps.time
    # ends a little BEHIND the grid (noise_gr: 4.9999986, a birth due at 0.5 s comes a
    # frame late). [Unity-verified 2026-10-08, probe tool, play mode at the game's 1/30
    # timing: final_grid_1 (509 cases) -- every reported remaining lifetime from a
    # track's 2nd frame on (7,162 tracks), every removal (10,306 tracks, 0 fail), every
    # particle's first / last frame in all 420 non-prewarm constant-rate cases and in the
    # whole-frame prewarm cases; real bundles voice02 hadou (every particle bit, 120
    # frames x 4 salts) and noise_gr (counts 120 x 4); tools/fx_verify/diag3/p1_*.py.]
    # Scope (_fgo_fx_fclock_scope): constant rate and lifetime, no bursts (zero-count
    # ones only), no rate / emission / start-value animation, not a sub-emitter parent;
    # a prewarm only on a loop without delay whose lifetime is a whole number of frames,
    # <= 5 s and <= the duration (new procedural loop_pw_clock specs also allow
    # life > duration, initializing percent from birth age). A partial step, life 0.417, is NOT
    # reproduced: kept on the old lattice). True = Unity's clock for those emitters
    # (births in Unity's frames, removal at P > 100, maxParticles on it); False = the
    # exact double lattice and the age > life test.

    # Global audit remains withheld; new verified specs may opt in with color_bytes.
    _fgo_fx_color_bytes = False
    _fgo_fx_color_key_cache = {}
    _fgo_fx_color_tint_cache = {}

    def _fgo_fx_cbyte(v):
        return min(255, max(0, int(math.floor(_fgo_fx_f32(_fgo_fx_f32(v) * 255.0) + .5))))

    def _fgo_fx_color_keys(keys, floating=False):
        key = (id(keys),floating)
        cached = _fgo_fx_color_key_cache.get(key)
        if cached is not None and cached[0] is keys:
            return cached[1]
        if len(_fgo_fx_color_key_cache)>8000:
            _fgo_fx_color_key_cache.clear()
        convert = _fgo_fx_f32 if floating else _fgo_fx_cbyte
        scalar = isinstance(keys[0][1], (int,float))
        result = tuple((math.floor(_fgo_fx_f32(t)*65535.0+.5)/65535.0,
                        convert(v) if scalar else tuple(convert(x) for x in v)) for t,v in keys)
        _fgo_fx_color_key_cache[key] = (keys,result)
        return result

    def _fgo_fx_bytegrad(keys, u, mode=0):
        if not keys:
            return 255
        keys = _fgo_fx_color_keys(keys)
        scalar = isinstance(keys[0][1], (int, float))
        vals = lambda v: (v,) if scalar else v
        if mode == 1:
            for t, value in keys:
                if u <= t + 1e-9:
                    result = vals(value)
                    break
            else:
                result = vals(keys[-1][1])
        elif u <= keys[0][0]:
            result = vals(keys[0][1])
        else:
            for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
                if u <= t1:
                    weight = min(255, max(0, int(_fgo_fx_f32(_fgo_fx_f32((u-t0)/(t1-t0)) * 255.0))))
                    result = tuple((a*(256-weight)+b*weight+128)//256 for a,b in zip(vals(v0),vals(v1)))
                    break
            else:
                result = vals(keys[-1][1])
        return result[0] if scalar else result

    def _fgo_fx_floatbytegrad(keys, u, mode=0):
        if not keys:
            return 255
        scalar = isinstance(keys[0][1], (int, float))
        keys = _fgo_fx_color_keys(keys, True)
        if mode == 1:
            value = _fgo_fx_gradm(keys, u, mode, scalar)
        elif u <= keys[0][0]:
            value = keys[0][1]
        else:
            for (t0,v0),(t1,v1) in zip(keys,keys[1:]):
                if u <= t1:
                    f = _fgo_fx_f32((u-t0)/(t1-t0))
                    def channel(a,b):
                        return _fgo_fx_f32(a+_fgo_fx_f32(_fgo_fx_f32(b-a)*f))
                    value = channel(v0,v1) if scalar else tuple(channel(a,b) for a,b in zip(v0,v1))
                    break
            else:
                value = keys[-1][1]
        return _fgo_fx_cbyte(value) if scalar else tuple(_fgo_fx_cbyte(v) for v in value)

    def _fgo_fx_bytecolor(e, u, tint_rgb, tint_a, colors, alphas, c2t):
        # Perceptual lifetime / start TwoGradients remain unmeasured.
        modes = e.get("col_mode", (0,))
        if any(mode not in (0,1) for mode in modes) or "tint_cycle2" in e or 2 in e.get("tint_mode", ()):
            return None
        if c2t is None:
            # New colour-textured Rectangles: native lifetime Gradient evaluates
            # float keys before C32; recttex_colorlife1,31 frames,31744 samples.
            shape_color = bool(e.get('shape_tex') and len(e['shape_tex']) > 5 and e['shape_tex'][5])
            grad = _fgo_fx_floatbytegrad if shape_color else _fgo_fx_bytegrad
            rgb = grad(colors, u, modes[0]) if colors else (255,255,255)
            alpha = grad(alphas, u, modes[0]) if alphas else 255
        else:
            lo_rgb = _fgo_fx_floatbytegrad(e["col2"][0], u, modes[1] if len(modes)>1 else 0)
            lo_alpha = _fgo_fx_floatbytegrad(e["col2"][1], u, modes[1] if len(modes)>1 else 0)
            hi_rgb = _fgo_fx_floatbytegrad(colors, u, modes[0])
            hi_alpha = _fgo_fx_floatbytegrad(alphas, u, modes[0])
            mix = min(255, max(0, int(_fgo_fx_f32(_fgo_fx_f32(c2t)*255.0))))
            rgb = tuple((a*(256-mix)+b*mix+128)//256 for a,b in zip(lo_rgb,hi_rgb))
            alpha = (lo_alpha*(256-mix)+hi_alpha*mix+128)//256
        tint_key = (tuple(tint_rgb),tint_a)
        tint = _fgo_fx_color_tint_cache.get(tint_key)
        if tint is None:
            if len(_fgo_fx_color_tint_cache)>8000:
                _fgo_fx_color_tint_cache.clear()
            tint = tuple(_fgo_fx_cbyte(v) for v in (*tint_rgb,tint_a))
            _fgo_fx_color_tint_cache[tint_key] = tint
        if e.get('shape_tex') and len(e['shape_tex']) > 5 and e['shape_tex'][5]:
            rgb = tuple((((a+1)*b)//256)/255.0 for a,b in zip(rgb,tint[:3]))
            alpha = (((alpha+1)*tint[3])//256)/255.0
        else:
            rgb = tuple(((a*b+127)//255)/255.0 for a,b in zip(rgb,tint[:3]))
            alpha = ((alpha*tint[3]+127)//255)/255.0
        return rgb, alpha

    _fgo_fx_pct_clock = True
    _fgo_fx_fclock_cache = {}

    class _FGOFXClockBirth(float):
        """A rate birth on Unity's float32 clock (_fgo_fx_fclock): the float is the
        emitter-local birth (its emission frame's end - fr frames); .bk = the local time
        from which it is drawn (that frame's end), .dk = the local time from which it is
        gone (the end of the frame whose step removes it; None = beyond the horizon),
        .ok = admitted by maxParticles."""
        def __new__(cls, value, bk, dk, ok):
            obj = float.__new__(cls, value)
            obj.bk = bk
            obj.dk = dk
            obj.ok = ok
            return obj

        def __getnewargs__(self):
            return (float(self), self.bk, self.dk, self.ok)

    def _fgo_fx_fclock_scope(e):
        """True when emitter e's rate births and deaths follow _fgo_fx_fclock."""
        if not _fgo_fx_pct_clock or abs(_FGO_FX_HZ - 30.0) > 1e-9 or (e.get('shape_tex') and e['shape_tex'][4]):
            return False
        rate = e.get("rate", 0) or 0
        life = e.get("life")
        if isinstance(rate, (tuple, list)) or rate <= 0:
            return False
        if isinstance(life, (tuple, list)) or life is None or life <= 0 or e.get("life_inf"):
            return False
        for k_ in ("rate_curve", "rate_anim", "emis_anim", "start_values", "start_anim", "sub",
                   "burst_prob", "sim_speed", "pw_legacy"):
            if k_ in e:
                return False
        if "anim_geo" in e and e["anim_geo"].get("rate_dist"):
            return False
        if id(e) in _fgo_fx_sub_parents:
            return False
        for bt_, cnt_ in e.get("bursts", ()):
            if isinstance(cnt_, (tuple, list)) or cnt_ != 0:
                return False
        if e.get("prewarm"):
            if not e["loop"] or e.get("delay"):
                return False
            n_ = life * _FGO_FX_HZ
            if (abs(n_ - round(n_)) > 1e-6 or life > 5.0
                    or (life > e["length"] + 1e-9 and not e.get("loop_pw_clock"))):
                return False
        return True

    def _fgo_fx_fclock_looping(e, mv, st, j):
        """The `looping` flag system frame j reads (as _fgo_fx_loopoff)."""
        if "loop_anim" not in e or mv is None:
            return e["loop"]
        spec, T7, on = mv
        if T7 is None and not e.get("loop_early"):
            return True
        if e.get("loop_early"):
            clip_t = st["clip_clock"]
            st["clip_clock"] = _fgo_fx_f32(clip_t + _fgo_fx_f32(_FGO_FX_FRAME))
        else:
            clip_t = on + j * _FGO_FX_FRAME
        ph, u = _fgo_fx_phase(spec, T7, clip_t)
        au = _fgo_fx_autos(spec, T7, clip_t) if "autos" in spec else None
        return _fgo_fx_track(e["loop_anim"], ph, u, au) >= 0.5

    def _fgo_fx_fclock_death(st, P, j):
        """First step index after j whose step removes a particle at percent P."""
        f32 = _fgo_fx_f32
        inc = st["inc"]
        while True:
            j += 1
            P = f32(P + inc)
            if P > 100.0:
                return j

    def _fgo_fx_fclock_emit(st, add, h, c, j, cap):
        """Births of one step: c from the accumulator, admitted while the cap has room."""
        f32 = _fgo_fx_f32
        heap = st["alive"]
        if j >= 0 or st["nonproc"]:
            # removed in an earlier step or this one (deaths before births); a procedural
            # prewarm frees no slot before t = 0 (_fgo_fx_pw_rule)
            while heap and heap[0] <= j:
                heapq.heappop(heap)
        r_add = f32(1.0 / add)
        room = max(0, cap - len(heap))
        end = st["t_end"]
        new = []
        for i in range(c):
            fr = f32(f32(st["acc"] + i) * r_add) if i else f32(st["acc"] * r_add)
            fr = max(fr, f32(1e-6))
            P = f32(f32(f32(fr * h) * 100.0) * st["inv"])
            new.append((P, fr))
        # i = 0 is the newest birth and Unity admits in that order (the newest take the
        # free slots); listed oldest first (the lattice order)
        for i in range(c - 1, -1, -1):
            P, fr = new[i]
            ok = i < room
            if ok:
                if j < 0 and st.get("loop_pw_clock"):
                    # Procedural whole-frame prewarm: initialize percent from the
                    # birth's age at t=0, then advance on the live float32 grid.
                    # Native loopclip controls + miasma aura layers; new specs only.
                    pb = f32(f32(-(end - fr * h) * 100.0) * st["inv"])
                    d = _fgo_fx_fclock_death(st, pb, -1)
                else:
                    d = st["pw_death"](P, j) if j < 0 else _fgo_fx_fclock_death(st, P, j)
                heapq.heappush(heap, d)
                dk = (d + 1) * _FGO_FX_FRAME if d >= 0 else 0.0
            else:
                dk = 0.0
            st["births"].append(_FGOFXClockBirth(end - fr * h, end if j >= 0 else 0.0, dk, ok))

    def _fgo_fx_fclock(e, mv, n_need):
        """Unity's float32 clock for emitter e (scope: _fgo_fx_fclock_scope), run until it
        holds n_need rate births or has ended. Returns the state: st["births"][n - 1] is
        the n-th rate birth from the activation (prewarm births first)."""
        key = (id(e),) + ((mv[1], mv[2]) if (mv is not None and "loop_anim" in e) else ())
        st = _fgo_fx_fclock_cache.get(key)
        f32 = _fgo_fx_f32
        dt = f32(_FGO_FX_FRAME)
        if st is None:
            if len(_fgo_fx_fclock_cache) > 400:
                _fgo_fx_fclock_cache.clear()
            L = f32(e["life"])
            inv = f32(1.0 / L)
            st = {"j": 0, "pst": 0.0, "acc": 0.0, "started": False, "ended": False,
                  "dl": f32(e.get("delay", 0.0)), "dur": f32(max(e["length"], 1e-6)),
                  "rate": f32(e["rate"]), "inv": inv, "inc": f32(f32(dt * 100.0) * inv),
                  "alive": [], "births": [], "t_end": 0.0, "nonproc": _fgo_fx_pw_nonproc(e),
                  "loop_pw_clock": e.get("loop_pw_clock", False),
                  "clip_clock": f32(mv[2]) if mv is not None else 0.0}
            _fgo_fx_fclock_cache[key] = st
            if e.get("prewarm"):
                # the prewarm: steps of the game's 1/30 from duration - P (P = the lifetime,
                # a whole number of frames in scope), the clock wrapping at the duration. In
                # the prewarm a particle's percent advances by the CLOCK's step (the pieces'
                # sum), births start at fr of it; a prewarm birth's death runs its remaining
                # prewarm steps, then frames. [Fit, 2026-10-08: every birth / removal frame of
                # the 56 whole-frame prewarm grid cases and noise_gr's live counts; the
                # remaining-lifetime bits after a prewarm are NOT exact, and a nominal 1/30
                # prewarm step misses 4 of the 56 (notes/diag3.md 1d).]
                P = f32(e["life"])
                rem = P
                steps = []
                pst = f32(st["dur"] - P)
                st["started"] = True
                while rem > 1e-6:
                    s_ = dt if rem > dt else rem
                    new = f32(pst + s_)
                    if new >= st["dur"]:
                        pieces = [f32(st["dur"] - pst), f32(new - st["dur"])]
                        pst = pieces[1]
                    else:
                        pieces = [f32(new - pst)]
                        pst = new
                    hp = 0.0
                    for x_ in pieces:
                        hp = f32(hp + x_)
                    steps.append((s_, pieces, hp))
                    rem = f32(rem - s_)
                st["pst"] = pst
                incs = [f32(f32(hp_ * 100.0) * inv) for _s, _p, hp_ in steps]

                def pw_death(Pb, j, incs=incs):
                    # j = -(prewarm steps from this one to t = 0)
                    for i in range(len(incs) + j + 1, len(incs)):
                        Pb = f32(Pb + incs[i])
                        if Pb > 100.0:
                            return -(len(incs) - i)          # removed before t = 0
                    return _fgo_fx_fclock_death(st, Pb, -1)
                st["pw_death"] = pw_death
                left_t = -sum(float(s_) for s_, _p, _h in steps)
                for i, (s_, pieces, hp) in enumerate(steps):
                    left_t += float(s_)
                    st["t_end"] = left_t
                    add = 0.0
                    for x_ in pieces:
                        add = f32(add + f32(st["rate"] * x_))
                    s2 = f32(st["acc"] + add)
                    c = int(math.floor(s2))
                    st["acc"] = f32(s2 - c)
                    if c:
                        _fgo_fx_fclock_emit(st, add, hp, c, -(len(steps) - i), e["maxp"])
        while len(st["births"]) < n_need and not st["ended"] and st["j"] < 200000:
            j = st["j"]
            st["j"] = j + 1
            st["t_end"] = (j + 1) * _FGO_FX_FRAME
            pieces = None
            looping = _fgo_fx_fclock_looping(e, mv, st, j)
            if not st["started"]:
                st["dl"] = f32(st["dl"] - dt)
                if st["dl"] < 0.0:
                    st["started"] = True
                    st["pst"] = f32(-st["dl"])
                    pieces = [st["pst"]]
            else:
                p0 = st["pst"]
                new = f32(p0 + dt)
                if new >= st["dur"]:
                    if not looping:
                        st["ended"] = True          # the end frame emits nothing (_fgo_fx_oneshot)
                        break
                    pieces = [f32(st["dur"] - p0), f32(new - st["dur"])]
                    st["pst"] = pieces[1]
                else:
                    pieces = [f32(new - p0)]
                    st["pst"] = new
            if pieces is None:
                continue
            add = 0.0
            for x_ in pieces:
                add = f32(add + f32(st["rate"] * x_))
            s2 = f32(st["acc"] + add)
            c = int(math.floor(s2))
            st["acc"] = f32(s2 - c)
            if c:
                _fgo_fx_fclock_emit(st, add, dt, c, j, e["maxp"])
        return st

    def _fgo_fx_fclock_births(e, mv, out):
        """Rate births `out` (the lattice) of an in-scope emitter replaced by Unity's:
        the n-th lattice birth is the clock's n-th birth (dropped when the clock emits
        fewer: a one-shot's last frame, looping switched off)."""
        rate = float(e["rate"])
        delay = e.get("delay", 0.0)
        pw = bool(e.get("prewarm"))
        ml = _fgo_fx_max_life(e)
        res = []
        for t in out:
            x = ((t + ml) if pw else (t - delay)) * rate
            n = int(round(x))
            if n < 1 or abs(x - n) > 1e-6:
                res.append(t)              # not a lattice rate birth (left unchanged)
                continue
            st = _fgo_fx_fclock(e, mv, n)
            if n <= len(st["births"]):
                res.append(st["births"][n - 1])
        return res

    _fgo_fx_loopoff_cache = {}


    def _fgo_fx_loopoff(e, mv):
        """A looping system whose `looping` flag a clip switches off
        (e["loop_anim"], a TRACK; converter _loop_anim_fields). Unity then
        finishes the cycle in progress and ends like a one-shot: the frame
        where ps.time reaches the duration emits nothing [Unity-verified
        2026-09-27, play mode, LaneModulesRateAnimRunner.cs loopoff_* ->
        probe_modules_rateanim/: off at 1/60, 0.5, 1.3, 1.95 and exactly
        2.0 s (the float32 clock decides a switch on a cycle boundary),
        bursts too]. The frame j of the system reads the flag the clip wrote
        in that frame, i.e. the track at on + j dt (as the animated rate).
        Runs the float32 clock of _fgo_fx_oneshot with the per-frame flag.
        Returns None while the flag never goes off, else {"n": rate births
        emitted (lattice index), "k": the last cycle, "last": ps.time
        (float32) at the end of its last emitting frame}."""
        spec, T7, on = mv
        key = (id(e), T7, on)
        if key in _fgo_fx_loopoff_cache:
            return _fgo_fx_loopoff_cache[key]
        if len(_fgo_fx_loopoff_cache) > 200:
            _fgo_fx_loopoff_cache.clear()
        f32 = _fgo_fx_f32
        dt = f32(_FGO_FX_FRAME)
        dur = f32(max(e["length"], 1e-6))
        delay = f32(e.get("delay", 0.0))
        rate = float(e.get("rate", 0) or 0)
        pst = acc = 0.0
        started = False
        total = 0
        k = 0
        last = 0.0
        got = None
        if T7 is None and not e.get("loop_early"):
            # the converter only accepts a flag that goes off in the end
            # clip, which starts at T7
            _fgo_fx_loopoff_cache[key] = None
            return None
        if T7 is None:
            # Unity-verified 2026-10-02, CRLoopRunner: an off write in
            # START (held into LOOP) can stop emission while the effect
            # remains endless. Repeating off/on loop clips stay gated.
            cl = spec["clips"]
            phase_end = ((cl.get("start") or spec["owners"][0].get("endtime", 0.0)) if not (_fgo_fx_absent_start and spec.get("start_absent")) else 0.0) + (cl.get("loop") or 0.0)
        else:
            phase_end = T7 + (spec["clips"].get("end") or 0.0)
        horizon = phase_end - on + 2.0 * max(e["length"], 1e-6) + e.get("delay", 0.0) + 1.0
        clip_clock = f32(on)
        for j in range(int(horizon * _FGO_FX_HZ) + 2):
            clip_t = clip_clock if e.get("loop_early") else on + j * _FGO_FX_FRAME
            clip_clock = f32(clip_clock + dt)
            ph, u = _fgo_fx_phase(spec, T7, clip_t)
            au = _fgo_fx_autos(spec, T7, clip_t) if "autos" in spec else None
            looping = _fgo_fx_track(e["loop_anim"], ph, u, au) >= 0.5
            if not started:
                delay = f32(delay - dt)
                if -delay <= 0.0:
                    continue
                started = True
                step = pst = f32(-delay)
            else:
                new = f32(pst + dt)
                if new >= dur:
                    if not looping:
                        got = {"n": total, "k": k, "last": last}
                        break
                    # the wrap: two pieces accumulated separately (the fit)
                    acc = f32(acc + f32(rate * f32(dur - pst)))
                    step = f32(new - dur)
                    pst = step
                    k += 1
                else:
                    step = f32(new - pst)
                    pst = new
            acc = f32(acc + f32(rate * step))
            n = int(math.floor(acc))
            acc = f32(acc - n)
            total += n
            last = pst
        _fgo_fx_loopoff_cache[key] = got
        return got

    def _fgo_fx_emit_frame(e, t):
        """Emission frame (0-based from the activation) of a one-shot
        constant-rate system's rate birth at emitter-local time t, or None
        (not a rate birth / not modelled). See _fgo_fx_oneshot."""
        os_ = _fgo_fx_oneshot(e)
        rate = e.get("rate", 0) or 0
        if os_ is None or e.get("rate_curve") is not None or isinstance(rate, (tuple, list)) or rate <= 0:
            return None
        x = (t - e.get("delay", 0.0)) * rate
        n = int(round(x))
        if n < 1 or n > os_["n"] or abs(x - n) > 1e-6:
            return None
        return os_["fr"][n - 1]

    _fgo_fx_wrot_cache = {}

    def _fgo_fx_world_vectors(gm, mm, gravity, force, vol):
        """Unity-verified 2026-10-04, RGNumericRunner: gravity maps through
        inverse K; world Force and VOL through inverse K * diag(scale).
        All arguments and returned vectors are screen-oriented (y down).
        Separate matrices matter under simultaneous rotation and scale."""
        g = _fgo_fx_mv(gm, gravity)
        f = _fgo_fx_mv(mm, force)
        return tuple(g[i] + f[i] for i in range(3)), _fgo_fx_mv(mm, vol)

    def _fgo_fx_world_frame(e, mv):
        """The animation pass visible to the particle step: Legacy start,
        Animator end. Restricted constant-module scope, no prewarm/shear."""
        spec, T7, on = mv
        ag = e["anim_geo"]
        end = e["world_step"]["clock"] - 1
        cache = {}
        def at(j):
            if j not in cache:
                st = on + (j + end) * _FGO_FX_FRAME
                Js = _fgo_fx_dyn_frame(spec, e, T7, st)[1]
                gm = _fgo_fx_inv3(Js) if Js is not None else ((1.,0.,0.),(0.,1.,0.),(0.,0.,1.))
                ph, u = _fgo_fx_phase(spec, T7, st)
                au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
                M, pos, R = _fgo_fx_nodes(spec, ph, u, au)[ag["node"]]
                sc = (tuple(sum(R[k][i] * M[k][i] for k in range(3)) for i in range(3)) if ag["mode"] == 0 else
                      _fgo_fx_own_t(spec, ag["node"], ph, u, au) if ag["mode"] == 1 else (1.,1.,1.))
                mm = tuple(tuple(gm[i][k] * sc[k] for k in range(3)) for i in range(3))
                cache[j] = gm, mm
            return cache[j]
        return at

    def _fgo_fx_wrot(e, mv):
        """j -> the screen-oriented 3x3 mapping a world vector into the
        particles' stored frame during step j of the activation:
        inverse(Js) of _fgo_fx_dyn_frame at the step's start (wvol 1, Legacy
        clip) or end (wvol 2, Animator) [converter; MOPsAnimRunner.cs, re-checked
        by LEPsAnimRunner.cs + tools/fx_verify/le_psanim_rt.py: 2.6e-6 units]."""
        spec, T7, on = mv
        key = (id(e), T7, on)
        tab = _fgo_fx_wrot_cache.get(key)
        if tab is None:
            if len(_fgo_fx_wrot_cache) > 64:
                _fgo_fx_wrot_cache.clear()
            tab = _fgo_fx_wrot_cache[key] = {}
        end = 1 if e["anim_geo"]["wvol"] == 2 else 0

        def f(j):
            m = tab.get(j)
            if m is None:
                Js = _fgo_fx_dyn_frame(spec, e, T7, on + (j + end) * _FGO_FX_FRAME)[1]
                m = tab[j] = _fgo_fx_inv3(Js) if Js is not None else ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
            return m
        return f

    def _fgo_fx_inv3(m):
        a, b, c = m[0]
        d, e_, f = m[1]
        g, h, i = m[2]
        A, B, C = e_ * i - f * h, -(d * i - f * g), d * h - e_ * g
        det = a * A + b * B + c * C
        if abs(det) < 1e-12:
            return ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
        return ((A / det, -(b * i - c * h) / det, (b * f - c * e_) / det),
                (B / det, (a * i - c * g) / det, -(a * f - c * d) / det),
                (C / det, -(a * h - b * g) / det, (a * e_ - b * d) / det))

    def _fgo_fx_pinv3(m):
        """Inverse on a 3x3 transform's visible column space.

        Particle-system scales can be flat (one local scale is zero).  For a
        full-rank matrix this is the ordinary inverse; at rank two/one it is
        the Moore-Penrose solution for independent transform columns.  Orbit
        uses it for planar effects whose simulation path stays in that
        visible subspace.
        """
        inv = _fgo_fx_inv3(m)
        # _fgo_fx_inv3 returns the zero matrix only for a singular input.
        if any(inv[i][j] for i in range(3) for j in range(3)):
            return inv
        cols = [tuple(m[r][c] for r in range(3)) for c in range(3)]
        dot = lambda a, b: sum(a[k] * b[k] for k in range(3))
        norms = [dot(c, c) for c in cols]
        scale = max(norms)
        if scale <= 1e-24:
            return ((0.0, 0.0, 0.0),) * 3
        pairs = []
        for i in range(3):
            for j in range(i + 1, 3):
                aa, bb, ab = norms[i], norms[j], dot(cols[i], cols[j])
                pairs.append((aa * bb - ab * ab, i, j, aa, bb, ab))
        gd, i, j, aa, bb, ab = max(pairs)
        rows = [[0.0, 0.0, 0.0] for unused in range(3)]
        if gd > 1e-12 * scale * scale:
            for k in range(3):
                rows[i][k] = (bb * cols[i][k] - ab * cols[j][k]) / gd
                rows[j][k] = (aa * cols[j][k] - ab * cols[i][k]) / gd
        else:
            i = max(range(3), key=lambda n: norms[n])
            rows[i] = [x / norms[i] for x in cols[i]]
        return tuple(tuple(row) for row in rows)

    def _fgo_fx_emit_times(e, k, rng, mv=None):
        """Birth times (emitter-local) of emission cycle k.

        rateOverTime runs on ONE continuous lattice from the first cycle of the
        activation: the n-th particle is born when the cumulative rate integral
        reaches n (constant rate r: births at n/r -- 0.333, 0.667, 1.0, ... for
        r=3; 2.5/s carries across cycles: 0.4, 0.8, 1.2, ...). Unity-verified
        2026-09-24 (ProbeBatch2.cs Emission). The old per-cycle accumulator
        restarted each cycle and lost births to float rounding (bit_talk_40's
        mist was 13/15 and 58/60 particles). Bursts fire at their cycle offset."""
        out = []
        L = max(e["length"], 1e-6)
        c0 = e.get("delay", 0.0) + k * L
        rc = e.get("rate_curve")
        rate = e.get("rate", 0) or 0
        if "rate_anim" in e or "emis_anim" in e:
            # rateOverTime multiplier / EmissionModule.enabled animated by a
            # clip (_fgo_fx_rate_births)
            if c0 < 1e-9 and _fgo_fx_prewarm_unified and e.get("prewarm") and e["loop"]:
                # the prewarm runs BEFORE the clip's first write: the rest rate on the
                # -maxLife lattice, nothing when emission rests disabled (_fgo_fx_pw_rest)
                r0, on0 = _fgo_fx_pw_rest(e)
                if on0 and r0 > 0:
                    ml = _fgo_fx_max_life(e)
                    m = max(1, int(math.floor((c0 + ml) * r0 + 1e-9)) + 1)
                    while -ml + m / r0 <= min(c0 + L, 0.0) + 1e-9:
                        out.append(-ml + m / r0)
                        m += 1
            out.extend(t for t in _fgo_fx_rate_births(e, mv, c0 + L) if c0 <= t < c0 + L and t > 1e-9)
        elif _fgo_fx_dist_comb(e, mv):
            pass        # the constant rate rides on the distance accumulator (_fgo_fx_distance_births2)
        elif _fgo_fx_prewarm_maxlife_lattice and rc is None and rate > 0 and e.get("prewarm") and e["loop"] \
                and not e.get("delay"):
            # A prewarmed loop's lattice starts at -maxLifetime, not at a
            # cycle boundary: births at -maxLife + m / rate, m >= 1, before
            # and after t = 0 [Unity-verified 2026-09-27, PLAY mode,
            # Assets/ProbeRuntime/Lane_modules/LaneModulesPrewarmRunner.cs ->
            # probe_modules_prewarm_play/: 40 systems (rates 3 / 6 / 7 / 10 /
            # 40, durations 0.37 / 0.5 / 1 / 2, life 1.1 / 3), all 1416 births
            # within 1.7e-6 s; edit-mode Simulate shows an extra offset and is
            # not the game's path]. Cycle k keeps births in (c0, c0 + L].
            ml = _fgo_fx_max_life(e)
            m = max(1, int(math.floor((c0 + ml) * rate + 1e-9)) + 1)
            while -ml + m / rate <= c0 + L + 1e-9:
                out.append(-ml + m / rate)
                m += 1
        elif rc is not None and (e.get("start_values") or not _fgo_fx_rc_aligned(e)):
            # frames that straddle the cycle boundary / the delay (_fgo_fx_rc_births)
            out.extend(t for t in _fgo_fx_rc_births(e, c0 + L) if c0 - 1e-9 <= t < c0 + L - 1e-9)
        elif rc is not None or rate > 0:
            tab = _fgo_fx_rate_tab(e) if rc is not None else None
            C = tab[-1] if tab is not None else rate * L
            if C > 0:
                j = k - _fgo_fx_first_cycle(e)
                base = j * C
                top = (j + 1) * C + 1e-9
                n = int(math.floor(base + 1e-9)) + 1
                N = len(tab) - 1 if tab is not None else 0
                i = 0
                while n <= top:
                    need = n - base
                    if tab is None:
                        tau = need / rate
                    else:
                        while i < N - 1 and tab[i + 1] < need:
                            i += 1
                        seg = tab[i + 1] - tab[i]
                        f = (need - tab[i]) / seg if seg > 0 else 0.0
                        tau = (i + min(1.0, max(0.0, f))) * L / N
                    out.append(c0 + tau)
                    n += 1
        fc_ = _fgo_fx_fclock_scope(e)
        if fc_:
            # Unity's float32 clock (_fgo_fx_fclock): the same births in Unity's frames,
            # trimmed by the clock itself (one-shot end frame, looping switched off)
            out = _fgo_fx_fclock_births(e, mv, out)
        lo_ = _fgo_fx_loopoff(e, mv) if ("loop_anim" in e and mv is not None and not fc_) else None
        if lo_ is not None and rate > 0:
            # a clip switched looping off: rate births up to the float model's
            # count (lattice index), none in later cycles
            out = [t for t in out if int(round((t - e.get("delay", 0.0)) * rate)) <= lo_["n"]]
        os_ = _fgo_fx_oneshot(e) if (not e["loop"] and _fgo_fx_oneshot_final_frame and not fc_) else None
        if os_ is not None:
            # a one-shot system emits nothing in its final frame (see
            # _fgo_fx_oneshot): the rate births Unity's float accumulator
            # gives before it, in lattice order
            if "rate_anim" in e:
                out = [t for t in out if _fgo_fx_f32(t - c0) <= os_["last"]]
            else:
                out = out[:os_["n"]]
        if e.get("start_values") or e.get("start_anim"):
            # Preserve the source of coincident rate and burst births. A
            # float remains compatible with every older birth-time consumer.
            out = [_fgo_fx_sv_birth(e, t, False) for t in out]
        cap_batch = _fgo_fx_cap_batch and e.get("cap_batch")
        if cap_batch:
            out = [_fgo_fx_cap_birth(e, t, False) for t in out]
        move_bursts = mv is not None and e.get("bursts") and _fgo_fx_move_spawn_scope(e)
        bs_ = _fgo_fx_bsched(e, k) if (((_fgo_fx_burst_frames and e.get("bsched")) or move_bursts)
                                       and not (k < 0 and e.get("prewarm") and e.get("rep_g"))) else None
        ba_ = {}
        if _fgo_fx_burst_anim and mv is not None and bs_ is not None:
            ba_ = {bi_: tr_ for grp_, tr_ in (e.get("burst_anim") or ()) for bi_ in grp_}
        for bi, (bt, cnt) in enumerate(e.get("bursts", ())):
            if bs_ is not None:
                if bi not in bs_:
                    continue       # not fired in this cycle (later than the duration, ...)
            elif bt >= L:
                continue
            if bi in ba_:
                # the clip value at the START of the step the burst fires in (Legacy; step end - 1 frame)
                tc_ = mv[2] + (int(round(bs_[bi].se * _FGO_FX_HZ)) - 1) * _FGO_FX_FRAME
                ph_, u_ = _fgo_fx_phase(mv[0], mv[1], tc_)
                au_ = _fgo_fx_autos(mv[0], mv[1], tc_) if "autos" in mv[0] else None
                cnt = _fgo_fx_track(ba_[bi], ph_, u_, au_)
            if isinstance(cnt, (tuple, list)):
                n = int(round(rng.uniform(cnt[0], cnt[1])))
            else:
                n = int(round(cnt))
            if os_ is not None and _fgo_fx_f32(bt) > os_["last"]:
                continue       # inside the final frame (count drawn all the same)
            if lo_ is not None and (k > lo_["k"] or (k == lo_["k"] and _fgo_fx_f32(bt) > lo_["last"])):
                continue       # after a clip switched looping off (_fgo_fx_loopoff)
            tb = c0 + bt if bs_ is None else bs_[bi]
            if bs_ is None and tb < 1e-4 and _fgo_fx_prewarm_unified and e.get("prewarm") and e["loop"]:
                # a prewarm burst (one at exactly 0 too) is born at the START of the
                # prewarm step holding its time (_fgo_fx_pw_burst_birth)
                tb = _fgo_fx_pw_burst_birth(_fgo_fx_max_life(e), tb)
                if tb is None:
                    continue
            if e.get("start_values") or e.get("start_anim"):
                tb = _fgo_fx_sv_birth(e, tb, True)
            if "burst_prob" in e:
                tb = _FGOFXProbBirth(tb, bi)
            if cap_batch:
                tb = _fgo_fx_cap_birth(e, tb, True)
            out.extend([tb] * n)
        if mv is not None and "anim_geo" in e and e["anim_geo"].get("rate_dist"):
            # emission (both kinds) only while the system plays: cycle k
            # covers [c0, c0 + L)
            out.extend(t for t in _fgo_fx_distance_births(e, mv, c0 + L) if c0 <= t < c0 + L)
        out.sort()
        return out

    _fgo_fx_cap_state = {}

    # Native rdelay_cap2: 480/480 frame age multisets, including deaths/loops.
    # Released scope is narrower: non-prewarmed one-shots with no deaths during
    # emission, static single-cycle bursts and rate. Only new random-delay specs
    # opt in; older specs and their random streams stay unchanged.
    _fgo_fx_cap_batch = True

    class _FGOFXCapBirth(float):
        pass

    def _fgo_fx_cap_birth(e, t, burst):
        b = _FGOFXCapBirth(t)
        b.cap_burst = burst
        if hasattr(t, "se"):
            b.se = t.se
        if hasattr(t, "frame"):
            b.frame = t.frame
        elif burst and hasattr(t, "se"):
            b.frame = int(round(t.se * _FGO_FX_HZ)) - 1
        else:
            j = _fgo_fx_emit_frame(e, t) if not burst else None
            b.frame = j if j is not None else max(0, int(math.ceil(t * _FGO_FX_HZ - 1e-6)) - 1)
        if hasattr(t, "phase"):
            b.phase = t.phase
        return b

    def _fgo_fx_cap_batch_order(parts):
        """Frame order: rate first (youngest first), then the burst batch."""
        return sorted(range(len(parts)), key=lambda i: (
            parts[i][0].frame, parts[i][0].cap_burst,
            0.0 if parts[i][0].cap_burst else -float(parts[i][0]), i))

    _FGO_FX_PREWARM_STEP = 0.02

    # --- Unity's prewarm, unified (prompts/PREWARM_UNIFY.md, notes/prewarm_unify.md) ---
    # The GAME's Time settings (FGO assets/il2cpp files/ExportedProject/ProjectSettings/
    # TimeManager.asset): Fixed Timestep 4703995 / 141120000 = 1/30 s, Maximum Particle
    # Timestep 0.0333333. Nothing in the decompile changes them. (The old play-mode
    # probes ran in the probe project: 0.02 / 0.03 -- the "0.02 s prewarm grid" and the
    # "0.03 s steps" of the older rules were those two numbers.) These constants only
    # drive the PREWARM; the frame grid after t = 0 is _FGO_FX_FRAME (1/30 s, the same number by the game's design).
    _FGO_FX_PW_FIXED = 4703995.0 / 141120000.0
    _FGO_FX_PW_MAXPART = 0.0333333
    # True = one Unity prewarm model for every prewarmed loop (the user's instruction
    # 2026-10-01: "everything consistent with Unity"): the step schedule below for
    # Limit Velocity / orbit / noise tracks and the maxParticles rule, the true maximum
    # lifetime for the former pw_legacy emitters (snow, yume01, suspicious, cosmic_light
    # stars), bursts at their step's start, start values sampled at their emission step's
    # end. False = the separate older paths (pw_legacy, 0.02 s cap steps, 0.03 s track
    # steps ending at 0, the orbit approximation, noise on the frame grid).
    # [Unity-verified 2026-10-01, PLAY mode, PWSynRunner.cs suites grid / thr / mod /
    # kill2 / burst / clip under the game's settings and four others; fits in
    # tools/fx_verify/prewarm2/fit.py, runtime check prewarm2/rt.py]
    _fgo_fx_prewarm_unified = True
    _fgo_fx_pw_sched_cache = {}

    def _fgo_fx_pw_schedule(P):
        """Unity's prewarm steps for a prewarm of P s (P = the lifetime's true maximum;
        it covers [-P, 0], whatever the duration): (ends, fine_start, fixed_grid).
        - coarse steps of 0.2 s (P <= 10) or 1 s (P > 10) from -P while more than 5 s
          remain (float32: P = 8 takes 16 x 0.2);
        - then steps of max(Fixed, Maximum Particle Timestep) from the fine start, the
          last one the remainder (it ends at 0);
        - start values of a fine-part birth are sampled on a Fixed Timestep grid from
          the same fine start.
        [Unity-verified 2026-10-01, play mode, PWSynRunner grid / thr: identical for
        Fixed / MaxParticle 0.02/0.03, 1/30, 0.025/0.05, 0.05/0.025, 0.02/0.1,
        0.04/0.03, 0.01/0.03; P 1.3 ... 100; durations 1 / 2.5 / 37; drag positions
        within 1.3e-4 of 40 units (prewarm2/fit_drag.py)]"""
        got = _fgo_fx_pw_sched_cache.get(P)
        if got is not None:
            return got
        f = _fgo_fx_f32
        rem = f(P)
        t = f(-P)
        c = f(0.2 if rem <= 10.0 else 1.0)
        ends = []
        while rem > 5.0:
            t = f(t + c)
            rem = f(rem - c)
            ends.append(t)
        fs = t
        h = max(f(_FGO_FX_PW_FIXED), f(_FGO_FX_PW_MAXPART))
        while rem > 1e-6:
            st = h if rem > h else rem
            t = f(t + st)
            rem = f(rem - st)
            ends.append(t)
        if ends:
            ends[-1] = 0.0
        got = (ends, fs, f(_FGO_FX_PW_FIXED))
        _fgo_fx_pw_sched_cache[P] = got
        return got

    def _fgo_fx_pw_steps(P, birth):
        """The prewarm steps of a particle born at `birth` (< 0) up to t = 0: the
        partial first step to the next step end (a birth on a step end takes the whole
        next step, the clamp2 track's on-grid rule), then whole steps."""
        ends = _fgo_fx_pw_schedule(P)[0]
        i = bisect.bisect_right(ends, birth + 1e-6)
        out = []
        prev = birth
        for j in range(i, len(ends)):
            out.append(ends[j] - prev)
            prev = ends[j]
        return out

    def _fgo_fx_pw_sample(P, t):
        """System time at which the start values (curves over the cycle: size, speed,
        rotation, colour) of a prewarm birth at t < 0 are sampled: the end of its coarse
        step, or the end of its Fixed Timestep sub-step in the fine part (capped at 0)
        [Unity-verified 2026-10-01, startRotation = pi x cycle phase sensor, 13
        lifetimes x 7 time settings, prewarm2/ana_grid.py]."""
        ends, fs, fx = _fgo_fx_pw_schedule(P)
        if t < fs - 1e-6:
            i = bisect.bisect_right(ends, t + 1e-6)
            return ends[i] if i < len(ends) else 0.0
        k = math.floor((t - fs) / fx + 1e-6) + 1
        return min(0.0, fs + k * fx)

    def _fgo_fx_pw_burst_birth(P, t):
        """Birth time of a prewarmed loop's burst due at time t (<= 0), or None when it
        doesn't fire. Born at the START of the prewarm step holding t (one step old);
        a burst at exactly 0 belongs to the last prewarm step; one inside the FIRST
        prewarm step never fires (except exactly at -P); exact coincidences with a step
        end go to the next step in the coarse part and to the step ending there in the
        fine part (Unity's float32 clock decides them; this is what it did in 13 of 15)
        [Unity-verified 2026-10-01, play mode, PWSynRunner burst suite, A and B; runtime
        check prewarm2/rt.py burst]."""
        if t < -P - 1e-6:
            return None            # before the prewarm: never emitted
        if abs(t + P) < 1e-6:
            return -P
        ends, fs, fx = _fgo_fx_pw_schedule(P)
        if not ends or t < ends[0] - 1e-4:
            return None            # inside the first prewarm step
        if abs(t - ends[0]) <= 1e-4 and ends[0] > fs - 1e-6:
            return None            # the end of a first FINE step closes it: still the first step
        if t < fs - 1e-4:
            i = bisect.bisect_right(ends, t + 1e-4)       # coarse: an end at t opens the next step
            return ends[i - 1]
        i = bisect.bisect_left(ends, t - 1e-4)            # fine: an end at t closes this step
        return ends[i - 1] if i > 0 else -P

    def _fgo_fx_pw_is_burst(e, P, t):
        """Is t (< 0) the birth time of a prewarm burst (_fgo_fx_pw_burst_birth)?"""
        if not e.get("bursts"):
            return False
        L = max(e["length"], 1e-6)
        for bt, c in e["bursts"]:
            k0 = int(math.floor((t - 1e-3 - bt) / L))
            k1 = int(math.ceil((t + 1.0 + 1e-3 - bt) / L))
            for k_ in range(k0, k1 + 1):
                tb = k_ * L + bt
                if tb > 1e-4:
                    break
                bb = _fgo_fx_pw_burst_birth(P, tb)
                if bb is not None and abs(bb - t) < 1e-7:
                    return True
        return False

    def _fgo_fx_pw_nonproc(e):
        """Is this a NON-procedural system for Unity's prewarm (it steps every particle and
        frees the dead at every step) rather than a procedural one (analytic, no slot
        freed)? [Unity-verified 2026-10-01, play mode, PWSynRunner proc suite: the same
        capped prewarm reaches the cap with a random / curve gravity, orbital velocity,
        Limit Velocity, Noise, Inherit Velocity or World simulation space, and stays far
        below it with constant gravity, VOL (constant / curve / random), Force (constant
        / curve), speedModifier, rotation, size or colour over lifetime; real bundles
        bit_talk_snow snow_2 (random gravity) and cosmic_light01 stars (orbit) agree]."""
        g = e.get("grav", 0)
        return bool("clamp2" in e or "clamp" in e or "clamp_K" in e or "orbit" in e or "noise" in e
                    or e.get("sim_space") == "world"
                    or (isinstance(g, (tuple, list)) and abs(g[0] - g[1]) > 1e-12))

    def _fgo_fx_pw_rule(e):
        """Does the lane-prewarm cap rule (_fgo_fx_prewarm_hold_slots) apply?
        Prewarmed loops with startDelay 0 whose births the probe covered:
        constant rate on the -maxLife lattice and / or bursts. Off for
        pw_legacy emitters [legacy approximation, the user's decision
        2026-09-27: binding caps with random lifetimes over 8.5 s, where
        Unity's coarse early prewarm steps free slots (unmodelled); the
        prewarm of 2b70f3d instead]."""
        if _fgo_fx_prewarm_unified:
            # every prewarmed loop: no prewarm step frees a slot, whatever the lifetime
            # [Unity-verified 2026-10-01, PWSynRunner kill2: lifetimes known per birth,
            # P 8 / 9.5 (0.2 s coarse steps) and 12 / 15 / 21 (1 s steps), caps 4 / 9,
            # 58 / 60 systems exact, the two others births exactly on a step end;
            # prewarm2/fit_kill.py]. A clip-driven rate prewarms at the rest rate
            # (_fgo_fx_emit_times), which the lattice rule covers.
            return bool(_fgo_fx_prewarm_maxlife_lattice and e.get("prewarm") and e["loop"]
                        and not e.get("delay") and e.get("rate_curve") is None)
        return bool(_fgo_fx_prewarm_hold_slots and _fgo_fx_prewarm_maxlife_lattice and e.get("prewarm")
                    and e["loop"] and not e.get("delay") and e.get("rate_curve") is None
                    and "rate_anim" not in e and not e.get("pw_legacy"))

    def _fgo_fx_pw_admit(e, pw, heap, maxp, t, life):
        """One birth under the lane-prewarm cap rule; True = admitted. pw =
        the running group state, heap = death times of admitted particles.
        [Unity-verified 2026-09-27, lane prewarm, PLAY mode: probe 3 (ramp
        lifetimes, deaths inside the prewarm known) 160/160 systems; per-frame
        live counts 159/160 and 169/192; bursts 336/336 (probe 4); the
        lane's model is tools/fx_verify/prewarm_rt.py unity_counts.]
        - A prewarm birth before -maxLife doesn't exist (the prewarm starts
          there; a burst exactly at -maxLife fires).
        - Prewarm steps: 0.02 s, anchored at 0, nothing removed (rule 2).
        - From 0: one frame per step; everything dying by the frame's end goes
          first (rules 3, 4).
        - Within a step, rate births are admitted while count < maxp, a
          burst takes min(n, maxp - count at the step's start) (rule 4: a
          step holding both can overshoot by one)."""
        ml = _fgo_fx_max_life(e)
        if t < -ml - 1e-6:
            return False
        if t < -1e-6 and _fgo_fx_prewarm_unified:
            # the prewarm step holding t (Unity's schedule, _fgo_fx_pw_schedule); a
            # birth on a step end belongs to the step ending there
            key = ("p", bisect.bisect_left(_fgo_fx_pw_schedule(ml)[0], t - 1e-6))
        elif t < -1e-6:
            key = ("p", math.floor(t / _FGO_FX_PREWARM_STEP + 1e-6))
        else:
            key = ("f", math.floor(t / _FGO_FX_FRAME + 1e-6))
        if key != pw.get("key"):
            if key[0] == "p" and _fgo_fx_prewarm_unified and _fgo_fx_pw_nonproc(e):
                # a non-procedural prewarm frees the dead at the start of every step
                ends_ = _fgo_fx_pw_schedule(ml)[0]
                ss = ends_[key[1] - 1] if key[1] > 0 else -ml
                while heap and heap[0] <= ss + 1e-7:
                    heapq.heappop(heap)
            if key[0] == "f":
                fe = (key[1] + 1) * _FGO_FX_FRAME
                while heap and heap[0] <= fe + 1e-7:
                    heapq.heappop(heap)
            pw["key"] = key
            pw["c0"] = len(heap)
            pw["nr"] = 0
        if pw.get("ts") is None or abs(t - pw["ts"]) > 1e-9:
            pw["ts"] = t
            pw["tn"] = 0
        tn = pw["tn"]
        pw["tn"] = tn + 1
        L = max(e["length"], 1e-6)
        if t < -1e-6 and _fgo_fx_prewarm_unified:
            burst_t = _fgo_fx_pw_is_burst(e, ml, t)
        else:
            burst_t = any(abs((t - bt) / L - round((t - bt) / L)) * L < 1e-6 for bt, c in e.get("bursts", ()))
        rate = e.get("rate", 0) or 0
        lat = False
        if rate > 0:
            m = (t + ml) * rate
            lat = m > 0.5 and abs(m - round(m)) < 1e-6
        if burst_t and not (lat and tn == 0):
            ok = (tn - (1 if lat else 0)) < maxp - pw["c0"]
        else:
            ok = pw["c0"] + pw["nr"] < maxp
            if ok:
                pw["nr"] += 1
        if ok:
            heapq.heappush(heap, t + life)
        return ok

    def _fgo_fx_accepted(name, ei, e, wi, k, mv=None):
        """maxParticles as Unity applies it: a birth is DROPPED (not queued)
        while the system already holds maxp live particles; the rate lattice
        keeps running, births resume as particles die, and a burst can be
        partly admitted. Unity-verified 2026-09-24 (ProbeBatch2.cs Emission:
        rate 10 life 6 cap 30 -> survivors born 6.1..9.0; burst 10 cap 15 ->
        10 then 5). Evaluated sequentially from the activation's first cycle
        and cached; the result depends only on (effect, emitter, window, cycle),
        so the cache is shared by every show of the effect. Returns the
        accept flags for cycle k's chunk."""
        key = (name, ei, wi)
        batch = _fgo_fx_cap_batch and e.get("cap_batch")
        if batch:
            key += ("cap_batch",)
        if mv is not None and ("rate_anim" in e or "loop_anim" in e):
            key = (name, ei, wi, mv[1])      # births depend on the stop (T7)
        st = _fgo_fx_cap_state.get(key)
        if st is not None and k < st["next"] and k not in st["flags"]:
            # cycle k's flags were pruned (the state had run far ahead: a
            # replay from the start, or a render at an earlier time) --
            # replay from the first cycle; the result is deterministic.
            st = None
        if st is None:
            if len(_fgo_fx_cap_state) > 400:
                _fgo_fx_cap_state.clear()
            st = {"next": _fgo_fx_first_cycle(e), "heap": [], "flags": {},
                  "pw": {} if _fgo_fx_pw_rule(e) else None}
            _fgo_fx_cap_state[key] = st
        maxp = e["maxp"]
        heap = st["heap"]
        pw = st.get("pw")
        fc_ = _fgo_fx_fclock_scope(e)
        while st["next"] <= k:
            kk = st["next"]
            parts = _fgo_fx_chunk(name, ei, e, wi, kk, mv)
            if e.get('shape_tex') and e['shape_tex'][4]:
                st['flags'][kk] = _fgo_fx_shape_accept(e, st.setdefault('shape', {}), heap, parts)
                st['next'] = kk + 1
                old = kk - int(_fgo_fx_max_life(e) / max(e['length'], 1e-6)) - 4
                if not e.get('life_inf'):
                    st['flags'].pop(old, None)
                continue
            flags = [False] * len(parts) if batch else []
            order = _fgo_fx_cap_batch_order(parts) if batch else range(len(parts))
            for pi in order:
                p = parts[pi]
                t = p[0]
                if fc_ and type(t) is _FGOFXClockBirth:
                    flags.append(t.ok)          # decided on Unity's clock (_fgo_fx_fclock)
                    continue
                if pw is not None:
                    flags.append(_fgo_fx_pw_admit(e, pw, heap, maxp, t, p[1]))
                    continue
                # a burst on Unity's step schedule (_fgo_fx_bsched): the step first removes
                # every particle whose float32 age passed its lifetime by the step's END
                tk = t.se if (_fgo_fx_burst_frames and hasattr(t, "se")) else t
                while heap and heap[0] <= tk + 1e-9:
                    heapq.heappop(heap)
                if len(heap) < maxp:
                    if batch:
                        flags[pi] = True
                    else:
                        flags.append(True)
                    heapq.heappush(heap, t + p[1])
                else:
                    if not batch:
                        flags.append(False)
            st["flags"][kk] = flags
            st["next"] = kk + 1
            old = kk - int(_fgo_fx_max_life(e) / max(e["length"], 1e-6)) - 4
            if not e.get("life_inf"):
                st["flags"].pop(old, None)
        return st["flags"].get(k)

    _fgo_fx_shape_pixels = {}

    def _fgo_fx_shape_sample(texture, canonical):
        """Original Unity CPU bytes, point UV0; recttex_source2: 36,864 exact samples."""
        key, w, h, channel, cutoff = texture[:5]
        data = _fgo_fx_shape_pixels.get(key)
        if data is None:
            import zlib
            with renpy.file('assets/fx/' + key + '.rgba.z') as f:
                data = zlib.decompress(f.read())
            if len(data) != w * h * 4:
                raise ValueError('Shape texture byte count does not match dimensions')
            _fgo_fx_shape_pixels[key] = data
        x = min(w - 1, max(0, int((canonical[0] + .5) * w)))
        y = min(h - 1, max(0, int((canonical[1] + .5) * h)))
        start = (y * w + x) * 4
        return tuple(data[start:start + 4])

    def _fgo_fx_shape_step(e, birth):
        forced = getattr(birth, 'shape_step', None)
        if forced is not None:
            return forced
        if birth < -1e-6 and e.get('prewarm'):
            return ('p', bisect.bisect_left(_fgo_fx_pw_schedule(_fgo_fx_max_life(e))[0], birth - 1e-6))
        return ('f', getattr(birth, 'frame', max(0, int(math.ceil(birth * _FGO_FX_HZ - 1e-6)) - 1)))

    def _fgo_fx_shape_color(texture, sample, rgb, alpha):
        # Start C32 then texture /255 half-up;8192 exact native controls.
        flags = texture[5] if len(texture) > 5 else 0
        if flags & 1:
            rgb = tuple(((_fgo_fx_cbyte(a) * b + 127) // 255) / 255.0 for a,b in zip(rgb,sample))
        if flags & 2:
            alpha = ((_fgo_fx_cbyte(alpha) * sample[3] + 127) // 255) / 255.0
        return rgb, alpha

    def _fgo_fx_shape_begin(e, state, heap, t):
        step = _fgo_fx_shape_step(e, t)
        if step != state.get('step'):
            if step[0] == 'f':
                death = (step[1] + 1) * _FGO_FX_FRAME
            elif _fgo_fx_pw_nonproc(e):
                ends = _fgo_fx_pw_schedule(_fgo_fx_max_life(e))[0]
                death = ends[step[1] - 1] if step[1] > 0 else -_fgo_fx_max_life(e)
            else:
                death = None
            if death is not None:
                while heap and heap[0] <= death + 1e-7:
                    heapq.heappop(heap)
            state['step'], state['remaining'] = step, max(0, e['maxp'] - len(heap))
        return step

    class _FGOFXShapeBirth(float):
        pass

    def _fgo_fx_shape_padding(e, births):
        """Initialize three padding slots per rate batch AFTER every older draw.

        recttex_emit1: clipping subtracts failed padding lanes from the real
        count and swaps survivors from the complete four-lane packet. Rate
        padding has zero age (recttex_width1); held start fields are gated.
        """
        groups = {}
        for t in births:
            if not getattr(t, 'cap_burst', False):
                groups[_fgo_fx_shape_step(e, t)] = t
        out = []
        for step, source in groups.items():
            end = (_fgo_fx_pw_schedule(_fgo_fx_max_life(e))[0][step[1]] if step[0] == 'p'
                   else (step[1] + 1) * _FGO_FX_FRAME)
            for i in range(3):
                t = _FGOFXShapeBirth(end)
                t.__dict__.update(getattr(source, '__dict__', {}))
                t.shape_step, t.shape_dummy = step, True
                out.append(t)
        return out

    def _fgo_fx_shape_accept(e, state, heap, parts):
        flags = [False] * len(parts)
        groups = {}
        for i, p in enumerate(parts):
            step = _fgo_fx_shape_step(e, p[0])
            key = (step, getattr(p[0], 'cap_burst', False))
            groups.setdefault(key, [[], []])[bool(getattr(p[0], 'shape_dummy', False))].append(i)
        for (step, burst), (real, padding) in sorted(groups.items(), key=lambda x: (
                0 if x[0][0][0] == 'p' else 1, x[0][0][1], x[0][1])):
            real.sort(key=lambda i: (0 if burst else -float(parts[i][0]), i))
            if not real:
                continue
            _fgo_fx_shape_begin(e, state, heap, parts[real[0]][0])
            n = min(len(real), state['remaining'])
            state['remaining'] -= n
            # Cutoff zero cannot reject a byte, so no padded clipping occurs.
            extra = ((-n) % 4) if e['shape_tex'][4] and not burst else 0
            packet = real[:n] + padding[:extra]
            if len(packet) != n + extra:
                raise ValueError('Missing Rectangle clipping padding slots')
            count, i = len(packet), 0
            while i < count:
                if not parts[packet[i]][26]['stx']:
                    count -= 1
                    packet[i] = packet[count]
                else:
                    i += 1
            survivors = packet[:max(0, n - (n + extra - count))]
            for i in survivors:
                flags[i] = True
                p = parts[i]
                heapq.heappush(heap, p[0] + p[1])
            if step[0] == 'p' and not _fgo_fx_pw_nonproc(e):
                for i in range(n - len(survivors)):
                    heapq.heappush(heap, float(parts[real[0]][0]))
        return flags

    def _fgo_fx_clamp_track(clamp, life, vx, vy, vz, grav):
        """ClampVelocity with dampen=1, integrated: each step gravity is added,
        then speed is cut to magnitude*curve(age/life), then the particle
        moves. Returns [(x, y, vx, vy, vz, z)] every _FGO_FX_CLAMP_DT (world
        units, screen-oriented; z = depth displacement, only used by the NGUI
        shared-queue depth sort) -- velocity is path-dependent, so no closed
        form.
        The limit applies to the full 3D speed: vz (depth) counts even
        though it's never drawn."""
        mag, keys = clamp
        g = grav * _FGO_FX_GRAVITY
        dt = _FGO_FX_CLAMP_DT
        x = y = z = 0.0
        out = [(0.0, 0.0, vx, vy, vz, 0.0)]
        n = int(life / dt) + 2
        for i in range(1, n + 1):
            vy += g * dt
            lim = mag * (_fgo_curve_at(keys, min(1.0, i * dt / life)) if keys else 1.0)
            s = math.sqrt(vx * vx + vy * vy + vz * vz)
            if s > lim:
                f = max(lim, 0.0) / s
                vx *= f
                vy *= f
                vz *= f
            x += vx * dt
            y += vy * dt
            z += vz * dt
            out.append((x, y, vx, vy, vz, z))
        return out

    def _fgo_fx_orbit_mix(e, rng):
        """Random mixes for orbital X/Y/Z, radial and offset X/Y/Z.
        Unity draws one stable channel per property for TwoConstants / TwoCurves
        [Unity-verified 2026-09-28, OBRandomRunner.cs, orbit_random_fit.py].
        Called after _fgo_fx_late_draws so older specs keep their stream."""
        ob = e["orbit"]
        out = []
        for a in tuple(ob["w"]) + (ob.get("r"),) + tuple(ob.get("off", (None,) * 3)):
            random_ = (isinstance(a, tuple) and
                       ((a[0] == "const" and a[1] != a[2]) or
                        (a[0] == "curve" and a[2] != a[3])))
            out.append(rng.random() if random_ else 0.0)
        return tuple(out)

    def _fgo_fx_speed_at(e, life, mix=0.0):
        """Step-start speedModifier; Unity-verified 2026-09-30,
        SMSyntheticRunner / speedmod_fit.py. Returns a lifetime-age function.
        """
        sm = e.get("speedmod")
        if sm is None:
            return lambda age: 1.0
        if sm[0] == "const":
            value = sm[1] + (sm[2] - sm[1]) * mix
            return lambda age: value
        def at(age):
            u = min(1.0, age / life)
            lo = _fgo_curve_at(sm[3], u)
            return sm[1] * (lo + (_fgo_curve_at(sm[2], u) - lo) * mix)
        return at

    def _fgo_fx_orbit_track(e, life, birth, p0, vx, vy, vz, grav, vel_mix, orbit_mix=None, fz=None, gu=1.0, smix=0.0):
        """velocityOverLifetime orbital / radial (e["orbit"], converter
        _orbit_fields, Unity-verified there), integrated on the frame
        grid from the particle's birth (partial first step as the clamp2
        track). World y-up inside; p0 = the spawn offset (screen-oriented,
        y down) relative to the emitter, in the same units as velocity
        (Local / Hierarchy scaling). Returns the clamp track's format:
        displacement from p0 (x, y down, vx, vy, vz, z) every
        _FGO_FX_CLAMP_DT.
        fz: a constant Force (_fgo_fx_force_particle "c") joins v like
        gravity. e["clamp2"]: Limit Velocity acts on v + vol + the orbit's own
        per-step chord (orbit(p) - p) / h -- rotation AND radial, an animated
        velocity like VOL -- and the position then moves by the chord plus
        (v + vol) h [Unity-verified 2026-09-29, play mode, OBComboRunner.cs ->
        probe_orbit_combo/, tools/fx_verify/orbit_combo_fit.py model D, 8
        cases incl. orbit XYZ + offset + radial + VOL + Force + gravity + a
        limit curve at dampen 0.4: positions within 1.5e-5].
        gu (spec clamp_gu, Hierarchy scaling under a scaled root): world
        gravity is g_u = 288 / charaScale in these hierarchy units, and the
        World-simulation Limit Velocity limit too ("gl"); everything else in
        the track is in hierarchy units already [Unity-verified 2026-09-29,
        OBHierRunner.cs -> probe_orbit_hier/, orbit_hier_fit.py,
        orbit_hier_rt.py]."""
        ob = e["orbit"]
        c2 = e.get("clamp2")
        c2m = c2["m"] * (gu if e.get("clamp_gu") == "gl" else 1.0) if c2 is not None else 0.0
        if fz is not None and fz[0] == "c":
            F_ = (fz[1], -fz[2], fz[3])          # screen (y down) -> y up
        else:
            F_ = (0.0, 0.0, 0.0)
        DT = _FGO_FX_FRAME
        Ml = e.get("vol_m") or ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        # the orbit's own frame (converter orbit["m"]): the emitter's local
        # axes even when the linear VOL is in world space
        M = ob.get("m") or Ml
        # Orbit is evaluated in the particle system's simulation frame, then
        # that frame is transformed for drawing.  A transpose only inverts a
        # pure rotation; Local / Hierarchy scaling can make the frame
        # non-uniform, sheared or mirrored, so use the real inverse.
        MI = _fgo_fx_pinv3(M)
        vols = [(i, a) for i, a in enumerate(e.get("vol", ())) if a]

        orbit_mix = orbit_mix or (0.0,) * 7

        def val(a, u, mix=0.0):
            if a is None:
                return 0.0
            if not isinstance(a, tuple):       # old probe/spec helper form
                return a
            if a[0] == "const":
                return a[1] + (a[2] - a[1]) * mix
            lo = _fgo_curve_at(a[3], u)
            return a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * mix)

        def lin(u):
            li = [0.0, 0.0, 0.0]
            for i, a in vols:
                if a[0] == "const":
                    li[i] = a[1] + (a[2] - a[1]) * (vel_mix[i] if type(vel_mix) is tuple else vel_mix)
                else:
                    lo = _fgo_curve_at(a[3], u)
                    li[i] = a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * (vel_mix[i] if type(vel_mix) is tuple else vel_mix))
            return _fgo_fx_mv(Ml, li)
        g = -grav * _FGO_FX_GRAVITY * gu
        p = [p0[0], -p0[1], p0[2]]
        v = [vx, -vy, vz]
        q_ = birth / DT
        q_ -= math.floor(q_)
        h = DT * (1.0 - q_) if q_ > 1e-6 else DT
        # A prewarmed particle (birth < 0) [approximated, 2026-09-29, the
        # user's decision; switch _fgo_fx_orbit_prewarm_approx]: 0.03 s steps
        # on a grid ending at t = 0, the first one partial (pre mod 0.03), then
        # the frame grid -- the Limit Velocity track's prewarm rule.  Unity's
        # play-mode prewarm orbit is NOT exactly this: probe_orbit_prewarm,
        # 392 samples, worst 1.3e-4 world units (0.037 FGO px), growing with
        # the number of steps (no step size or anchor tried fits exactly; the
        # cases whose lifetime is a multiple of 0.03 s fit to 5e-6).
        pre = -birth if (birth < -1e-9 and _fgo_fx_orbit_prewarm_approx) else 0.0
        pws = None
        if birth < -1e-9 and _fgo_fx_prewarm_unified:
            # Unity's prewarm schedule (_fgo_fx_pw_schedule) replaces the approximation:
            # the orbit is stepped on it [Unity-verified 2026-10-01, PWSynRunner mod
            # suite "ob", A and B settings, P 2.3 / 7.3 / 12.3: positions <= 1.0e-4]
            pws = _fgo_fx_pw_steps(_fgo_fx_max_life(e), birth)
            pre = 0.0
            h = pws[0] if pws else DT
        elif pre > 0.0:
            h = math.fmod(pre, 0.03)
            if h < 1e-7:
                h = 0.03
        speed_at = _fgo_fx_speed_at(e, life, smix) if "speedmod" in e else lambda age: 1.0
        pairs = {2: (0, 1), 0: (1, 2), 1: (2, 0)}
        first = (0.0, p[0], p[1], p[2])

        def steps(p, v, h):
            pi_ = 0
            age = 0.0
            while age < life + DT:
                u = min(1.0, age / life)
                sm = speed_at(age)
                v[0] += F_[0] * h
                v[1] += (g + F_[1]) * h
                v[2] += F_[2] * h
                off = _fgo_fx_mv(M, [val(a, u, orbit_mix[4 + i])
                                      for i, a in enumerate(ob.get("off", (None,) * 3))])
                q = list(_fgo_fx_mv(MI, [p[i] - off[i] for i in range(3)]))
                for ax in (2, 0, 1):
                    ang = val(ob["w"][ax], u, orbit_mix[ax]) * h * sm
                    if ang:
                        c, s_ = math.cos(ang), math.sin(ang)
                        i, j = pairs[ax]
                        qi, qj = q[i], q[j]
                        q[i], q[j] = c * qi - s_ * qj, s_ * qi + c * qj
                r = val(ob["r"], u, orbit_mix[3]) * h * sm
                if r:
                    n = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2])
                    if n > 0.0:
                        q = [x + x / n * r for x in q]
                q = _fgo_fx_mv(M, q)
                lv = lin(u)
                if c2 is not None:
                    # Limit Velocity sees v + vol + the orbit chord
                    hs = h * sm
                    tx = v[0] + lv[0] + ((q[0] + off[0] - p[0]) / hs if hs else 0.0)
                    ty = v[1] + lv[1] + ((q[1] + off[1] - p[1]) / hs if hs else 0.0)
                    tz = v[2] + lv[2] + ((q[2] + off[2] - p[2]) / hs if hs else 0.0)
                    lim = _fgo_fx_limit_at(c2, c2m, age, life)
                    sp = math.sqrt(tx * tx + ty * ty + tz * tz)
                    if sp > lim:
                        f = (sp - max(lim, 0.0)) / sp * (1.0 - (1.0 - c2["damp"]) ** (_FGO_FX_DAMPEN_HZ * h))
                        v[0] -= tx * f
                        v[1] -= ty * f
                        v[2] -= tz * f
                p = [q[i] + off[i] + (v[i] + lv[i]) * h * sm for i in range(3)]
                age += h
                yield (age, p[0], p[1], p[2])
                if pws is not None:
                    pi_ += 1
                    h = pws[pi_] if pi_ < len(pws) else DT
                else:
                    h = min(0.03, pre - age) if age < pre - 1e-7 else DT

        if _fgo_fx_lazy_orbit:
            return _FGOFXOrbitTrack(first, steps(p, v, h), life, p0)
        states = [first]
        states.extend(steps(p, v, h))
        out = []
        j = 0
        n = int(life / _FGO_FX_CLAMP_DT) + 2
        for k in range(n + 1):
            a = k * _FGO_FX_CLAMP_DT
            while j < len(states) - 2 and states[j + 1][0] < a:
                j += 1
            s0, s1 = states[j], states[j + 1]
            dt_ = s1[0] - s0[0]
            f = min(1.0, max(0.0, (a - s0[0]) / dt_)) if dt_ > 0 else 0.0
            x = s0[1] + (s1[1] - s0[1]) * f
            y = s0[2] + (s1[2] - s0[2]) * f
            z = s0[3] + (s1[3] - s0[3]) * f
            wv = [(s1[i] - s0[i]) / dt_ if dt_ > 0 else 0.0 for i in (1, 2, 3)]
            out.append((x - p0[0], -y - p0[1], wv[0], -wv[1], wv[2], z - p0[2]))
        return out

    def _fgo_fx_late_draws(e, rng):
        """Per-particle randoms added by the modules lane, drawn after every
        older draw (so specs without these keys draw exactly as before):
        "c2" ColorOverLifetime TwoGradients mix, "fr" frameOverTime TwoCurves
        mix. None when the emitter needs none."""
        lx = None
        if "col2" in e:
            lx = {"c2": rng.random()}
        if "frame_min" in e:
            lx = lx or {}
            lx["fr"] = rng.random()
        if "rot_cycle" in e:
            lx = lx or {}
            lx["rot"] = rng.random()
        if "spin_curve" in e and e["spin_curve"][2] is not None:
            lx = lx or {}
            lx["sc"] = rng.random()      # RotationOverLifetime TwoCurves mix
        if "flip" in e:
            # renderer flip: each axis independently with its fraction
            # [Unity-verified 2026-09-27, SG2Flip.cs frac_*; converter
            # _flip_field]; bits 1 = x, 2 = y, 4 = z (mesh only)
            lx = lx or {}
            f_ = e["flip"]
            lx["fl"] = sum(b_ for b_, v_ in zip((1, 2, 4), f_) if rng.random() < v_)
        return lx

    def _fgo_fx_spin_table(sc, birth, life, mix, sign):
        """RotationOverLifetime curve (e["spin_curve"] = (deg/s scalar, max
        keys, min keys or None)): (d, [angle at ages 0, d, d + dt, ...]),
        a per-frame sum of the angular velocity at each step's START age
        (the first step partial: d = next frame end - birth) [Unity-verified
        2026-09-27, SG2RotCurve.cs, see the converter]."""
        DT = _FGO_FX_FRAME
        q = birth / DT
        q -= math.floor(q)
        d = DT * (1.0 - q) if q > 1e-6 else DT
        scal, hi_k, lo_k = sc
        tab = [0.0]
        a, h = 0.0, d

        def w_at(a):
            u = min(1.0, a / life)
            w = _fgo_curve_at(hi_k, u)
            if lo_k is not None:
                lo = _fgo_curve_at(lo_k, u)
                w = lo + (w - lo) * mix
            return w
        if birth < -1e-9 and _fgo_fx_prewarm_unified:
            # a prewarmed particle: the exact integral over its prewarm part (the
            # prewarm is analytic), then the frame sums [Unity-verified 2026-10-01,
            # PWSynRunner mod "rc" (200 deg/s x age / life) and "rk", A and B: <= 4e-4 deg]
            p_ = -birth
            n_ = max(8, 2 * int(math.ceil(p_ / 0.01)))
            hs = p_ / n_
            acc_ = w_at(0.0) + w_at(p_)
            for k_ in range(1, n_):
                acc_ += (4 if k_ % 2 else 2) * w_at(k_ * hs)
            tab.append(sign * scal * acc_ * hs / 3.0)
            a, h, d = p_, DT, p_
        while a < life + DT:
            u = min(1.0, a / life)
            w = _fgo_curve_at(hi_k, u)
            if lo_k is not None:
                lo = _fgo_curve_at(lo_k, u)
                w = lo + (w - lo) * mix
            tab.append(tab[-1] + sign * scal * w * h)
            a += h
            h = DT
        return (d, tab)

    def _fgo_fx_spin_at(lx, age):
        """Extra rotation (deg) of a RotationOverLifetime-curve particle at
        `age`, linear inside a step (0 when the emitter has no curve)."""
        spt = lx.get("spt") if lx else None
        if spt is None:
            return 0.0
        d, tab = spt
        if age < d:
            i, f = 0, age / d
        else:
            fi = (age - d) / _FGO_FX_FRAME
            i = 1 + int(fi)
            f = fi - int(fi)
        if i >= len(tab) - 1:
            return tab[-1]
        return tab[i] + (tab[i + 1] - tab[i]) * f

    def _fgo_fx_tilt_at(tilt, lx, age):
        """Separate-axis rotation curves share the existing frame table.
        Unity-verified 2026-10-02 GMBundle, including shared mix and flip."""
        x, y = (tilt[0] + tilt[2] * age, tilt[1] + tilt[3] * age) if tilt is not None else (0.0, 0.0)
        if lx and "spt3" in lx:
            x += _fgo_fx_spin_at({"spt": lx["spt3"][0]}, age)
            y += _fgo_fx_spin_at({"spt": lx["spt3"][1]}, age)
        return x, y

    # A burst due EXACTLY on a frame boundary is emitted in whichever frame Unity's float32 clock says:
    # the first frame whose clock is strictly greater than the burst's time (float32). At 1/30 s the
    # 12-frame clock equals the float32 0.4, so a burst at 0.4 s is emitted in the NEXT frame (its
    # start values are sampled at 0.4333, not 0.4); at 1/60 s the 24-frame clock is just above it
    # (the same frame) [Unity-verified 2026-10-01, play mode, game timing, F3GradRunner
    # burst0 / burst4 / burstoff / burst35 / burst1 / burst2 and F3GridRunner bursts (every burst
    # in the frame the rule gives), NCGradRunner at 1/60 s (burst4 in the frame ending at 0.4)].
    # True = that rule for the start values sampled at a burst's frame end (startSize / startColor /
    # startRotation curves); False = the older approximations (colour: the frame ending at the burst;
    # size / rotation: the next one).
    _fgo_fx_burst_clock = True
    _fgo_fx_burst_cache = {}

    def _fgo_fx_burst_end(e, t):
        """Emitter-local end of the frame a burst due at emitter-local time t is emitted in
        (float32 clock, see _fgo_fx_burst_clock), or None (t is no burst of the first cycle)."""
        if not _fgo_fx_burst_clock or not e.get("bursts"):
            return None
        d = e.get("delay", 0.0)
        for bt, c in e["bursts"]:
            if bt >= max(e["length"], 1e-6) or abs(d + bt - t) > 1e-7:
                continue
            key = (id(e), bt)
            got = _fgo_fx_burst_cache.get(key)
            if got is None:
                f = _fgo_fx_f32
                dt = f(_FGO_FX_FRAME)
                dl = f(d)
                tb = f(bt)
                pst = 0.0
                started = False
                got = 0.0
                for j in range(100000):
                    if not started:
                        dl = f(dl - dt)
                        if -dl > 0.0:
                            started = True
                            pst = f(-dl)
                        else:
                            continue
                    else:
                        pst = f(pst + dt)
                    if pst > tb:
                        got = (j + 1) * _FGO_FX_FRAME
                        break
                if len(_fgo_fx_burst_cache) > 2000:
                    _fgo_fx_burst_cache.clear()
                _fgo_fx_burst_cache[key] = got
            return got or None
        return None

    # --- Unity's burst schedule (prompts/OPUS_PUZZLES.md puzzle A, notes/opus_puzzles.md) ---
    # [Unity-verified 2026-10-06, play mode, game timing 1/30, PZBurstRunner -> probe_puzzles_burst
    # / _burst2: 654 systems, tools/fx_verify/puzzles/a_fit2.py: births per frame, ages, live counts
    # with maxParticles, one-shots and Legacy looping-off all pass; the alternatives fail]
    # Per frame the float32 system clock runs [p0, p1); a looping system's wrap step runs W1 = [p0, L]
    # (a burst AT the duration fires) then W2 = [0, p1w); the first step after startDelay [0, p1].
    # Everything a step emits shares ONE age: per segment the youngest firing burst gives
    # T * (end - bt) / (end - start), T = the step's real length for a linear / first step but a FULL
    # frame for each half of a wrap step, W1's end being L + 1e-4; the step takes the FIRST segment
    # with a firing burst (zero-count bursts count). So a loop's burst at 0 after an off-grid wrap is
    # born at the wrap frame's START, and two bursts in one frame share the later one's age. Only
    # emitters with spec key "bsched" (converter: single-cycle bursts whose schedule differs from the
    # continuous one; no prewarm / probability / start-value / moving / animated-emission combos).
    # True = that schedule (and a step-end kill test for maxParticles); False = the older continuous
    # births c0 + bt (bursts at / after the duration skipped).
    _fgo_fx_burst_frames = True
    # Repeat bursts at least a step apart (converter REPEAT_CYCLES, spec key rep_s): cycle pos >= 1 is due at
    # float32(base) + float32(pos * float32(interval)), fires in the first step whose window (s0, s1] holds it
    # (closed on the right), is born at the segment START (age T) and never at / after the duration
    # [Unity-verified 2026-10-20, play mode, 148 synthetic systems + real bit_talk_sunlight01, sprites_anim
    # target 3]. False = the older continuous births for those emitters (the spec then needs no rep_s).
    _fgo_fx_rep_cycles = True
    # Repeat cycles (pos >= 1 of rep_s / rep_g) due in the W1 half of a wrap step never fire; the new loop's
    # cycle 0 in W2 still does (spec key rep_w1, converter CYC0_FOREVER: bursts with cycleCount 0)
    # [Unity-verified 2026-10-22, play mode, 40 synthetic systems, notes/order_releases_2.md Part 0].
    # False = the older W1 windows.
    _fgo_fx_rep_w1 = True
    # A Legacy clip writing a burst count (spec key burst_anim, converter _burst_anim_fields): the burst fires
    # with the clip value at the START of its step [Unity-verified 2026-10-20, real bit_talk_sunlight01 /
    # bit_talk_sunlight, 23 stops]. False = the serialized count.
    _fgo_fx_burst_anim = True
    _fgo_fx_bsched_cache = {}
    _FGO_FX_BURST_W1_EPS = 1e-4

    class _FGOFXStepBirth(float):
        """A burst birth on Unity's step schedule: the float is the equivalent emitter-local
        birth (step end - the step's shared age); .se = that step's end."""
        def __new__(cls, value, se):
            obj = float.__new__(cls, value)
            obj.se = se
            return obj

        def __getnewargs__(self):
            return (float(self), self.se)

    def _fgo_fx_bsched(e, k):
        """{burst index: _FGOFXStepBirth} of emission cycle k (the float32 clock's own cycle
        count) on Unity's burst schedule (see _fgo_fx_burst_frames)."""
        bursts = e.get("bursts") or ()
        rep_g = tuple(tuple(g) for g in (e.get("rep_g") or ()))
        rep_s = tuple((tuple(g), iv) for g, iv in (e.get("rep_s") or ())) if _fgo_fx_rep_cycles else ()
        w1rep = bool(_fgo_fx_rep_w1 and e.get("rep_w1"))
        key = (e["length"], e.get("delay", 0.0), bool(e["loop"]), tuple(bt for bt, c in bursts)) + ((rep_g,) if rep_g else ()) + (("rs", rep_s) if rep_s else ()) + (("w1",) if w1rep else ())
        st = _fgo_fx_bsched_cache.get(key)
        if st is None:
            if len(_fgo_fx_bsched_cache) > 256:
                _fgo_fx_bsched_cache.clear()
            f = _fgo_fx_f32
            st = {"j": 0, "pst": 0.0, "dl": f(e.get("delay", 0.0)), "started": False, "cyc": 0,
                  "done": False, "cycles": {}}
            _fgo_fx_bsched_cache[key] = st
        f = _fgo_fx_f32
        dt = f(_FGO_FX_FRAME)
        L = f(max(e["length"], 1e-6))
        bts = [f(bt) for bt, c in bursts]
        gmap = {bi: (gx, pos) for gx, g in enumerate(rep_g) for pos, bi in enumerate(g)}
        rmap = {bi: (g[0], pos, iv) for g, iv in rep_s for pos, bi in enumerate(g) if pos > 0}
        eps = _FGO_FX_BURST_W1_EPS
        while not st["done"] and st["cyc"] <= k:
            j = st["j"]
            st["j"] = j + 1
            cyc = st["cyc"]
            if not st["started"]:
                st["dl"] = f(st["dl"] - dt)
                if st["dl"] > 0.0:
                    continue
                st["started"] = True
                p1 = f(-st["dl"])
                segs = [(0.0, p1, p1, 0, cyc)]
                st["pst"] = p1
            else:
                p0 = st["pst"]
                p1 = f(p0 + dt)
                if p1 >= L:
                    if not e["loop"]:
                        st["done"] = True      # the one-shot's final frame emits nothing
                        continue
                    p1w = f(p1 - L)
                    segs = [(p0, L, dt, 1, cyc), (0.0, p1w, dt, 2, cyc + 1)]
                    st["pst"] = p1w
                    st["cyc"] = cyc + 1
                else:
                    segs = [(p0, p1, f(p1 - p0), 3, cyc)]
                    st["pst"] = p1
            age = None
            fired = []
            for s0, s1, T, kind, cy in segs:
                seg_age = None
                gdone = set()
                for bi, bt in enumerate(bts):
                    if bi in rmap:
                        b0, pos, iv = rmap[bi]
                        rt = f(bts[b0] + f(pos * f(iv)))
                        if rt >= L or not (s0 < rt <= s1) or (w1rep and kind == 1):
                            continue
                        fired.append((bi, cy))
                        seg_age = T if seg_age is None else min(seg_age, T)
                        continue
                    if bt > L:
                        continue
                    if kind == 0 or kind == 1:
                        hit = s0 <= bt <= s1
                    else:
                        hit = s0 <= bt < s1
                    if not hit:
                        continue
                    gi = gmap.get(bi)
                    if gi is not None and w1rep and kind == 1 and gi[1] > 0:
                        continue
                    if gi is not None:
                        if gi[0] in gdone:
                            continue        # repeat group: one birth per step, dropped cycles never carried
                        gdone.add(gi[0])
                    fired.append((bi, cy))
                    if kind == 1:
                        a = T * (L + eps - bt) / (L + eps - s0)
                    else:
                        a = T * (s1 - bt) / (s1 - s0) if s1 > s0 else T
                    if gi is not None and gi[1] > 0:
                        a = T
                    a = max(0.0, a)
                    seg_age = a if seg_age is None else min(seg_age, a)
                if seg_age is not None and age is None:
                    age = seg_age
            if fired:
                se = (j + 1) * _FGO_FX_FRAME
                for bi, cy in fired:
                    st["cycles"].setdefault(cy, {})[bi] = _FGOFXStepBirth(se - age, se)
        return st["cycles"].get(k, {})

    def _fgo_fx_fe_time(e, t):
        """Emitter-local time of the END of the frame a particle born
        at t is emitted in: the next boundary; exactly on a boundary, the
        frame after it (bursts, edit-mode rule) unless a one-shot's rate
        birth, whose frame the float32 model gives (_fgo_fx_emit_frame)."""
        be_ = _fgo_fx_burst_end(e, t) if t > 1e-9 else None
        if be_ is not None:
            return be_
        ts = (math.floor(t * _FGO_FX_HZ + 1e-6) + 1) / _FGO_FX_HZ
        if not e["loop"] and abs(t * _FGO_FX_HZ - round(t * _FGO_FX_HZ)) < 1e-6:
            fe_ = _fgo_fx_emit_frame(e, t)
            if fe_ is not None:
                ts = (fe_ + 1) / _FGO_FX_HZ
        return ts

    class _FGOFXProbBirth(float):
        def __new__(cls, value, entry):
            obj = float.__new__(cls, value)
            obj.entry = entry
            # Preserve the sampled start-value clock where both apply.
            if hasattr(value, "frame"):
                obj.frame, obj.phase = value.frame, value.phase
            return obj

        def __getnewargs__(self):
            return (float(self), self.entry)

    def _fgo_fx_prob_filter(parts, probs, rng):
        """SFExtraRunner: one independent decision per scheduled firing.
        Called AFTER all older particle draws, before maxParticles acceptance.
        Failed bursts discard their whole group, not individual particles."""
        keep = [rng.random() < p for p in probs]
        return [p for p in parts if not hasattr(p[0], "entry") or keep[p[0].entry]]

    class _FGOFXStartBirth(float):
        pass

    _fgo_fx_sv_clocks = {}

    def _fgo_fx_sv_clock(e, n):
        """New start-value specs only. Float32 system clock, including delay
        and loop wrap. Unity-verified 2026-10-02, SVGridRunner at 30 fps."""
        key = (e["length"], e.get("delay", 0.0), e["loop"])
        got = _fgo_fx_sv_clocks.get(key)
        if got is None:
            got = [[], _fgo_fx_f32(e.get("delay", 0.0)), False, 0.0]
            if len(_fgo_fx_sv_clocks) > 256:
                _fgo_fx_sv_clocks.clear()
            _fgo_fx_sv_clocks[key] = got
        clocks, delay, started, pst = got
        f = _fgo_fx_f32
        dt, L = f(_FGO_FX_FRAME), f(e["length"])
        while len(clocks) <= n:
            old = pst
            if not started:
                delay = f(delay - dt)
                step = f(-delay) if delay < 0.0 else 0.0
                if step > 0:
                    started = True
                    pst = step
            else:
                pst = f(pst + dt)
                step = f(pst - old)
            if e["loop"] and pst >= L:
                pst = f(pst - L)
            elif not e["loop"] and pst >= L:
                pst = L
            clocks.append((pst, step))
        got[1:] = [delay, started, pst]
        return clocks[n]

    def _fgo_fx_sv_birth(e, t, burst):
        """Sampling metadata distinguishes a burst from a rate particle at
        the same time. Keep old particle times and random streams unchanged."""
        if isinstance(t, _FGOFXStartBirth):
            return t
        b = _FGOFXStartBirth(t)
        if t < 0.0:
            ts = _fgo_fx_pw_sample(_fgo_fx_max_life(e), t)
            b.phase = ((ts - e.get("delay", 0.0)) % e["length"]) / e["length"]
            b.frame = int(math.floor(ts * _FGO_FX_HZ))
            return b
        if burst:
            ts = _fgo_fx_burst_end(e, t)
            j = int(round(ts * _FGO_FX_HZ)) - 1 if ts else int(math.floor(t * _FGO_FX_HZ + 1e-6))
            if e["loop"]:
                j = 0
                cycles = 0
                prev = 0.0
                L_ = e["length"]
                cycle_ = max(0, int(math.floor((t - e.get("delay", 0.0)) / L_ + 1e-6)))
                due = _fgo_fx_f32(t - e.get("delay", 0.0) - cycle_ * L_)
                while j < 100000:
                    clock = _fgo_fx_sv_clock(e, j)[0]
                    if clock < prev:
                        cycles += 1
                    if cycles > cycle_ or (cycles == cycle_ and clock > due):
                        break
                    prev = clock
                    j += 1
                if cycle_ > 0 and abs(due) < 1e-7:
                    # A loop's zero-offset burst starts at the beginning
                    # of its wrap frame (SVGridRunner), not the boundary.
                    b = _FGOFXStartBirth(j * _FGO_FX_FRAME)
        else:
            j = _fgo_fx_emit_frame(e, t) if not e["loop"] else None
            if e["loop"] and e.get("rate", 0) > 0 and not e.get("rate_curve"):
                index_ = int(round((t - e.get("delay", 0.0)) * e["rate"]))
                j = _fgo_fx_sv_rate_frame(e, index_)
            if j is None:
                j = max(0, int(math.ceil(t * _FGO_FX_HZ - 1e-6)) - 1)
        b.frame = j
        b.phase = _fgo_fx_f32(_fgo_fx_sv_clock(e, j)[0] / _fgo_fx_f32(e["length"]))
        return b

    _fgo_fx_sv_rate_frames = {}

    def _fgo_fx_sv_rate_frame(e, index):
        """Constant-rate loops: retain the float32 accumulator's birth
        frame instead of rounding an exact lattice time onto a frame."""
        key = (e["length"], e.get("delay", 0.0), e.get("rate", 0))
        got = _fgo_fx_sv_rate_frames.get(key)
        if got is None:
            got = [[], 0.0, 0]
            if len(_fgo_fx_sv_rate_frames) > 256:
                _fgo_fx_sv_rate_frames.clear()
            _fgo_fx_sv_rate_frames[key] = got
        frames, acc, j = got
        f = _fgo_fx_f32
        while len(frames) < index and j < 100000:
            step = _fgo_fx_sv_clock(e, j)[1]
            acc = f(acc + f(f(e["rate"]) * step))
            n = int(math.floor(acc))
            acc = f(acc - n)
            frames.extend([j] * n)
            j += 1
        got[1:] = [acc, j]
        return frames[index - 1] if 0 < index <= len(frames) else None

    # Legacy frame-end size curves: prevent a one-shot's final sample from
    # wrapping back to phase zero. Native endpoint_grid (73 cases), Oct 11:
    # the delay counts down in float32; the last emitting step may be just
    # BELOW duration although the analytic frame end reaches it. Preserve
    # the exact emitting frame and use its float32 phase, not a guessed snap.
    _fgo_fx_oneshot_end = True

    def _fgo_fx_oneshot_end_phase(e, t, ts, phase):
        """Endpoint-only correction; no birth/RNG/schedule changes.
        Unverified clocks retain their historical phase. start_values already
        has its verified clamped clock, including stopped one-shot phase1.
        """
        if (not _fgo_fx_oneshot_end or e["loop"] or not e.get("size_fe")
                or not e.get("size_cycle") or e["length"] < 0.05
                or ts - e.get("delay", 0.0) < e["length"]
                or any(e.get(k) for k in ("prewarm", "bursts", "rate_curve",
                       "rate_anim", "emis_anim", "loop_anim", "start_anim",
                       "start_values", "sub"))
                or e.get("anim_geo", {}).get("rate_dist")
                or id(e) in _fgo_fx_sub_parents):
            return phase
        # The float32 birth already records its emitting step. Recovering
        # the lattice index from its rounded age can fail at larger rates.
        frame = (int(round(t.bk * _FGO_FX_HZ)) - 1
                 if isinstance(t, _FGOFXClockBirth) else _fgo_fx_emit_frame(e, t))
        if frame is None:
            return phase
        return _fgo_fx_f32(_fgo_fx_sv_clock(e, frame)[0] /
                           _fgo_fx_f32(e["length"]))

    def _fgo_fx_sv_value(ax, birth, mix):
        """Shared start-value sampler; axes use independent mixes."""
        scal, hi, lo = ax
        a = _fgo_curve_at(lo, birth.phase)
        return scal * (a + (_fgo_curve_at(hi, birth.phase) - a) * mix)

    _fgo_fx_sv_gravity_cache = {}

    def _fgo_fx_sv_gravity(e, birth, age, mix, mv=None):
        """Free curve gravity: system phase at each frame END, velocity then
        position. SVGridRunner, 2026-10-02; birth-held gravity fails by .97.
        Returns downward displacement and velocity, in world units."""
        key = (id(e), float(birth), age, mix, (mv[1], mv[2]) if mv is not None else None)
        got = _fgo_fx_sv_gravity_cache.get(key)
        if got is not None:
            return got
        ax_ = e.get("grav_cycle")
        j = max(0, int(math.floor(birth * _FGO_FX_HZ + 1e-6)))
        end = (j + 1) * _FGO_FX_FRAME
        elapsed = 0.0
        pos = vel = 0.0
        while elapsed < age - 1e-9:
            h = min(age - elapsed, max(0.0, end - birth - elapsed))
            if h > 1e-9:
                if ax_ is not None:
                    scal, hi, lo = ax_
                    phase = _fgo_fx_f32(_fgo_fx_sv_clock(e, j)[0] / _fgo_fx_f32(e["length"]))
                    a = _fgo_curve_at(lo, phase)
                    g = scal * (a + (_fgo_curve_at(hi, phase) - a) * mix)
                else:
                    g = _fgo_fx_sv_anim(e, "grav", j, mv, mix)
                vel += g * _FGO_FX_GRAVITY * h
                pos += vel * h
                elapsed += h
            j += 1
            end = (j + 1) * _FGO_FX_FRAME
        if len(_fgo_fx_sv_gravity_cache) > 5000:
            _fgo_fx_sv_gravity_cache.clear()
        got = (pos, vel)
        _fgo_fx_sv_gravity_cache[key] = got
        return got

    def _fgo_fx_sv_anim(e, key, frame, mv, mix):
        """InitialModule clips: frame-start Legacy, frame-end Animator.
        Production sampler shared by birth fields and changing gravity."""
        hi, lo, scale, end = e["start_anim"][key]
        spec, T7, on = mv
        t = on + (frame + end) * _FGO_FX_FRAME
        ph, u = _fgo_fx_phase(spec, T7, t)
        au = _fgo_fx_autos(spec, T7, t) if "autos" in spec else None
        h = _fgo_fx_track(hi, ph, u, au)
        l = _fgo_fx_track(lo, ph, u, au) if lo is not None else h
        return scale * (l + (h - l) * mix)

    def _fgo_fx_rot_cycle(e, t, mix):
        """startRotation curve (e["rot_cycle"] = (deg scalar, max, min)) at
        the cycle phase of the emission frame's end."""
        scal, hi_k, lo_k = e["rot_cycle"]
        L = e["length"]
        if t < -1e-6 and _fgo_fx_prewarm_unified and e.get("prewarm") and e["loop"]:
            ts = _fgo_fx_pw_sample(_fgo_fx_max_life(e), t)     # a prewarm birth
        else:
            ts = _fgo_fx_fe_time(e, t)
        ph = ((ts - e.get("delay", 0.0)) % L) / L if L > 0 else 0.0
        lo = _fgo_curve_at(lo_k, ph)
        return scal * (lo + (_fgo_curve_at(hi_k, ph) - lo) * mix)

    def _fgo_fx_oklab(c, inv=False):
        """linear RGB <-> OKLab (Bjorn Ottosson's matrices)."""
        if not inv:
            r, g, b = c
            l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
            m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
            s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
            l, m, s = [math.copysign(abs(x) ** (1.0 / 3.0), x) for x in (l, m, s)]
            return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
                    1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
                    0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)
        L, a, b = c
        l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
        return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
                -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
                -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)

    def _fgo_fx_gradm(keys, t, mode, alpha, fixed_epsilon=1e-9):
        """A gradient's colour or alpha keys at t with Unity's m_Mode:
        0 Blend (linear, _fgo_grad); 1 Fixed = the value of the first key at
        or after t; 2 PerceptualBlend = colours interpolated in OKLab of the
        LINEAR colour (alpha linear) [Unity-verified 2026-09-26,
        LaneModulesProbe.cs Gradients(), lane_modules_grad_fit.py: Fixed
        exact vs Gradient.Evaluate, PerceptualBlend within 0.002; particle
        colour = Gradient.Evaluate(age/life) within 0.5 levels]. The default
        Fixed epsilon preserves older COL behavior; startColor uses exact
        inclusive comparisons with float32 key times (NBEdges)."""
        if mode == 1:
            for kt, v in keys:
                if t <= kt + fixed_epsilon:
                    return v
            return keys[-1][1]
        if mode != 2 or alpha:
            return _fgo_grad(keys, t)
        if t <= keys[0][0]:
            return keys[0][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t <= t1:
                f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                s2l = lambda c: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                l2s = lambda c: 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1.0 / 2.4) - 0.055
                A = _fgo_fx_oklab(tuple(s2l(x) for x in v0))
                B = _fgo_fx_oklab(tuple(s2l(x) for x in v1))
                c = _fgo_fx_oklab(tuple(a + (b - a) * f for a, b in zip(A, B)), True)
                return tuple(min(1.0, max(0.0, l2s(max(0.0, x)))) for x in c)
        return keys[-1][1]

    _fgo_fx_tint_clock = [0.0]

    def _fgo_fx_start_phase(e, t, ph):
        """Fixed startColor's one-shot phase, including float32 clock drift.
        Unity-verified 2026-09-30, NBRunner / NBClockRunner (rates 20/37/60,
        durations .55/1/2, bursts 0/.4/.3417; 487 clock phases exact).
        Loop / prewarm / delay remain refused for Fixed cycle gradients.
        The sampling frame is unchanged; its precise clock selects the key.
        """
        if 1 not in e["tint_mode"] or not _fgo_fx_tint_frame_end:
            return ph
        n = max(1, int(math.ceil(t * _FGO_FX_HZ - 1e-6)))
        # A rate birth exactly on the grid can belong to the next frame
        # when the float32 accumulator is just short. Bursts use their
        # own frame (a burst can coincide with a rate birth's lattice time).
        if not any(abs(t - bt) < 1e-7 for bt, count in e.get("bursts", ())):
            fr = _fgo_fx_emit_frame(e, t)
            if fr is not None:
                n = fr + 1
        dt = _fgo_fx_f32(_FGO_FX_FRAME)
        while len(_fgo_fx_tint_clock) <= n:
            _fgo_fx_tint_clock.append(_fgo_fx_f32(_fgo_fx_tint_clock[-1] + dt))
        return _fgo_fx_f32(_fgo_fx_tint_clock[n] / _fgo_fx_f32(e["length"]))

    def _fgo_fx_start_tint(e, ph, mix=0.0):
        """Non-blend startColor, shared RGBA sample / TwoGradients mix.
        Unity-verified 2026-09-30: NBRunner, nonblend_fit.py / nonblend_rt.py.
        Fixed is next-key inclusive; PerceptualBlend reuses the COL evaluator
        (OKLab of linear RGB, linear alpha). RandomColor passes its one random
        phase here and holds the result for the particle's life.
        """
        hi_mode, lo_mode = e["tint_mode"]
        if "tint_cycle2" in e:
            (lc, la), (hc, ha) = e["tint_cycle2"]
            lo = _fgo_fx_gradm(lc, ph, lo_mode, False, 0.0)
            hi = _fgo_fx_gradm(hc, ph, hi_mode, False, 0.0)
            a = _fgo_fx_gradm(la, ph, lo_mode, True, 0.0)
            b = _fgo_fx_gradm(ha, ph, hi_mode, True, 0.0)
            return tuple(x + (y - x) * mix for x, y in zip(lo, hi)), a + (b - a) * mix
        colors, alphas = e.get("tint_cycle", e.get("tint_gradient"))
        return (_fgo_fx_gradm(colors, ph, hi_mode, False, 0.0),
                _fgo_fx_gradm(alphas, ph, hi_mode, True, 0.0))

    def _fgo_fx_sing_map(K, x, y, z):
        """A screen-oriented (y down) simulation-space vector through the
        simulation frame K (rows, y-up world axes): a flat emitter's (a zero
        scale, spec `sing`) drawn offset / velocity, whatever the lost axis
        -- its component is never seen."""
        w = _fgo_fx_mv(K, (x, -y, z))
        return w[0], -w[1], w[2]

    def _fgo_fx_sing_track(K, track):
        """A clamp track computed in simulation space -> draw space: each
        (x, y, vx, vy, vz, z) state's offset and velocity through K
        (_fgo_fx_sing_map)."""
        if not isinstance(track, list):
            return _FGOFXSingTrack(K, track)         # a lazy noise track
        return [_fgo_fx_sing_state(K, s) for s in track]

    def _fgo_fx_sing_state(K, s):
        x, y, vx, vy, vz, z = s
        x, y, z = _fgo_fx_sing_map(K, x, y, z)
        vx, vy, vz = _fgo_fx_sing_map(K, vx, vy, vz)
        return (x, y, vx, vy, vz, z)

    class _FGOFXSingTrack(_FGO_FX_OBJECT):
        """_fgo_fx_sing_track over a lazy track: mapped on access."""

        def __init__(self, K, track):
            self.K = K
            self.t = track

        def __len__(self):
            return len(self.t)

        def __getitem__(self, i):
            return _fgo_fx_sing_state(self.K, self.t[i])

    def _fgo_fx_clampK_track(e, c2, lim0, g, life, birth, vx, vy, vz, vol_at=None, F=(0.0, 0.0, 0.0), dmix=None, size=1.0):
        """Limit Velocity in Local simulation space under a NON-uniform scale
        K (e["clamp_K"], rows, simulation -> draw axes, world y up): the
        stored simulation-space velocity is limited as a whole, world gravity
        enters as K^-1 g [Unity-verified 2026-09-27, small gates 2,
        SG2NuClamp.cs, sg2_nuclamp_fit.py: 1.7e-6]. velocityOverLifetime and a
        constant Force enter in SIMULATION space: the draw-space vector the
        clamp2 track builds (vol_m / force.m: K for a local-space module,
        diag(lossy) for a world-space one) mapped back through K^-1 -- a
        local-space module is its own value, a world-space one K^-1 diag(lossy)
        v -- and the limit acts on the whole v + vol [Unity-verified
        2026-09-29, CCSynRunner.cs -> probe_csclamp_syn/, csclamp_fit.py: 17
        frames incl. sheared (1,2,1) x (1.5,1,1) and Local scaling (2,1,1.5) /
        (4,1,1), 1e-6]. Drag acts in simulation space on the whole v + vol
        (_fgo_fx_drag_step). Same step grid and output format as
        _fgo_fx_clamp2_track."""
        K = e["clamp_K"]
        a_, b_, c_ = K[0]
        d_, e_, f_ = K[1]
        g_, h_, i_ = K[2]
        det = a_ * (e_ * i_ - f_ * h_) - b_ * (d_ * i_ - f_ * g_) + c_ * (d_ * h_ - e_ * g_)
        Ki = ((e_ * i_ - f_ * h_) / det, (c_ * h_ - b_ * i_) / det, (b_ * f_ - c_ * e_) / det),              ((f_ * g_ - d_ * i_) / det, (a_ * i_ - c_ * g_) / det, (c_ * d_ - a_ * f_) / det),              ((d_ * h_ - e_ * g_) / det, (b_ * g_ - a_ * h_) / det, (a_ * e_ - b_ * d_) / det)
        DT = _FGO_FX_FRAME
        v = _fgo_fx_mv(Ki, (vx, -vy, vz))             # screen (y down) -> sim
        gs = _fgo_fx_mv(Ki, (0.0, -g, 0.0))            # world gravity -> sim
        Fc = None if type(F) is tuple else F
        if Fc is None and (F[0] or F[1] or F[2]):
            gs = tuple(a + b for a, b in zip(gs, _fgo_fx_mv(Ki, (F[0], -F[1], F[2]))))
        dc_ = c2.get("drag_c")
        q = birth / DT
        q -= math.floor(q)
        h = DT * (1.0 - q) if q > 1e-6 else DT
        # a prewarmed particle: Unity's prewarm steps first (_fgo_fx_clamp2_track)
        pws = (_fgo_fx_pw_steps(_fgo_fx_max_life(e), birth)
               if (birth < -1e-9 and _fgo_fx_prewarm_unified) else [])
        pi_ = 0
        if pws:
            h = pws[0]
        xs = [0.0, 0.0, 0.0]
        age = 0.0
        vd = vol_at(0.0) if vol_at is not None else (0.0, 0.0, 0.0)
        states = [(0.0, 0.0, 0.0, 0.0, vx + vd[0], vy + vd[1], vz + vd[2], 0.0)]
        gs0 = gs
        while age < life + DT:
            if Fc is not None:
                Fa = Fc(age)                                  # a Force curve, at the step's start age
                gs = tuple(a + b for a, b in zip(gs0, _fgo_fx_mv(Ki, (Fa[0], -Fa[1], Fa[2]))))
            v = (v[0] + gs[0] * h, v[1] + gs[1] * h, v[2] + gs[2] * h)
            vo = _fgo_fx_mv(Ki, (vd[0], -vd[1], vd[2]))   # VOL now (step start), simulation space
            tx, ty, tz = v[0] + vo[0], v[1] + vo[1], v[2] + vo[2]
            lim = _fgo_fx_limit_at(c2, lim0, age, life)
            sp = math.sqrt(tx * tx + ty * ty + tz * tz)
            if sp > lim:
                f = (sp - max(lim, 0.0)) / sp * (1.0 - (1.0 - c2["damp"]) ** (_FGO_FX_DAMPEN_HZ * h))
                v = (v[0] - tx * f, v[1] - ty * f, v[2] - tz * f)
            if c2["drag"] or dc_:
                v = _fgo_fx_drag_step(c2, dc_, age, life, size, dmix, h, v[0], v[1], v[2], vo)
            xs = [xs[0] + (v[0] + vo[0]) * h, xs[1] + (v[1] + vo[1]) * h, xs[2] + (v[2] + vo[2]) * h]
            age += h
            vd = vol_at(age) if vol_at is not None else (0.0, 0.0, 0.0)
            w = _fgo_fx_mv(K, xs)
            wv = _fgo_fx_mv(K, v)
            states.append((age, w[0], -w[1], w[2], wv[0] + vd[0], -wv[1] + vd[1], wv[2] + vd[2], 0.0))
            pi_ += 1
            h = pws[pi_] if pi_ < len(pws) else DT
        out = []
        j = 0
        n = int(life / _FGO_FX_CLAMP_DT) + 2
        for i in range(n + 1):
            a = i * _FGO_FX_CLAMP_DT
            while j < len(states) - 2 and states[j + 1][0] < a:
                j += 1
            s0, s1 = states[j], states[j + 1]
            f = (a - s0[0]) / (s1[0] - s0[0]) if s1[0] > s0[0] else 0.0
            f = min(1.0, max(0.0, f))
            out.append((s0[1] + (s1[1] - s0[1]) * f, s0[2] + (s1[2] - s0[2]) * f, s0[4] + (s1[4] - s0[4]) * f,
                        s0[5] + (s1[5] - s0[5]) * f, s0[6] + (s1[6] - s0[6]) * f, s0[3] + (s1[3] - s0[3]) * f))
        return out

    def _fgo_fx_clamp2_track(e, life, birth, vx, vy, vz, grav, size, vel_mix, fz, gu=1.0, dmix=None, wrot=None,
                             nz=None, ffr=None, smix=0.0, world=None, wind=None):
        """Limit Velocity integrated on Unity's frame grid (1/30 s), with
        dampen, drag, velocityOverLifetime and Force (e["clamp2"], converter
        _extra_fields). Per step of length h from age a (the first step is
        the partial one up to the next frame boundary, d = next frame end -
        birth, in (0, dt]; an on-grid birth takes d = dt -- the same state
        whichever frame Unity emits it in):
          v += (gravity + Force) h
          vol = velocityOverLifetime at a / life (animated velocity: never
                shrunk itself)
          tot = v + vol; lim = magnitude * curve(a / life)
          |tot| > lim: v -= tot (|tot| - lim) / |tot| * k, k = 1 - (1 - dampen)^(30 h)
          drag: |v| -= drag h [x pi (size/2)^2] [x |v|^2], not below 0
          x += (v + vol) h
        [Unity-verified 2026-09-26, Assets/Editor/Lane_modules/
        LaneModulesProbe.cs Clamp() -> probe_modules_clamp/, fitted by
        tools/fx_verify/lane_modules_clamp_fit.py: dampen 1 / 0.8 / 0.5 / 0.1
        / 0.02, a limit curve, drag x1 / x size / x velocity / both, drag +
        dampen, gravity, Force, constant and curve VOL -- positions and
        velocities within 2e-5 over 90 frames; rate-born particles' partial
        first step within 1e-5.]
        Returns the old clamp track's format: (x, y, vx, vy, vz, z) world
        units, screen-oriented (y down), every _FGO_FX_CLAMP_DT of age.
        nz (a particle's _FGOFXNoise): the noise velocity joins vol for the
        step -- Unity's Limit Velocity sees it exactly like VOL (c_lim_noise:
        3e-8, notes/noise.md) -- and the track is computed lazily
        (_FGOFXLazyTrack)."""
        c2 = e.get("clamp2") or {"m":1e30,"damp":1.,"drag":0.,"ds":False,"dv":False,"k":None}
        # (merge wave 1) Local simulation space under a uniform scale k: the
        # limit is on the simulation-space speed -> magnitude x k, as the old
        # clamp does [lane xform, LaneXformClamp.cs]. The q-space path pops
        # clamp_k (limit on |q'|, unscaled); drag + clamp_k is refused.
        c2m = c2["m"] * e.get("clamp_k", 1.0)
        DT = _FGO_FX_FRAME
        g = grav * _FGO_FX_GRAVITY
        if world is not None and e.get("scaling_mode") == 0:
            g *= gu
        cg_ = e.get("clamp_gu")
        if cg_:
            # Hierarchy scaling, unit prefab scale: the track runs in hierarchy
            # units; world gravity (and under World simulation the world-unit
            # limit) convert by gu = 288 / charaScale [Unity-verified
            # 2026-09-27, SG2HierClamp.cs, see the converter]
            g *= gu
            if cg_ == "gl":
                c2m *= gu
        dc_ = c2.get("drag_c")
        vols = [(i, a) for i, a in enumerate(e.get("vol", ())) if a]
        vm = e.get("vol_m")

        def vol_at(age):
            if not vols:
                return (0.0, 0.0, 0.0)
            u = min(1.0, age / life)
            li = [0.0, 0.0, 0.0]
            for i, a in vols:
                if a[0] == "const":
                    li[i] = a[1] + (a[2] - a[1]) * (vel_mix[i] if type(vel_mix) is tuple else vel_mix)
                else:
                    lo = _fgo_curve_at(a[3], u)
                    li[i] = a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * (vel_mix[i] if type(vel_mix) is tuple else vel_mix))
            w = _fgo_fx_mv(vm, li)
            if world is not None and e["world_step"].get("vol"):
                m_ = world(int(math.floor((birth + age) / DT + 1e-6)))[1]
                return _fgo_fx_mv(m_, (w[0], -w[1], w[2]))
            if wrot is not None:
                # world-space VOL on a rotating Local-space emitter: into the
                # particles' frame through the emitter's frame of this
                # step (anim_geo "wvol", converter; screen-oriented matrix)
                m_ = wrot(int(math.floor((birth + age) / DT + 1e-6)))
                w = (w[0], -w[1], w[2])
                w = _fgo_fx_mv(m_, w)
                return (w[0], w[1], w[2])
            return (w[0], -w[1], w[2])
        F = (fz[1], fz[2], fz[3]) if (fz is not None and fz[0] == "c") else (0.0, 0.0, 0.0)
        if fz is not None and fz[0] == "t" and ffr is not None:
            F = _fgo_fx_force_acc(e["force"], ffr, life)          # a Force curve: sampled per step
        if wind is not None:
            # WindZone: an extra per-step acceleration (the step's own start time)
            F0 = F
            F = (lambda age, F0=F0, wind=wind: tuple(((F0 if type(F0) is tuple else F0(age))[i] + wind(age)[i])
                                                    for i in range(3)))
        if world is not None:
            base_F, base_g = F, g
            def acceleration(age):
                gm, mm = world(int(math.floor((birth + age) / DT + 1e-6)))
                fa = base_F if type(base_F) is tuple else base_F(age)
                if not e["world_step"].get("force"):
                    gg = _fgo_fx_mv(gm, (0., base_g, 0.))
                    return tuple(fa[i] + gg[i] for i in range(3))
                return _fgo_fx_world_vectors(gm, mm, (0., base_g, 0.), fa, (0.,0.,0.))[0]
            F, g = acceleration, 0.
        if "clamp_K" in e:
            return _fgo_fx_clampK_track(e, c2, c2m, g, life, birth, vx, vy, vz, vol_at, F, dmix, size)
        q = birth / DT
        q -= math.floor(q)
        h = DT * (1.0 - q) if q > 1e-6 else DT
        # A prewarmed particle (birth < 0) is integrated in 0.03 s steps (the
        # maximum particle timestep) on a grid ending at t = 0, the first one
        # partial (age mod 0.03), then on the frame grid [Unity-verified
        # 2026-09-26, LaneModulesProbe.cs Prewarm(): plain / gravity / clamp
        # dampen 1 and 0.1 / clamp + VOL curves, every prewarmed particle
        # within 1e-5].
        pre = -birth if birth < -1e-9 else 0.0
        if pre > 0.0 and _fgo_fx_prewarm_unified:
            # Unity's prewarm schedule (_fgo_fx_pw_schedule): with Limit Velocity the
            # prewarm is STEPPED -- this per-step rule over coarse 0.2 / 1 s steps, then
            # 1/30 s (the game's Fixed / Maximum Particle Timestep) from the fine start
            # [Unity-verified 2026-10-01, PWSynRunner mod suite, B settings: dampen 1 /
            # 0.5, gravity + limit, constant VOL + limit, drag alone, P 2.3 / 7.3 / 12.3:
            # positions <= 1.6e-4, velocities <= 2e-5 (prewarm2/fit_mod.py, rt.py)].
            pre = _fgo_fx_pw_steps(_fgo_fx_max_life(e), birth)
            h = pre[0] if pre else DT
        elif pre > 0.0:
            h = math.fmod(pre, 0.03)
            if h < 1e-7:
                h = 0.03
        vo = vol_at(0.0)
        first = (0.0, 0.0, 0.0, 0.0, vx + vo[0], vy + vo[1], vz + vo[2], 0.0)
        # drag's units: g_u under Hierarchy + World simulation (world units), k for
        # a uniform K = k R (simulation units); see _fgo_fx_drag_step
        dsc = gu if cg_ == "gl" else e.get("clamp_k", 1.0)
        steps = _fgo_fx_clamp2_steps(c2, c2m, dc_, vol_at, F, g, life, pre, h, vx, vy, vz, size, dmix, nz, dsc,
                                    _fgo_fx_speed_at(e, life, smix) if "speedmod" in e else None, world is not None)
        if nz is not None or _fgo_fx_lazy_tracks:
            return _FGOFXLazyTrack(first, steps, life)
        states = [first]
        states.extend(steps)
        out = []
        j = 0
        n = int(life / _FGO_FX_CLAMP_DT) + 2
        for i in range(n + 1):
            a = i * _FGO_FX_CLAMP_DT
            while j < len(states) - 2 and states[j + 1][0] < a:
                j += 1
            s0, s1 = states[j], states[j + 1]
            f = (a - s0[0]) / (s1[0] - s0[0]) if s1[0] > s0[0] else 0.0
            f = min(1.0, max(0.0, f))
            out.append((s0[1] + (s1[1] - s0[1]) * f, s0[2] + (s1[2] - s0[2]) * f, s0[4] + (s1[4] - s0[4]) * f,
                        s0[5] + (s1[5] - s0[5]) * f, s0[6] + (s1[6] - s0[6]) * f, s0[3] + (s1[3] - s0[3]) * f))
        return out

    # (runtime_performance 2026-10-23) Performance only, the same numbers: True = every Limit
    # Velocity / world-step / wind track is resampled on demand (_FGOFXLazyTrack, the noise
    # tracks' path: the same states, the same resampling arithmetic) instead of for the whole life
    # when its chunk is built -- a burst of stepped particles no longer costs one frame seconds
    # (bit_talk_07), and the tracks a chunk builds and then replaces cost nothing. False = eager.
    _fgo_fx_lazy_tracks = True
    # (runtime_performance) True = a lazy track keeps only its 1/30 s steps and computes each
    # 1/240 s sample when asked (_FGOFXLazyTrack.sample: the same formula; a frame reads 2 of the
    # 8 samples it passes, and 8x fewer objects stay alive); False = every sample is stored.
    _fgo_fx_track_nostore = True
    # (runtime_performance) True = orbit / radial tracks are computed on demand too
    # (_FGOFXOrbitTrack, the eager resampling's formula): a prewarmed orbit chunk no longer
    # integrates every particle's whole life at its first frame (bit_talk_bkn_light: 27 s).
    _fgo_fx_lazy_orbit = True
    from bisect import bisect_left as _fgo_fx_bisect_left

    def _fgo_fx_drag_step(c2, dc_, age, life, size, dmix, h, vx, vy, vz, vo, dsc=1.0):
        """One step of Limit Velocity's drag: |tot| -= drag h, tot = v + vol
        (the TOTAL velocity: direction and, times velocity, |.|^2), the stored v
        left with tot' - vol. dsc = the units of the drag's own space in the
        track's: 1 in simulation units (Local simulation, identity or rotation),
        g_u under Hierarchy + World simulation (the drag is in world units), k for
        a uniform K = k R (simulation units). [Unity-verified 2026-09-29,
        CCSynRunner.cs -> probe_csclamp_syn/, csclamp_fit.py / csclamp_rt.py:
        drag const / curve / x velocity, with VOL, in 17 frames, 1.5e-6 ..
        4e-6; the |v|-only rule is 0.5 .. 1.3 off.] With dsc 1 and no VOL /
        noise this is bit-identical to the older |v| drag.
        The curve at the step's START age, one mix per particle
        [Unity-verified 2026-09-27, SG2Drag.cs]."""
        d = c2["drag"]
        if dc_:
            if dc_[0] == "r":
                d = dc_[1] + (dc_[2] - dc_[1]) * dmix
            else:
                u_ = min(1.0, age / life)
                d = _fgo_curve_at(dc_[2], u_)
                if dc_[0] == "cc":
                    lo_ = _fgo_curve_at(dc_[3], u_)
                    d = lo_ + (d - lo_) * dmix
                d *= dc_[1]
        if c2["ds"]:
            # x pi/4 S^2, S = the CURRENT size (a function of the age when
            # sizeOverLifetime is on) at the step's start, raw module units
            # [Unity-verified 2026-09-29, CCDragRunner.cs, csclamp_drag_fit.py]
            sz_ = size(age) if callable(size) else size
            d *= math.pi * 0.25 * sz_ * sz_
        tx, ty, tz = vx + vo[0], vy + vo[1], vz + vo[2]
        s2 = math.sqrt(tx * tx + ty * ty + tz * tz)
        if c2["dv"]:
            d *= (s2 / dsc) * (s2 / dsc)
        d *= dsc
        if s2 > 0.0:
            fac = max(0.0, s2 - d * h) / s2
            return (tx * fac - vo[0], ty * fac - vo[1], tz * fac - vo[2])
        return (vx, vy, vz)

    def _fgo_fx_limit_at(c2, magnitude, age, life):
        """Unity-verified 2026-10-02 (CRSynRunner, clamprand/fit.py).
        Random magnitude uses one independent particle mix; TwoCurves
        sample BOTH curves at the step-start age. magnitude carries the
        existing simulation/draw unit scale, including clamp_k / clamp_gu.
        Older nonrandom specs take exactly their previous arithmetic path.
        """
        c = c2.get("limit_c")
        if c is None:
            return magnitude * (_fgo_curve_at(c2["k"], min(1.0, age / life)) if c2["k"] else 1.0)
        mix = c2.get("limit_mix", 0.0)
        scale = magnitude / c2["m"] if c2["m"] else c2.get("limit_scale", 1.0)
        if c[0] == "r":
            return (c[1] + (c[2] - c[1]) * mix) * scale
        u = min(1.0, age / life)
        lo = _fgo_curve_at(c[3], u)
        return c[1] * (lo + (_fgo_curve_at(c[2], u) - lo) * mix) * scale

    def _fgo_fx_clamp2_steps(c2, c2m, dc_, vol_at, F, g, life, pre, h, vx, vy, vz, size, dmix, nz, dsc=1.0, speed_at=None, world_vel=False):
        """_fgo_fx_clamp2_track's frame-grid integration, one yielded state
        (age, x, y, z, vx, vy, vz, 0) per step (split out of the track
        function for the lazy noise tracks; the same operations in the same
        order as before)."""
        DT = _FGO_FX_FRAME
        x = y = z = 0.0
        age = 0.0
        pi_ = 0
        nv = (0.0, 0.0, 0.0)
        Fk = F if type(F) is tuple else None
        while age < life + DT:
            Fa = F if Fk is not None else F(age)
            vx += Fa[0] * h
            vy += (g + Fa[1]) * h
            vz += Fa[2] * h
            vo = vol_at(age)
            if nz is not None:
                # noise = animated velocity like VOL, at the step's start
                # position (_FGOFXNoise.clamp_vel)
                nv = nz.clamp_vel(age, x, y, z)
                vo = (vo[0] + nv[0], vo[1] + nv[1], vo[2] + nv[2])
            tx, ty, tz = vx + vo[0], vy + vo[1], vz + vo[2]
            lim = _fgo_fx_limit_at(c2, c2m, age, life)
            sp = math.sqrt(tx * tx + ty * ty + tz * tz)
            if sp > lim and sp > 0.0:
                f = (sp - max(lim, 0.0)) / sp * (1.0 - (1.0 - c2["damp"]) ** (_FGO_FX_DAMPEN_HZ * h))
                vx -= tx * f
                vy -= ty * f
                vz -= tz * f
            if c2["drag"] or dc_:
                vx, vy, vz = _fgo_fx_drag_step(c2, dc_, age, life, size, dmix, h, vx, vy, vz, vo, dsc)
            sm = speed_at(age) if speed_at is not None else 1.0
            x += (vx + vo[0]) * h * sm
            y += (vy + vo[1]) * h * sm
            z += (vz + vo[2]) * h * sm
            age += h
            # Newly verified moving-frame path: animated velocity belongs to
            # the step just completed, not the following transform sample.
            vn = vo if world_vel else vol_at(age)
            if nz is not None:
                vn = (vn[0] + nv[0], vn[1] + nv[1], vn[2] + nv[2])
            yield (age, x, y, z, (vx + vn[0]) * sm, (vy + vn[1]) * sm, (vz + vn[2]) * sm, 0.0)
            if type(pre) is list:
                # unified prewarm: the remaining prewarm steps, then the frame grid
                pi_ += 1
                h = pre[pi_] if pi_ < len(pre) else DT
            else:
                h = min(0.03, pre - age) if age < pre - 1e-7 else DT

    # --- ForceOverLifetime (e["force"], converter _force_fields) -------------
    # Unity integrates per frame: v += F dt, then x += v dt; a particle
    # born inside a frame first takes a partial step d = next frame end - birth,
    # d in (0, dt]; F is sampled at the particle's age at the START of each
    # step. Constant F: x = F/2 (age^2 + d^2 + (age - d) dt), v = F age -- exact
    # at every frame instant (and the same whichever frame a boundary birth
    # is given). TwoConstants / TwoCurves: one fraction per particle per axis.
    # [Unity-verified 2026-09-26, ProbeSmallGates.cs force_*: errors ~1e-7.]
    # Frames are counted from the activation (emitter-local time 0), as the
    # rate-curve integration does. Between frame instants the runtime
    # interpolates (Unity has no state there).
    _fgo_fx_pw_ks_cache = {}

    def _fgo_fx_pw_k_stepped(P, birth, age):
        """_fgo_fx_pw_k for a NON-procedural prewarm (_fgo_fx_pw_nonproc): a constant
        acceleration is integrated semi-implicitly over the prewarm steps (x = a sum h_i
        c_i, c_i the age at the end of step i), then the frame rule from t = 0 [Unity-
        verified 2026-10-01, PWSynRunner mod "gl": gravity in a stepped prewarm fits the
        semi-implicit steps (<= 1.6e-4), the exact formula is 0.1-12 off]."""
        key = (P, birth)
        K0 = _fgo_fx_pw_ks_cache.get(key)
        if K0 is None:
            if len(_fgo_fx_pw_ks_cache) > 20000:
                _fgo_fx_pw_ks_cache.clear()
            c = K0 = 0.0
            for h in _fgo_fx_pw_steps(P, birth):
                c += h
                K0 += h * c
            _fgo_fx_pw_ks_cache[key] = K0
        p = -birth
        s_ = age - p
        if s_ <= 0.0:
            return 0.5 * age * age
        return K0 + p * s_ + 0.5 * (s_ * s_ + s_ * _FGO_FX_FRAME)

    def _fgo_fx_pw_k(p, age):
        """Displacement factor k (x = a k) of a constant acceleration for a
        prewarmed particle born p s before t = 0, at `age` (>= p when drawn):
        exact over the prewarm part, then the frame rule from t = 0 (an
        on-grid start: 0.5 (s^2 + s dt), s = age - p) -- see
        _fgo_fx_prewarm_exact_accel."""
        s_ = age - p
        if s_ <= 0.0:
            return 0.5 * age * age
        return 0.5 * p * p + p * s_ + 0.5 * (s_ * s_ + s_ * _FGO_FX_FRAME)

    def _fgo_fx_sizespeed_u(age, birth, life):
        """VOL clock of the last completed frame; SSSyntheticRunner,
        velcurve_m1 (2026-09-30), not the next frame's velocity."""
        start = (math.floor((birth + age) / _FGO_FX_FRAME + 1e-5) - 1) * _FGO_FX_FRAME
        return max(0.0, min(1.0, (start - birth) / life))

    def _fgo_fx_sizespeed(e, vx, vy, vz, mix=0.0, gu=1.0):
        """Unity-verified 2026-09-30, SSSyntheticRunner / sizespeed_fit:
        total velocity in simulation units, multiply start size and SOL;
        the draw applies size_k / maxParticleSize afterwards."""
        ss = e["size_speed"]
        v = (vx, -vy, vz)       # draw velocity is screen-oriented
        if "Ki" in ss:
            v = _fgo_fx_mv(ss["Ki"], v)
        if ss.get("gu"):
            v = tuple(x / gu for x in v)
        speed = math.sqrt(sum(x * x for x in v))
        lo, hi = ss["range"]
        t = min(1.0, max(0.0, (speed - lo) / (hi - lo)))
        return _fgo_fx_size_axis(ss["axis"], t, mix)

    def _fgo_fx_size_at(e, size, size_mix, life):
        """A particle's current size as a function of its age, for Limit
        Velocity's drag x size: startSize x the sizeOverLifetime curve at
        min(1, age / life) (a TwoCurves pair mixed by the particle's size mix),
        the way the draw evaluates it."""
        sc, sm = e["size_curve"], e.get("size_curve_min")

        def at(age):
            u = min(1.0, age / life)
            hi = _fgo_curve_at(sc[1], u)
            if sm:
                lo = _fgo_curve_at(sm, u)
                hi = lo + (hi - lo) * size_mix
            return size * sc[0] * hi
        return at

    def _fgo_fx_force_acc(fo, fr, life):
        """A Force whose axes are curves as an acceleration function of the
        particle's age (world units / s^2, screen-oriented: y down): the axes at
        normalised age min(1, age / life), mixed by the particle's fractions fr,
        through fo["m"]. Inside a Limit Velocity track it is sampled at each
        step's START age (Force acts on the stored velocity like gravity:
        v += (g + F) h) [Unity-verified 2026-09-29, CCSynRunner.cs ->
        probe_csclamp_syn/, csclamp_fit.py / csclamp_rt.py: curve and TwoCurves
        Force, local and world space, 17 frames, 1e-6]."""
        ax, M = fo["ax"], fo["m"]

        def acc(age):
            u = min(1.0, age / life)
            loc = []
            for i, a in enumerate(ax):
                if a is None:
                    loc.append(0.0)
                elif a[0] == "const":
                    loc.append(a[1] + (a[2] - a[1]) * fr[i])
                else:
                    lo = _fgo_curve_at(a[3], u)
                    loc.append(a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * fr[i]))
            w = _fgo_fx_mv(M, loc)
            return (w[0], -w[1], w[2])
        return acc

    def _fgo_fx_force_particle(fo, birth, life, rng, fr=None, np_P=None):
        """Per-particle force state: ("c", ax, ay, az, d) (constant world
        acceleration, screen-oriented: y down) or ("t", table, d) with the
        (x, y, z, vx, vy, vz) state at ages 0, d, d + dt, ... (curves). fr =
        the particle's three fractions when the caller drew them."""
        DT = _FGO_FX_FRAME
        if fr is None:
            fr = (rng.random(), rng.random(), rng.random())
        q = birth / DT
        q -= math.floor(q)
        d = DT * (1.0 - q) if q > 1e-6 else DT
        ax, M = fo["ax"], fo["m"]
        if all(a is None or a[0] == "const" for a in ax):
            loc = [a[1] + (a[2] - a[1]) * fr[i] if a is not None else 0.0 for i, a in enumerate(ax)]
            w = _fgo_fx_mv(M, loc)
            if birth < -1e-9 and np_P is not None:
                d = ("s", np_P, birth)   # non-procedural prewarm: stepped (_fgo_fx_pw_k_stepped)
            elif birth < -1e-9 and _fgo_fx_prewarm_exact_accel:
                d = birth        # < 0: prewarmed, exact over -birth s (_fgo_fx_pw_k)
            return ("c", w[0], -w[1], w[2], d)
        table = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)]
        x = y = z = vx = vy = vz = 0.0
        age, h = 0.0, d

        def acc_at(age):
            u = min(1.0, age / life)
            loc = []
            for i, a in enumerate(ax):
                if a is None:
                    loc.append(0.0)
                elif a[0] == "const":
                    loc.append(a[1] + (a[2] - a[1]) * fr[i])
                else:
                    lo = _fgo_curve_at(a[3], u)
                    loc.append(a[1] * (lo + (_fgo_curve_at(a[2], u) - lo) * fr[i]))
            return _fgo_fx_mv(M, loc)
        if birth < -1e-9 and _fgo_fx_prewarm_unified:
            # a prewarmed particle without Limit Velocity: the prewarm is ANALYTIC --
            # the Force curve's exact double integral over its prewarm part, then the
            # frame rule from t = 0 [Unity-verified 2026-10-01, PWSynRunner mod "ff",
            # Force y = 2 age / life, P 2.3 / 7.3 / 12.3, A and B: <= 1.1e-4]
            p_ = -birth
            n_ = max(8, int(math.ceil(p_ / 0.004)))
            hs = p_ / n_
            for k_ in range(n_):
                a0_ = acc_at(k_ * hs)
                am_ = acc_at((k_ + 0.5) * hs)
                a1_ = acc_at((k_ + 1) * hs)
                # Simpson: velocity gain and the exact displacement of a quadratic acceleration
                dvx = (a0_[0] + 4 * am_[0] + a1_[0]) * hs / 6.0
                dvy = (a0_[1] + 4 * am_[1] + a1_[1]) * hs / 6.0
                dvz = (a0_[2] + 4 * am_[2] + a1_[2]) * hs / 6.0
                x += vx * hs + (a0_[0] + 2 * am_[0]) * hs * hs / 6.0
                y -= -vy * hs + (a0_[1] + 2 * am_[1]) * hs * hs / 6.0
                z += vz * hs + (a0_[2] + 2 * am_[2]) * hs * hs / 6.0
                vx += dvx
                vy -= dvy
                vz += dvz
            table.append((x, y, z, vx, vy, vz))
            age, h, d = p_, DT, p_
        while age < life + DT:
            w = acc_at(age)
            vx += w[0] * h
            vy -= w[1] * h
            vz += w[2] * h
            x += vx * h
            y += vy * h
            z += vz * h
            table.append((x, y, z, vx, vy, vz))
            age += h
            h = DT
        return ("t", table, d)

    def _fgo_fx_force_at(fz, age):
        """(dx, dy, dz, vx, vy, vz) of a particle's force state at `age`
        (world units, screen-oriented)."""
        d = fz[-1]
        if fz[0] == "c":
            _, ax, ay, az, d = fz
            if type(d) is tuple:
                k = _fgo_fx_pw_k_stepped(d[1], d[2], age)
            elif d < 0.0:
                k = _fgo_fx_pw_k(-d, age)
            else:
                k = age * age if age < d else 0.5 * (age * age + d * d + (age - d) * _FGO_FX_FRAME)
            return (ax * k, ay * k, az * k, ax * age, ay * age, az * age)
        tab = fz[1]
        if age < d:
            i, f = 0, age / d
        else:
            fi = (age - d) / _FGO_FX_FRAME
            i = 1 + int(fi)
            f = fi - int(fi)
        if i >= len(tab) - 1:
            return tab[-1]
        a0, a1 = tab[i], tab[i + 1]
        return tuple(a + (b - a) * f for a, b in zip(a0, a1))

    # --- Noise (e["noise"], converter _noise_fields) -- APPROXIMATED ----------
    # [approximated, statistically matched 2026-09-28, Assets/ProbeRuntime/
    # Noise/NZFieldRunner.cs -> probe_noise_field/; amplitudes fitted by
    # tools/fx_verify/nz_fit.py, statistics checked by tools/fx_verify/
    # noise_rt.py; notes/noise.md]
    # Unity's rules (measured exactly, play mode): the noise is a VELOCITY
    # added like velocityOverLifetime (x += (v + vol + noise) dt per frame
    # step, sampled at the step's START position; Limit Velocity sees it,
    # _fgo_fx_clamp2_track), a function of the SIMULATION-space position only,
    # in simulation-space units whatever the scaling mode; linear in strength
    # x positionAmount (separateAxes per axis, TwoConstants one draw per
    # particle); damping on = divided by the frequency; octaves
    # sum m^o s^o curl(s^o f x) / sum m^o; scroll slides the noise potentials
    # at scrollSpeed units / s. OURS (the approximation): the field itself --
    # the curl of Perlin-type gradient noise on a unit lattice in noise units
    # f x, High = three 3-D potentials (potential i slides along axis i),
    # Medium = 2-D potentials psi_x(y, z), psi_y(z, x), psi_z(x, y), Low =
    # three 1-D noises v = (g(y), g(z), g(x)) -- the structures Unity's field
    # measures like -- with our own gradients (periodic lattices 16^3, 32^2,
    # 256) and amplitudes fitted to Unity's RMS (_FGO_FX_NZ_AMP). So the
    # amount, smoothness, correlation length / time and isotropy match
    # Unity's; particle paths never do (the game's own seed changes per play).
    _FGO_FX_NZ_AMP = (3.5414, 1.4035, 1.4302)   # Low, Medium, High (nz_fit.py amp)

    def _fgo_fx_nz_tables():
        """Gradient lattices: 16^3 unit vectors, 32^2 unit vectors, 256
        slopes in [-1, 1], from a fixed stream (random() only: stable across
        Python versions)."""
        r = random.Random(20260928)
        g3 = []
        for _ in range(16 * 16 * 16):
            while True:
                x, y, z = r.random() * 2.0 - 1.0, r.random() * 2.0 - 1.0, r.random() * 2.0 - 1.0
                d = x * x + y * y + z * z
                if 1e-4 < d <= 1.0:
                    break
            d = math.sqrt(d)
            g3 += [x / d, y / d, z / d]
        g2 = []
        for _ in range(32 * 32):
            a = r.random() * 2.0 * math.pi
            g2 += [math.cos(a), math.sin(a)]
        g1 = [r.random() * 2.0 - 1.0 for _ in range(256)]
        return g3, g2, g1

    _fgo_fx_nz_G3, _fgo_fx_nz_G2, _fgo_fx_nz_G1 = _fgo_fx_nz_tables()

    def _fgo_fx_nz_g3(x, y, z):
        """Gradient of the 3-D gradient noise at (x, y, z): corner dot
        products interpolated with the quintic fade 6t^5 - 15t^4 + 10t^3,
        period 16."""
        G = _fgo_fx_nz_G3
        fx, fy, fz = math.floor(x), math.floor(y), math.floor(z)
        tx, ty, tz = x - fx, y - fy, z - fz
        ix, iy, iz = int(fx) & 15, int(fy) & 15, int(fz) & 15
        jx, jy, jz = (ix + 1) & 15, (iy + 1) & 15, (iz + 1) & 15
        a0, a1, b0, b1 = ix * 256 + iy * 16, ix * 256 + jy * 16, jx * 256 + iy * 16, jx * 256 + jy * 16
        i = (a0 + iz) * 3
        g000x, g000y, g000z = G[i], G[i + 1], G[i + 2]
        i = (b0 + iz) * 3
        g100x, g100y, g100z = G[i], G[i + 1], G[i + 2]
        i = (a1 + iz) * 3
        g010x, g010y, g010z = G[i], G[i + 1], G[i + 2]
        i = (b1 + iz) * 3
        g110x, g110y, g110z = G[i], G[i + 1], G[i + 2]
        i = (a0 + jz) * 3
        g001x, g001y, g001z = G[i], G[i + 1], G[i + 2]
        i = (b0 + jz) * 3
        g101x, g101y, g101z = G[i], G[i + 1], G[i + 2]
        i = (a1 + jz) * 3
        g011x, g011y, g011z = G[i], G[i + 1], G[i + 2]
        i = (b1 + jz) * 3
        g111x, g111y, g111z = G[i], G[i + 1], G[i + 2]
        sx, sy, sz = tx - 1.0, ty - 1.0, tz - 1.0
        n000 = g000x * tx + g000y * ty + g000z * tz
        n100 = g100x * sx + g100y * ty + g100z * tz
        n010 = g010x * tx + g010y * sy + g010z * tz
        n110 = g110x * sx + g110y * sy + g110z * tz
        n001 = g001x * tx + g001y * ty + g001z * sz
        n101 = g101x * sx + g101y * ty + g101z * sz
        n011 = g011x * tx + g011y * sy + g011z * sz
        n111 = g111x * sx + g111y * sy + g111z * sz
        u = tx * tx * tx * (tx * (tx * 6.0 - 15.0) + 10.0)
        v = ty * ty * ty * (ty * (ty * 6.0 - 15.0) + 10.0)
        w = tz * tz * tz * (tz * (tz * 6.0 - 15.0) + 10.0)
        du = 30.0 * tx * tx * (tx * (tx - 2.0) + 1.0)
        dv = 30.0 * ty * ty * (ty * (ty - 2.0) + 1.0)
        dw = 30.0 * tz * tz * (tz * (tz - 2.0) + 1.0)
        k1, k2, k3 = n100 - n000, n010 - n000, n001 - n000
        k4 = n000 - n100 - n010 + n110
        k5 = n000 - n010 - n001 + n011
        k6 = n000 - n100 - n001 + n101
        k7 = -n000 + n100 + n010 - n110 + n001 - n101 - n011 + n111
        uv, vw, wu = u * v, v * w, w * u
        uvw = uv * w
        gx = (g000x + u * (g100x - g000x) + v * (g010x - g000x) + w * (g001x - g000x)
              + uv * (g000x - g100x - g010x + g110x) + vw * (g000x - g010x - g001x + g011x)
              + wu * (g000x - g100x - g001x + g101x)
              + uvw * (-g000x + g100x + g010x - g110x + g001x - g101x - g011x + g111x))
        gy = (g000y + u * (g100y - g000y) + v * (g010y - g000y) + w * (g001y - g000y)
              + uv * (g000y - g100y - g010y + g110y) + vw * (g000y - g010y - g001y + g011y)
              + wu * (g000y - g100y - g001y + g101y)
              + uvw * (-g000y + g100y + g010y - g110y + g001y - g101y - g011y + g111y))
        gz = (g000z + u * (g100z - g000z) + v * (g010z - g000z) + w * (g001z - g000z)
              + uv * (g000z - g100z - g010z + g110z) + vw * (g000z - g010z - g001z + g011z)
              + wu * (g000z - g100z - g001z + g101z)
              + uvw * (-g000z + g100z + g010z - g110z + g001z - g101z - g011z + g111z))
        return (gx + du * (k1 + k4 * v + k6 * w + k7 * vw),
                gy + dv * (k2 + k5 * w + k4 * u + k7 * wu),
                gz + dw * (k3 + k6 * u + k5 * v + k7 * uv))

    def _fgo_fx_nz_g2(x, y):
        """Gradient of the 2-D gradient noise (quintic fade, period 32)."""
        G = _fgo_fx_nz_G2
        fx, fy = math.floor(x), math.floor(y)
        tx, ty = x - fx, y - fy
        ix, iy = int(fx) & 31, int(fy) & 31
        jx, jy = (ix + 1) & 31, (iy + 1) & 31
        i = (ix * 32 + iy) * 2
        g00x, g00y = G[i], G[i + 1]
        i = (jx * 32 + iy) * 2
        g10x, g10y = G[i], G[i + 1]
        i = (ix * 32 + jy) * 2
        g01x, g01y = G[i], G[i + 1]
        i = (jx * 32 + jy) * 2
        g11x, g11y = G[i], G[i + 1]
        sx, sy = tx - 1.0, ty - 1.0
        n00 = g00x * tx + g00y * ty
        n10 = g10x * sx + g10y * ty
        n01 = g01x * tx + g01y * sy
        n11 = g11x * sx + g11y * sy
        u = tx * tx * tx * (tx * (tx * 6.0 - 15.0) + 10.0)
        v = ty * ty * ty * (ty * (ty * 6.0 - 15.0) + 10.0)
        du = 30.0 * tx * tx * (tx * (tx - 2.0) + 1.0)
        dv = 30.0 * ty * ty * (ty * (ty - 2.0) + 1.0)
        k1, k2, k3 = n10 - n00, n01 - n00, n00 - n10 - n01 + n11
        uv = u * v
        gx = g00x + u * (g10x - g00x) + v * (g01x - g00x) + uv * (g00x - g10x - g01x + g11x)
        gy = g00y + u * (g10y - g00y) + v * (g01y - g00y) + uv * (g00y - g10y - g01y + g11y)
        return (gx + du * (k1 + k3 * v), gy + dv * (k2 + k3 * u))

    def _fgo_fx_nz_g1(x):
        """Derivative of the 1-D gradient noise (quintic fade, period 256)."""
        G = _fgo_fx_nz_G1
        fx = math.floor(x)
        t = x - fx
        i = int(fx) & 255
        g0, g1 = G[i], G[(i + 1) & 255]
        u = t * t * t * (t * (t * 6.0 - 15.0) + 10.0)
        du = 30.0 * t * t * (t * (t - 2.0) + 1.0)
        return g0 + u * (g1 - g0) + du * (g1 * (t - 1.0) - g0 * t)

    def _fgo_fx_nz_curl(q, x, y, z, w, o):
        """The unit-frequency field (before _FGO_FX_NZ_AMP) at noise-space
        (x, y, z), scroll shift w, system offset o. q = quality 0 / 1 / 2."""
        x, y, z = x + o[0], y + o[1], z + o[2]
        if q == 2:
            # psi_i = G3(p + O_i + w e_i): potential i slides along axis i
            # (the only one of the 27 assignments whose temporal correlation
            # matches Unity's, max error 0.007: nz_fit.py)
            ax_, ay_, az_ = _fgo_fx_nz_g3(x + w, y, z)
            bx_, by_, bz_ = _fgo_fx_nz_g3(x + 5.0, y + 11.0 + w, z + 3.0)
            cx_, cy_, cz_ = _fgo_fx_nz_g3(x + 13.0, y + 7.0, z + 9.0 + w)
            return (cy_ - bz_, az_ - cx_, bx_ - ay_)
        if q == 1:
            a1, a2 = _fgo_fx_nz_g2(y, z + w)                  # psi_x(y, z)
            b1, b2 = _fgo_fx_nz_g2(z + 3.0, x + 5.0 + w)      # psi_y(z, x)
            c1, c2 = _fgo_fx_nz_g2(x + 13.0, y + 7.0 + w)     # psi_z(x, y)
            return (c2 - b1, a2 - c1, b2 - a1)
        return (_fgo_fx_nz_g1(y + 7.0 + w), _fgo_fx_nz_g1(z + w), _fgo_fx_nz_g1(x + 5.0 + w))

    def _fgo_fx_nz_vel(nz, o, x, y, z, t):
        """Simulation-space noise velocity per unit strength at simulation-
        space (x, y, z), emitter time t: the octave sum, amplitude, damping."""
        f = nz["f"]
        q = nz["q"]
        n_, m_, sc_ = nz["oct"]
        scroll = nz["sc"]
        tx = ty = tz = 0.0
        wsum = 0.0
        w_ = s_ = 1.0
        for _ in range(n_):
            k = f * s_
            vx, vy, vz = _fgo_fx_nz_curl(q, x * k, y * k, z * k, scroll * t * k, o)
            tx += w_ * s_ * vx
            ty += w_ * s_ * vy
            tz += w_ * s_ * vz
            wsum += w_
            w_ *= m_
            s_ *= sc_
        a = _FGO_FX_NZ_AMP[q] / wsum
        if not nz["damp"]:
            a *= f
        return (tx * a, ty * a, tz * a)

    _fgo_fx_nz_inv = {}

    def _fgo_fx_nz_Ki(K):
        """K^-1 (rows) of the simulation frame K (sim -> draw units, y up)."""
        K = tuple(tuple(r) for r in K)
        got = _fgo_fx_nz_inv.get(K)
        if got is None:
            got = _fgo_fx_inv3(K)
            _fgo_fx_nz_inv[K] = got
        return got

    class _FGOFXNoise(_FGO_FX_OBJECT):
        """One particle's noise: the simulation-space displacement D it adds,
        integrated lazily per frame step as frames are drawn (a mid-frame
        birth first takes the partial step d; a prewarmed particle is
        stepped on the same grid -- a further approximation). The field is
        sampled at q = K^-1 base(age) + D, base = spawn + start velocity x age
        + (gravity + constant Force) age^2 / 2 (velocityOverLifetime curves
        aren't part of the SAMPLING position). The draw adds K D (screen-
        oriented) to the displacement and K n to the velocity."""

        def __init__(self, nz, o, birth, life, p0, v0, a0, S):
            self.nz, self.o, self.birth, self.life = nz, o, birth, life
            self.p0, self.v0, self.a0, self.S = p0, v0, a0, S
            self.K = nz["K"]
            self.Ki = nz["Ki"] if "Ki" in nz else _fgo_fx_nz_Ki(nz["K"])
            q = birth / _FGO_FX_FRAME
            q -= math.floor(q)
            self.d = _FGO_FX_FRAME * (1.0 - q) if q > 1e-6 else _FGO_FX_FRAME
            self.ages = [0.0]
            self.D = [(0.0, 0.0, 0.0)]
            self.V = []

        def to_sim(self, x, y, z):
            """A draw-unit position (y up) in simulation space: K^-1 p."""
            Ki = self.Ki
            qx = Ki[0][0] * x + Ki[0][1] * y + Ki[0][2] * z
            qy = Ki[1][0] * x + Ki[1][1] * y + Ki[1][2] * z
            qz = Ki[2][0] * x + Ki[2][1] * y + Ki[2][2] * z
            return qx, qy, qz

        pws = None

        def _step(self):
            a = self.ages[-1]
            n_ = len(self.ages) - 1
            if self.pws:
                h = self.pws[n_] if n_ < len(self.pws) else _FGO_FX_FRAME
            else:
                h = self.d if n_ == 0 else _FGO_FX_FRAME
            p0, v0, a0 = self.p0, self.v0, self.a0
            k_ = 0.5 * a * a
            qx, qy, qz = self.to_sim(p0[0] + v0[0] * a + a0[0] * k_, p0[1] + v0[1] * a + a0[1] * k_,
                                     p0[2] + v0[2] * a + a0[2] * k_)
            Dx, Dy, Dz = self.D[-1]
            n = _fgo_fx_nz_vel(self.nz, self.o, qx + Dx, qy + Dy, qz + Dz, self.birth + a)
            S = self.S
            n = (n[0] * S[0], n[1] * S[1], n[2] * S[2])
            self.V.append(n)
            self.D.append((Dx + n[0] * h, Dy + n[1] * h, Dz + n[2] * h))
            self.ages.append(a + h)

        def at(self, age):
            """(dx, dy, dz, vx, vy, vz) the noise adds at `age`: draw units,
            screen-oriented (y down)."""
            ages = self.ages
            while ages[-1] <= age and ages[-1] < self.life + _FGO_FX_FRAME:
                self._step()
            if len(ages) < 2:
                return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            if age >= ages[-1]:
                i = len(ages) - 2
            elif age < self.d:
                i = 0
            else:
                i = min(len(ages) - 2, 1 + int((age - self.d) / _FGO_FX_FRAME))
                while i > 0 and ages[i] > age:
                    i -= 1
                while i < len(ages) - 2 and ages[i + 1] <= age:
                    i += 1
            n = self.V[i]
            s = min(age, ages[i + 1]) - ages[i]
            D = self.D[i]
            Dx, Dy, Dz = D[0] + n[0] * s, D[1] + n[1] * s, D[2] + n[2] * s
            K = self.K
            return (K[0][0] * Dx + K[0][1] * Dy + K[0][2] * Dz,
                    -(K[1][0] * Dx + K[1][1] * Dy + K[1][2] * Dz),
                    K[2][0] * Dx + K[2][1] * Dy + K[2][2] * Dz,
                    K[0][0] * n[0] + K[0][1] * n[1] + K[0][2] * n[2],
                    -(K[1][0] * n[0] + K[1][1] * n[1] + K[1][2] * n[2]),
                    K[2][0] * n[0] + K[2][1] * n[1] + K[2][2] * n[2])

        def clamp_vel(self, age, x, y, z):
            """Inside the Limit Velocity track: the noise velocity (draw
            units, screen-oriented) for the step starting at `age`, the
            particle displaced by (x, y, z) (screen-oriented) from its spawn;
            that displacement already contains the noise."""
            p0 = self.p0
            qx, qy, qz = self.to_sim(p0[0] + x, p0[1] - y, p0[2] + z)
            n = _fgo_fx_nz_vel(self.nz, self.o, qx, qy, qz, self.birth + age)
            S = self.S
            n = (n[0] * S[0], n[1] * S[1], n[2] * S[2])
            K = self.K
            return (K[0][0] * n[0] + K[0][1] * n[1] + K[0][2] * n[2],
                    -(K[1][0] * n[0] + K[1][1] * n[1] + K[1][2] * n[2]),
                    K[2][0] * n[0] + K[2][1] * n[1] + K[2][2] * n[2])

    def _fgo_fx_nz_particle(name, ei, e, wi, rng_nz, t, life, ox, oy, oz, vx, vy, vz, grav, fz, bxy=None):
        """A particle's _FGOFXNoise. Draw units are those of the runtime's
        displacement (world units; prefab px under Hierarchy scaling); the
        spawn offset (shape units) converts by nz["of"] and Hierarchy
        gravity by 288 -- both at charaScale 1 (they only move where the
        field is sampled). The TwoConstants strength draw comes from its own
        stream, so nothing else about the particle changes."""
        nz = e["noise"]
        okey = (name, ei, wi)
        o = _fgo_fx_nz_offsets.get(okey)
        if o is None:
            r_ = random.Random(zlib.crc32(("%s|%d|%s|nz" % okey).encode("utf-8")) & 0xffffffff)
            o = (r_.random() * 256.0, r_.random() * 256.0, r_.random() * 256.0)
            _fgo_fx_nz_offsets[okey] = o
        s = nz["s"]
        if s[0] == "r":
            m = s[1] + (s[2] - s[1]) * rng_nz.random()
            S = (m, m, m)
        else:
            S = (s[1], s[2], s[3])
        of = nz.get("of", 1.0)
        gu = 288.0 if e.get("scaling_mode") == 0 else 1.0
        a0 = [0.0, -grav * _FGO_FX_GRAVITY * gu, 0.0]
        if fz is not None and fz[0] == "c":
            a0 = [a0[0] + fz[1], a0[1] - fz[2], a0[2] + fz[3]]
        p0 = (ox * of, -oy * of, oz * of)
        if bxy is not None:
            # a moving World-space emitter: the particle's birth origin
            # (prefab px, y up) in world units, at charaScale 1
            p0 = (p0[0] + bxy[0] / 288.0, p0[1] + bxy[1] / 288.0, p0[2] + bxy[2] / 288.0)
        nzp = _FGOFXNoise(nz, o, t, life, p0, (vx, -vy, vz), a0, S)
        if t < -1e-9 and _fgo_fx_prewarm_unified and e.get("prewarm"):
            # a prewarmed particle steps on Unity's prewarm schedule (_fgo_fx_pw_steps)
            # before the frame grid (the noise itself stays approximated)
            nzp.pws = _fgo_fx_pw_steps(_fgo_fx_max_life(e), t)
            if nzp.pws:
                nzp.d = nzp.pws[0]
        return nzp

    _fgo_fx_nz_offsets = {}

    class _FGOFXOrbitTrack(_FGO_FX_OBJECT):
        """_fgo_fx_orbit_track's track computed on demand from its step generator: the steps
        are stored, each 1/240 s sample is computed when asked with the eager resampling's
        formula (velocity = the step's chord; j as in _FGOFXLazyTrack.sample)."""

        def __init__(self, first, gen, life, p0):
            self.states = [first]
            self.times = [first[0]]
            self.gen = gen
            self.done = False
            self.n = int(life / _FGO_FX_CLAMP_DT) + 3
            self.p0 = p0

        def __len__(self):
            return self.n

        def __getitem__(self, i):
            if i < 0:
                i += self.n
            a = i * _FGO_FX_CLAMP_DT
            states, times = self.states, self.times
            if not self.done and (times[-1] < a or len(states) < 2):
                gen = self.gen
                try:
                    while times[-1] < a or len(states) < 2:
                        s_ = next(gen)
                        states.append(s_)
                        times.append(s_[0])
                except StopIteration:
                    self.done = True
            j = _fgo_fx_bisect_left(times, a) - 1
            if j > len(states) - 2:
                j = len(states) - 2
            if j < 0:
                j = 0
            s0, s1 = states[j], states[j + 1]
            dt_ = s1[0] - s0[0]
            f = min(1.0, max(0.0, (a - s0[0]) / dt_)) if dt_ > 0 else 0.0
            x = s0[1] + (s1[1] - s0[1]) * f
            y = s0[2] + (s1[2] - s0[2]) * f
            z = s0[3] + (s1[3] - s0[3]) * f
            p0 = self.p0
            if dt_ > 0:
                return (x - p0[0], -y - p0[1], (s1[1] - s0[1]) / dt_, -((s1[2] - s0[2]) / dt_), (s1[3] - s0[3]) / dt_, z - p0[2])
            return (x - p0[0], -y - p0[1], 0.0, -0.0, 0.0, z - p0[2])

    class _FGOFXLazyTrack(_FGO_FX_OBJECT):
        """A clamp track (the (x, y, vx, vy, vz, z) list every
        _FGO_FX_CLAMP_DT of age) computed on demand from a step generator --
        the same resampling as _fgo_fx_clamp2_track's (noise particles: their
        steps sample the noise field, too costly to run a whole life at
        once)."""

        def __init__(self, first, gen, life):
            self.states = [first]
            self.times = [first[0]]
            self.gen = gen
            self.done = False
            self.n = int(life / _FGO_FX_CLAMP_DT) + 3
            self.out = []
            self.j = 0

        def __len__(self):
            return self.n

        def __getitem__(self, i):
            if i < 0:
                i += self.n
            if _fgo_fx_track_nostore:
                return self.sample(i)
            out = self.out
            while len(out) <= i:
                a = len(out) * _FGO_FX_CLAMP_DT
                states = self.states
                while not self.done and states[-1][0] < a:
                    try:
                        states.append(next(self.gen))
                    except StopIteration:
                        self.done = True
                while not self.done and len(states) < 2:
                    try:
                        states.append(next(self.gen))
                    except StopIteration:
                        self.done = True
                j = self.j
                while j < len(states) - 2 and states[j + 1][0] < a:
                    j += 1
                self.j = j
                s0, s1 = states[j], states[min(j + 1, len(states) - 1)]
                f = (a - s0[0]) / (s1[0] - s0[0]) if s1[0] > s0[0] else 0.0
                f = min(1.0, max(0.0, f))
                out.append(tuple(s0[k] + (s1[k] - s0[k]) * f for k in (1, 2, 4, 5, 6, 3)))
            return out[i]

        def sample(self, i):
            """Sample i computed from the stored steps, nothing else kept (the stored path's
            values: its j is the first step whose successor reaches a, capped at len - 2, and the
            step times only grow, so a bisection finds the same j)."""
            a = i * _FGO_FX_CLAMP_DT
            states, times = self.states, self.times
            if not self.done and (times[-1] < a or len(states) < 2):
                gen = self.gen
                try:
                    while times[-1] < a or len(states) < 2:
                        s_ = next(gen)
                        states.append(s_)
                        times.append(s_[0])
                except StopIteration:
                    self.done = True
            j = _fgo_fx_bisect_left(times, a) - 1
            if j > len(states) - 2:
                j = len(states) - 2
            if j < 0:
                j = 0
            s0, s1 = states[j], states[min(j + 1, len(states) - 1)]
            f = (a - s0[0]) / (s1[0] - s0[0]) if s1[0] > s0[0] else 0.0
            f = min(1.0, max(0.0, f))
            return (s0[1] + (s1[1] - s0[1]) * f, s0[2] + (s1[2] - s0[2]) * f, s0[4] + (s1[4] - s0[4]) * f,
                    s0[5] + (s1[5] - s0[5]) * f, s0[6] + (s1[6] - s0[6]) * f, s0[3] + (s1[3] - s0[3]) * f)

    def _fgo_fx_size_axis(ax, u, f):
        """One sizeOverLifetime separateAxes axis at normalised age u, f =
        the particle's shared random fraction (converter _size_axis)."""
        k = ax[0]
        if k == "c":
            return ax[1]
        if k == "t":
            return ax[1] + (ax[2] - ax[1]) * f
        if k == "k":
            return ax[1] * _fgo_curve_at(ax[2], u)
        lo = _fgo_curve_at(ax[3], u)
        return ax[1] * (lo + (_fgo_curve_at(ax[2], u) - lo) * f)

    _fgo_fx_chunk_cache = {}

    def _fgo_fx_mesh_zsize(e, size_z, size, grow, unit, age_u, mix):
        """Unity-verified 2026-10-02: mesh lifetime Z sizing before G / Rp.
        No new channel for older scalar-size meshes."""
        if "size_sep_z" in e:
            grow = _fgo_fx_size_axis(e["size_sep_z"], age_u, mix)
        return (size_z if size_z is not None else size) * grow * unit

    def _fgo_fx_speed_spin(axes, mix, flipped):
        """Unity-verified GMBundle: separate RotationBySpeed constants share
        one fraction, ignore speed, and obey the particle's rotation flip."""
        sign = -1.0 if flipped else 1.0
        return tuple(sign * (lo + (hi - lo) * mix) for lo, hi in axes)

    def _fgo_fx_chunk(name, ei, e, wi, k, mv=None, gu=None, sub=None, wt0=None):
        """Every particle born in emission cycle k of emitter ei's wi-th
        activation window. Deterministic per (effect, emitter, window, cycle):
        the same frame always renders the same, and each loop cycle gets its
        own independent draw.
        mv = (spec, T7, on) for a moving World-space emitter (anim_geo): each
        particle is spawned through the transform at its birth, and carries
        that birth origin."""
        key = (name, ei, wi, k) if mv is None else (name, ei, wi, k, mv[1])
        if _fgo_fx_cap_batch and e.get("cap_batch"):
            key += ("cap_batch",)
        if sub is not None:
            # a sub-emitter child's particles of ONE parent particle (index
            # sub["j"] in its cycle k; _fgo_fx_sub_cycle): births, spawn
            # frames and parent anchors precomputed
            key = key + ("sub", sub["j"]) + tuple(sub["key"])
        if gu is not None:
            key = key + (gu,)        # charaScale-dependent Hierarchy clamp track
        nz_on = "noise" in e and _fgo_fx_noise_approx
        if nz_on:
            key = key + ("nz",)      # noise tracks exist only with the switch on
        move_spawn = mv is not None and _fgo_fx_move_spawn_scope(e)
        if move_spawn:
            key = key + ("ms",)
        if _fgo_fx_oneshot_end and e.get("size_fe") and not e["loop"]:
            key = key + ("oe",)  # held start sizes must follow live OFF/ON toggles
        if _fgo_fx_pct_clock and e.get("burst_pct"):
            key = key + ("bpct",)
        wind_on = "wind" in e and _fgo_fx_wind
        if wind_on:
            if wt0 is None:
                wt0 = 0.0
            key = key + ("wind", wt0)      # the play's game clock (the track depends on it)
        got = _fgo_fx_chunk_cache.get(key)
        if got is not None:
            return got
        if len(_fgo_fx_chunk_cache) > 4000:
            _fgo_fx_chunk_cache.clear()

        seed = zlib.crc32(("%s|%d|%s|%d" % (name, ei, wi, k)).encode("utf-8")) & 0xffffffff
        if sub is not None:
            # new crc32 key: + the parent particle's index (2026-09-30)
            seed = zlib.crc32(("%s|%d|%s|%d|%d" % (name, ei, wi, k, sub["j"])).encode("utf-8")) & 0xffffffff
        rng = random.Random(seed)
        L = e["length"]
        births = _fgo_fx_emit_times(e, k, rng, mv) if sub is None else sub["births"]

        # Frames available to the flipbook: the whole sheet, or one row of it
        # (single-row mode, e["uv_row"]: fixed row index or "random" per particle).
        uv_row = e.get("uv_row")
        cols = e["tex"][1] if uv_row is not None else e["tex"][1] * e["tex"][2]
        fr = e.get("frame", ("const", 0))
        size_cycle = e.get("size_cycle")
        vol_c = e.get("vol") if any(a and a[0] == "const" for a in e.get("vol", ())) else None
        # per-axis VOL mixes (_fgo_fx_vol_mix_axes): two or more random axes
        rng_ax = None
        if _fgo_fx_vol_mix_axes and sum(1 for a in e.get("vol", ()) if a and (
                (a[0] == "const" and a[1] != a[2]) or (a[0] == "curve" and a[3] is not None))) >= 2:
            rng_ax = random.Random(seed ^ 0x9E3779B9)
        # noise (e["noise"]): a random strength draws from its own stream
        rng_nz = random.Random(seed ^ 0x5BD1E995) if nz_on else None
        out = []
        bsp = None
        if e["geo"].get("bsp") or e["geo"].get("edge_bsp"):
            # BurstSpread: each particle's (index, size) in its burst (births
            # sharing a time; the converter refuses rate emission here)
            bsp, cnt_ = [], collections.Counter(births)
            seen_ = collections.Counter()
            for t_ in births:
                bsp.append((seen_[t_], cnt_[t_]))
                seen_[t_] += 1
        if e.get('shape_tex') and e['shape_tex'][4]:
            births = births + _fgo_fx_shape_padding(e, births)
        for bi_, t in enumerate(births):
            # maxParticles is applied afterwards, across cycles (_fgo_fx_accepted).
            g_r = None
            shape_point_ = [] if e.get('shape_tex') else None
            if "radius_anim" in e and mv is not None:
                # ShapeModule.radius animated: every birth of the step
                # (j dt, (j+1) dt] spawns with the step's radius -- the clip at
                # its start (Legacy) / end (Animator) [converter
                # _emis_radius_fields]
                j_ = int(math.ceil(t / _FGO_FX_FRAME - 1e-6)) - 1
                tc_ = mv[2] + (j_ + (1 if e.get("anim_end") else 0)) * _FGO_FX_FRAME
                ph_, u_ = _fgo_fx_phase(mv[0], mv[1], tc_)
                au_ = _fgo_fx_autos(mv[0], mv[1], tc_) if "autos" in mv[0] else None
                g_r = dict(e["geo"], r=_fgo_fx_track(e["radius_anim"], ph_, u_, au_))
            if "sm_anim" in e and mv is not None:
                g_r = _fgo_fx_shape_scale_geo(e, mv, t, g_r)
            if sub is not None:
                # sub-emitter child: its shape frame turned by the parent's
                # velocity (_fgo_fx_sub_cycle), the offset relative to the
                # parent particle (lx["pa"], added by the draw)
                g_r = sub["geo"][bi_]
            if mv is None or "anim_geo" not in e or e["anim_geo"].get("local"):
                # (mv is also passed for rate_anim emitters that don't move --
                # lane modules; Local simulation space: spawned in the REST
                # frame, the draw maps the whole cloud through the current
                # frame -- _fgo_fx_dyn_frame)
                bxy = None
                ox, oy, dx, dy, dz, oz = _fgo_fx_spawn(e, rng, g_r, bsp[bi_] if bsp else None, shape_point_)
            else:
                if _fgo_fx_dist_new(e):
                    g_b, bxy = _fgo_fx_dist_spawn(mv[0], e, mv[1], mv[2], t)
                elif move_spawn:
                    g_b, bxy = _fgo_fx_move_spawn_frame(mv[0], e, mv[1], mv[2], t)
                else:
                    g_b, bxy = _fgo_fx_emitter_frame(mv[0], e, mv[1], mv[2] + t)
                ox, oy, dx, dy, dz, oz = _fgo_fx_spawn(e, rng, g_b, bsp[bi_] if bsp else None, shape_point_)
            if "speed_cycle" in e:
                # startSpeed curve: scalar * curve(((birth - delay) mod length) /
                # length), TwoCurves = uniform mix between the two. [Unity-
                # verified per particle 2026-09-24, ProbeInferred.cs SpeedCurve():
                # error 3e-5 with and without startDelay, across loop cycles.]
                scal, hi_k, lo_k = e["speed_cycle"]
                ph = ((t - e.get("delay", 0.0)) % L) / L if L > 0 else 0.0
                lo = _fgo_curve_at(lo_k, ph)
                sp = scal * (lo + (_fgo_curve_at(hi_k, ph) - lo) * rng.random())
            else:
                sp = _fgo_fx_rnd(e["speed"], rng)
            sv_ = e.get("start_values", {})
            life = max(1e-3, _fgo_fx_rnd(e["life"], rng))
            if e.get("life_inf"):
                # Unity-verified E3SynRunner inf0/1/2 (2026-10-03).
                # Keep slots forever and normalized age exactly zero.
                life = float("inf")
            sa_ = e.get("start_anim", {})
            if "life" in sa_ and sa_["life"][1] is None:
                life = _fgo_fx_sv_anim(e, "life", t.frame, mv, 0.0)
                if life <= 0.0:
                    continue
            if "life" in sv_ and sv_["life"][1] == sv_["life"][2]:
                life = _fgo_fx_sv_value(sv_["life"], t, 0.0)
                if life <= 0.0:
                    continue
            if size_cycle:
                scal, hi_k, lo_k = size_cycle
                ts = t
                if e.get("size_fe"):
                    # a startSize Curve is sampled at the END of the
                    # frame the particle is emitted in (a birth exactly on a
                    # boundary: the frame after it, as for a burst) [Unity-
                    # verified 2026-09-26, ProbeSmallGates.cs ss_*; the
                    # converter refuses the ambiguous lattices].
                    ts = (math.floor(t * _FGO_FX_HZ + 1e-6) + 1) / _FGO_FX_HZ
                    if not e["loop"] and abs(t * _FGO_FX_HZ - round(t * _FGO_FX_HZ)) < 1e-6:
                        # a one-shot's rate birth ON the grid: the frame
                        # Unity's float accumulator emits it in (own or next)
                        fe_ = _fgo_fx_emit_frame(e, t)
                        if fe_ is not None:
                            ts = (fe_ + 1) / _FGO_FX_HZ
                ph = ((ts - e.get("delay", 0.0)) % L) / L if L > 0 else 0.0
                ph = _fgo_fx_oneshot_end_phase(e, t, ts, ph)
                hi = _fgo_curve_at(hi_k, ph)
                lo = _fgo_curve_at(lo_k, ph)
                size = scal * (lo + (hi - lo) * rng.random())
            else:
                size = _fgo_fx_rnd(e.get("size", 1.0), rng)
            sv_ = e.get("start_values", {})
            if "size" in sv_ and sv_["size"][1] == sv_["size"][2]:
                size = _fgo_fx_sv_value(sv_["size"], t, 0.0)
            size_y = _fgo_fx_rnd(e["size_y"], rng) if "size_y" in e else None
            if "size_y" in sv_ and sv_["size_y"][1] == sv_["size_y"][2]:
                size_y = _fgo_fx_sv_value(sv_["size_y"], t, 0.0)
            rr = e.get("rot_range")
            rot0 = rng.uniform(rr[0], rr[1]) if rr else 0.0
            spin = _fgo_fx_rnd(e.get("spin", 0.0), rng)
            flipped = bool(e.get("rot_flip")) and rng.random() < e["rot_flip"]
            if flipped:
                rot0, spin = -rot0, -spin
            grav = _fgo_fx_rnd(e.get("grav", 0.0), rng)
            # Constant velocityOverLifetime axes: one draw per particle,
            # local axes -> world by vol_m -> screen (y down).
            vcx = vcy = vcz = 0.0
            if vol_c:
                lv = tuple(rng.uniform(a[1], a[2]) if (a and a[0] == "const") else 0.0 for a in vol_c)
                w = _fgo_fx_mv(e["vol_m"], lv)
                vcx, vcy, vcz = w[0], -w[1], w[2]
            if "tint_cycle" in e or "tint_cycle2" in e:
                # startColor Gradient / TwoGradients: sampled at the emission-
                # cycle phase of the birth (Unity-verified, ProbeBatch2.cs).
                tt_ = t
                if _fgo_fx_tint_frame_end and t >= 0.0:
                    # The Gradient is evaluated at the system time at the END
                    # of the frame the particle is emitted in: rate
                    # births, off-grid bursts, and a burst at 0 (the first
                    # frame's end (one frame) -- bit_talk_debuff's line_down was
                    # invisible: its alpha key at phase 0 is 0) [Unity-verified
                    # 2026-09-29, play mode, NCGradRunner.cs ->
                    # probe_nguiclip_grad/grad.txt: alpha-ramp gradient, 36
                    # rate births + bursts at 0 / 0.4 / 0.3417 all sample
                    # ps.time after the update; the older "birth phase" rule
                    # was off by up to one frame]. Real bundle:
                    # NCRunner.cs debuff line_down P0 / P1 colours.
                    tt_ = max(1.0, math.ceil(t * _FGO_FX_HZ - 1e-6)) / _FGO_FX_HZ
                    be_ = _fgo_fx_burst_end(e, t) if t > 1e-9 else None
                    if be_ is not None:
                        tt_ = be_
                elif t < -1e-6 and _fgo_fx_prewarm_unified and e.get("prewarm") and e["loop"]:
                    # a prewarm birth: sampled at the end of its prewarm emission step
                    # (_fgo_fx_pw_sample)
                    tt_ = _fgo_fx_pw_sample(_fgo_fx_max_life(e), t)
                ph = ((tt_ - e.get("delay", 0.0)) % L) / L if L > 0 else 0.0
                if "tint_mode" in e:
                    ph = _fgo_fx_start_phase(e, t, ph)
                    tint_rgb, tint_a = _fgo_fx_start_tint(e, ph, rng.random() if "tint_cycle2" in e else 0.0)
                elif "tint_cycle" in e:
                    colors, alphas = e["tint_cycle"]
                    tint_rgb, tint_a = _fgo_grad(colors, ph), _fgo_grad(alphas, ph)
                else:
                    (c0_, a0_), (c1_, a1_) = e["tint_cycle2"]
                    f = rng.random()
                    lo_c, hi_c = _fgo_grad(c0_, ph), _fgo_grad(c1_, ph)
                    tint_rgb = tuple(a + (b - a) * f for a, b in zip(lo_c, hi_c))
                    tint_a = _fgo_grad(a0_, ph) + (_fgo_grad(a1_, ph) - _fgo_grad(a0_, ph)) * f
            elif "tint_gradient" in e:
                colors, alphas = e["tint_gradient"]
                tt = rng.random()
                if "tint_mode" in e:
                    tint_rgb, tint_a = _fgo_fx_start_tint(e, tt)
                else:
                    tint_rgb, tint_a = _fgo_grad(colors, tt), _fgo_grad(alphas, tt)
            elif "tint_range" in e:
                lo, hi = e["tint_range"]
                f = rng.random()
                tint_rgb = tuple(a + (b - a) * f for a, b in zip(lo[0], hi[0]))
                tint_a = lo[1] + (hi[1] - lo[1]) * f
            elif "tint" in e:
                tint_rgb, tint_a = e["tint"]
            else:
                tint_rgb, tint_a = (1.0, 1.0, 1.0), 1.0
            if uv_row is None:
                row_base = 0
            elif uv_row == "random":
                row_base = rng.randrange(e["tex"][2]) * e["tex"][1]
            else:
                row_base = int(uv_row) * e["tex"][1]
            fu = None
            if fr[0] == "random":
                fu = rng.uniform(fr[1], fr[2])
                frame = row_base + min(cols - 1, int(fu * cols))
            elif fr[0] == "const":
                frame = row_base + min(cols - 1, int(fr[1]))
            else:
                frame = None
            clamp = e.get("clamp")
            if clamp and "clamp_k" in e:
                # Local simulation space under a uniform non-unit scale k:
                # Unity limits the SIMULATION-space speed, i.e. the drawn
                # speed at magnitude x k [Unity-verified 2026-09-27, lane
                # xform, LaneXformClamp.cs; converter _scaled_modules].
                clamp = (clamp[0] * e["clamp_k"], clamp[1])
            track = (_fgo_fx_clamp_track(clamp, life, dx * sp + vcx, dy * sp + vcy, dz * sp + vcz, grav)
                     if clamp else None)
            size_mix, vel_mix = rng.random(), rng.random()
            if rng_ax is not None:
                vel_mix = (vel_mix, rng_ax.random(), rng_ax.random())
            # 3D rotation (e["rot3d"], converter _extra_fields; Unity-verified
            # 2026-09-25, ProbeRotation3D.cs): X, Y start angles independent
            # uniforms; separateAxes spins share one random across X/Y/Z;
            # randomizeRotationDirection negates everything. Drawn after
            # every older draw, so specs without rot3d are unchanged.
            tilt = None
            r3 = e.get("rot3d")
            if r3 is not None:
                rx0 = rng.uniform(r3[0][0], r3[0][1])
                ry0 = rng.uniform(r3[1][0], r3[1][1])
                sx = sy = 0.0
                s3 = e.get("spin3d")
                if s3 is not None:
                    f = rng.random()
                    sx = s3[0][0] + (s3[0][1] - s3[0][0]) * f
                    sy = s3[1][0] + (s3[1][1] - s3[1][0]) * f
                    spin = s3[2][0] + (s3[2][1] - s3[2][0]) * f
                    if flipped:
                        spin = -spin
                if flipped:
                    rx0, ry0, sx, sy = -rx0, -ry0, -sx, -sy
                if e.get("align_dir") and _fgo_fx_align_dir:
                    # ShapeModule.alignToDirection: the launch direction's
                    # euler angles join the start rotation (X += -asin(d.y),
                    # Y += atan2(d.x, -d.z), world Y up, d in simulation
                    # space; dy is y-down here) [Unity-verified 2026-10-17,
                    # small_rules probes atd_rot / atd_wit / atd_spin].
                    ux_, uy_, uz_ = dx, -dy, dz
                    un_ = math.sqrt(ux_ * ux_ + uy_ * uy_ + uz_ * uz_)
                    if un_ > 1e-9:
                        rx0 += -math.degrees(math.asin(max(-1.0, min(1.0, uy_ / un_))))
                        ry0 += math.degrees(math.atan2(ux_, -uz_))
                tilt = (rx0, ry0, sx, sy)
            # Mesh particles (e["mesh"], converter _render_fields): the Z size
            # (size3D: an independent draw) and which mesh, UniformRandom or
            # weighted by mesh_w [Unity-verified 2026-09-26,
            # ProbeMeshParticles.cs Distribution()]. Drawn last, so specs
            # without meshes draw exactly as before.
            mx = None
            if "mesh" in e:
                size_z = _fgo_fx_rnd(e["size_z"], rng) if "size_z" in e else None
                ids = e["mesh"]
                if len(ids) == 1:
                    mi = ids[0]
                elif "mesh_w" in e:
                    x = rng.random() * sum(e["mesh_w"])
                    mi = ids[-1]
                    for id_, w_ in zip(ids, e["mesh_w"]):
                        if x < w_:
                            mi = id_
                            break
                        x -= w_
                else:
                    mi = ids[rng.randrange(len(ids))]
                mx = (size_z, mi)
            # ForceOverLifetime (drawn after everything else: specs without
            # "force" draw exactly as before).
            ffr_ = None
            if "force" in e:
                ffr_ = (rng.random(), rng.random(), rng.random())
            fz = (_fgo_fx_force_particle(e["force"], t, life, rng, ffr_,
                                         _fgo_fx_max_life(e) if (_fgo_fx_prewarm_unified and e.get("prewarm")
                                                                 and _fgo_fx_pw_nonproc(e)) else None)
                  if "force" in e else None)
            # ShapeModule randomPositionAmount and TextureSheetAnimation
            # startFrame (lane shape, 2026-09-26): drawn after every older
            # draw, so specs without them draw exactly as before.
            # (merge wave 1: the geo the particle was spawned with -- a
            # Local-space moving emitter (lane xform) or a rate_anim emitter
            # (lane modules) has mv set but spawns with e["geo"].)
            gg = e["geo"] if bxy is None else g_b
            if gg.get("rp"):
                # + rp * (uniform unit vector) in the canonical shape frame,
                # through sm and A like the sample itself [Unity-verified,
                # LaneShapeProbe.cs Shape2(): |offset| = rp to 1e-5]
                uu = _fgo_fx_unit3(rng)
                w = _fgo_fx_mv(gg["A"], _fgo_fx_mv(gg["sm"], (uu[0] * gg["rp"], uu[1] * gg["rp"], uu[2] * gg["rp"])))
                ox, oy, oz = ox + w[0], oy - w[1], oz + w[2]
            sfs = e.get("sf")
            if sfs is not None:
                # frame = row_base + floor(((fot + startFrame) mod 1) * n)
                # [Unity-verified, LaneShapeProbe.cs UV()/UV2()/UV3()]; with a
                # frameOverTime curve the draw applies it (a float frame).
                s_ = sfs[1] if sfs[0] == "const" else rng.uniform(sfs[1], sfs[2])
                base_ = sfs[-1] if sfs[-1] is not None else fu
                if base_ is None:
                    frame = float(s_)
                else:
                    frame = row_base + min(cols - 1, int(((base_ + s_) % 1.0) * cols))
            cr_track_ = None
            if "clamp2" in e and "orbit" not in e and "speedmod" not in e:
                # frame-grid Limit Velocity (dampen / drag / VOL / Force): the
                # whole motion, like the old clamp track (after every draw)
                dmix_ = rng.random() if (e["clamp2"].get("drag_c") or ("",))[0] in ("r", "cc") else None
                # noise inside the Limit Velocity track (Unity limits v + vol +
                # noise); the particle's spawn point is where it samples
                nz_ = (_fgo_fx_nz_particle(name, ei, e, wi, rng_nz, t, life, ox, oy, oz,
                                           dx * sp, dy * sp, dz * sp, grav, fz, bxy) if nz_on else None)
                size_t_ = size
                if e["clamp2"]["ds"] and e.get("size_curve"):
                    size_t_ = _fgo_fx_size_at(e, size, size_mix, life)
                track = _fgo_fx_clamp2_track(e, life, t, dx * sp, dy * sp, dz * sp, grav, size_t_, vel_mix, fz,
                                             gu if gu is not None else 1.0, dmix_,
                                             wrot=_fgo_fx_wrot(e, mv) if (mv is not None and "anim_geo" in e
                                                                          and e["anim_geo"].get("wvol")) else None,
                                             nz=nz_, ffr=ffr_)
                if e.get("world_step") and mv is not None:
                    track = _fgo_fx_clamp2_track(e, life, t, dx * sp, dy * sp, dz * sp, grav, size_t_, vel_mix, fz,
                                gu if gu is not None else 1.0, dmix_, ffr=ffr_, world=_fgo_fx_world_frame(e, mv))
                if e["clamp2"].get("limit_c"):
                    # Rebuild after ALL existing draws using the frozen
                    # spawn state (before the singular draw-space map).
                    cr_args_ = (life, t, dx * sp, dy * sp, dz * sp, grav, size_t_, vel_mix, fz,
                                gu if gu is not None else 1.0, dmix_)
                    cr_kwargs_ = dict(wrot=_fgo_fx_wrot(e, mv) if (mv is not None and "anim_geo" in e
                                      and e["anim_geo"].get("wvol")) else None, nz=nz_, ffr=ffr_)
                    cr_track_ = lambda ee, aa=cr_args_, kk=cr_kwargs_: _fgo_fx_clamp2_track(ee, *aa, **kk)
            if e.get("world_step") and "clamp2" not in e and mv is not None:
                track = _fgo_fx_clamp2_track(e, life, t, dx * sp, dy * sp, dz * sp, grav, size, vel_mix, fz,
                            gu if gu is not None else 1.0, ffr=ffr_, world=_fgo_fx_world_frame(e, mv))
            if wind_on and "clamp2" not in e and "orbit" not in e and "speedmod" not in e and "world_step" not in e:
                track = _fgo_fx_clamp2_track(e, life, t, dx * sp, dy * sp, dz * sp, grav, size, vel_mix, fz,
                                             gu if gu is not None else 1.0, ffr=ffr_,
                                             wind=_fgo_fx_wind_acc(e["wind"], wt0, t))
            if "sing" in e:
                sing_spawn_ = (ox, oy, oz)
                sing_vel_ = (dx * sp, dy * sp, dz * sp)
                # A flat emitter (a zero scale) with Limit Velocity: everything
                # above ran in SIMULATION space (spawn A = Rs, VOL / Force
                # unmapped, the limit on the whole stored velocity, the lost
                # axis included, gravity and world modules absent) -- draw it
                # through K [Unity-verified 2026-09-29, SCSynRunner.cs,
                # singular_clamp_fit.py; converter _scaled_modules]. No random
                # draw happens here.
                Ks_ = e["sing"]
                ox, oy, oz = _fgo_fx_sing_map(Ks_, ox, oy, oz)
                dx, dy, dz = _fgo_fx_sing_map(Ks_, dx, dy, dz)
                vcx, vcy, vcz = _fgo_fx_sing_map(Ks_, vcx, vcy, vcz)
                track = _fgo_fx_sing_track(Ks_, track)
            # Existing late random draws stay in their old place.  Orbit's
            # seven property channels are new and deliberately follow them.
            late_ = _fgo_fx_late_draws(e, rng)
            # (merge wave 1: the randomPosition offset above is added BEFORE
            # the orbit track reads the position.)
            if "orbit" in e:
                gt_ = gu if gu is not None else 1.0
                p0_ = (ox, oy, oz)
                orbit_vel_ = (dx * sp, dy * sp, dz * sp)
                if e["orbit"].get("sim"):
                    # CRSyn singular Y-orbit: keep the lost axis in the
                    # simulation path; project only after orbit + limit.
                    p0_, orbit_vel_ = sing_spawn_, sing_vel_
                if e["orbit"].get("sh"):
                    # Shape scaling: the track runs in WORLD units (module
                    # values, gravity and velocity all are); the spawn offset
                    # is in prefab px: x spread / unit = 1 / g_u [Unity-
                    # verified 2026-09-29, OBHierRunner.cs _sog / _sogc]
                    p0_ = (ox / gt_, oy / gt_, oz / gt_)
                    gt_ = 1.0
                orbit_mix_ = _fgo_fx_orbit_mix(e, rng)
                smix_ = rng.random() if e.get("speedmod_random") else 0.0
                track = _fgo_fx_orbit_track(e, life, t, p0_,
                                            orbit_vel_[0], orbit_vel_[1], orbit_vel_[2], grav, vel_mix,
                                            orbit_mix_, fz, gt_, smix_)
                if e["orbit"].get("sim"):
                    track = _fgo_fx_sing_track(e["sing"], track)
                if e.get("clamp2", {}).get("limit_c"):
                    cr_args_ = (life, t, p0_, orbit_vel_[0], orbit_vel_[1], orbit_vel_[2], grav, vel_mix,
                                orbit_mix_, fz, gt_, smix_)
                    cr_track_ = lambda ee, aa=cr_args_: _fgo_fx_orbit_track(ee, *aa)
            elif "speedmod" in e:
                # New random draw follows every old particle draw. Reuse the
                # frame-grid track with a zero-dampen limit on the plain path.
                smix_ = rng.random() if e.get("speedmod_random") else 0.0
                se_ = dict(e)
                if "clamp2" not in se_:
                    se_["clamp2"] = {"m": 0.0, "k": None, "damp": 0.0, "drag": 0.0, "ds": False, "dv": False}
                    if e.get("speedmod_gu"):
                        se_["clamp_gu"] = "g"
                track = _fgo_fx_clamp2_track(se_, life, t, dx * sp, dy * sp, dz * sp, grav,
                                             size, vel_mix, fz, gu if gu is not None else 1.0,
                                             ffr=ffr_, smix=smix_)
                if e.get("clamp2", {}).get("limit_c"):
                    cr_args_ = (life, t, dx * sp, dy * sp, dz * sp, grav, size, vel_mix, fz,
                                gu if gu is not None else 1.0)
                    cr_track_ = lambda ee, aa=cr_args_, ff=ffr_, sm=smix_: _fgo_fx_clamp2_track(ee, *aa, ffr=ff, smix=sm)
            if e.get("size_speed", {}).get("random"):
                # New channel after every previous random draw; independent
                # of sizeOverLifetime, shared by SizeBySpeed axes in Unity.
                late_ = dict(late_) if late_ is not None else {}
                late_["ss"] = rng.random()
            if sub is not None:
                # where the parent particle was (no random draw here)
                late_ = dict(late_) if late_ is not None else {}
                late_["pa"] = sub["pa"][bi_]
            if "spin_axes" in e:
                # New shared fraction after all older random channels.
                late_ = dict(late_) if late_ is not None else {}
                mix_ = rng.random() if any(a_[2] is not None for a_ in e["spin_axes"]) else 1.0
                late_["spt3"] = tuple(_fgo_fx_spin_table(a_, t, life, mix_, -1.0 if flipped else 1.0)
                                      for a_ in e["spin_axes"])
                late_["spt"] = late_["spt3"][2]
            if "speed_spin_axes" in e:
                axes_ = e["speed_spin_axes"]
                mix_ = rng.random() if any(a_[0] != a_[1] for a_ in axes_) else 1.0
                sx_, sy_, sz_ = _fgo_fx_speed_spin(axes_, mix_, flipped)
                tilt = (tilt[0], tilt[1], tilt[2] + sx_, tilt[3] + sy_) if tilt is not None else (0.0, 0.0, sx_, sy_)
                spin += sz_
            # Independent TwoCurves axis channels follow all older draws.
            for key_ in ("size", "size_y", "size_z"):
                ax_ = sv_.get(key_)
                if ax_ is not None and (ax_[1] != ax_[2] or key_ == "size_z"):
                    val_ = _fgo_fx_sv_value(ax_, t, rng.random() if ax_[1] != ax_[2] else 0.0)
                    if key_ == "size":
                        size = val_
                    elif key_ == "size_y":
                        size_y = val_
                    elif mx is not None:
                        mx = (val_, mx[1])
            if "grav_cycle" in e:
                late_ = dict(late_) if late_ is not None else {}
                ax_ = e["grav_cycle"]
                late_["gm"] = rng.random() if ax_[1] != ax_[2] else 0.0
            if "life" in sv_ and sv_["life"][1] != sv_["life"][2]:
                life = _fgo_fx_sv_value(sv_["life"], t, rng.random())
                if life <= 0.0:
                    continue
            for key_ in ("size", "size_y", "life", "rot_z", "grav"):
                if key_ not in sa_:
                    continue
                mix_ = rng.random() if sa_[key_][1] is not None else 0.0
                if key_ == "grav":
                    late_ = dict(late_) if late_ is not None else {}
                    late_["ga"] = (mv, mix_)
                    continue
                val_ = _fgo_fx_sv_anim(e, key_, t.frame, mv, mix_)
                if key_ == "size":
                    size = val_
                elif key_ == "size_y":
                    size_y = val_
                elif key_ == "life":
                    life = val_
                elif key_ == "rot_z":
                    rot0 = -val_ if flipped else val_
            if life <= 0.0:
                continue
            for key_ in ("rot_x", "rot_y", "rot_z"):
                ax_ = sv_.get(key_)
                if ax_ is None:
                    continue
                val_ = _fgo_fx_sv_value(ax_, t, rng.random() if ax_[1] != ax_[2] else 0.0)
                if flipped:
                    val_ = -val_
                if key_ == "rot_z":
                    rot0 = val_
                elif tilt is not None:
                    tilt = ((val_, tilt[1], tilt[2], tilt[3]) if key_ == "rot_x"
                            else (tilt[0], val_, tilt[2], tilt[3]))
            if cr_track_ is not None:
                # Independent random limit channel follows every old draw.
                # Only the per-particle copy has a mix; emitter specs/cache
                # keys remain shared. Singular limits run in simulation
                # space, then their completed track maps through K.
                cr_e_ = dict(e)
                cr_e_["clamp2"] = dict(e["clamp2"], limit_mix=rng.random())
                track = cr_track_(cr_e_)
                if "sing" in e:
                    track = _fgo_fx_sing_track(e["sing"], track)
            if shape_point_ is not None:
                sample_ = _fgo_fx_shape_sample(e['shape_tex'], shape_point_)
                tint_rgb, tint_a = _fgo_fx_shape_color(e['shape_tex'], sample_, tint_rgb, tint_a)
                late_ = dict(late_) if late_ is not None else {}
                late_['stx'] = sample_[e['shape_tex'][3]] >= e['shape_tex'][4]
            out.append((t, life, ox, oy, dx * sp, dy * sp, vcx, vcy, grav,
                        size, size_y, rot0, spin, tint_rgb, tint_a, frame,
                        size_mix, vel_mix, track, dz * sp + vcz, row_base, bxy, tilt, oz, mx, fz,
                        late_))
            if "spin_curve" in e:
                # RotationOverLifetime curve: cumulative angle table
                # (_fgo_fx_spin_table), negated for a flipped particle
                p_ = out[-1]
                lx_ = p_[26] if p_[26] is not None else {}
                lx_["spt"] = _fgo_fx_spin_table(e["spin_curve"], t, life, lx_.get("sc", 1.0), -1.0 if flipped else 1.0)
                if p_[26] is None:
                    out[-1] = p_[:26] + (lx_,)
            if _fgo_fx_vol_frames and track is None and any(
                    a_ and a_[0] == "curve" for a_ in e.get("vol", ())):
                # free VOL curves: the per-frame sum (_fgo_fx_vol_table)
                p_ = out[-1]
                lx_ = dict(p_[26]) if p_[26] is not None else {}
                lx_["vt"] = _fgo_fx_vol_table(e, t, life, vel_mix)
                out[-1] = p_[:26] + (lx_,)
            if "rot_cycle" in e:
                # startRotation curve: at the end of the emission frame
                # (converter rot_cycle), mix drawn last (_fgo_fx_late_draws)
                p_ = out[-1]
                r_ = _fgo_fx_rot_cycle(e, t, p_[26]["rot"])
                if flipped:
                    r_ = -r_
                out[-1] = p_[:11] + (r_,) + p_[12:]
            if nz_on and "clamp2" not in e:
                # noise without a Limit Velocity: its own lazily integrated
                # displacement, added by the draw (lx["nz"], _FGOFXNoise)
                p_ = out[-1]
                lx_ = dict(p_[26]) if p_[26] is not None else {}
                lx_["nz"] = _fgo_fx_nz_particle(name, ei, e, wi, rng_nz, t, life, ox, oy, oz,
                                                dx * sp + vcx, dy * sp + vcy, dz * sp + vcz, grav, fz, bxy)
                out[-1] = p_[:26] + (lx_,)

        if "burst_prob" in e and sub is None:
            out = _fgo_fx_prob_filter(out, e["burst_prob"], rng)
        if _fgo_fx_pct_clock and e.get("burst_pct"):
            # Scoped single burst at zero: native P starts at a full first
            # step, then advances in float32 and removes strictly above100.
            # Reuse the existing metadata consumed by drawing/counts/bounds.
            f32 = _fgo_fx_f32
            inv = f32(1.0 / f32(e["life"]))
            inc = f32(f32(f32(_FGO_FX_FRAME) * 100.0) * inv)
            d = _fgo_fx_fclock_death({"inc": inc}, inc, 0)
            dk = (d + 1) * _FGO_FX_FRAME
            out = [(_FGOFXClockBirth(float(p[0]), _FGO_FX_FRAME, dk, True),) + p[1:] for p in out]
        _fgo_fx_chunk_cache[key] = out
        return out

    # --- drawing ------------------------------------------------------------------

    def _fgo_fx_tilt2(rx, ry, rz):
        """Upper-left 2x2 of Ry(ry) * Rx(rx) * Rz(-rz) (degrees), world y up:
        a 3D-rotated billboard's quad plane seen by the ortho camera."""
        cx_, sx_ = math.cos(math.radians(rx)), math.sin(math.radians(rx))
        cy_, sy_ = math.cos(math.radians(ry)), math.sin(math.radians(ry))
        cz_, sz_ = math.cos(math.radians(-rz)), math.sin(math.radians(-rz))
        return (cy_ * cz_ + sy_ * sx_ * sz_, -cy_ * sz_ + sy_ * sx_ * cz_,
                cx_ * sz_, cx_ * cz_)

    def _fgo_fx_shader_alpha(e, alpha):
        # Game Metal 7de2fbf798, SHRestGrid: split shaders read only
        # SubTex.r for alpha; vertex alpha / param1 are unused.
        return 1.0 if e.get("split_alpha") else alpha

    def _fgo_fx_mote3d(cell_key, w, h, rx, ry, rz, rgb, alpha, blend, mul=None, qs=None, geom=False, poly=None):
        """A billboard with a 3D particle rotation (X/Y tilt), as the affine
        map its projected quad is. Unity (View/World alignment): corners
        (+-w/2, +-h/2, 0), world Y up, rotated by Ry(ry) * Rx(rx) * Rz(-rz),
        Z dropped; the Local-mode own scale (qs) then zooms the screen axes.
        [Unity-verified 2026-09-25, ProbeRotation3D.cs Tilt()/Extra(): baked
        vertices within 1e-5.] Angles are bucketed like _fgo_fx_mote's."""
        geom = geom or poly is not None or len(cell_key) > 6
        step = _fgo_fx_step_for(w, h, qs)
        rx = _fgo_fx_rb(rx, step)
        ry = _fgo_fx_rb(ry, step)
        rz = _fgo_fx_rb(rz, step)
        if not geom:
            w, h = _fgo_mote_safe_size(w, h, 1)
        key = ("3d", cell_key, w, h, rx, ry, rz, rgb, alpha, blend, mul, qs, geom, poly)
        if key in _fgo_fx_motes:
            return _fgo_fx_motes[key]
        r00, r01, r10, r11 = _fgo_fx_tilt2(rx, ry, rz)
        # world (y up) -> screen (y down): conjugate by diag(1, -1)
        L00, L01, L10, L11 = r00, -r01, -r10, r11
        if qs is not None:
            L00, L01 = L00 * qs[0], L01 * qs[0]
            L10, L11 = L10 * qs[1], L11 * qs[1]
        if abs(L00 * L11 - L01 * L10) < 1e-6:
            d = None
        else:
            if len(_fgo_fx_motes) > 8000:
                _fgo_fx_motes.clear()
            m = Matrix([L00, L01, 0, 0, L10, L11, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
            d = _fgo_fx_mote_affine(cell_key, w, h, m, rgb, alpha, blend, mul, geom, poly)
        _fgo_fx_motes[key] = d
        return d

    # --- Local render alignment + mesh particles (2026-09-26) ----------------
    # Rules (Unity-verified 2026-09-26, Assets/Editor/ProbeMeshParticles.cs;
    # full citation in tools/fgo_fx_convert.py _render_fields): world offset
    # from the particle centre =
    #   billboard: G * Ry(y) Rx(x) Rz(-z) * diag(sx, sy, .) * (+-0.5, +-0.5, 0)
    #   mesh:      G * Ry(y) Rx(x) Rz(+z) * diag(sx, sy, sz) * vertex
    # with G = e["align_m"] (alignment x the scaling mode's scale), dropped Z,
    # world Y up. BakeMesh vertices match to 1.5e-7.

    def _fgo_fx_rot3(rx, ry, rz, mesh):
        """Ry(ry) * Rx(rx) * Rz(+-rz) (degrees) as three rows. Billboards turn
        by -z, mesh particles by +z (Quaternion.Euler(x, y, z))."""
        cx_, sx_ = math.cos(math.radians(rx)), math.sin(math.radians(rx))
        cy_, sy_ = math.cos(math.radians(ry)), math.sin(math.radians(ry))
        a = math.radians(rz if mesh else -rz)
        cz_, sz_ = math.cos(a), math.sin(a)
        # Ry * Rx = [[cy, sy sx, sy cx], [0, cx, -sx], [-sy, cy sx, cy cx]]
        rows = ((cy_, sy_ * sx_, sy_ * cx_), (0.0, cx_, -sx_), (-sy_, cy_ * sx_, cy_ * cx_))
        return tuple((r[0] * cz_ + r[1] * sz_, -r[0] * sz_ + r[1] * cz_, r[2]) for r in rows)

    # (runtime_performance) Performance only: True = _fgo_fx_m3mul without nested generators (the
    # same sum() calls); False = the older generator form.
    _fgo_fx_m3mul_flat = True

    def _fgo_fx_m3mul(A, B):
        if not _fgo_fx_m3mul_flat:
            return tuple(tuple(sum(A[i][k] * B[k][j] for k in range(3)) for j in range(3)) for i in range(3))
        # sum() over the same three products in the same order (Python 3.12's sum is compensated:
        # an explicit a + b + c would not be the same bits); a tuple instead of nested generators
        (a0, a1, a2), (b0, b1, b2), (c0, c1, c2) = A[0], A[1], A[2]
        B0, B1, B2 = B[0], B[1], B[2]
        return ((sum((a0 * B0[0], a1 * B1[0], a2 * B2[0])), sum((a0 * B0[1], a1 * B1[1], a2 * B2[1])),
                 sum((a0 * B0[2], a1 * B1[2], a2 * B2[2]))),
                (sum((b0 * B0[0], b1 * B1[0], b2 * B2[0])), sum((b0 * B0[1], b1 * B1[1], b2 * B2[1])),
                 sum((b0 * B0[2], b1 * B1[2], b2 * B2[2]))),
                (sum((c0 * B0[0], c1 * B1[0], c2 * B2[0])), sum((c0 * B0[1], c1 * B1[1], c2 * B2[1])),
                 sum((c0 * B0[2], c1 * B1[2], c2 * B2[2]))))

    def _fgo_fx_velocity_G(spec, vx, vy, vz):
        """E1Velocity, 2026-10-03: the normal follows current velocity.
        World Y up. Local uniform frame uses its emitter rotation, with a
        world +X right-axis fallback when the LOCAL velocity is along Z/zero.
        The last column negates Z so the existing billboard rotation/pivot
        path yields Ry(-y) Rx(-x) Rz(-z) and positive pivot.z in this basis.
        """
        R = spec["r"]
        v = (vx, -vy, vz)
        if spec["local"]:
            v = tuple(sum(R[j][i] * v[j] for j in range(3)) for i in range(3))
        n = math.sqrt(sum(a * a for a in v))
        z = tuple(a / n for a in v) if n else (0.0, 0.0, 1.0)
        xy = math.hypot(v[0], v[1])
        x = (-v[1] / xy, v[0] / xy, 0.0) if xy > 1e-10 else (1.0, 0.0, 0.0)
        if spec["local"]:
            z = _fgo_fx_mv(R, z)
            if xy > 1e-10:
                x = _fgo_fx_mv(R, x)
        y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
        s = spec["s"]
        return tuple((x[i] * s[0], y[i] * s[1], -z[i] * s[2]) for i in range(3))

    def _fgo_fx_moteG(cell_key, w, h, G, rx, ry, rz, rgb, alpha, blend, mul=None, geom=False):
        """A billboard through the general matrix G (Local alignment, or a
        tilted quad with a pivot.z): the projected quad is the affine map
        proj(G * Ry Rx Rz(-z)) of the (w, h) cell -- exact, the camera being
        orthographic. None when seen edge-on. Angles bucketed like
        _fgo_fx_mote3d's. geom: the geometry leaf, never shrunk."""
        geom = geom or len(cell_key) > 6
        step = _fgo_fx_step_for(w, h, None, G)
        rx = _fgo_fx_rb(rx, step)
        ry = _fgo_fx_rb(ry, step)
        rz = _fgo_fx_rb(rz, step)
        if not geom:
            w, h = _fgo_mote_safe_size(w, h, 1)
        key = ("G", cell_key, w, h, G, rx, ry, rz, rgb, alpha, blend, mul, geom)
        if key in _fgo_fx_motes:
            return _fgo_fx_motes[key]
        L = _fgo_fx_m3mul(G, _fgo_fx_rot3(rx, ry, rz, False))
        # world (y up) -> screen (y down): conjugate by diag(1, -1)
        L00, L01, L10, L11 = L[0][0], -L[0][1], -L[1][0], L[1][1]
        if abs(L00 * L11 - L01 * L10) < 1e-6:
            d = None
        else:
            if len(_fgo_fx_motes) > 8000:
                _fgo_fx_motes.clear()
            m = Matrix([L00, L01, 0, 0, L10, L11, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
            d = _fgo_fx_mote_affine(cell_key, w, h, m, rgb, alpha, blend, mul, geom)
        _fgo_fx_motes[key] = d
        return d

    def _fgo_fx_mote_affine(cell_key, w, h, m, rgb, alpha, blend, mul, geom, poly=None):
        """The (w, h) cell through matrixtransform m about its centre (the
        tilted / general-matrix motes); geom: the _FGOFXQuad leaf."""
        bl = _fgo_blend_names.get(blend, "normal")
        if mul is None:
            leaf = _FGOFXQuad(cell_key, w, h, poly=poly) if geom else Transform(_fgo_fx_cell(*cell_key), **_fgo_fx_xy(w, h))
            return Transform(leaf, matrixtransform=m, matrixanchor=(0.5, 0.5),
                             matrixcolor=TintMatrix(Color(rgb=rgb)), alpha=alpha, blend=bl)
        u = (rgb[0] * mul[0], rgb[1] * mul[1], rgb[2] * mul[2], alpha * mul[3])
        if geom:
            inner = _FGOFXQuad(cell_key, w, h, u, poly)
        else:
            inner = Transform(_fgo_fx_cell(*cell_key), mesh=True, **_fgo_fx_xy(w, h),
                              shader="fgo.fx_matmul", u_fgo_mul=u)
        return Transform(inner, matrixtransform=m, matrixanchor=(0.5, 0.5), blend=bl)

    def _fgo_fx_stretch_pivot_x(cvx, cvy, raw_x, raw_y, sx, cap, length, pivot_x):
        """Screen shift for Stretch pivot.x; length is signed, cap in pixels.
        Unity-verified 2026-09-30 (PivotX/PXGrid.cs, 1536 cases): total
        length sign, signed X scale on BOTH axes, cap from X * max(X,Y).
        Rotation, flip and render alignment do not change this shift.
        """
        spd = math.hypot(cvx, cvy)
        if spd < 1e-6 or length == 0.0:
            return (0.0, 0.0)
        big = sx * max(raw_x, raw_y)
        k = cap / big if big > cap > 0 else 1.0
        q = pivot_x * raw_x * sx * k * (1.0 if length > 0 else -1.0) / spd
        return (-cvy * q, cvx * q)

    def _fgo_fx_stretch_axes(cvx, cvy, cvz, raw_x, raw_y, sv, ss, cap, u_sz, piv_y):
        """Stretched billboard on a non-uniformly scaled or mirrored emitter
        (spec stretch_s = (s.x, s.y), converter _scaled_modules): the quad is
        a PARALLELOGRAM. Screen px, y down. (cvx, cvy, cvz) = world velocity
        (screen-projected x / y, depth), raw_x / raw_y = sizes in px after
        sizeOverLifetime, sv = (signed lengthScale, velocityScale), u_sz = px
        per world unit, piv_y = renderer pivot.y. Returns (A, W, P) or None
        (nothing drawn):
          A  particle -> far end (texture u 0 -> 1) = proj(-L d)
          W  texture v 0 -> 1 = proj(sign(L) k size_x diag(s) side)
          P  pivot shift = proj(2 pivot.y size_y s.y d)
        with d = v / |v|, L = size_y lengthScale s.x + |v| velocityScale
        (signed), k = min(1, cap / (s.x max(size_x, size_y))) for s.x > 0
        (s.x <= 0 never caps), side = the screen-plane perpendicular of d.
        [Unity-verified 2026-09-27, lane stretchscale (LaneStretchScaleProbe
        Run/Run2/Run3, 9,648 BakeMesh cases); this function checked on them
        by tools/fx_verify/stretchscale_rt.py.] Zero world velocity or L == 0:
        Unity collapses the quad (None). side: Unity takes it from the
        camera-to-particle direction, flattened onto the screen plane; this
        uses the camera forward axis instead, which agrees for every velocity
        not within a few degrees of the view axis (there the streak is
        foreshortened to almost nothing), and gives up (None) for a velocity
        exactly along it (as the uniform-scale path does)."""
        n3 = math.sqrt(cvx * cvx + cvy * cvy + cvz * cvz)
        spd = math.hypot(cvx, cvy)
        if n3 < 1e-9 or spd < 1e-9:
            return None
        sx, sy = ss
        L = raw_y * sv[0] * sx + n3 * sv[1] * u_sz
        if L == 0.0:
            return None
        dx, dy = cvx / n3, cvy / n3
        k = 1.0
        big = sx * max(raw_x, raw_y)
        if cap > 0 and big > cap:
            k = cap / big
        # side (world, y up) = normalize(d_y, -d_x); screen (x, -y)
        wk = (1.0 if L > 0 else -1.0) * k * raw_x / spd
        W = (wk * sx * -cvy, -(wk * sy * -cvx))
        pk = 2.0 * piv_y * raw_y * sy
        return (-L * dx, -L * dy), W, (pk * dx, pk * dy)

    def _fgo_fx_moteS(cell_key, A, B, rgb, alpha, blend, mul=None, poly=None):
        """A parallelogram mote: the cell's x axis (texture u, left -> right)
        maps to screen vector A, its y axis (image top -> bottom) to B,
        centred -- exact for a planar quad under the orthographic camera, as
        _fgo_fx_moteG. Lengths are rounded to px and directions bucketed to
        _FGO_FX_ROT_STEP like every other mote. None when degenerate."""
        la, lb = math.hypot(*A), math.hypot(*B)
        if la < 1.0 or lb < 0.5:
            return None
        step = _fgo_fx_step_reach(0.5 * math.hypot(la, lb))
        if _fgo_fx_rot_exact_all:
            aa = _fgo_fx_rb(math.degrees(math.atan2(A[1], A[0])), step)
            ab = _fgo_fx_rb(math.degrees(math.atan2(B[1], B[0])), step)
            if abs(math.sin(math.radians(ab - aa))) < 1e-6:
                return None
        else:
            aa = int(round(math.degrees(math.atan2(A[1], A[0])) / step) * step) % 360
            ab = int(round(math.degrees(math.atan2(B[1], B[0])) / step) * step) % 360
            if (ab - aa) % 180 == 0:
                return None
        w, h = int(round(la)), max(1, int(round(lb)))
        w, h = _fgo_fx_wh(la, lb, w, h)
        key = ("S", cell_key, w, h, aa, ab, rgb, alpha, blend, mul, poly)
        d = _fgo_fx_motes.get(key)
        if d is not None:
            return d
        if len(_fgo_fx_motes) > 8000:
            _fgo_fx_motes.clear()
        ca, sa = math.cos(math.radians(aa)), math.sin(math.radians(aa))
        cb, sb = math.cos(math.radians(ab)), math.sin(math.radians(ab))
        m = Matrix([ca, cb, 0, 0, sa, sb, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        bl = _fgo_blend_names.get(blend, "normal")
        if mul is None:
            d = Transform((_FGOFXQuad(cell_key, w, h, poly=poly) if poly is not None else Transform(_fgo_fx_cell(*cell_key), **_fgo_fx_xy(w, h))), matrixtransform=m,
                          matrixanchor=(0.5, 0.5), matrixcolor=TintMatrix(Color(rgb=rgb)),
                          alpha=alpha, blend=bl)
        else:
            u = (rgb[0] * mul[0], rgb[1] * mul[1], rgb[2] * mul[2], alpha * mul[3])
            inner = (_FGOFXQuad(cell_key, w, h, u, poly) if (poly is not None or len(cell_key) > 6) else
                     Transform(_fgo_fx_cell(*cell_key), mesh=True, **_fgo_fx_xy(w, h),
                               shader="fgo.fx_matmul", u_fgo_mul=u))
            d = Transform(inner, matrixtransform=m, matrixanchor=(0.5, 0.5), blend=bl)
        _fgo_fx_motes[key] = d
        return d

    _fgo_fx_mesh_layout = []
    _fgo_fx_sheet_disp = {}

    def _fgo_fx_mesh_draw(rv, spec, e, parts, st, at, mul, uvst=None, mt=None):
        """Every mesh particle of one emitter as ONE Mesh2 (triangles in
        particle order, each mesh's own index order -- Unity's draw order,
        no depth sort [ProbeMeshParticles.cs DrawOrder()]) textured with the
        whole sheet. parts: (px, py, T, mesh index, frame, rgb, alpha), T =
        the 2x3 screen-pixel map (world y up) of a mesh vertex."""
        import renpy.gl2.gl2mesh as gm
        import renpy.gl2.gl2mesh2 as gm2
        if not _fgo_fx_mesh_layout:
            lay = gm.AttributeLayout()
            lay.add_attribute("a_tex_coord", 2)
            lay.add_attribute("a_fgo_color", 4)
            _fgo_fx_mesh_layout.append(lay)
        asset, cols, rows = e["tex"]
        meshes = spec["meshes"]
        geo, att, tri = [], [], []
        n = 0
        inv255 = 1.0 / 255.0
        for part in parts:
            px, py, T, mi, frame, rgb, a = part[:7]
            pj = part[7] if len(part) > 7 else None
            m = meshes[mi]
            v, uv, c = m["v"], m["uv"], m.get("c")
            t00, t01, t02, t10, t11, t12 = T
            # texture-sheet tile: frame 0 = top-left, Unity uv v up -> Ren'Py
            # texture v down [UvSheet()]
            fc, fr_ = frame % cols, frame // cols
            r0, g0, b0 = rgb
            nv = len(v) // 3
            if pj is not None:
                # a perspective picture camera (OPUS_CAMERA_PERSPECTIVE): each vertex
                # in camera-relative world units, rotated into camera space, projected;
                # a particle with any vertex outside [near, far] is not drawn
                d_, (t20, t21, t22), un_, W2_, H2_, f_, R_, nr_, fr2_ = pj
                X0, Y0 = (px - W2_) / un_, -(py - H2_) / un_
                pg_ = []
                for i in range(nv):
                    x, y, z = v[3 * i], v[3 * i + 1], v[3 * i + 2]
                    w_ = (X0 + (t00 * x + t01 * y + t02 * z) / un_, Y0 + (t10 * x + t11 * y + t12 * z) / un_,
                          d_ + (t20 * x + t21 * y + t22 * z) / un_)
                    if R_ is not None:
                        w_ = _fgo_fx_rt(R_, w_)
                    if w_[2] < nr_ or w_[2] > fr2_:
                        pg_ = None
                        break
                    pg_.append((W2_ + f_ * w_[0] / w_[2], H2_ - f_ * w_[1] / w_[2]))
                if pg_ is None:
                    continue
            for i in range(nv):
                x, y, z = v[3 * i], v[3 * i + 1], v[3 * i + 2]
                if pj is not None:
                    geo.extend(pg_[i])
                else:
                    geo.append(px + t00 * x + t01 * y + t02 * z)
                    geo.append(py - (t10 * x + t11 * y + t12 * z))
                if mt is not None:
                    # raw sheet uv (Unity, v up) for fgo.fx_mt
                    att.append((fc + uv[2 * i]) / cols)
                    att.append(1.0 - (fr_ + 1.0 - uv[2 * i + 1]) / rows)
                elif uvst is None:
                    att.append((fc + uv[2 * i]) / cols)
                    att.append((fr_ + 1.0 - uv[2 * i + 1]) / rows)
                else:
                    att.extend(_fgo_fx_uv_map((fc + uv[2 * i]) / cols,
                                              1.0 - (fr_ + 1.0 - uv[2 * i + 1]) / rows, uvst))
                if c is None:
                    att.extend((r0, g0, b0, a))
                else:
                    att.extend((r0 * c[4 * i] * inv255, g0 * c[4 * i + 1] * inv255,
                                b0 * c[4 * i + 2] * inv255, a * c[4 * i + 3] * inv255))
            tri.extend(n + k for k in m["t"])
            n += nv
        if not tri:
            return
        if mt is not None:
            # att = (u, v, r, g, b, a) per vertex; colour x material multiplier
            m_ = tuple(mul) if mul is not None else (1.0, 1.0, 1.0, 1.0)
            uv0 = [x for i in range(n) for x in att[6 * i:6 * i + 2]]
            col = [att[6 * i + 2 + k] * m_[k] for i in range(n) for k in range(4)]
            rv.blit(_fgo_fx_mt_render(asset, mt, rv.width, rv.height, geo, uv0, [0.0] * (2 * n), col, tri,
                                      e["blend"], st, at), (0, 0))
            return
        mesh = gm2.Mesh2(_fgo_fx_mesh_layout[0], n, len(tri) // 3)
        mesh.set_geometry_data(geo)
        mesh.set_attribute_data(att)
        mesh.set_triangle_data(tri)
        d = _fgo_fx_sheet_disp.get(asset)
        if d is None:
            d = _fgo_fx_sheet_disp[asset] = renpy.displayable("assets/fx/%s.png" % asset)
        tex = renpy.display.im.render_for_texture(d, rv.width, rv.height, st, at)
        mr = renpy.Render(rv.width, rv.height)
        mr.blit(tex, (0, 0))
        mr.mesh = mesh
        mr.add_shader("renpy.texture")
        mr.add_shader("fgo.fx_meshcol")
        mr.add_uniform("u_fgo_mul", tuple(mul) if mul is not None else (1.0, 1.0, 1.0, 1.0))
        mr.add_property("blend_func", config.gl_blend_func[_fgo_blend_names.get(e["blend"], "normal")])
        if uvst is not None:
            mr.add_property("texture_wrap_tex0", (_FGO_FX_GL_WRAP[uvst[4]], _FGO_FX_GL_WRAP[uvst[5]]))
        rv.blit(mr, (0, 0))

    _fgo_fx_cells = {}
    _fgo_fx_motes = {}

    # --- texture scale / offset (e["uv"], converter _uv_fields) ---------------
    # uv' = uv * scale + offset on the texture-sheet UV, in Unity's UV frame (v
    # up), sampled with the texture's wrap mode (0 Repeat, 1 Clamp, 2 Mirror) -- what the
    # game's particle shaders do in their vertex stage (uv * _MainTex_ST.xy +
    # _MainTex_ST.zw). Drawn as one Mesh2 quad over the WHOLE sheet texture
    # whose corner texture coordinates are the transformed tile corners
    # (affine, so exact under interpolation), with GL's texture wrap doing
    # Repeat / Clamp. uvst = (sx, sy, ox, oy, wrap_u, wrap_v).
    _FGO_FX_GL_WRAP = {0: 0x2901, 1: 0x812F, 2: 0x8370}   # GL_REPEAT, GL_CLAMP_TO_EDGE, GL_MIRRORED_REPEAT
    _fgo_fx_quad_layout = []

    def _fgo_fx_uv_map(u, v, uvst):
        """Unity sheet uv (v up) -> Ren'Py texture coordinates (v down)."""
        return (u * uvst[0] + uvst[2], 1.0 - (v * uvst[1] + uvst[3]))

    class _FGOFXUVCell(renpy.Displayable):
        def __init__(self, asset, cols, rows, idx, uvst):
            super(_FGOFXUVCell, self).__init__()
            self.asset, self.cols, self.rows, self.idx, self.uvst = asset, cols, rows, idx, uvst

        def render(self, width, height, st, at):
            import renpy.gl2.gl2mesh as gm
            import renpy.gl2.gl2mesh2 as gm2
            if not _fgo_fx_quad_layout:
                lay = gm.AttributeLayout()
                lay.add_attribute("a_tex_coord", 2)
                _fgo_fx_quad_layout.append(lay)
            path = "assets/fx/%s.png" % self.asset
            sw, sh = renpy.image_size(path)
            cols, rows = self.cols, self.rows
            tw, th = sw // cols, sh // rows
            c, r = self.idx % cols, self.idx // cols
            u0, u1 = float(c) / cols, float(c + 1) / cols
            vt, vb = 1.0 - float(r) / rows, 1.0 - float(r + 1) / rows
            att = []
            for (u, v) in ((u0, vt), (u1, vt), (u0, vb), (u1, vb)):
                att.extend(_fgo_fx_uv_map(u, v, self.uvst))
            mesh = gm2.Mesh2(_fgo_fx_quad_layout[0], 4, 2)
            mesh.set_geometry_data([0.0, 0.0, float(tw), 0.0, 0.0, float(th), float(tw), float(th)])
            mesh.set_attribute_data(att)
            mesh.set_triangle_data([0, 1, 2, 1, 3, 2])
            d = _fgo_fx_sheet_disp.get(self.asset)
            if d is None:
                d = _fgo_fx_sheet_disp[self.asset] = renpy.displayable(path)
            tex = renpy.display.im.render_for_texture(d, sw, sh, st, at)
            rv = renpy.Render(tw, th)
            rv.blit(tex, (0, 0))
            rv.mesh = mesh
            rv.add_shader("renpy.texture")
            rv.add_property("texture_wrap_tex0", (_FGO_FX_GL_WRAP[self.uvst[4]], _FGO_FX_GL_WRAP[self.uvst[5]]))
            return rv

        def visit(self):
            return []

    def _fgo_fx_cell(asset, cols, rows, idx, uvst=None, fl=0):
        if fl & 3:
            # renderer flip on a billboard / streak: the texture mirrored
            # inside its sheet tile (u for x, v for y), geometry unchanged
            # [Unity-verified 2026-09-27, SG2Flip.cs; converter _flip_field]
            key = (asset, cols, rows, idx, uvst, fl & 3)
            c = _fgo_fx_cells.get(key)
            if c is None:
                c = _fgo_fx_cells[key] = Transform(_fgo_fx_cell(asset, cols, rows, idx, uvst),
                                                   xzoom=-1.0 if fl & 1 else 1.0, yzoom=-1.0 if fl & 2 else 1.0)
            return c
        key = (asset, cols, rows, idx, uvst)
        c = _fgo_fx_cells.get(key)
        if c is None and uvst is not None:
            if len(_fgo_fx_cells) > 4000:
                _fgo_fx_cells.clear()
            c = _fgo_fx_cells[key] = _FGOFXUVCell(asset, cols, rows, idx, uvst)
        if c is None:
            path = "assets/fx/%s.png" % asset
            if cols * rows == 1:
                c = path
            else:
                sw, sh = renpy.image_size(path)
                tw, th = sw // cols, sh // rows
                c = im.Crop(path, ((idx % cols) * tw, (idx // cols) * th, tw, th))
            _fgo_fx_cells[key] = c
        return c

    # --- Oversized quads as geometry (BigMesh, 2026-10-06, notes/big_quads_mesh.md) ---
    # A quad above the 4000 px safe size used to be SHRUNK (fgo_core
    # _fgo_mote_safe_size); Unity draws it at full size. _FGOFXQuad is the
    # w x h quad as ONE Mesh2 (two triangles) sampling the same cell texture
    # with the same UVs as _fgo_fx_cell (plain crop, flip = mirrored UVs,
    # ST / scroll = the _FGOFXUVCell corner UVs + wrap), the material
    # multiplier applied per fragment (fgo.fx_matmul). It replaces only the
    # leaf Transform(cell, xysize=(w, h)) of a mote: the outer rotate /
    # matrixtransform / zoom / tint / alpha / blend chain is unchanged, so
    # the placement is Ren'Py's own; nothing renders into an intermediate
    # texture, so no size limit applies. True = an oversized quad on a
    # verified path draws at full size; False = the older shrink.
    _fgo_fx_big_mesh = True
    # TEST HOOK (identity grid only, never set in production): every quad
    # on the three paths through the geometry leaf.
    _fgo_fx_big_mesh_all = False
    _fgo_fx_quad_tex = {}

    class _FGOFXQuad(renpy.Displayable):
        def __init__(self, cell_key, w, h, u=None, poly=None):
            super(_FGOFXQuad, self).__init__()
            self.cell_key, self.w, self.h, self.u, self.poly = cell_key, w, h, u, poly

        def render(self, width, height, st, at):
            import renpy.gl2.gl2mesh as gm
            import renpy.gl2.gl2mesh2 as gm2
            if not _fgo_fx_quad_layout:
                lay = gm.AttributeLayout()
                lay.add_attribute("a_tex_coord", 2)
                _fgo_fx_quad_layout.append(lay)
            ck = self.cell_key
            poly = getattr(self, "poly", None)  # older saved geometry leaves
            asset, cols, rows, idx = ck[:4]
            uvst = ck[4] if len(ck) > 4 else None
            fl = (ck[5] if len(ck) > 5 else 0) & 3
            if len(ck) > 6 and ck[6] is not None:
                # an mtex material: raw tile-corner uvs (Unity, v up), the program does the rest
                c, r = idx % cols, idx // cols
                u0, u1 = float(c) / cols, float(c + 1) / cols
                vt, vb = 1.0 - float(r) / rows, 1.0 - float(r + 1) / rows
                uv = [(u0, vt), (u1, vt), (u0, vb), (u1, vb)]
                if fl & 1:
                    uv = [uv[1], uv[0], uv[3], uv[2]]
                if fl & 2:
                    uv = [uv[2], uv[3], uv[0], uv[1]]
                self._uv = uv
                w, h = float(self.w), float(self.h)
                if poly is None:
                    geo = [0.0, 0.0, w, 0.0, 0.0, h, w, h]
                    raw = [v for p in uv for v in p]
                    tri = [0, 1, 2, 1, 3, 2]
                else:
                    geo = [v for x, y in poly for v in (x * w, y * h)]
                    raw = [uv[0][k] + x * (uv[1][k] - uv[0][k]) + y * (uv[2][k] - uv[0][k])
                           for x, y in poly for k in (0, 1)]
                    tri = [v for i in range(1, len(poly) - 1) for v in (0, i, i + 1)]
                n = len(geo) // 2
                col = list(self.u or (1.0, 1.0, 1.0, 1.0)) * n
                return _fgo_fx_mt_render(asset, ck[6], self.w, self.h, geo, raw, [0.0] * (2 * n), col, tri,
                                         None, st, at)
            if uvst is None:
                # the whole cell texture: Ren'Py trims an image's fully
                # transparent margins on load, and a trimmed child would be
                # flattened (resampled) again under the mesh
                d = _fgo_fx_quad_tex.get((asset, cols, rows, idx))
                if d is None:
                    d = _fgo_fx_quad_tex[(asset, cols, rows, idx)] = renpy.display.im.UnoptimizedTexture(
                        _fgo_fx_cell(asset, cols, rows, idx))
                # corners (0,0) (w,0) (0,h) (w,h): texture u right, v down
                uv = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
            else:
                path = "assets/fx/%s.png" % asset
                d = _fgo_fx_sheet_disp.get(asset)
                if d is None:
                    d = _fgo_fx_sheet_disp[asset] = renpy.displayable(path)
                c, r = idx % cols, idx // cols
                u0, u1 = float(c) / cols, float(c + 1) / cols
                vt, vb = 1.0 - float(r) / rows, 1.0 - float(r + 1) / rows
                uv = [_fgo_fx_uv_map(u_, v_, uvst) for (u_, v_) in ((u0, vt), (u1, vt), (u0, vb), (u1, vb))]
            if fl & 1:
                uv = [uv[1], uv[0], uv[3], uv[2]]
            if fl & 2:
                uv = [uv[2], uv[3], uv[0], uv[1]]
            self._uv = uv          # (_FGOFXPQuad)
            w, h = float(self.w), float(self.h)
            if poly is None:
                mesh = gm2.Mesh2(_fgo_fx_quad_layout[0], 4, 2)
                mesh.set_geometry_data([0.0, 0.0, w, 0.0, 0.0, h, w, h])
                mesh.set_attribute_data([v for p in uv for v in p])
                mesh.set_triangle_data([0, 1, 2, 1, 3, 2])
            else:
                # UV and position share the same orthographic edge fraction.
                mesh = gm2.Mesh2(_fgo_fx_quad_layout[0], len(poly), len(poly) - 2)
                mesh.set_geometry_data([v for x, y in poly for v in (x * w, y * h)])
                mesh.set_attribute_data([uv[0][k] + x * (uv[1][k] - uv[0][k])
                    + y * (uv[2][k] - uv[0][k]) for x, y in poly for k in (0, 1)])
                mesh.set_triangle_data([v for i in range(1, len(poly) - 1) for v in (0, i, i + 1)])
            tex = renpy.display.im.render_for_texture(d, width, height, st, at)
            rv = renpy.Render(self.w, self.h)
            rv.blit(tex, (0, 0))
            rv.mesh = mesh
            rv.add_shader("renpy.texture")
            if self.u is not None:
                rv.add_shader("fgo.fx_matmul")
                rv.add_uniform("u_fgo_mul", self.u)
            if uvst is not None:
                rv.add_property("texture_wrap_tex0", (_FGO_FX_GL_WRAP[uvst[4]], _FGO_FX_GL_WRAP[uvst[5]]))
            return rv

        def visit(self):
            return []

    class _FGOFXPQuad(_FGOFXQuad):
        """A perspective-projected particle quad (OPUS_CAMERA_PERSPECTIVE): an
        n x n grid of the cell, every grid vertex projected exactly by the
        picture camera (pts: (n + 1)^2 screen points relative to this
        displayable's origin, row-major over image (x, y) in [0, 1]); the
        affine texture interpolation inside each small cell is the only
        approximation of Unity's perspective-correct one."""
        def __init__(self, cell_key, w, h, pts, n, u=None):
            super(_FGOFXPQuad, self).__init__(cell_key, w, h, u)
            self.pts, self.n = pts, n

        def render(self, width, height, st, at):
            import renpy.gl2.gl2mesh2 as gm2
            rv = super(_FGOFXPQuad, self).render(width, height, st, at)
            uv, n = self._uv, self.n
            k = n + 1
            att = []
            for j in range(k):
                b = j / float(n)
                for i in range(k):
                    a = i / float(n)
                    att.append(uv[0][0] + a * (uv[1][0] - uv[0][0]) + b * (uv[2][0] - uv[0][0]))
                    att.append(uv[0][1] + a * (uv[1][1] - uv[0][1]) + b * (uv[2][1] - uv[0][1]))
            tri = []
            for j in range(n):
                for i in range(n):
                    q = j * k + i
                    tri.extend((q, q + 1, q + k, q + 1, q + k + 1, q + k))
            mesh = gm2.Mesh2(_fgo_fx_quad_layout[0], k * k, 2 * n * n)
            mesh.set_geometry_data([v for p_ in self.pts for v in p_])
            mesh.set_attribute_data(att)
            mesh.set_triangle_data(tri)
            rv.mesh = mesh
            return rv

    def _fgo_fx_rt(R, p):
        """R^T p: a camera-relative world vector (y up) in the camera's own axes
        (R = the camera's world rotation, rows; converter cams "rot")."""
        return (R[0][0] * p[0] + R[1][0] * p[1] + R[2][0] * p[2],
                R[0][1] * p[0] + R[1][1] * p[1] + R[2][1] * p[2],
                R[0][2] * p[0] + R[1][2] * p[1] + R[2][2] * p[2])

    def _fgo_fx_pquad(cell_key, corners, f, W2, H2, rgb, alpha, blend, mul=None, n=4):
        """(displayable, x, y) for a 3D particle quad seen by a perspective
        picture camera at the origin looking down +z (world units, y up):
        corners = (P, A, B), image point (a, b) at P + a A + b B. f = focal
        length px, (W2, H2) the view centre. None when any grid vertex lies
        at or behind the camera plane."""
        P, A, B = corners
        pts = []
        for j in range(n + 1):
            b = j / float(n)
            for i in range(n + 1):
                a = i / float(n)
                x = P[0] + a * A[0] + b * B[0]
                y = P[1] + a * A[1] + b * B[1]
                z = P[2] + a * A[2] + b * B[2]
                if z <= 1e-6:
                    return None
                pts.append((W2 + f * x / z, H2 - f * y / z))
        x0 = min(p_[0] for p_ in pts)
        y0 = min(p_[1] for p_ in pts)
        x1 = max(p_[0] for p_ in pts)
        y1 = max(p_[1] for p_ in pts)
        if x1 - x0 < 0.5 and y1 - y0 < 0.5:
            return None
        ix, iy = int(math.floor(x0)), int(math.floor(y0))
        w, h = max(1, int(math.ceil(x1)) - ix), max(1, int(math.ceil(y1)) - iy)
        pts = [(p_[0] - ix, p_[1] - iy) for p_ in pts]
        bl = _fgo_blend_names.get(blend, "normal")
        if mul is None:
            d = Transform(_FGOFXPQuad(cell_key, w, h, pts, n), matrixcolor=TintMatrix(Color(rgb=rgb)),
                          alpha=alpha, blend=bl)
        else:
            u = (rgb[0] * mul[0], rgb[1] * mul[1], rgb[2] * mul[2], alpha * mul[3])
            d = Transform(_FGOFXPQuad(cell_key, w, h, pts, n, u), blend=bl)
        return d, ix, iy, w, h

    def _fgo_fx_mote(cell_key, w, h, rot, rgb, alpha, blend, mul=None, qs=None, geom=False, poly=None):
        """mul: the material's (r, g, b, a) colour multiplier (e["mat_mul"]),
        applied with the particle colour through fgo.fx_matmul, unclamped.
        qs: e["quad_scale"] -- scalingMode=Local own scale, a screen-axis
        zoom applied AFTER the rotation (Unity-verified; see the converter).
        geom: the quad as the geometry leaf _FGOFXQuad, never shrunk."""
        geom = geom or poly is not None or len(cell_key) > 6
        rot = _fgo_fx_rb(rot, _fgo_fx_step_for(w, h, qs))
        if not geom:
            w, h = _fgo_mote_safe_size(w, h, rot)
        key = (cell_key, w, h, rot, rgb, alpha, blend, mul, qs, geom, poly)
        d = _fgo_fx_motes.get(key)
        if d is None:
            if len(_fgo_fx_motes) > 8000:
                _fgo_fx_motes.clear()
            bl = _fgo_blend_names.get(blend, "normal")
            if mul is None:
                if geom:
                    d = Transform(_FGOFXQuad(cell_key, w, h, poly=poly), rotate=rot or None,
                                  matrixcolor=TintMatrix(Color(rgb=rgb)), alpha=alpha, blend=bl)
                else:
                    d = Transform(_fgo_fx_cell(*cell_key), rotate=rot or None, **_fgo_fx_xy(w, h),
                                  matrixcolor=TintMatrix(Color(rgb=rgb)), alpha=alpha, blend=bl)
            else:
                u = (rgb[0] * mul[0], rgb[1] * mul[1], rgb[2] * mul[2], alpha * mul[3])
                if geom:
                    inner = _FGOFXQuad(cell_key, w, h, u, poly)
                else:
                    inner = Transform(_fgo_fx_cell(*cell_key), mesh=True, **_fgo_fx_xy(w, h),
                                      shader="fgo.fx_matmul", u_fgo_mul=u)
                d = Transform(inner, rotate=rot or None, blend=bl)
            if qs is not None:
                d = Transform(d, xzoom=qs[0], yzoom=qs[1])
            _fgo_fx_motes[key] = d
        return d

    # Direct particle renders (notes/mesh_batch.md): a plain billboard's Render
    # built the way Ren'Py's Transform builds it (xysize + zoom + rotate with
    # rotate_pad, matrixcolor TintMatrix, alpha, blend), without constructing or
    # rendering a Transform. The render tree is bit-identical to the Transform
    # path's (sizes, every reverse / forward entry, the truncated rotate offset,
    # shaders, uniforms, blend_func; 20,000 random particles), so the GL draws
    # are the same. One mesh for many particles cannot be identical: Ren'Py rounds
    # projection x particle matrix once, a shared mesh must round the corners
    # first (1-level pixel flips). True = plain billboards (no mul / qs / geometry
    # leaf / clip polygon) draw through this; False = the Transform path.
    _fgo_fx_direct_motes = True
    _fgo_fx_direct_tint = {}
    _fgo_fx_direct_disp = {}
    _FGO_FX_IDENTITY = Matrix.offset(0.0, 0.0, 0.0)

    def _fgo_fx_mote_render(cell_key, w, h, rot, rgb, alpha, blend, st, at):
        """renpy.render(_fgo_fx_mote(cell_key, w, h, rot, rgb, alpha, blend), ...), built directly."""
        rot = _fgo_fx_rb(rot, _fgo_fx_step_for(w, h, None))
        w, h = _fgo_mote_safe_size(w, h, rot)
        c = _fgo_fx_cell(*cell_key)
        d = _fgo_fx_direct_disp.get(c)
        if d is None:
            if len(_fgo_fx_direct_disp) > 4000:
                _fgo_fx_direct_disp.clear()
            d = _fgo_fx_direct_disp[c] = renpy.easy.displayable(c)
        cr = renpy.render(d, w, h, st, at)
        width, height = cr.get_size()
        iw, ih = int(w), int(h)
        # Ren'Py's order of operations (xysize, then xzoom / yzoom, then rotate)
        rxdx = 1.0 * iw / width
        rydy = 1.0 * ih / height
        width, height = iw, ih
        if iw != w or ih != h:
            xz = 1.0 * (w / iw)
            yz = 1.0 * (h / ih)
            rxdx *= xz
            width *= xz
            rydy *= yz
            height *= yz
        rxdy = rydx = 0.0
        xo = yo = 0.0
        if rot:
            angle = rot * 3.1415926535897931 / 180
            cosa = math.cos(angle)
            sina = math.sin(angle)
            rxdy = rydy * -sina
            rydx = rxdx * sina
            rxdx *= cosa
            rydy *= cosa
            px = width / 2.0
            py = height / 2.0
            width = height = math.hypot(width, height)
            xo = -px * cosa + py * sina
            yo = -px * sina - py * cosa
            xo += width / 2.0
            yo += height / 2.0
        rv = renpy.Render(width, height)
        if rxdx != 1.0 or rydy != 1.0 or rxdy != 0.0 or rydx != 0.0:
            rv.reverse = Matrix((rxdx, rxdy, 0, rydx, rydy, 0, 0, 0, 1))
            rv.forward = rv.reverse.inverse()
        rv.blit(cr, (xo, yo))
        tm = _fgo_fx_direct_tint.get(rgb)
        if tm is None:
            if len(_fgo_fx_direct_tint) > 8000:
                _fgo_fx_direct_tint.clear()
            tm = _fgo_fx_direct_tint[rgb] = TintMatrix(Color(rgb=rgb))(None, 1.0)
        rv.add_shader("renpy.matrixcolor")
        rv.add_uniform("u_renpy_matrixcolor", tm)
        if alpha != 1.0:
            rv.add_shader("renpy.alpha")
            rv.add_uniform("u_renpy_alpha", alpha)
            rv.add_uniform("u_renpy_over", 1.0)
        rv.add_property("blend_func", renpy.config.gl_blend_func[_fgo_blend_names.get(blend, "normal")])
        rv.alpha = alpha
        rv.over = 1.0
        return rv

    def _fgo_fx_cell_render(cell_key, w, h, st, at):
        """renpy.render(Transform(_fgo_fx_cell(*cell_key), **_fgo_fx_xy(w, h)), ...), built directly."""
        c = _fgo_fx_cell(*cell_key)
        d = _fgo_fx_direct_disp.get(c)
        if d is None:
            if len(_fgo_fx_direct_disp) > 4000:
                _fgo_fx_direct_disp.clear()
            d = _fgo_fx_direct_disp[c] = renpy.easy.displayable(c)
        cr = renpy.render(d, w, h, st, at)
        width, height = cr.get_size()
        iw, ih = int(w), int(h)
        rxdx = 1.0 * iw / width
        rydy = 1.0 * ih / height
        width, height = iw, ih
        if iw != w or ih != h:
            xz = 1.0 * (w / iw)
            yz = 1.0 * (h / ih)
            rxdx *= xz
            width *= xz
            rydy *= yz
            height *= yz
        rv = renpy.Render(width, height)
        if rxdx != 1.0 or rydy != 1.0:
            rv.reverse = Matrix((rxdx, 0.0, 0, 0.0, rydy, 0, 0, 0, 1))
            rv.forward = rv.reverse.inverse()
        rv.blit(cr, (0.0, 0.0))
        return rv

    def _fgo_fx_mote_affine_render(cell_key, w, h, m, rgb, alpha, blend, st, at):
        """renpy.render(_fgo_fx_mote_affine(cell_key, w, h, m, rgb, alpha, blend, None, False)), built
        directly: matrixtransform m about matrixanchor (0.5, 0.5) of the sized cell."""
        cr = _fgo_fx_cell_render(cell_key, w, h, st, at)
        width, height = cr.get_size()
        ax = 0.5 * width
        ay = 0.5 * height
        M = Matrix.offset(-ax, -ay, 0.0)
        M = m * M
        M = Matrix.offset(ax, ay, 0.0) * M
        rv = renpy.Render(width, height)
        rv.reverse = M * _FGO_FX_IDENTITY
        rv.forward = rv.reverse.inverse()
        rv.blit(cr, (0, 0))
        tm = _fgo_fx_direct_tint.get(rgb)
        if tm is None:
            if len(_fgo_fx_direct_tint) > 8000:
                _fgo_fx_direct_tint.clear()
            tm = _fgo_fx_direct_tint[rgb] = TintMatrix(Color(rgb=rgb))(None, 1.0)
        rv.add_shader("renpy.matrixcolor")
        rv.add_uniform("u_renpy_matrixcolor", tm)
        if alpha != 1.0:
            rv.add_shader("renpy.alpha")
            rv.add_uniform("u_renpy_alpha", alpha)
            rv.add_uniform("u_renpy_over", 1.0)
        rv.add_property("blend_func", renpy.config.gl_blend_func[_fgo_blend_names.get(blend, "normal")])
        rv.alpha = alpha
        rv.over = 1.0
        return rv

    def _fgo_fx_moteG_render(cell_key, w, h, G, rx, ry, rz, rgb, alpha, blend, st, at):
        """renpy.render(_fgo_fx_moteG(...)) for mul None, no geometry leaf; None = edge-on."""
        step = _fgo_fx_step_for(w, h, None, G)
        rx = _fgo_fx_rb(rx, step)
        ry = _fgo_fx_rb(ry, step)
        rz = _fgo_fx_rb(rz, step)
        w, h = _fgo_mote_safe_size(w, h, 1)
        L = _fgo_fx_m3mul(G, _fgo_fx_rot3(rx, ry, rz, False))
        L00, L01, L10, L11 = L[0][0], -L[0][1], -L[1][0], L[1][1]
        if abs(L00 * L11 - L01 * L10) < 1e-6:
            return None
        m = Matrix([L00, L01, 0, 0, L10, L11, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        return _fgo_fx_mote_affine_render(cell_key, w, h, m, rgb, alpha, blend, st, at)

    def _fgo_fx_moteS_render(cell_key, A, B, rgb, alpha, blend, st, at):
        """renpy.render(_fgo_fx_moteS(...)) for mul None, no clip polygon; None = degenerate."""
        la, lb = math.hypot(*A), math.hypot(*B)
        if la < 1.0 or lb < 0.5:
            return None
        step = _fgo_fx_step_reach(0.5 * math.hypot(la, lb))
        if _fgo_fx_rot_exact_all:
            aa = _fgo_fx_rb(math.degrees(math.atan2(A[1], A[0])), step)
            ab = _fgo_fx_rb(math.degrees(math.atan2(B[1], B[0])), step)
            if abs(math.sin(math.radians(ab - aa))) < 1e-6:
                return None
        else:
            aa = int(round(math.degrees(math.atan2(A[1], A[0])) / step) * step) % 360
            ab = int(round(math.degrees(math.atan2(B[1], B[0])) / step) * step) % 360
            if (ab - aa) % 180 == 0:
                return None
        w, h = int(round(la)), max(1, int(round(lb)))
        w, h = _fgo_fx_wh(la, lb, w, h)
        ca, sa = math.cos(math.radians(aa)), math.sin(math.radians(aa))
        cb, sb = math.cos(math.radians(ab)), math.sin(math.radians(ab))
        m = Matrix([ca, cb, 0, 0, sa, sb, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        return _fgo_fx_mote_affine_render(cell_key, w, h, m, rgb, alpha, blend, st, at)

    class _FGOFXCropBlank(renpy.Displayable):
        """Geometry-only child: measuring a transform allocates no texture."""
        def __init__(self, w, h):
            super(_FGOFXCropBlank, self).__init__()
            self.w, self.h = w, h

        def render(self, width, height, st, at):
            return renpy.Render(self.w, self.h)

    _fgo_fx_crop_geometry = {}

    def _fgo_fx_crop_corners(w, h, rot, G=None, tilt=None, qs=None):
        """Use Ren'Py's own transform sizing/offsets, including rotate padding
        and integer output sizing, without drawing the oversized texture."""
        step = _fgo_fx_step_for(w, h, qs, G)
        rot = _fgo_fx_rb(rot, step)
        tilt = tuple(_fgo_fx_rb(a, step) for a in tilt) if tilt is not None else None
        key = (w, h, rot, G, tilt, qs)
        got = _fgo_fx_crop_geometry.get(key)
        if got is not None:
            return got
        blank = _FGOFXCropBlank(w, h)
        chain = []
        if G is not None or tilt is not None:
            rx, ry = tilt or (0, 0)
            if G is not None:
                L = _fgo_fx_m3mul(G, _fgo_fx_rot3(rx, ry, rot, False))
                a, b, c, d = L[0][0], -L[0][1], -L[1][0], L[1][1]
            else:
                a, b, c, d = _fgo_fx_tilt2(rx, ry, rot)
                b, c = -b, -c
                if qs is not None:
                    a, b, c, d = a * qs[0], b * qs[0], c * qs[1], d * qs[1]
            if abs(a * d - b * c) < 1e-6:
                return None
            m = Matrix([a, b, 0, 0, c, d, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
            chain.append(Transform(blank, matrixtransform=m, matrixanchor=(0.5, 0.5)))
        else:
            chain.append(Transform(blank, rotate=rot or None))
            if qs is not None:
                chain.append(Transform(chain[-1], xzoom=qs[0], yzoom=qs[1]))
        cr = renpy.render(chain[-1], w, h, 0, 0)
        cw, ch = cr.get_size()
        corners = []
        for x, y in ((0, 0), (w, 0), (w, h), (0, h)):
            for tr in chain:
                x, y = tr.reverse.transform(x, y)
                ox, oy = tr.offsets[0]
                x, y = x + ox, y + oy
            corners.append((x, y))
        got = (cw, ch, corners)
        if len(_fgo_fx_crop_geometry) > 8000:
            _fgo_fx_crop_geometry.clear()
        _fgo_fx_crop_geometry[key] = got
        return got

    def _fgo_fx_crop_clip(poly, bounds):
        """Sutherland-Hodgman, interpolating UV alongside screen coordinates."""
        for axis, edge, sign in ((0, bounds[0], 1), (0, bounds[2], -1),
                                 (1, bounds[1], 1), (1, bounds[3], -1)):
            if not poly:
                break
            out = []
            prev = poly[-1]
            inside = sign * (prev[axis] - edge) >= 0
            for cur in poly:
                now = sign * (cur[axis] - edge) >= 0
                if now != inside:
                    t = (edge - prev[axis]) / (cur[axis] - prev[axis])
                    out.append(tuple(a + t * (b - a) for a, b in zip(prev, cur)))
                if now:
                    out.append(cur)
                prev, inside = cur, now
            poly = out
        return poly

    def _fgo_fx_mote_crop_general(rv, cell, w, h, px, py, rot, rgb, alpha,
                                  blend, st, at, G=None, tilt=None, qs=None, mul=None):
        import renpy.gl2.gl2mesh as gm
        import renpy.gl2.gl2mesh2 as gm2
        got = _fgo_fx_crop_corners(w, h, rot, G, tilt, qs)
        if got is None:
            return
        cw, ch, corners = got
        ox, oy = _fgo_fx_ipos(px - cw * 0.5), _fgo_fx_ipos(py - ch * 0.5)
        poly = [(x + ox, y + oy, u, v) for (x, y), (u, v) in
                zip(corners, ((0, 0), (1, 0), (1, 1), (0, 1)))]
        poly = _fgo_fx_crop_clip(poly, (-64, -64, rv.width + 64, rv.height + 64))
        if len(poly) < 3:
            return
        if not _fgo_fx_quad_layout:
            lay = gm.AttributeLayout()
            lay.add_attribute("a_tex_coord", 2)
            _fgo_fx_quad_layout.append(lay)
        mesh = gm2.Mesh2(_fgo_fx_quad_layout[0], len(poly), len(poly) - 2)
        mesh.set_geometry_data([v for p in poly for v in p[:2]])
        mesh.set_attribute_data([v for p in poly for v in p[2:]])
        mesh.set_triangle_data([v for i in range(1, len(poly) - 1) for v in (0, i, i + 1)])
        # Reuse the production native cell (including ST, scroll and flips).
        # Only this small texture is materialized; the large quad is geometry.
        # Do not flatten a flip Transform into a DPI-scaled texture.
        # Mirror the sampled UVs instead, keeping the same native cell.
        fl_ = cell[5] if len(cell) > 5 and mul is None else 0
        native_ = cell[:5] if fl_ else cell
        if fl_:
            mesh.set_attribute_data([v for p_ in poly for v in
                (1.0-p_[2] if fl_ & 1 else p_[2], 1.0-p_[3] if fl_ & 2 else p_[3])])
        child = renpy.displayable(_fgo_fx_cell(*native_))
        tex = renpy.display.im.render_for_texture(child, w, h, st, at)
        mr = renpy.Render(rv.width, rv.height)
        mr.blit(tex, (0, 0))
        mr.mesh = mesh
        mr.add_shader("renpy.texture")
        mr.add_shader("fgo.fx_matmul")
        m = mul or (1.0, 1.0, 1.0, 1.0)
        mr.add_uniform("u_fgo_mul", (rgb[0] * m[0], rgb[1] * m[1], rgb[2] * m[2], alpha * m[3]))
        mr.add_property("blend_func", config.gl_blend_func[_fgo_blend_names.get(blend, "normal")])
        rv.blit(mr, (0, 0))

    def _fgo_fx_crop_fullframe(rv, got, px, py):
        """Every visible pixel >=2px inside the quad; no changed edge coverage.

        BigCrop2 2026-10-04: 180 plain-cell affine cases, interior max1/255.
        Visible-edge, ST, multiplied and rotated/stretched paths stay shrunk.
        """
        if got is None:
            return False
        cw, ch, corners = got
        ox, oy = _fgo_fx_ipos(px-cw*.5), _fgo_fx_ipos(py-ch*.5)
        q = [(x+ox,y+oy) for x,y in corners]
        area = sum(q[i][0]*q[(i+1)%4][1]-q[(i+1)%4][0]*q[i][1] for i in range(4))
        sign = 1.0 if area > 0 else -1.0
        for i in range(4):
            x,y=q[i];ex,ey=q[(i+1)%4][0]-x,q[(i+1)%4][1]-y
            margin = 2.0*math.hypot(ex,ey)
            if any(sign*(ex*(b-y)-ey*(a-x)) <= margin for a,b in
                    ((0,0),(rv.width,0),(rv.width,rv.height),(0,rv.height))):
                return False
        return True

    def _fgo_fx_mote_crop(cell_key, w, h, px, py, rwh, rgb, alpha, blend):
        """(displayable, x, y, vw, vh) or None: the part of an unrotated w x h
        quad centred on (px, py) that lies inside a <= 3800 px window around
        the render area's centre, drawn from the matching texture
        sub-rectangle (_fgo_fx_big_crop). Plain cells only (no scroll /
        flip / material multiplier / own-scale zoom: the caller checks)."""
        half = 1900
        cx, cy = rwh[0] * 0.5, rwh[1] * 0.5
        x0, y0 = int(math.floor(px - w * 0.5)), int(math.floor(py - h * 0.5))
        wx0, wy0 = max(x0, int(cx) - half), max(y0, int(cy) - half)
        wx1, wy1 = min(x0 + w, int(cx) + half), min(y0 + h, int(cy) + half)
        if wx1 <= wx0 or wy1 <= wy0:
            return None
        vw, vh = wx1 - wx0, wy1 - wy0
        cf = ((wx0 - x0) / float(w), (wy0 - y0) / float(h), vw / float(w), vh / float(h))
        key = ("crop", cell_key, cf, vw, vh, rgb, alpha, blend)
        d = _fgo_fx_motes.get(key)
        if d is None:
            if len(_fgo_fx_motes) > 8000:
                _fgo_fx_motes.clear()
            d = Transform(_fgo_fx_cell(*cell_key), crop=cf, crop_relative=True, xysize=(vw, vh),
                          matrixcolor=TintMatrix(Color(rgb=rgb)), alpha=alpha,
                          blend=_fgo_blend_names.get(blend, "normal"))
            _fgo_fx_motes[key] = d
        return d, wx0, wy0, vw, vh

    # --- animated tracks + transform nodes (tools/fgo_fx_anim.py) -------------

    def _fgo_fx_anim_keys(keys, t):
        """Unity AnimationCurve.Evaluate, clamp-wrapped: cubic Hermite; a
        segment whose left out-slope or right in-slope is infinite (None
        here) holds the left key's value (stepped). _fgo_curve_at can't be
        used: it has no stepped case. [Checked against Unity's own
        SampleAnimation in Assets/Editor/ProbeNGUI.cs Curves.]"""
        if any(len(k) == 7 for k in keys):
            # G3Weighted: inverse cubic Bezier time, then value. Keep this
            # path local to the animation sampler; ordinary curves and the
            # core evaluator retain their original execution/data paths.
            if t <= keys[0][0]:
                return keys[0][1]
            for k0, k1 in zip(keys, keys[1:]):
                t0, v0, _, m0 = k0[:4]
                t1, v1, m1, _ = k1[:4]
                if t <= t1:
                    if m0 is None or m1 is None:
                        return v0
                    dt = t1 - t0
                    if dt <= 0:
                        return v1
                    if len(k0) != 7 and len(k1) != 7:
                        x = (t - t0) / dt
                        x2 = x * x
                        x3 = x2 * x
                        return ((2 * x3 - 3 * x2 + 1) * v0 + (x3 - 2 * x2 + x) * dt * m0
                                + (-2 * x3 + 3 * x2) * v1 + (x3 - x2) * dt * m1)
                    w0 = k0[6] if len(k0) == 7 and k0[4] & 2 else 1.0 / 3.0
                    w1 = k1[5] if len(k1) == 7 and k1[4] & 1 else 1.0 / 3.0
                    x = (t - t0) / dt
                    lo, hi = 0.0, 1.0
                    for _ in range(44):
                        u = (lo + hi) * 0.5
                        z = 1.0 - u
                        bx = 3 * z * z * u * w0 + 3 * z * u * u * (1 - w1) + u * u * u
                        if bx < x:
                            lo = u
                        else:
                            hi = u
                    u = (lo + hi) * 0.5
                    z = 1.0 - u
                    return z*z*z*v0 + 3*z*z*u*(v0 + dt*w0*m0) + 3*z*u*u*(v1 - dt*w1*m1) + u*u*u*v1
            return keys[-1][1]
        if t <= keys[0][0]:
            return keys[0][1]
        for k0, k1 in zip(keys, keys[1:]):
            t1 = k1[0]
            if t <= t1:
                t0, v0, m0 = k0[0], k0[1], k0[3]
                v1, m1 = k1[1], k1[2]
                if m0 is None or m1 is None:
                    return v0
                dt = t1 - t0
                if dt <= 0:
                    return v1
                x = (t - t0) / dt
                x2 = x * x
                x3 = x2 * x
                return ((2 * x3 - 3 * x2 + 1) * v0 + (x3 - 2 * x2 + x) * dt * m0
                        + (-2 * x3 + 3 * x2) * v1 + (x3 - x2) * dt * m1)
        return keys[-1][1]

    _fgo_fx_pivot_anim = True

    def _fgo_fx_pivot_now(spec, e, T7, st):
        pivot = e.get('pivot')
        if not (_fgo_fx_pivot_anim and 'pivot_anim' in e):
            return pivot
        # Renderer value for this draw, shared by every live particle. Animator
        # advances in the creation frame; Legacy Play begins at zero.
        tc = st + (_FGO_FX_FRAME if e.get('pivot_end') else 0.0)
        ph, u = _fgo_fx_phase(spec, T7, tc)
        au = _fgo_fx_autos(spec, T7, tc) if 'autos' in spec else None
        return ((pivot or (0.0, 0.0))[0], _fgo_fx_track(e['pivot_anim'], ph, u, au))

    def _fgo_fx_autos(spec, T7, st):
        """Clock of every autoplaying Animation (spec["autos"]): seconds since
        its GameObject's current activation, None while inactive. Legacy
        autoplay starts at activation and restarts from 0 on every
        re-activation [Unity-verified 2026-09-25, ProbeAnimAuto.cs
        syn_react*] -- the same activation window an emitter uses."""
        out = []
        for a in spec.get("autos", ()):
            w = _fgo_fx_window(a, spec, T7, st)
            out.append(None if w is None else st - w[0])
        return out

    def _fgo_fx_track(tr, ph, u, au=None):
        """A track at (phase, u): a plain number is a constant; else (rest,
        {phase: ("c", keys) | ("h", held value)}), missing phase = rest; or
        an autoplay track ("A", i, rest, keys, length, loop) on clock au[i]
        (a Once clip holds its end value, a Loop clip wraps -- ProbeAnimAuto.cs
        syn_*)."""
        if not isinstance(tr, tuple):
            return tr
        if tr[0] == "A":
            ua = au[tr[1]] if au else None
            if ua is None:
                return tr[2]       # inactive: nothing under it is drawn
            if tr[5] and tr[4] > 0:
                ua -= math.floor(ua / tr[4]) * tr[4]
            return _fgo_fx_anim_keys(tr[3], ua)
        ent = tr[1].get(ph)
        if ent is None:
            return tr[0]
        if ent[0] == "h":
            return ent[1]
        return _fgo_fx_anim_keys(ent[1], u)

    def _fgo_fx_rot_m(r, ph, u, au=None):
        """Local rotation matrix: ("q", [x, y, z, w]) quaternion tracks,
        normalised after per-component evaluation, or ("e", [x, y, z])
        Euler degrees composed Z-X-Y (Quaternion.Euler) -- both as Legacy
        Animation writes them [checked: ProbeNGUI.cs Curves]."""
        v = [_fgo_fx_track(t, ph, u, au) for t in r[1]]
        if r[0] == "q":
            x, y, z, w = v
            n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
            x, y, z, w = x / n, y / n, z / n, w / n
            return ((1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
                    (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
                    (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)))
        ax, ay, az = (math.radians(a) for a in v)
        cx, sx = math.cos(ax), math.sin(ax)
        cy, sy = math.cos(ay), math.sin(ay)
        cz, sz = math.cos(az), math.sin(az)
        # Ry * Rx * Rz
        return ((cy * cz + sy * sx * sz, -cy * sz + sy * sx * cz, sy * cx),
                (cx * sz, cx * cz, -sx),
                (-sy * cz + cy * sx * sz, sy * sz + cy * sx * cz, cy * cx))

    def _fgo_fx_mm(a, b):
        return tuple(tuple(a[i][0] * b[0][j] + a[i][1] * b[1][j] + a[i][2] * b[2][j]
                           for j in range(3)) for i in range(3))

    # Unity-verified gs04_syn2: reflected ancestors conjugate child rotation.
    # Must agree with converter MIRROR_ROTATION when regenerating specs.
    _fgo_fx_mirror_rotation = True

    def _fgo_fx_mirror_child_rotation(R, M, local):
        if not _fgo_fx_mirror_rotation:
            return local
        signs = tuple(-1 if sum(R[k][i] * M[k][i] for k in range(3)) < 0 else 1
                      for i in range(3))
        return tuple(tuple(local[i][j] * signs[i] * signs[j] for j in range(3))
                     for i in range(3))

    def _fgo_fx_nodes(spec, ph, u, au=None):
        """[(M, pos, R)] per spec node: world linear map (3x3, prefab units
        = FGO px, world-Y-up), origin, and world rotation (signed-parent
        conjugation -- Transform.rotation), composed parent * T*R*S as
        Unity does (same order as the converter's _walk3d)."""
        out = []
        for n in spec["nodes"]:
            p = [_fgo_fx_track(t, ph, u, au) for t in n["p"]]
            s = [_fgo_fx_track(t, ph, u, au) for t in n["s"]]
            R = _fgo_fx_rot_m(n["r"], ph, u, au)
            lm = tuple(tuple(R[i][j] * s[j] for j in range(3)) for i in range(3))
            if n["parent"] < 0:
                out.append((lm, tuple(p), R))
            else:
                PM, pp, PR = out[n["parent"]]
                o = _fgo_fx_mv(PM, p)
                out.append((_fgo_fx_mm(PM, lm), (pp[0] + o[0], pp[1] + o[1], pp[2] + o[2]),
                            _fgo_fx_mm(PR, _fgo_fx_mirror_child_rotation(PR, PM, R))))
        return out

    def _fgo_fx_emitter_frame(spec, e, T7, st):
        """A moving emitter's (geo with A, (x, y) origin) at absolute time
        st -- the same construction as the converter's static _geometry:
        A = R * diag(lossy) * Rs (Shape / Hierarchy; lossy = diag(R^T M),
        Unity-verified in ProbeShape3D.cs) or R * diag(own) * Rs (Local)."""
        ag = e["anim_geo"]
        ph, u = _fgo_fx_phase(spec, T7, st)
        au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
        M, pos, R = _fgo_fx_nodes(spec, ph, u, au)[ag["node"]]
        if ag["mode"] == 1:
            sc = _fgo_fx_own_t(spec, ag["node"], ph, u, au) if ag.get("own_t") else ag["own"]
        else:
            sc = tuple(sum(R[k][i] * M[k][i] for k in range(3)) for i in range(3))
        if ag.get("full"):
            # Hierarchy with a non-unit scale: the full localToWorld, shear
            # included [lane xform 2026-09-27, LaneXformAnim.cs Positions]
            return dict(e["geo"], A=_fgo_fx_mm(M, ag["Rs"]), norm=False), (pos[0], pos[1], pos[2])
        RS = tuple(tuple(R[i][j] * sc[j] for j in range(3)) for i in range(3))
        g = dict(e["geo"], A=_fgo_fx_mm(RS, ag["Rs"]), norm=(ag["mode"] == 2))
        return g, (pos[0], pos[1], pos[2])

    def _fgo_fx_own_t(spec, ni, ph, u, au=None):
        """A spec node's own local scale now (its s tracks)."""
        return tuple(_fgo_fx_track(t, ph, u, au) for t in spec["nodes"][ni]["s"])

    def _fgo_fx_dyn_frame(spec, e, T7, st):
        """Per-frame state of a moving emitter whose particles follow it or
        whose quads change with it (lane xform 2026-09-27; converter
        anim_geo "local" / "Ki" / "dyn"). Returns (O, Js, G, cap_x): O = the
        emitter origin now (prefab units, world y up); Js = J(t) = K(t) Ki
        conjugated to screen orientation (y down), None for a pure
        translation; G = the quad matrix now (align_m's meaning), None if
        static; cap_x = the signed maxParticleSize scale x. K = M
        (Hierarchy) / R diag(own) (Local) / R (Shape) -- the simulation
        frame; Local simulation space draws world = O(t) + K(t) q [Unity-
        verified, LaneXformAnim.cs Anim, xform_anim_fit.py]."""
        ag = e["anim_geo"]
        ph, u = _fgo_fx_phase(spec, T7, st)
        au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
        M, pos, R = _fgo_fx_nodes(spec, ph, u, au)[ag["node"]]
        mode = ag["mode"]
        own = _fgo_fx_own_t(spec, ag["node"], ph, u, au) if mode == 1 else None
        if mode == 0:
            K = M
            sc = tuple(sum(R[k][i] * M[k][i] for k in range(3)) for i in range(3))
        elif mode == 1:
            K = tuple(tuple(R[i][j] * own[j] for j in range(3)) for i in range(3))
            sc = own
        else:
            K = R
            sc = (1.0, 1.0, 1.0)
        Js = None
        if "Ki" in ag or ag.get("Kq"):
            # Kq: particles were spawned in K's own frame (q space)
            J = K if ag.get("Kq") else _fgo_fx_mm(K, ag["Ki"])
            Js = ((J[0][0], -J[0][1], J[0][2]), (-J[1][0], J[1][1], -J[1][2]), (J[2][0], -J[2][1], J[2][2]))
        G = None
        dyn = ag.get("dyn")
        if dyn is not None:
            S = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0))
            D3 = ((sc[0], 0.0, 0.0), (0.0, sc[1], 0.0), (0.0, 0.0, sc[2]))
            al, rm = dyn["al"], dyn["rm"]
            if rm == 4:
                # mesh: Local alignment K (Hierarchy M, Local R diag(own),
                # Shape R); otherwise diag(scale) [ProbeMeshParticles.cs]
                G = K if al == 2 else D3
            elif al == 2:
                if mode == 1:
                    G = _fgo_fx_mm(_fgo_fx_mm(D3, R), S)
                else:
                    G = _fgo_fx_mm(K, S)
            elif al == 1:
                G = _fgo_fx_mm(D3, S)
            else:
                G = D3
            G = tuple(tuple(round(v, 4) + 0.0 for v in r) for r in G)
        return (pos[0], pos[1], pos[2]), Js, G, sc[0]

    # --- NGUI widgets ------------------------------------------------------------
    # One textured quad per UISprite / UITexture, as NGUI builds it
    # (decompiled UIBasicSprite.SimpleFill): corners (x0, y0)..(x1, y1) of
    # w["dd"] (drawingDimensions, local px, y up) carry the crop's corners,
    # bottom-left <-> the crop's bottom-left unless flipped; vertex colour =
    # (mColor.rgb, finalAlpha) as Color32; finalAlpha = the product of the
    # widget's and every UIRect ancestor's alpha. The quad goes through the
    # widget's world transform (animated nodes), projected by dropping Z --
    # an affine map, drawn with matrixtransform (fgo_vslash.rpy's approach).

    _fgo_fx_crops = {}

    def _fgo_fx_crop(asset, crop):
        key = (asset, crop)
        c = _fgo_fx_crops.get(key)
        if c is None:
            c = im.Crop("assets/fx/%s.png" % asset, crop)
            _fgo_fx_crops[key] = c
        return c

    def _fgo_fx_c32(v):
        # Color -> Color32: clamp, x255, rounded.
        return round(max(0.0, min(1.0, v)) * 255.0) / 255.0

    def _fgo_fx_widget_draw(rv, spec, w, nodes, ph, u, cx, cy, up, scale, st, at, au=None):
        a = 1.0
        for ri in w["rects"]:
            a *= _fgo_fx_track(spec["rects"][ri]["a"], ph, u, au)
        a = _fgo_fx_c32(a)
        if a <= 0.001:
            return None      # UIWidget: not visible by alpha
        widget_alpha = a
        if w.get("one_one"):
            # Unlit/Additive Colored, opaque texels: alpha clips at .01 but
            # never scales added RGB (game Metal/GLES3, single_blockers).
            if a <= 0.01:
                return None
            a = 1.0
        rgb = tuple(_fgo_fx_c32(_fgo_fx_track(t, ph, u, au)) for t in w["rgb"])
        M, pos, _ = nodes[w["node"]]
        x0, y0, x1, y1 = w["dd"]
        cxp, cyp, cw, ch = w["crop"]
        if cw <= 0 or ch <= 0 or x1 == x0 or y1 == y0:
            return None
        sx = (x1 - x0) / float(cw)
        sy = (y1 - y0) / float(ch)
        if w["flip"] in (1, 3):
            sx = -sx
        if w["flip"] in (2, 3):
            sy = -sy
        k = up * scale
        # crop pixel (px, py) (y down) -> local (x0 + px*sx, y1 - py*sy)
        # [flips: mirrored about the quad centre] -> world M*local + pos ->
        # screen (cx + k*wx, cy - k*wy).
        L00 = k * M[0][0] * sx
        L01 = -k * M[0][1] * sy
        L10 = -k * M[1][0] * sx
        L11 = k * M[1][1] * sy
        mx, my = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        scx = cx + k * (M[0][0] * mx + M[0][1] * my + pos[0])
        scy = cy - k * (M[1][0] * mx + M[1][1] * my + pos[1])
        if abs(L00 * L11 - L01 * L10) < 1e-9:
            return None      # edge-on
        if "pair_alpha" in w:
            cell = _fgo_fx_pair_crop(w["tex"][0], w["pair_alpha"], w["crop"], wrap=w["pair_wrap"])
        elif "uvr" in w:
            # uvRect beyond the texture: sampled with its wrap mode, as a 1 x 1
            # sheet with scale (w, h) and offset (x, y) (converter _build_widget)
            cell = _fgo_fx_cell(w["tex"][0], 1, 1, 0, tuple(w["uvr"]))
        else:
            cell = _fgo_fx_crop(w["tex"][0], w["crop"])
        m = Matrix([L00, L01, 0, 0, L10, L11, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
        bl = _fgo_blend_names.get(w["blend"], "normal")
        mul = w.get("mat_mul")
        if mul is None:
            d = Transform(cell, matrixtransform=m, matrixanchor=(0.5, 0.5),
                          matrixcolor=TintMatrix(Color(rgb=rgb)), alpha=a, blend=bl)
        else:
            uu = (rgb[0] * mul[0], rgb[1] * mul[1], rgb[2] * mul[2], a * mul[3])
            inner = Transform(cell, mesh=True, shader="fgo.fx_matmul", u_fgo_mul=uu)
            d = Transform(inner, matrixtransform=m, matrixanchor=(0.5, 0.5), blend=bl)
        cr = renpy.render(d, cw, ch, st, at)
        rw, rh = cr.get_size()
        if _fgo_fx_debug is not None:
            # + the quad's corners in FGO px, y down from the anchor (the
            # Unity rig's w_<t>.csv convention), NGUI vertex order.
            cs = []
            for lx, ly in ((x0, y0), (x0, y1), (x1, y1), (x1, y0)):
                cs += [scale * (M[0][0] * lx + M[0][1] * ly + pos[0]),
                       -scale * (M[1][0] * lx + M[1][1] * ly + pos[1])]
            _fgo_fx_debug.append((-1, w["path"], scx, scy, rw, rh, widget_alpha, cs))
        rv.subpixel_blit(cr, (scx - rw * 0.5, scy - rh * 0.5))
        return True

    # --- standalone mesh objects (spec "mobjs", converter _mesh_objects) -----
    # A MeshRenderer: its mesh through the GameObject's world matrix (the
    # node chain, prefab px, y up; the anchor's charaScale scales it entirely,
    # like a widget), projected orthographically (drop z, flip y), ONE Mesh2
    # per renderer per frame, triangles in index order (the accepted shaders
    # are ZWrite Off / Cull Off in the game build: no depth test, no culling).
    # Colour = texel x vertex colour (white when the mesh has none --
    # Unity-verified 2026-09-27, MOOrderRunner.cs nocol_*: a colourless mesh
    # under Mobile/Particles draws the texture unchanged) x the material
    # multiplier (mat_mul: 2 _TintColor / _customColor0) with alpha x
    # _oneValue ("one"), clamped like the LDR target (fgo.fx_meshcol). UVs
    # through the material's _MainTex_ST / scroll with the texture's wrap
    # mode (o["uv"], _fgo_fx_uvst -- the particle shaders' vertex stage).

    def _fgo_fx_mul_now(spec, x, T7, st):
        """The renderer's material multiplier at st from its animated colour (spec "mul_tr" = (factor,
        (r, g, b, a) tracks): a Legacy clip writing material._TintColor / _customColor0 into the
        property block, immediate like material._oneValue; shaders_distortion 2026-10-23)."""
        k, trs = x["mul_tr"]
        ph, u = _fgo_fx_phase(spec, T7, st)
        au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
        return tuple(k * float(_fgo_fx_track(tr, ph, u, au)) for tr in trs)

    def _fgo_fx_wipe_vol(spec, o, T7, st):
        """A WipeSheder sprite's _Volume at st (spec "wvol": the material value, or its clip track;
        a Legacy material binding writes the renderer's property block, no lag)."""
        tr = o["wvol"]
        if not isinstance(tr, tuple):
            return tr
        ph, u = _fgo_fx_phase(spec, T7, st)
        au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
        return _fgo_fx_track(tr, ph, u, au)

    def _fgo_fx_sprite_color(spec, o, T7, st):
        """A SpriteRenderer's vertex colour (m_Color as Color32, floats 0..1) at st: o["scol"] RGB
        bytes (absent = white) and o["sca"] the alpha (a constant, or a Legacy float track like
        the wipe _Volume -- the clip writes the component before the draw, no lag), alpha byte =
        floor(a * 255 + 0.5) clamped. None for a mesh object without either key."""
        if "scol" not in o and "sca" not in o:
            return None
        a = o.get("sca", 1.0)
        if isinstance(a, tuple):
            ph, u = _fgo_fx_phase(spec, T7, st)
            au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
            a = _fgo_fx_track(a, ph, u, au)
        r, g, b = o.get("scol", (255, 255, 255))
        a8 = min(255, max(0, int(math.floor(float(a) * 255.0 + 0.5))))
        return (r / 255.0, g / 255.0, b / 255.0, a8 / 255.0)

    def _fgo_fx_mobj_tris(tri, o, M, geo):
        """A mesh object's triangles, Cull Back applied when o["cull"]."""
        if not o.get("cull"):
            return tri
        m00, m01, m02 = M[0]
        m10, m11, m12 = M[1]
        # Cull Back (shaders_easy target 4, Unity-probed 2026-10-22, tools/fx_verify/shaders_easy/t4):
        # a triangle is front-facing when its projected winding is clockwise (Unity, y up) for a
        # positive-determinant node matrix; a negative determinant (mirrored node chain) flips the
        # front face with it. Zero-area triangles draw nothing.
        m22 = M[2]
        dsign = 1.0 if (m00 * (m11 * m22[2] - m12 * m22[1]) - m01 * (m10 * m22[2] - m12 * m22[0])
                        + m02 * (m10 * m22[1] - m11 * m22[0])) >= 0.0 else -1.0
        keep = []
        for j in range(0, len(tri) - 2, 3):
            a, b, c = tri[j], tri[j + 1], tri[j + 2]
            ax, ay = geo[2 * a], geo[2 * a + 1]
            # screen y is flipped (down): clockwise on screen with y up = counter-clockwise here
            cr = (geo[2 * b] - ax) * (geo[2 * c + 1] - ay) - (geo[2 * b + 1] - ay) * (geo[2 * c] - ax)
            if cr * dsign > 0.0:
                keep.extend((a, b, c))
        return keep

    def _fgo_fx_mobj_draw(rv, spec, o, nodes, cx, cy, up, scale, st, at, one, uvst, cam_rv=None, wvol=None,
                          scol=None, mt_time=None, mul_now=None):
        import renpy.gl2.gl2mesh as gm
        import renpy.gl2.gl2mesh2 as gm2
        if not _fgo_fx_mesh_layout:
            lay = gm.AttributeLayout()
            lay.add_attribute("a_tex_coord", 2)
            lay.add_attribute("a_fgo_color", 4)
            _fgo_fx_mesh_layout.append(lay)
        M, pos, _ = nodes[o["node"]]
        m = spec["meshes"][o["mesh"]]
        v, uv, c = m["v"], m["uv"], m.get("c")
        k = up * scale
        m00, m01, m02 = M[0]
        m10, m11, m12 = M[1]
        px, py = pos[0], pos[1]
        nv = len(v) // 3
        geo, att = [], []
        inv255 = 1.0 / 255.0
        dbg = [] if _fgo_fx_debug is not None else None
        for i in range(nv):
            x, y, z = v[3 * i], v[3 * i + 1], v[3 * i + 2]
            wx = m00 * x + m01 * y + m02 * z + px
            wy = m10 * x + m11 * y + m12 * z + py
            geo.append(cx + k * wx)
            geo.append(cy - k * wy)
            if dbg is not None:
                dbg.append((wx * scale, -wy * scale))
            att.extend(_fgo_fx_uv_map(uv[2 * i], uv[2 * i + 1], uvst))
            if scol is not None:
                att.extend(scol)
            elif c is None:
                att.extend((1.0, 1.0, 1.0, 1.0))
            else:
                att.extend((c[4 * i] * inv255, c[4 * i + 1] * inv255, c[4 * i + 2] * inv255, c[4 * i + 3] * inv255))
        if dbg is not None:
            _fgo_fx_debug.append((-2, o["path"], dbg))
        if mt_time is not None:
            # an mtex material (fgo.fx_mt): raw mesh uv / uv2, mesh colours x material multiplier
            mt_ = dict(o["mtex"], st0=tuple(uvst[:4]), w0=tuple(uvst[4:6]))
            mm_ = mul_now or o.get("mat_mul") or (1.0, 1.0, 1.0, 1.0)
            col_ = [att[6 * i + 2 + q] * mm_[q] for i in range(nv) for q in range(4)]
            uv1_ = m.get("uv1") or [0.0] * (2 * nv)
            rv.blit(_fgo_fx_mt_render(o["tex"][0], _fgo_fx_mt_key(mt_, mt_time, one), rv.width, rv.height, geo,
                                      list(uv), list(uv1_), col_, _fgo_fx_mobj_tris(m["t"], o, M, geo),
                                      o["blend"], st, at), (0, 0))
            return
        tri = _fgo_fx_mobj_tris(m["t"], o, M, geo)
        if not tri:
            return
        mesh = gm2.Mesh2(_fgo_fx_mesh_layout[0], nv, len(tri) // 3)
        mesh.set_geometry_data(geo)
        mesh.set_attribute_data(att)
        mesh.set_triangle_data(list(tri))
        if cam_rv is not None:
            # a picture camera's live target (FGOFXCloud._cam_view), rendered to a
            # texture of its own size and sampled bilinearly with the target's wrap
            tex = cam_rv
        else:
            asset = o["tex"][0]
            d = _fgo_fx_sheet_disp.get(asset)
            if d is None:
                d = _fgo_fx_sheet_disp[asset] = renpy.displayable("assets/fx/%s.png" % asset)
            tex = renpy.display.im.render_for_texture(d, rv.width, rv.height, st, at)
        mul = mul_now or o.get("mat_mul") or (1.0, 1.0, 1.0, 1.0)
        mul = (mul[0], mul[1], mul[2], mul[3] * one)
        mr = renpy.Render(rv.width, rv.height)
        mr.blit(tex, (0, 0))
        mr.mesh = mesh
        mr.add_shader("renpy.texture")
        if wvol is not None:
            wp = o["wipe"]
            mr.add_shader("fgo.fx_wipe")
            mr.add_uniform("u_fgo_wipe", (wp["color"][0], wp["color"][1], wp["color"][2], wp["gradation"]))
            mr.add_uniform("u_fgo_vol", float(wvol))
        else:
            mr.add_shader("fgo.fx_meshcol")
            mr.add_uniform("u_fgo_mul", tuple(mul))
        mr.add_property("blend_func", config.gl_blend_func[_fgo_blend_names.get(o["blend"], "normal")])
        mr.add_property("texture_wrap_tex0", (_FGO_FX_GL_WRAP[uvst[4]], _FGO_FX_GL_WRAP[uvst[5]]))
        if cam_rv is not None:
            # the target's own texel grid (Unity: a fixed-size RenderTexture), not the window's
            mr.add_property("gl_drawable_resolution", False)
        rv.blit(mr, (0, 0))

    def _fgo_fx_mobj_z(o, nodes):
        """World depth (prefab px) of a mesh object's renderer bounds centre:
        the mesh's local bounds centre through the world matrix."""
        M, pos, _ = nodes[o["node"]]
        b = o["bc"]
        return M[2][0] * b[0] + M[2][1] * b[1] + M[2][2] * b[2] + pos[2]

    # Renderer-list history (order_ties 2026-10-18): exact one-material particle ties of a zsort
    # group flagged "lh" follow Unity's global renderer list as it changes -- registrations
    # appended, a drained non-looping system swap-removed (last entry into its slot) in frame
    # k + 1, k = the first frame ending with its time >= duration and no particles; removals of
    # one frame in reverse list order [real bit_talk_ttn_spell, every pair decoded: 914 / 914
    # pair-frames; converter ORDER_LIST_HISTORY, notes/order_ties.md]. The drains come from the
    # port's own particles. False = the older activation / hierarchy key.
    _fgo_fx_list_history = True

    # WindZone (small_rules_3 t0, 2026-10-19) -- APPROXIMATED, user-approved 2026-10-18.
    # A directional WindZone accelerates every listening particle by main x multiplier x r(T)
    # (px / s^2 in simulation units; spec e["wind"]) while the zone is active. Unity's
    # r(T) = 1 + 11 x 0.999 x mean_i cos(2 pi f_i (T - 1/30)) of the game clock T
    # (f = 0.01 x (0.5, 0.1875, 0.025), period 8,000 s; [Unity-measured 3,600 s trace,
    # rms 2e-4 of the unit curve]). The game's uptime at a play is unknowable, so each
    # play draws T0 (the clock at the effect's start) uniformly over one period from a
    # crc32 of (effect, play number); _fgo_fx_wind_t0 pins it (comparisons with a Unity
    # capture's clock). False = the effect stays refused at conversion; this switch only
    # turns the wind OFF for comparisons.
    _fgo_fx_wind = True
    _fgo_fx_frame_wrap = True        # False = old clamp of a frameOverTime value above 1
    _fgo_fx_wind_t0 = None
    _FGO_FX_WIND_FREQ = (0.005, 0.001875, 0.00025)
    _FGO_FX_WIND_PERIOD = 8000.0
    _fgo_fx_wind_plays = {}

    def _fgo_fx_wind_r(T):
        """The pulse factor r at game time T (see above)."""
        c = sum(math.cos(2.0 * math.pi * f * (T - 1.0 / 30.0)) for f in _FGO_FX_WIND_FREQ) / 3.0
        return 1.0 + 11.0 * 0.999 * c

    def _fgo_fx_wind_start(name):
        """The game clock at this play's start: pinned, else a crc32 draw per (effect, play number)."""
        if isinstance(_fgo_fx_wind_t0, dict) and name in _fgo_fx_wind_t0:
            return float(_fgo_fx_wind_t0[name])           # per-effect pin (look harness)
        if _fgo_fx_wind_t0 is not None and not isinstance(_fgo_fx_wind_t0, dict):
            return float(_fgo_fx_wind_t0)
        n = _fgo_fx_wind_plays.get(name, 0)
        _fgo_fx_wind_plays[name] = n + 1
        h = zlib.crc32(("%s|%d|wind" % (name, n)).encode("utf-8")) & 0xffffffff
        return (h / 4294967296.0) * _FGO_FX_WIND_PERIOD

    def _fgo_fx_wind_acc(w, t0, birth):
        """Acceleration function of age (track units: world units / s^2, y down) for a wind spec w,
        the play's clock t0 and the particle's effect-clock birth: the step starting at birth + age
        (its frame) inside an activity window takes (main x multiplier / 288) x r(t0 + that time)."""
        dx, dy, dz = w["d"]
        a = w["a"] / 288.0
        wins = w["w"]
        zero = (0.0, 0.0, 0.0)

        def acc(age):
            fs = math.floor((birth + age) / _FGO_FX_FRAME + 1e-6) * _FGO_FX_FRAME
            for on, off in wins:
                if on - 1e-9 <= fs < off - 1e-9:
                    m = a * _fgo_fx_wind_r(t0 + fs)
                    return (dx * m, -dy * m, dz * m)
            return zero
        return acc

    def _fgo_fx_lh_replay(lh, kf, has_parts):
        """The renderer list (bottom first) of spec "lhist" in frame kf. has_parts(ei, j): does
        emitter ei draw anything in frame j."""
        lst = [tuple(r) for r in lh["inst"]]
        on = dict((f, [tuple(r) for r in rs]) for f, rs in lh["on"])
        off = dict((f, [tuple(r) for r in rs]) for f, rs in lh["off"])
        dur = dict((i, n) for i, n in lh["dr"])
        act = dict((r[1], 0) for r in lst if r[0] == "e")
        for f in range(kf + 1):
            rem = [r for r in off.get(f, ()) if r in lst]
            j = f - 1
            for r in lst:
                if r[0] == "e" and r not in rem and r[1] in dur:
                    a = act[r[1]]
                    if j >= a and j - a + 1 >= dur[r[1]] and not has_parts(r[1], j):
                        rem.append(r)
            for r in sorted(set(rem), key=lst.index, reverse=True):
                i = lst.index(r)
                last = lst.pop()
                if last != r:
                    lst[i] = last
            for r in on.get(f, ()):
                if r not in lst:
                    lst.append(r)
                    if r[0] == "e":
                        act[r[1]] = f
        return lst

    def _fgo_fx_zsort(sp, order, nodes, zof, aof=None, lpos=None):
        """Per-frame depth sort of the renderer groups the converter listed
        in spec["zsort"] (_plain_seq: one sortingOrder + queue, a mesh object
        in it, not all additive): far first by the bounds-centre depth, then
        the tie key (optional same-source shader rank, then material class /
        bundle rank, mesh before particles)
        [Unity-verified, see the converter]. The group keeps its slots in the
        order list. zof(ref) -> depth in prefab px, None = draws nothing."""
        order = [tuple(r) for r in order]
        for g in sp["zsort"]:
            refs = [tuple(r) for r in g["refs"]]
            slots = sorted(order.index(r) for r in refs if r in order)
            if len(slots) < 2:
                continue
            keyed = []
            for n, r in enumerate(refs):
                z = zof(r)
                k = tuple(g["k"][n])
                # registration: the current activation's start (meshes on
                # one material draw later-(re)enabled on top), then hierarchy
                t0 = aof(r) if aof is not None else 0.0
                sk = (g["sk"][n],) if _fgo_fx_mesh_shader_order and "sk" in g else ()
                if lpos is not None and g.get("lh"):
                    # the replayed renderer list decides exact ties (later entry on top)
                    keyed.append(((-round(z if z is not None else 0.0, 4),) + sk + k[:3] + (lpos(r), n), r))
                    continue
                keyed.append(((-round(z if z is not None else 0.0, 4),) + sk + k[:3] + (t0,) + k[3:] + (n,), r))
            keyed.sort(key=lambda kv: kv[0])
            for s_, (_, r) in zip(slots, keyed):
                order[s_] = r
        return order

    def _fgo_fx_tie_order(sp, order, T7, st):
        """Equal-depth NGUI ties resolved from activation times (see the
        converter's _draw_order): "enable" -> later-enabled on top,
        simultaneous in hierarchy order; "copy" (UIUnityRenderer material
        copies) -> later-enabled BELOW, simultaneous in reverse hierarchy
        order. Inactive items keep their slot (they draw nothing). A copy is
        made at the FIRST enable; the current activation is used, which is
        the same for systems activated once."""
        order = [tuple(x) for x in order]
        for tie in sp["ties"]:
            items = [tuple(x) for x in tie["items"]]
            keyed = []
            for h, it in enumerate(items):
                obj = sp["widgets"][it[1]] if it[0] == "w" else sp["emitters"][it[1]]
                win = _fgo_fx_window(obj, sp, T7, st)
                keyed.append(((win[0] if win else -1.0), h, it))
            if tie["kind"] == "enable":
                keyed.sort(key=lambda k: (k[0], k[1]))
            else:
                keyed.sort(key=lambda k: (-k[0], -k[1]))
            pos = sorted(order.index(it) for it in items)
            for p, k in zip(pos, keyed):
                order[p] = k[2]
        return order

    def _fgo_fx_live_parts(name, ei, e, wi, tau, mv=None):
        """Every accepted particle of emitter ei's activation wi that can be
        alive at tau (the cycles _draw_emitter walks)."""
        L = max(e["length"], 1e-6)
        if e["loop"]:
            start = e.get("delay", 0.0)
            k_hi = int(math.floor((tau - start) / L))
            k_lo = int(math.floor((tau - start - _fgo_fx_max_life(e)) / L))
            if _fgo_fx_fclock_scope(e):
                # Unity's clock births / removals trail their lattice twins by up to a frame
                k_lo = int(math.floor((tau - start - _fgo_fx_max_life(e) - 2 * _FGO_FX_FRAME) / L))
            if _fgo_fx_burst_frames and e.get("bsched"):
                # the float32 schedule's cycle k can fire up to a frame before / after
                # c0(k) (_fgo_fx_bsched): walk one more cycle on each side
                k_hi += 1
                k_lo -= 1
            k_lo = max(k_lo, _fgo_fx_first_cycle(e))
            if e.get("life_inf"):
                k_lo = _fgo_fx_first_cycle(e)
            ks = range(k_lo, k_hi + 1)
        else:
            ks = (0,)
        parts = []
        for k in ks:
            chunk = _fgo_fx_chunk(name, ei, e, wi, k, mv)
            ok = _fgo_fx_accepted(name, ei, e, wi, k, mv)
            parts.extend(p for p, keep in zip(chunk, ok) if keep)
        return parts

    _fgo_fx_force_bounds_units = True

    def _fgo_fx_zmid(name, ei, e, wi, tau, stop_tau, spread, unit, mv=None):
        """World depth of the centre of the renderer bounds, or None without
        live particles -- what Unity sorts equal-queue transparent renderers
        by, back to front. World-space systems: the midpoint of the live
        particles' depths; Local-space ones: the emitter (zk "e", see below)
        [Unity-verified 2026-09-26, ProbeSameQueue.cs: the particles' bounds
        centre, not the emitter position nor the particles' mean]. Size
        widens the bounds symmetrically, so it doesn't move the centre."""
        hier = e.get("scaling_mode") == 0
        u_sz = spread if hier else unit
        u_shape = unit if e.get("scaling_mode") == 1 else spread
        vol_curves = [(i, a) for i, a in enumerate(e.get("vol", ())) if a and a[0] == "curve"]
        vm = e.get("vol_m")
        # Local simulation space: Unity bounds the system ANALYTICALLY (shape
        # + farthest travel, constant), which the converter has proven
        # centred on the emitter in depth (zk "e", _local_bounds_on_emitter;
        # Unity-verified 2026-09-26, ProbeSameQueue.cs Live()). Only whether
        # anything is alive still matters (an empty system isn't drawn).
        on_emitter = e.get("zk") == "e"
        lo = hi = None
        for p in _fgo_fx_live_parts(name, ei, e, wi, tau, mv):
            birth, life = p[0], p[1]
            if stop_tau is not None and birth > stop_tau:
                continue
            age = tau - birth
            if type(birth) is _FGOFXClockBirth:
                # Unity's float32 clock: drawn from its frame, gone from the frame removing it
                if tau < birth.bk - 1e-9 or (birth.dk is not None and tau >= birth.dk - 1e-9):
                    continue
                age = min(max(age, 0.0), life)
            elif age < 0.0 or age > life or (_fgo_fx_burst_frames and tau < getattr(birth, "se", -1.0) - 1e-9):
                continue
            if on_emitter:
                if mv is not None and "anim_geo" in e and e["anim_geo"].get("local"):
                    # a moving Local-space emitter: its bounds move with it
                    return _fgo_fx_dyn_frame(mv[0], e, mv[1], mv[2] + tau)[0][2] * spread
                if _fgo_fx_order_list and "zo" in e:
                    # the analytic bounds centre's float32 offset from the emitter
                    # (converter _order_list_group, tools/fgo_fx_depth32.py)
                    if _fgo_fx_force_bounds_units and e.get("zo_world"):
                        return e.get("dz", 0.0) * spread + e["zo"] * unit / 288.0
                    return (e.get("dz", 0.0) + e["zo"]) * spread
                return e.get("dz", 0.0) * spread
            track, vz, bxy, oz = p[18], p[19], p[21], p[23]
            if track is not None:
                fi = age / _FGO_FX_CLAMP_DT
                i0 = min(int(fi), len(track) - 2)
                f = fi - i0
                dz_ = track[i0][5] + (track[i0 + 1][5] - track[i0][5]) * f
            else:
                dz_ = vz * age
                if p[25] is not None:
                    dz_ += _fgo_fx_force_at(p[25], age)[2]
                if p[26] is not None and p[26].get("nz") is not None:
                    dz_ += p[26]["nz"].at(age)[2]      # noise (approximated)
                if vol_curves:
                    u = age / life
                    li = [0.0, 0.0, 0.0]
                    for i, (_, scal, hi_k, lo_k) in vol_curves:
                        i_lo = _fgo_fx_curve_int(lo_k, u)
                        li[i] = scal * life * (i_lo + (_fgo_fx_curve_int(hi_k, u) - i_lo) * p[17])
                    dz_ += _fgo_fx_mv(vm, li)[2]
            z0 = bxy[2] if bxy is not None else e.get("dz", 0.0)
            z = z0 * spread + oz * u_shape + dz_ * u_sz
            lo = z if lo is None or z < lo else lo
            hi = z if hi is None or z > hi else hi
        return None if lo is None else 0.5 * (lo + hi)

    def _fgo_fx_ngui_cmp(a, b):
        """UIWidget.PanelCompareFunc (disassembled, libil2cpp 0x35ADB40):
        depth; then 0 for the same material, -1 when the left material is
        non-null, 1 when the right is (the instance-ID compare after it is
        unreachable). m == -1 = a null material; b None = the talk scene's
        depth-0 null-material widgets (ActionPanel/.../Stretch)."""
        da, db = a["d"], (0 if b is None else b["d"])
        if da != db:
            return -1 if da < db else 1
        ma, mb = a["m"], (-1 if b is None else b["m"])
        if ma == mb:
            return 0
        if ma != -1:
            return -1
        return 1

    def _fgo_fx_ngui_order(sp, T7, st, zmid):
        """Bottom-to-top draw order for a spec with an "ngui" replay block
        (converter _ngui_model, where every rule is cited):
          1. replay UIPanel.AddWidget over the present items, in enable order
             (activation start, then hierarchy);
          2. draw calls = runs of equal merge keys; queue = run index;
             wrappers sharing an asset material take the queue of the last
             one to register that has live particles (the last
             OnWillRenderObject write wins);
          3. sort by sortingOrder, queue, then -- particles sharing a queue --
             bounds-centre depth (far first) and registration (instantiation:
             bundle renderer order; later activations appended).
        zmid(item) -> the item's bounds-centre depth or None."""
        ng = sp["ngui"]
        present = []
        pending = []
        for it in ng["items"]:
            k, i = it["ref"]
            # "g": a UIUnityRenderer that is in the widget list but draws
            # nothing (converter _uur_resolve); its activation rides on it
            # "m": own mesh wrapper, Unity-verified 2026-09-30 UMRunner;
            # same replay rules, with the mesh's existing activation window.
            obj = it if k == "g" else (sp["widgets"][i] if k == "w" else
                                      sp["mobjs"][i] if k == "m" else sp["emitters"][i])
            win = _fgo_fx_window(obj, sp, T7, st)
            if win is not None:
                rf = it.get("rf") if _fgo_fx_first_registration else None
                first = (rf[1] + (_fgo_fx_start_len(sp, sp["owners"][0]) if rf[0] == "l" else 0.0)) if rf else -1
                first = math.ceil(first * _FGO_FX_HZ - 2e-3) / _FGO_FX_HZ
                if rf and abs(win[0] - first) < 1e-6 and st + 1e-6 < first + _FGO_FX_FRAME:
                    # First Start occurs after this frame's panel update. The
                    # renderer already draws; widgets await the next DC mesh.
                    if it["u"]:
                        pending.append((it, obj))
                    continue
                present.append((win[0], it["h"], it))
        present.sort(key=lambda x: (x[0], x[1]))
        on = dict((id(it), t0) for t0, _, it in present)
        lst = [] if ng["panel"] else [None]
        for _, _, it in present:
            if not lst:
                lst.append(it)
            elif _fgo_fx_ngui_cmp(it, lst[0]) == -1:
                lst.insert(0, it)
            else:
                j = len(lst) - 1
                while j > 0 and _fgo_fx_ngui_cmp(it, lst[j]) == -1:
                    j -= 1
                lst.insert(j + 1, it)
        lst = [x for x in lst if x is not None]
        dc = {}
        run, prev = -1, None
        for x in lst:
            if x["k"] != prev:
                run += 1
                prev = x["k"]
            dc[id(x)] = run

        def reg(x):
            # Registration = the frame (1/30 s, the game's) of the first particle after the
            # current activation ("fe", converter _first_emission); same-frame
            # ties: bundle renderer order at instantiation, else hierarchy.
            t0 = on[id(x)]
            fr = int(math.ceil((t0 + x.get("fe", 0.0)) * _FGO_FX_HZ - 1e-6))
            if x["i"] and t0 == 0.0:
                return (fr, 0, x.get("r", x["h"]))
            return (fr, 1, x["h"])

        def reg_list(x):
            # (order bundle) the renderer list: registration at the switch-on, not the first
            # particle; frame-0 Instantiate in source GameObject pathID order, then the start
            # clip's switch-ons; a switch-on in its binding's clip order, children first
            # [Unity-verified 2026-10-02, notes/order_bundle.md A.1-A.3]
            t0 = on[id(x)]
            fr = int(math.ceil(t0 * _FGO_FX_HZ - 1e-6))
            if x["pre"] and t0 == 0.0:
                return (fr, 0, x.get("gr", x["h"]), 0)
            return (fr, 1, x.get("bi", 0), x.get("po", x["h"]))
        zs = {}

        def zof(x):
            if id(x) not in zs:
                zs[id(x)] = zmid(x)
            return zs[id(x)]
        q = {}
        groups = {}
        for x in lst:
            if x["u"] and "r" in x:
                groups.setdefault(x["m"], []).append(x)
        def wo(x):
            # queue-write (OnWillRenderObject) order = the renderer list:
            # bundle order at instantiation, post-order ("po") for a clip
            # activation (converter _ngui_model, lane ngui 2026-09-27)
            t0 = on[id(x)]
            if x["i"] and t0 == 0.0:
                return (t0, 0, x.get("r", x["h"]))
            return (t0, 1, x.get("po", x["h"]))
        for g in groups.values():
            if any("lw" in x or "po" in x for x in g):
                # Local-space systems write while active, particles or not
                live = [x for x in g if x.get("lw") or zof(x) is not None] or g
                last = max(live, key=wo)
            else:
                live = [x for x in g if zof(x) is not None] or g
                last = max(live, key=reg)
            for x in g:
                q[id(x)] = dc[id(last)]
        # item D (order bundle finish 2026-10-03): a draw call is ONE MeshRenderer whose bounds hold
        # its widgets' vertices -> its depth is the midpoint of the present widgets' static planes
        # ("pz", prefab px); it sorts against plain particles of the same queue ("queue_depth") far
        # first [Unity real bundles with a draw-call replica, ODDcRunner; converter _dc_depth_decide]
        pzr = {}
        if _fgo_fx_dc_depth:
            for x in lst:
                if "pz" in x:
                    lo_hi = pzr.get(dc[id(x)])
                    pzr[dc[id(x)]] = (x["pz"], x["pz"]) if lo_hi is None else (min(lo_hi[0], x["pz"]), max(lo_hi[1], x["pz"]))
        keyed = []
        for n, x in enumerate(lst):
            if x["ref"][0] == "g":
                continue
            qq = q.get(id(x), dc[id(x)])
            if "r" in x:
                z = zof(x)
                sub = (-(z if z is not None else 0.0), reg_list(x) if (_fgo_fx_order_list and "pre" in x) else reg(x))
            elif "pz" in x and dc[id(x)] in pzr:
                lo_hi = pzr[dc[id(x)]]
                sub = (-0.5 * (lo_hi[0] + lo_hi[1]), (float(n), 0, 0))
            else:
                sub = (0.0, (float(n), 0, 0))
            # the panel's draw call i draws at queue 3001 + i (the talk effect's
            # panel base, COORDINATION Decisions 2026-09-30); a plain particle keeps
            # its own material queue (<= 3000 is below every item, as before)
            keyed.append(((x["so"], 3001 + qq) + sub, tuple(x["ref"])))
        for so, refs in ng["plain"]:
            for n, r in enumerate(refs):
                px = sp["emitters"][r[1]] if r[0] == "e" else sp["mobjs"][r[1]]
                keyed.append(((so, _fgo_fx_queue_at(px, st), -px.get("queue_depth", 0.0), (float(n), 0, 0)), tuple(r)))
        for it, obj in pending:
            z = zmid(it)
            peer = next((x for x in lst if x["u"] and x["m"] == it["m"]), None)
            queue = 3001 + q.get(id(peer), dc[id(peer)]) if peer is not None else obj.get("queue", 3000)
            keyed.append(((it["so"], queue, -(z or 0.0), (-1.0, 0, it["h"])), tuple(it["ref"])))
        if pending:
            # Temporary source queues may tie before NGUI separates the runs.
            # Same-source shader/material IDs precede the registration key.
            # The converter excludes existing visible widgets/DCs in this scope.
            byref = dict((tuple(x["ref"]), x) for x in ng["items"])
            replaced = []
            for key, ref in keyed:
                it = byref.get(ref)
                if it and ref[0] in ("e", "m"):
                    z = zmid(it)
                    key = key[:2] + (-(z or 0.0), it.get("sk", 0), it.get("mk", 0), key[3])
                else:
                    key = key[:3] + (-1, -1, key[3])
                replaced.append((key, ref))
            keyed = replaced
        keyed.sort(key=lambda kv: kv[0])
        return [r for _, r in keyed]

    def _fgo_fx_queue_at(renderer, st):
        """Last material write persists after the writer is disabled/stopped.
        Unity-verified 2026-10-01: QWRunner (three bundles, full lifecycle)
        and QWSynthetic (own-renderer dispatch and persistent last write).
        Only specs with verified constant panel/visibility intervals use it.
        """
        tr = renderer.get("queue_track")
        if tr is None:
            return renderer.get("queue", 3000)
        queue = tr["initial"]
        for t, value in tr["writes"]:
            if tr.get("unit") == "frames":
                t = t * _FGO_FX_FRAME        # the first write lands one FRAME after the instantiation
            if t > st + 1e-9:
                break
            queue = value
        return queue

    def _fgo_fx_one_at(spec, x, T7, st):
        """The renderer's _oneValue now ("one" track; "one_lag": one frame
        late, the serialized value on the first frame -- see
        _fgo_fx_param1_lag)."""
        tr = x["one"]
        if x.get("one_lag") and _fgo_fx_param1_lag:
            st = st - _FGO_FX_FRAME
            if st < 0.0:
                return _fgo_fx_track_rest(tr)
        ph, u = _fgo_fx_phase(spec, T7, st)
        au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
        return _fgo_fx_track(tr, ph, u, au)

    # OPUS puzzles B (2026-10-06): FGO_param1anim's Start / Update run in frame f only when the
    # object was active after frame f - 1's animation pass, each writing the field as the clip
    # left it in frame f - 1; the material draws the LAST write. So a renderer switched on
    # mid-effect (or switched on by the start clip at 0 while inactive in the prefab) draws its
    # material's own _oneValue (spec "one_act") in that frame, and a re-activated one the value
    # written before it was switched off [Unity-verified, play mode, exact replica:
    # Assets/ProbeRuntime/Puzzles/PZOneRunner.cs, tools/fx_verify/puzzles/b_fit.py: 56 cases x
    # 41 frames]. True = that rule; False = the activation frame draws the lagged field like every
    # other frame (the runtime before 2026-10-06).
    _fgo_fx_param1_act = True

    def _fgo_fx_one_now(spec, x, T7, st):
        """The renderer's _oneValue drawn at st: _fgo_fx_one_at, except on an activation frame
        of a "one_act" renderer (_fgo_fx_param1_act)."""
        base = _fgo_fx_one_at(spec, x, T7, st) if "one" in x else 1.0
        if not (_fgo_fx_param1_act and "one_act" in x):
            return base
        w = _fgo_fx_window(x, spec, T7, st)
        if w is None:
            return base
        f = int(math.floor(st * _FGO_FX_HZ + 1e-6))
        fon = int(round(w[0] * _FGO_FX_HZ))
        if f != fon:
            return base                       # not the frame it was switched on
        if "one_pb" in x and _fgo_fx_phase(spec, T7, st)[0] in x["one_pb"]:
            return base                       # a clip's material._oneValue (property block) decides
        rest_on = bool(x["act"]["rest"][0])
        if fon == 0 and rest_on:
            return base                       # active from the instantiation: Start ran in frame 0
        # the last frame g < fon it was active in: Start / Update in frame g + 1 wrote field(g)
        g = fon - 1
        while g >= 0 and _fgo_fx_window(x, spec, T7, g * _FGO_FX_FRAME) is None:
            g -= 1
        if g >= 0:
            return _fgo_fx_one_at(spec, x, T7, (g + 1) * _FGO_FX_FRAME) if "one" in x else 1.0
        if rest_on:
            # active at the instantiation (Start wrote the serialized value), then off from frame 0
            return (_fgo_fx_track_rest(x["one"]) if "one" in x else 1.0)
        return x["one_act"]

    def _fgo_fx_track_rest(tr):
        if isinstance(tr, (tuple, list)):
            return tr[2] if tr[0] == "A" else tr[0]
        return tr

    # Only newly admitted shared-mesh clones serialize uv.initial. Older
    # specs retain their existing first-frame UV convention.
    _fgo_fx_mesh_clone_initial_uv = True

    def _fgo_fx_uvst(spec, e, T7, st):
        """The emitter's texture (sx, sy, ox, oy, wrap_u, wrap_v) at effect
        time st (converter _uv_fields / UVSCROLL_SCRIPT):
        UVScrollAnimation: offset = Repeat(t * perSec, 1) per axis, t from when
        the effect is shown (the game uses Time.time, whose phase at spawn is
        arbitrary -- a judgement call); UVScroll: the m_Offset track one frame
        (one frame) late -- its Update runs before Legacy Animation writes
        m_Offset -- and the serialized value on the first frame; otherwise the
        material's static offset. Offsets rounded to 1e-4 (Transform cache)."""
        uv = e["uv"]
        sx, sy = uv["s"]
        if "two" in uv:
            # Unity-verified SHParamRunner / SHFamilyGrid; compiled game
            # vertex 71ffda134d / edeeb40a80: ST - t/20 * current speed.
            # A param2 Update sees the previous Animation sample; a material
            # binding is immediate. Global clock starts at effect show by
            # the user's existing UVScrollAnimation phase convention.
            t = st - (_FGO_FX_FRAME if uv.get("two_lag") else 0.0)
            if t < 0:
                vx, vy = (_fgo_fx_track_rest(q) for q in uv["two"])
            else:
                ph, u = _fgo_fx_phase(spec, T7, t)
                au = _fgo_fx_autos(spec, T7, t) if "autos" in spec else None
                vx, vy = (_fgo_fx_track(q, ph, u, au) for q in uv["two"])
            rest = dict(uv)
            del rest["two"]
            # Preserve independent ST / UVScroll before the fragment's scroll.
            a = _fgo_fx_uvst(spec, dict(e, uv=rest), T7, st)
            return (a[0], a[1], round(a[2] - st * vx / 20.0, 4),
                    round(a[3] - st * vy / 20.0, 4), a[4], a[5])
        if "st" in uv:
            # a clip curving material._MainTex_ST (converter _uv_fields):
            # scale and offset tracks, used the same frame
            ph, u = _fgo_fx_phase(spec, T7, st)
            au = _fgo_fx_autos(spec, T7, st) if "autos" in spec else None
            sx, sy, ox, oy = (_fgo_fx_track(t, ph, u, au) for t in uv["st"])
            return (round(sx, 5), round(sy, 5), round(ox, 4), round(oy, 4), uv["wrap"][0], uv["wrap"][1])
        if "anim" in uv:
            vx, vy = uv["anim"]
            ox, oy = st * vx, st * vy
            ox = min(1.0, max(0.0, ox - math.floor(ox)))
            oy = min(1.0, max(0.0, oy - math.floor(oy)))
        else:
            tox, toy = uv["o"]
            t = st - (_FGO_FX_FRAME if uv.get("lag") else 0.0)
            if t < 0.0:
                if _fgo_fx_mesh_clone_initial_uv and "initial" in uv:
                    ox, oy = uv["initial"]
                else:
                    ox, oy = _fgo_fx_track_rest(tox), _fgo_fx_track_rest(toy)
            else:
                ph, u = _fgo_fx_phase(spec, T7, t)
                au = _fgo_fx_autos(spec, T7, t) if "autos" in spec else None
                ox, oy = _fgo_fx_track(tox, ph, u, au), _fgo_fx_track(toy, ph, u, au)
        return (sx, sy, round(ox, 4), round(oy, 4), uv["wrap"][0], uv["wrap"][1])

    # (runtime_performance 2026-10-23) Performance only: True = a particle with a track skips the
    # velocity / gravity / Force / VOL-curve terms the track overwrites anyway; False = computes them
    # first, as before (the same returned numbers either way).
    _fgo_fx_disp_track_first = True

    def _fgo_fx_disp_track(track, age, lx):
        """_fgo_fx_disp for a particle with a track (+ its noise)."""
        fi = age / _FGO_FX_CLAMP_DT
        i0 = min(int(fi), len(track) - 2)
        f = fi - i0
        a0, a1 = track[i0], track[i0 + 1]
        disp_x = a0[0] + (a1[0] - a0[0]) * f
        disp_y = a0[1] + (a1[1] - a0[1]) * f
        cvx = a0[2] + (a1[2] - a0[2]) * f
        cvy = a0[3] + (a1[3] - a0[3]) * f
        cvz = a0[4] + (a1[4] - a0[4]) * f
        dzz = a0[5] + (a1[5] - a0[5]) * f
        nzs_ = lx.get("nz") if lx else None
        if nzs_ is not None:
            n_ = nzs_.at(age)
            disp_x += n_[0]
            disp_y += n_[1]
            dzz += n_[2]
            cvx += n_[3]
            cvy += n_[4]
            cvz += n_[5]
        return disp_x, disp_y, dzz, cvx, cvy, cvz

    def _fgo_fx_disp(e, p, age, g_u, vol_curves, vm):
        """A particle's displacement and current velocity at `age`: (disp_x,
        disp_y, dzz, cvx, cvy, cvz), world units, screen-oriented (y down);
        drawn at ex + ox * u_shape + disp * u_sz. Pulled out of
        FGOFXCloud._draw_emitter unchanged (2026-09-30) so a sub-emitter can
        read its parent particle's path (_fgo_fx_sub_anchor)."""
        (birth, life, ox, oy, vx, vy, vcx, vcy, grav, size, size_y, rot0, spin,
         tint_rgb, tint_a, frame, size_mix, vel_mix, track, vz, row_base, bxy, tilt, oz, mx, fz, lx) = p
        if track is not None and _fgo_fx_disp_track_first:
            # a track replaces every value computed below (only the noise adds on top)
            return _fgo_fx_disp_track(track, age, lx)
        u = age / life
        # Velocity-driven displacement (world units), screen-oriented
        # (y down). startSpeed (vx, vy) and constant VOL (vcx, vcy) were
        # projected at spawn; gravity is world -Y whatever the emitter's
        # rotation [Unity-verified].
        disp_x = (vx + vcx) * age
        if _fgo_fx_grav_per_frame and grav and birth < -1e-9 and _fgo_fx_prewarm_unified and _fgo_fx_pw_nonproc(e):
            # a non-procedural prewarm steps gravity semi-implicitly (_fgo_fx_pw_k_stepped)
            disp_y = (vy + vcy) * age + grav * g_u * _FGO_FX_GRAVITY * _fgo_fx_pw_k_stepped(
                _fgo_fx_max_life(e), birth, age)
        elif _fgo_fx_grav_per_frame and grav and birth < -1e-9 and _fgo_fx_prewarm_exact_accel:
            # prewarmed particle: exact over its prewarm part (_fgo_fx_pw_k)
            disp_y = (vy + vcy) * age + grav * g_u * _FGO_FX_GRAVITY * _fgo_fx_pw_k(-birth, age)
        elif _fgo_fx_grav_per_frame and grav:
            q_ = birth / _FGO_FX_FRAME
            q_ -= math.floor(q_)
            d_ = _FGO_FX_FRAME * (1.0 - q_) if q_ > 1e-6 else _FGO_FX_FRAME
            k_ = age * age if age < d_ else (age * age + d_ * d_ + (age - d_) * _FGO_FX_FRAME)
            disp_y = (vy + vcy) * age + 0.5 * grav * g_u * _FGO_FX_GRAVITY * k_
        else:
            disp_y = (vy + vcy) * age + 0.5 * grav * g_u * _FGO_FX_GRAVITY * age * age
        cvx = vx + vcx
        cvy = vy + vcy + grav * g_u * _FGO_FX_GRAVITY * age
        cvz = vz                 # depth: never drawn, but it foreshortens stretch
        dzz = vz * age           # depth displacement (Local-space J(t) only)
        if "grav_cycle" in e:
            gd_, gv_ = _fgo_fx_sv_gravity(e, birth, age, lx["gm"])
            disp_y += gd_ * g_u
            cvy += gv_ * g_u
        if lx and "ga" in lx:
            mv_, mix_ = lx["ga"]
            gd_, gv_ = _fgo_fx_sv_gravity(e, birth, age, mix_, mv_)
            disp_y += gd_ * g_u
            cvy += gv_ * g_u
        if vol_curves:
            vt_ = lx.get("vt") if lx else None
            if vt_ is not None:
                # the frame sum (_fgo_fx_vol_table, switch _fgo_fx_vol_frames)
                li, lv = _fgo_fx_vol_at(vt_, age)
            else:
                ss_u = _fgo_fx_sizespeed_u(age, birth, life) if "size_speed" in e else u
                # Curves are velocities, so displacement is their integral.
                li = [0.0, 0.0, 0.0]
                lv = [0.0, 0.0, 0.0]
                for i, (_, scal, hi_k, lo_k) in vol_curves:
                    i_lo = _fgo_fx_curve_int(lo_k, u)
                    v_lo = _fgo_curve_at(lo_k, ss_u)
                    m_ = vel_mix[i] if type(vel_mix) is tuple else vel_mix
                    li[i] = scal * life * (i_lo + (_fgo_fx_curve_int(hi_k, u) - i_lo) * m_)
                    lv[i] = scal * (v_lo + (_fgo_curve_at(hi_k, ss_u) - v_lo) * m_)
            wi = _fgo_fx_mv(vm, li)
            wv = _fgo_fx_mv(vm, lv)
            disp_x += wi[0]
            disp_y -= wi[1]
            dzz += wi[2]
            cvx += wv[0]
            cvy -= wv[1]
            cvz += wv[2]
        if fz is not None:
            # ForceOverLifetime, integrated per frame (never with a
            # clamp track: the converter refuses force + clamp)
            fdx, fdy, fdz, fvx, fvy, fvz = _fgo_fx_force_at(fz, age)
            disp_x += fdx
            disp_y += fdy
            dzz += fdz
            cvx += fvx
            cvy += fvy
            cvz += fvz
        if track is not None:
            return _fgo_fx_disp_track(track, age, lx)
        nzs_ = lx.get("nz") if lx else None
        if nzs_ is not None:
            # noise [approximated, statistically matched 2026-09-28]:
            # its displacement and velocity (stretch direction) on top
            n_ = nzs_.at(age)
            disp_x += n_[0]
            disp_y += n_[1]
            dzz += n_[2]
            cvx += n_[3]
            cvy += n_[4]
            cvz += n_[5]
        return disp_x, disp_y, dzz, cvx, cvy, cvz

    # --- sub-emitters (2026-09-30, prompts/SUB_EMITTERS.md) ---------------------
    # A Birth sub-emitter child (spec e["sub"], converter _sub_fields) never
    # emits on its own: every live parent particle runs the child's emission
    # on its own clock and spawns the child's particles where it is. All rules
    # Unity-verified 2026-09-30 in play mode (Assets/ProbeRuntime/SubEmit/
    # SESyn*Runner.cs -> probe_subemit_syn*/; fits tools/fx_verify/subemit/
    # timing_fit.py, cur_fit.py, ori_fit.py; notes/sub_emitters.md).

    _fgo_fx_sub_cache = {}

    def _fgo_fx_sub_births(e, tp, lp, rng):
        """Window-clock birth times of child e's particles for ONE parent
        particle born at tp with lifetime lp [timing_fit.py: 15 cases, 503
        births within 1.2e-6 s]: rate births at tp + delay + n / rate (n >= 1),
        across loop cycles when the child loops; bursts at tp + delay + time,
        the FIRST cycle only even when the child loops; a one-shot child emits
        nothing in its final frame (the global frame in which its clock
        reaches the duration, as for any one-shot system); nothing at or after
        the parent's death. Burst counts draw from rng (per parent)."""
        out = []
        delay = e.get("delay", 0.0)
        dur = max(e["length"], 1e-6)
        lim = tp + lp
        if _fgo_fx_sub_death_frame:
            parent_cut = (math.ceil(lim / _FGO_FX_FRAME - 1e-6) - 1) * _FGO_FX_FRAME
        else:
            parent_cut = lim
        cut_t = None
        if not e["loop"]:
            f_last = int(math.ceil((tp + delay + dur) / _FGO_FX_FRAME - 1e-6)) - 1
            cut_t = max(f_last * _FGO_FX_FRAME, tp)
        rate = float(e.get("rate", 0) or 0)
        if rate > 0:
            n = 1
            while True:
                t = tp + delay + n / rate
                if t >= lim - 1e-9 or t > parent_cut + 1e-9 or (cut_t is not None and t > cut_t + 1e-9):
                    break
                out.append(t)
                n += 1
        snap = _fgo_fx_sub_onset and e["sub"].get("snap")
        for bt, cnt in e.get("bursts", ()):
            n = int(round(rng.uniform(cnt[0], cnt[1]))) if isinstance(cnt, (tuple, list)) else int(round(cnt))
            t = tp + delay + bt
            if snap:
                # Unity: a delayed child burst at time 0 fires at the START of the frame that
                # contains parent birth + delay (full-step age); exact ties never occur (random tp).
                t = math.floor(t / _FGO_FX_FRAME + 1e-6) * _FGO_FX_FRAME
            if bt >= dur or t >= lim - 1e-9 or t > parent_cut + 1e-9 or (cut_t is not None and t > cut_t + 1e-9):
                continue
            out.extend([t] * n)
        out.sort()
        return out

    def _fgo_fx_sub_look(n):
        """The child's shape frame turned by the parent's velocity n (child-
        local axes): LookRotation(n, up = +Z) as a 3x3 (columns x, y, n)
        [Unity-verified 2026-09-30, ori_fit.py: exact for every velocity in
        the local XY plane -- the only case the converter lets through --
        and identity (None here) for a zero velocity]."""
        m = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        if m < 1e-12:
            return None
        n = (n[0] / m, n[1] / m, n[2] / m)
        mx = math.sqrt(n[0] * n[0] + n[1] * n[1])
        if mx < 1e-12:
            return None
        x = (-n[1] / mx, n[0] / mx, 0.0)
        y = (n[1] * x[2] - n[2] * x[1], n[2] * x[0] - n[0] * x[2], n[0] * x[1] - n[1] * x[0])
        return ((x[0], y[0], n[0]), (x[1], y[1], n[1]), (x[2], y[2], n[2]))

    def _fgo_fx_sub_anchor(ep, p, t, g_u):
        """Parent particle p at a child's birth t: (disp_x, disp_y) to add to
        the parent's spawn offset, and the parent's world velocity (y up) at
        the END of that frame. Position: linear between the parent's
        positions at the frame's start (or its own birth, if later) and end;
        a child born at its parent's birth sits on the parent's spawn point
        [Unity-verified 2026-09-30, cur_fit.py: gravity / Force / orbit /
        Limit Velocity parents, 9.6e-5 = the probe's cone radius; the frame
        start / end alone are 1.7e-2 .. 1e-1 off]. Velocity at the frame end
        [direction 4e-8; the start / lerp 5e-2 .. 1.6e-1 off; a parent born
        at the frame start orients a child born with it by the velocity
        after its first step: probe_subemit_syn3 force_v0]."""
        vol_curves = [(i, a) for i, a in enumerate(ep.get("vol", ())) if a and a[0] == "curve"]
        vm = ep.get("vol_m")
        tp = p[0]
        F = _FGO_FX_FRAME
        if t <= tp + 1e-9:
            d0 = _fgo_fx_disp(ep, p, 0.0, g_u, vol_curves, vm)
            te = (math.floor(tp / F + 1e-6) + 1) * F
            dx, dy = d0[0], d0[1]
        else:
            j = int(math.ceil(t / F - 1e-6)) - 1
            te = (j + 1) * F
            ts = max(j * F, tp)
            a = (t - ts) / (te - ts) if te > ts + 1e-12 else 1.0
            d0 = _fgo_fx_disp(ep, p, ts - tp, g_u, vol_curves, vm)
            d1 = _fgo_fx_disp(ep, p, te - tp, g_u, vol_curves, vm)
            dx = d0[0] + (d1[0] - d0[0]) * a
            dy = d0[1] + (d1[1] - d0[1]) * a
        de = _fgo_fx_disp(ep, p, te - tp, g_u, vol_curves, vm)
        return dx, dy, (de[3], -de[4], de[5])

    def _fgo_fx_sub_gu(e, gsc):
        """(chunk key gu, gravity factor g_u) as _draw_emitter derives them for
        emitter e at a show scale gsc = unit / spread."""
        gu_ = round(gsc, 6) if ((e.get("clamp_gu") or e.get("speedmod_gu")) and _fgo_fx_grav_units and gsc) else None
        g_u = gsc if (e.get("scaling_mode") == 0 and _fgo_fx_grav_units) else 1.0
        return gu_, g_u

    def _fgo_fx_sub_cycle(name, ci, e, wi, k, sp, pcut, gsc):
        """The child particles of the parent particles born in the parent's
        emission cycle k (accepted under the parent's maxParticles, born
        before the parent's stop pcut). Seeds: crc32 of (effect, child,
        window, cycle, parent index) -- new keys (_fgo_fx_chunk sub=)."""
        key = (name, ci, wi, k, None if pcut is None else round(pcut, 6), round(gsc, 6), bool(_fgo_fx_sub_death_frame))
        got = _fgo_fx_sub_cache.get(key)
        if got is not None:
            return got
        if len(_fgo_fx_sub_cache) > 2000:
            _fgo_fx_sub_cache.clear()
        sub = e["sub"]
        pi = sub["p"]
        ep = sp["emitters"][pi]
        gu_p, g_u_p = _fgo_fx_sub_gu(ep, gsc)
        gu_c, g_u_c = _fgo_fx_sub_gu(e, gsc)
        chunk = _fgo_fx_chunk(name, pi, ep, wi, k, None, gu_p)
        ok = _fgo_fx_accepted(name, pi, ep, wi, k)
        A = e["geo"]["A"]
        Ri = sub.get("Ri")
        out = []
        for j, (p, keep) in enumerate(zip(chunk, ok)):
            if not keep or (pcut is not None and p[0] > pcut):
                continue
            rng_b = random.Random(zlib.crc32(("%s|%d|%s|%d|%d|b" % (name, ci, wi, k, j)).encode("utf-8")) & 0xffffffff)
            births = _fgo_fx_sub_births(e, p[0], p[1], rng_b)
            if not births:
                continue
            geos, pas = [], []
            for t in births:
                dx, dy, vw = _fgo_fx_sub_anchor(ep, p, t, g_u_p)
                g = e["geo"]
                if sub.get("orient"):
                    nl = _fgo_fx_mv(Ri, vw) if Ri is not None else vw
                    B = _fgo_fx_sub_look(nl)
                    if B is not None:
                        if "Rs" in sub:
                            # A = M Rs (shape rotation folded in): M B Rs
                            M_, Rs_ = sub["M"], sub["Rs"]
                            MB = tuple(tuple(sum(M_[r][m] * B[m][c] for m in range(3)) for c in range(3))
                                       for r in range(3))
                            g = dict(g, A=tuple(tuple(sum(MB[r][m] * Rs_[m][c] for m in range(3)) for c in range(3))
                                                for r in range(3)))
                        else:
                            g = dict(g, A=tuple(tuple(sum(A[r][m] * B[m][c] for m in range(3)) for c in range(3))
                                                for r in range(3)))
                geos.append(g)
                pas.append((p[2], p[3], dx, dy))
            out.extend(_fgo_fx_chunk(name, ci, e, wi, k, None, gu_c,
                                     sub={"j": j, "births": births, "geo": geos, "pa": pas,
                                          "key": key[4:]}))
        _fgo_fx_sub_cache[key] = out
        return out

    _fgo_fx_sub_state = {}

    def _fgo_fx_sub_parts(name, ci, e, wi, sp, tau, pcut, gsc):
        """Every child particle that can be alive at tau. maxParticles is
        shared by all parents and applied in birth order: a birth is DROPPED
        while the child system is full [Unity-verified 2026-09-30,
        timing_fit.py maxp15 / maxp15_rate; real helloween01's fire holds at
        its cap of 10]. Under a looping parent the children of successive
        parent cycles interleave in time, so the cap runs over one merged,
        time-ordered stream: cycle k's parents are born at or after the cycle
        start c0(k), so once the cycles up to k_hi are merged every birth
        before c0(k_hi + 1) is final. Incremental and cached per (effect,
        child, window, parent stop, scale); a request for an earlier time
        than the state reached replays from the first cycle (deterministic)."""
        ep = sp["emitters"][e["sub"]["p"]]
        mlc = _fgo_fx_max_life(e)
        if ep["loop"]:
            Lp = max(ep["length"], 1e-6)
            s0 = ep.get("delay", 0.0)
            k0 = _fgo_fx_first_cycle(ep)
            k_hi = int(math.floor((tau - s0) / Lp))
            k_lo = max(int(math.floor((tau - s0 - _fgo_fx_max_life(ep) - mlc) / Lp)), k0)
        else:
            Lp, s0, k0, k_hi, k_lo = None, 0.0, 0, 0, 0
        if not e["sub"].get("cap"):
            parts = []
            for k in range(k_lo, k_hi + 1):
                parts.extend(_fgo_fx_sub_cycle(name, ci, e, wi, k, sp, pcut, gsc))
            return parts
        key = (name, ci, wi, None if pcut is None else round(pcut, 6), round(gsc, 6), bool(_fgo_fx_sub_death_frame))
        st = _fgo_fx_sub_state.get(key)
        if st is not None and k_hi < st["k_hi"]:
            st = None                         # an earlier time: replay
        if st is None:
            if len(_fgo_fx_sub_state) > 200:
                _fgo_fx_sub_state.clear()
            st = {"k_next": k0, "k_hi": k0 - 1, "pend": [], "heap": [], "acc": []}
            _fgo_fx_sub_state[key] = st
        if k_hi > st["k_hi"]:
            for k in range(st["k_next"], k_hi + 1):
                st["pend"].extend(_fgo_fx_sub_cycle(name, ci, e, wi, k, sp, pcut, gsc))
            st["k_next"] = k_hi + 1
            st["k_hi"] = k_hi
            st["pend"].sort(key=lambda q: q[0])
            fin = (s0 + (k_hi + 1) * Lp) if Lp is not None else float("inf")
            heap, acc, keep = st["heap"], st["acc"], []
            for q in st["pend"]:
                if q[0] >= fin - 1e-9:
                    keep.append(q)
                    continue
                while heap and heap[0] <= q[0] + 1e-9:
                    heapq.heappop(heap)
                if len(heap) < e["maxp"]:
                    heapq.heappush(heap, q[0] + q[1])
                    acc.append(q)
            st["pend"] = keep
            if Lp is not None:
                # nothing born before the current cycle minus a child's life
                # can be alive at any tau this state still answers
                lo = s0 + k_hi * Lp - mlc - 1e-6
                st["acc"] = [q for q in acc if q[0] + q[1] >= lo]
        return st["acc"]

    def _fgo_fx_delay_spec(name, spec):
        """New independent system channel; old particle draws are untouched.
        Draw once per show/emitter, held through every loop and activation.
        Phase-zero curve endpoints were already reduced by the converter.
        Like existing particle populations, the show is deterministic by name;
        verification harness names include its independent seed suffix.
        """
        if not any("delay_range" in e for e in spec["emitters"]):
            return spec
        sp = dict(spec)
        sp["emitters"] = []
        for i, e in enumerate(spec["emitters"]):
            if "delay_range" in e:
                e = dict(e)
                seed = zlib.crc32(("%s|%d|startDelay" % (name, i)).encode("utf-8")) & 0xffffffff
                lo, hi = e["delay_range"]
                e["delay"] = _fgo_fx_f32(random.Random(seed).uniform(lo, hi))
            sp["emitters"].append(e)
        return sp

    def _vn_fx_parent_nodes(nodes, root):
        if root is None:
            return nodes
        return [(_fgo_fx_m3mul(root, M), _fgo_fx_mv(root, p), _fgo_fx_m3mul(root, R))
                for M, p, R in nodes]

    class FGOFXCloud(renpy.Displayable):
        """Plays one converted effect (fgo_fx_defs[name]) at an FGO-px
        centre-origin offset (x, y), with `scale` = the anchor's charaScale
        (shape spread only) and `layer13` choosing the maxParticleSize
        reference. Not cached across shows: stop_at is per-instance."""

        def __init__(self, name, x=0.0, y=0.0, scale=1.0, layer13=True, anchor="effect", **kw):
            super(FGOFXCloud, self).__init__(**kw)
            self.name = name
            self.spec = _fgo_fx_delay_spec(name, fgo_fx_defs[name])
            self.x = x
            self.y = y
            self.scale = scale
            if "cam_scale" in self.spec and scale != self.spec["cam_scale"]:
                raise ValueError("Picture-camera frustum proof requires scale %s" % self.spec["cam_scale"])
            self.layer13 = layer13
            self.anchor = anchor
            self.stop_at = None
            self.last_st = 0.0
            self.max_life = max([_fgo_fx_max_life(e) for e in self.spec["emitters"]] or [0.0])
            # the game clock at this play's start (WindZone effects only)
            self.wind_t0 = (_fgo_fx_wind_start(name) if any("wind" in e for e in self.spec["emitters"])
                            else None)
            # the shader clock at this play's start (mtex materials only)
            self.shader_t0 = (_fgo_fx_shader_start(name) if any("mtex" in e for e in
                              self.spec["emitters"] + self.spec.get("mobjs", [])) else 0.0)

        def done_time(self):
            return _fgo_fx_done_time(self.spec, self.stop_at)

        def render(self, width, height, st, at):
            _fgo_fx_sync_frame_hz()
            st_real = st
            lead = 0.0
            if _fgo_fx_display_30:
                # the latest frame boundary of the effect's own clock (an epsilon keeps a render at
                # exactly k / hz on frame k: 0.1, 0.3, 0.6 ... are multiples of 1/30 within float noise)
                st = math.floor(st * _FGO_FX_HZ + 1e-6) * _FGO_FX_FRAME
                if _fgo_fx_display_lead:
                    lead = _FGO_FX_FRAME
            self.last_st = st
            W, H = int(width), int(height)
            self._rwh = (W, H)           # (_fgo_fx_big_crop's window)
            rv = renpy.Render(W, H)
            sp = self.spec
            done = self.done_time()
            if done is not None and st > done + 0.1:
                return rv  # finished: draw nothing, stop redrawing
            owners_t7 = [_fgo_fx_t7(sp, i, self.stop_at if o["root"] else None)
                         for i, o in enumerate(sp["owners"])]
            T7 = owners_t7[0]
            destroy = _fgo_fx_destroy_time(sp, 0, self.stop_at)
            pdestroy = _fgo_fx_particle_destroy(sp, self.stop_at)
            infinite_destroy = pdestroy if (_fgo_fx_destroy_cut_all and sp.get("destroy_clock") and pdestroy is not None) else destroy

            cams = sp.get("cams")
            cam_imgs = None
            if cams:
                # Picture cameras (OPUS_CAMERA_WRAP): each renders what its culling
                # mask sees into its own target first (game: camera depth order,
                # before ActionCamera), then the scene draws, the camera-image mesh
                # objects sampling those targets.
                cam_imgs = [self._cam_view(c, st, at, lead, owners_t7, T7, destroy, pdestroy, infinite_destroy)
                            for c in cams]
                if _fgo_fx_cam_only is not None and _fgo_fx_cam_only < len(cam_imgs):
                    # diagnostic: the camera's target alone, texel-for-pixel at the top left
                    rv.blit(cam_imgs[_fgo_fx_cam_only], (0, 0))
                    return rv
            self._draw_view(rv, W, H, st, at, lead, owners_t7, T7, destroy, pdestroy, infinite_destroy,
                            None, cam_imgs)

            if _fgo_fx_display_30:
                # the next frame boundary of the effect's clock
                nxt = (math.floor(st_real * _FGO_FX_HZ + 1e-6) + 1) * _FGO_FX_FRAME
                renpy.redraw(self, max(0.002, nxt - st_real))
            else:
                renpy.redraw(self, 1.0 / fgo_flash_fps)
            return rv

        def _draw_view(self, rv, W, H, st, at, lead, owners_t7, T7, destroy, pdestroy, infinite_destroy,
                       view=None, cam_imgs=None):
            """Draws the effect's items into rv: the game screen (view None, items
            the ActionCamera's layer-13 mask sees), or one picture camera's view."""
            sp = self.spec
            if view is None:
                up = W / float(getattr(self, "native_width", fgo_native_width))
                spread = up * self.scale
                unit = fgo_px_per_unit * up
                cx = W * 0.5 + self.x * up
                cy = H * 0.5 + self.y * up
                frame_ref = (FGO_LAYER13_REF_FGOPX * up) if self.layer13 else getattr(self, "frame_width", W)
                keep = None
                hide = sp.get("cam_hide")
                hide = set((k, i) for k, i in hide) if hide else None
            else:
                # a picture camera's own orthographic view: identity rotation, its
                # position at the target's centre, the target's texels over
                # 2 x size world units (288 prefab px each at charaScale 1).
                # Perspective: the same frame at the reference depth d0 (world
                # units); _draw_emitter rescales each particle by d0 / depth.
                if view.get("fov") is not None:
                    up = H * 0.5 / math.tan(math.radians(view["fov"]) * 0.5) / (fgo_px_per_unit * view["d0"])
                else:
                    up = H / (2.0 * fgo_px_per_unit * view["size"])
                spread = up * self.scale
                unit = fgo_px_per_unit * up
                cx = W * 0.5 - view["p"][0] * spread
                cy = H * 0.5 + view["p"][1] * spread
                frame_ref = W           # maxParticleSize: the camera's own viewport
                keep = set((k, i) for k, i in view["items"])
                hide = None

            order = sp.get("order")
            if order is None:
                if _fgo_fx_mixed_list and sp.get("dorder"):
                    # plain alpha / additive systems at one sortingOrder + queue: far first by
                    # bounds depth, exact ties by the global renderer list (converter
                    # _plain_mixed_decide, diag2 Part 2)
                    order = [("e", i) for i in sp["dorder"]]
                else:
                    order = [("e", i) for i in range(len(sp["emitters"]))]
            elif sp.get("ngui"):
                name_ = self.name
                stop_ = self.stop_at

                def zmid(it):
                    if it["ref"][0] == "m":
                        ph_, u_ = _fgo_fx_phase(sp, T7, st)
                        au_ = _fgo_fx_autos(sp, T7, st) if "autos" in sp else None
                        return _fgo_fx_mobj_z(sp["mobjs"][it["ref"][1]], _fgo_fx_nodes(sp, ph_, u_, au_)) * spread
                    ei_ = it["ref"][1]
                    e_ = sp["emitters"][ei_]
                    w_ = _fgo_fx_window(e_, sp, T7, st)
                    if w_ is None:
                        return None
                    cut_ = owners_t7[e_["plist"]] if "plist" in e_ else None
                    if destroy is not None and (cut_ is None or destroy < cut_):
                        cut_ = destroy
                    ctau = (cut_ - w_[0]) if (cut_ is not None and w_[0] < cut_) else None
                    mv_ = (sp, T7, w_[0]) if ("anim_geo" in e_ or "rate_anim" in e_ or "loop_anim" in e_ or "emis_anim" in e_ or "radius_anim" in e_ or "sm_anim" in e_ or "start_anim" in e_ or "burst_anim" in e_) else None
                    zz_ = _fgo_fx_zmid(name_, ei_, e_, w_[1], st - w_[0] + lead, ctau, spread, unit, mv_)
                    if zz_ is not None and _fgo_fx_order_list and e_.get("sort_fudge"):
                        # the renderer's sortingFudge adds to its sorting distance, world units
                        # (288 prefab px; MOOrderRunner fudge_*, the _plain_seq / zsort rule) --
                        # also between the members of one NGUI shared-material group
                        # [order bundle: real bit_talk_gouka, fudge -100 / -2000]
                        zz_ = zz_ + 288.0 * e_["sort_fudge"] * spread
                    return zz_
                order = _fgo_fx_ngui_order(sp, T7, st, zmid)
            elif sp.get("ties"):
                order = _fgo_fx_tie_order(sp, order, T7, st)
            nodes = None
            if "mobjs" in sp:
                ph, u = _fgo_fx_phase(sp, T7, st)
                au = _fgo_fx_autos(sp, T7, st) if "autos" in sp else None
                nodes = _fgo_fx_nodes(sp, ph, u, au)
            if sp.get("zsort"):
                name_z, stop_z = self.name, self.stop_at

                def zof(r):
                    if r[0] == "m":
                        o_ = sp["mobjs"][r[1]]
                        if _fgo_fx_window(o_, sp, T7, st) is None:
                            return None
                        return _fgo_fx_mobj_z(o_, nodes)
                    e_ = sp["emitters"][r[1]]
                    w_ = _fgo_fx_window(e_, sp, T7, st)
                    if w_ is None:
                        return None
                    cut_ = owners_t7[e_["plist"]] if "plist" in e_ else None
                    if destroy is not None and (cut_ is None or destroy < cut_):
                        cut_ = destroy
                    ctau = (cut_ - w_[0]) if (cut_ is not None and w_[0] < cut_) else None
                    mv_ = (sp, T7, w_[0]) if ("anim_geo" in e_ or "rate_anim" in e_ or "loop_anim" in e_ or "emis_anim" in e_ or "radius_anim" in e_ or "sm_anim" in e_ or "start_anim" in e_ or "burst_anim" in e_) else None
                    zz = _fgo_fx_zmid(name_z, r[1], e_, w_[1], st - w_[0] + lead, ctau, spread, unit, mv_)
                    # + the renderer's sortingFudge (world units; 288 prefab
                    # px at charaScale 1 -- converter _plain_seq, MOOrderRunner
                    # fudge_*)
                    return None if zz is None else zz / spread + 288.0 * e_.get("sort_fudge", 0.0)
                def aof(r):
                    o_ = sp["mobjs"][r[1]] if r[0] == "m" else sp["emitters"][r[1]]
                    w_ = _fgo_fx_window(o_, sp, T7, st)
                    return round(w_[0], 4) if (w_ is not None and r[0] == "m") else 0.0
                lpos = None
                if _fgo_fx_list_history and sp.get("lhist"):
                    lh_cache = self.__dict__.setdefault("_lh_cache", {})

                    def has_parts(ei, j):
                        key = (ei, j, stop_z)
                        if key not in lh_cache:
                            e_ = sp["emitters"][ei]
                            st_j = j / _FGO_FX_HZ
                            w_ = _fgo_fx_window(e_, sp, T7, st_j)
                            if w_ is None:
                                lh_cache[key] = False
                            else:
                                cut_ = owners_t7[e_["plist"]] if "plist" in e_ else None
                                if destroy is not None and (cut_ is None or destroy < cut_):
                                    cut_ = destroy
                                ctau = (cut_ - w_[0]) if (cut_ is not None and w_[0] < cut_) else None
                                lh_cache[key] = _fgo_fx_zmid(name_z, ei, e_, w_[1], st_j - w_[0] + lead, ctau,
                                                             spread, unit) is not None
                        return lh_cache[key]
                    kf = int(math.floor(st * _FGO_FX_HZ + 1e-6))
                    lst_ = _fgo_fx_lh_replay(sp["lhist"], kf, has_parts)
                    lidx = dict((r, i) for i, r in enumerate(lst_))

                    def lpos(r):
                        return lidx.get(tuple(r), 10 ** 6)
                order = _fgo_fx_zsort(sp, order, nodes, zof, aof, lpos)
            for kind, idx in order:
                if _fgo_fx_only_mobjs and kind != "m":
                    continue
                if (keep is not None and (kind, idx) not in keep) or (hide is not None and (kind, idx) in hide):
                    continue
                if kind == "m":
                    o = sp["mobjs"][idx]
                    if _fgo_fx_window(o, sp, T7, st) is None:
                        continue
                    ds = [_fgo_fx_destroy_time(sp, i, self.stop_at) for i in o.get("owners", (0,))]
                    if any(d is not None and st >= d for d in ds):
                        continue       # its GameObject has been destroyed
                    one = 1.0
                    if "one" in o or "one_act" in o:
                        one = _fgo_fx_one_now(sp, o, T7, st)
                        if one <= 0.0:
                            continue
                    _fgo_fx_mobj_draw(rv, sp, o, _vn_fx_parent_nodes(nodes, getattr(self, "root_matrix", None) if view is None else None), cx, cy, up, self.scale, st, at, one,
                                      _fgo_fx_uvst(sp, o, T7, st),
                                      cam_imgs[o["cam"]] if "cam" in o else None,
                                      _fgo_fx_wipe_vol(sp, o, T7, st) if "wipe" in o else None,
                                      _fgo_fx_sprite_color(sp, o, T7, st),
                                      self.shader_t0 + st if "mtex" in o else None,
                                      _fgo_fx_mul_now(sp, o, T7, st) if "mul_tr" in o else None)
                    continue
                if kind == "w":
                    w = sp["widgets"][idx]
                    if _fgo_fx_window(w, sp, T7, st) is None:
                        continue
                    ds = [_fgo_fx_destroy_time(sp, i, self.stop_at) for i in w.get("owners", (0,))]
                    if any(d is not None and st >= d for d in ds):
                        continue       # its GameObject has been destroyed
                    if nodes is None:
                        ph, u = _fgo_fx_phase(sp, T7, st)
                        au = _fgo_fx_autos(sp, T7, st) if "autos" in sp else None
                        nodes = _fgo_fx_nodes(sp, ph, u, au)
                    _fgo_fx_widget_draw(rv, sp, w, _vn_fx_parent_nodes(nodes, getattr(self, "root_matrix", None) if view is None else None), ph, u, cx, cy, up, self.scale, st, at, au)
                    continue
                ei = idx
                e = sp["emitters"][ei]
                win = _fgo_fx_window(e, sp, T7, st)
                if win is None:
                    continue
                on, wkey = win
                # particlelist emitters stop emitting when their owner enters
                # _end -- only if this activation was already running by then.
                cut = owners_t7[e["plist"]] if "plist" in e else None
                # Nothing emits once the root has Destroy()ed the effect
                # (CommonEffectComponent.Update, decompiled). Live particles
                # still finish (known approximation 16). Matters for systems
                # in no particlelist that are still active then
                # (bit_talk_10*'s ef_fuzz01); before 2026-09-25 they emitted
                # forever.
                if destroy is not None and (cut is None or destroy < cut):
                    cut = destroy
                cut_tau = (cut - on) if (cut is not None and on < cut) else None
                mv = (sp, T7, on) if ("anim_geo" in e or "rate_anim" in e or "loop_anim" in e or "emis_anim" in e or "radius_anim" in e or "sm_anim" in e or "start_anim" in e or "burst_anim" in e) else None
                one = 1.0
                if "one" in e or "one_act" in e:
                    # The renderer's material _oneValue now (FGO_param1anim /
                    # a material clip; one uniform for all its particles):
                    # the FGO_EF shaders multiply alpha by it.
                    one = _fgo_fx_one_now(sp, e, T7, st)
                    if one <= 0.0:
                        continue
                uvst = _fgo_fx_uvst(sp, e, T7, st) if "uv" in e else None
                pcut_tau = None
                if "sub" in e:
                    # A sub-emitter child: Stop() on the child changes nothing,
                    # the parent's stop only ends new parents -- live parents
                    # keep spawning children [Unity-verified 2026-09-30,
                    # SESyn3Runner.cs stop_*]; the root's Destroy still ends
                    # all emission (approximation 16).
                    ep_ = sp["emitters"][e["sub"]["p"]]
                    pc_ = owners_t7[ep_["plist"]] if "plist" in ep_ else None
                    if destroy is not None and (pc_ is None or destroy < pc_):
                        pc_ = destroy
                    pcut_tau = (pc_ - on) if (pc_ is not None and on < pc_) else None
                    cut_tau = (destroy - on) if (destroy is not None and on < destroy) else None
                if e.get("life_inf") and infinite_destroy is not None and st >= infinite_destroy:
                    continue  # Infinite particles also obey the verified extended Destroy frame.
                if pdestroy is not None and st >= pdestroy - 1e-6:
                    continue  # the root's Destroy() removed its particles (_fgo_fx_particle_destroy)
                self._draw_emitter(rv, ei, e, wkey, st - on + lead, cut_tau,
                                   cx, cy, spread, unit, up, frame_ref, st, at, mv, one, uvst, pcut_tau,
                                   _fgo_fx_pivot_now(sp, e, T7, st),
                                   mul_now=_fgo_fx_mul_now(sp, e, T7, st) if "mul_tr" in e else None)

        def _cam_view(self, c, st, at, lead, owners_t7, T7, destroy, pdestroy, infinite_destroy):
            """A picture camera's target: c["w"] x c["h"] texels cleared to its
            opaque background colour, then its view (_draw_view)."""
            tw, th = c["w"], c["h"]
            rv = renpy.Render(tw, th)
            bg = c["bg"]
            rv.blit(renpy.render(Solid((int(round(bg[0] * 255)), int(round(bg[1] * 255)),
                                        int(round(bg[2] * 255)), 255)), tw, th, st, at), (0, 0))
            global _fgo_fx_debug
            saved = (getattr(self, "_rwh", None), getattr(self, "_view", None))
            self._rwh = (tw, th)
            self._view = c
            # debug rows: a particle the main view also draws is recorded there once;
            # only items hidden from the main view (cam_hide) keep their camera rows
            dbg_ = _fgo_fx_debug
            if dbg_ is not None:
                _fgo_fx_debug = []
            try:
                self._draw_view(rv, tw, th, st, at, lead, owners_t7, T7, destroy, pdestroy, infinite_destroy, c)
            finally:
                self._rwh, self._view = saved
                if dbg_ is not None:
                    hide_ = set(("e", i) for k, i in self.spec.get("cam_hide", ()) if k == "e")
                    dbg_.extend(r for r in _fgo_fx_debug
                                if type(r[0]) is int and r[0] >= 0 and ("e", r[0]) in hide_)
                    _fgo_fx_debug = dbg_
            return rv

        def _draw_emitter(self, rv, ei, e, wi, tau, stop_tau, cx, cy, spread, unit, up, frame_ref, st, at, mv=None, one=1.0, uvst=None, pcut=None, pivot_now=None, mul_now=None):
            global _FGO_FX_ROT_STEP
            _FGO_FX_ROT_STEP = 3.0
            rr_ = e.get("rot_range")
            rot_static_ = (rr_ is not None and rr_[0] == rr_[1] and not e.get("rot_flip")
                           and not e.get("rot3d") and not e.get("start_values") and not e.get("start_anim"))
            L = max(e["length"], 1e-6)
            start = e.get("delay", 0.0)
            ml = _fgo_fx_max_life(e)
            if e["loop"]:
                k_hi = int(math.floor((tau - start) / L))
                k_lo = int(math.floor((tau - start - ml) / L))
                if _fgo_fx_fclock_scope(e):
                    # Unity's clock births / removals trail their lattice twins by up to a frame
                    k_lo = int(math.floor((tau - start - ml - 2 * _FGO_FX_FRAME) / L))
                # Prewarm = steady state: the system behaves as if it had
                # always been running (n = rate x lifetime, remaining lives
                # uniform, clock restarting at 0), so every earlier cycle that
                # can still have live particles counts -- not just one.
                # [Unity-verified 2026-09-24, Assets/Editor/ProbeOpen.cs
                # Prewarm(): duration 0.3 / 1 / 2 s, life 6, rate 10 -> 60.]
                if _fgo_fx_burst_frames and e.get("bsched"):
                    # see _fgo_fx_live_parts: a scheduled cycle can fire a frame early / late
                    k_hi += 1
                    k_lo -= 1
                k_lo = max(k_lo, _fgo_fx_first_cycle(e))
                if e.get("life_inf"):
                    k_lo = _fgo_fx_first_cycle(e)      # an infinite particle outlives every cycle
                ks = range(k_lo, k_hi + 1)
            else:
                ks = (0,)
            parts = []
            # a charaScale-dependent Hierarchy clamp (converter e["clamp_gu"]):
            # the particle tracks depend on this show's scale, g_u = 288 / c
            gu_ = round(unit / spread, 6) if ((e.get("clamp_gu") or e.get("speedmod_gu") or
                       (e.get("world_step") and e.get("scaling_mode") == 0)) and _fgo_fx_grav_units and spread) else None
            sub_ = e.get("sub")
            if sub_ is not None:
                # Birth sub-emitter child: particles only at the parent's
                # particles (_fgo_fx_sub_parts)
                parts = _fgo_fx_sub_parts(self.name, ei, e, wi, self.spec, tau, pcut,
                                          (unit / spread) if spread else 0.0)
                ep_ = self.spec["emitters"][sub_["p"]]
                pu_sz = spread if ep_.get("scaling_mode") == 0 else unit
                pu_shape = unit if ep_.get("scaling_mode") == 1 else spread
                pdx_ = (ep_.get("dx", 0.0) - e.get("dx", 0.0)) * spread
                pdy_ = -(ep_["dy"] - e["dy"]) * spread
                ks = ()
            for k in ks:
                chunk = _fgo_fx_chunk(self.name, ei, e, wi, k, mv, gu_, None, self.wind_t0)
                ok = _fgo_fx_accepted(self.name, ei, e, wi, k, mv)
                parts.extend(p for p, keep in zip(chunk, ok) if keep)
            if e.get("sort_mode") == 2:          # OldestInFront
                parts = parts[::-1]

            hier = e.get("scaling_mode") == 0    # Hierarchy: size/velocity hierarchy-scaled too
            u_sz = spread if hier else unit
            # Gravity is WORLD gravity in every scaling mode, never hierarchy-
            # scaled: under Hierarchy the drawn displacement unit (spread =
            # prefab px) must be converted back to world units [Unity-
            # verified 2026-09-27, lane xform, LaneXformHier.cs grav /
            # velgrav, xform_hier_fit2.py: 3e-5 over every scale incl. a
            # uniform (1,1,1) hierarchy]. Until then Hierarchy gravity was
            # drawn 288 x too weak (bit_talk_crystal_break, _magatsuhi_burn01,
            # _pink_fog01).
            g_u = (unit / u_sz) if (hier and _fgo_fx_grav_units and u_sz) else 1.0
            # scalingMode=Local ignores parent scale for the shape too: its
            # extents are 288-px world units like size/velocity, and charaScale
            # doesn't reach them. [Unity-verified 2026-09-23, bit_talk_explosion
            # smoke_up box + impactlanding smoke_screen; see fgo_fx_convert.py]
            u_shape = unit if e.get("scaling_mode") == 1 else spread
            ex = cx + e.get("dx", 0.0) * spread
            ey = cy - e["dy"] * spread
            cap = frame_ref * e.get("max_particle_size", 1.0)
            # a perspective picture camera's view (_draw_view): view-aligned billboards
            # stay parallel to the image plane, so each projects exactly as a
            # similarity about the view centre, scale (d0 * charaScale) / depth
            view_ = getattr(self, "_view", None)
            persp_ = view_ is not None and view_.get("fov") is not None
            if persp_:
                W2_, H2_ = rv.width * 0.5, rv.height * 0.5
                camz_ = view_["p"][2] * self.scale / fgo_px_per_unit
                d0s_ = view_["d0"] * self.scale
                # a rotated camera (converter CAMERA_ROTATED): camera space = R^T x
                # (camera-relative world, y up); f_ = focal length px
                Rc_ = view_.get("rot")
                f_ = H2_ / math.tan(math.radians(view_["fov"]) * 0.5)
            floor = frame_ref * e.get("min_particle_size", 0.0)
            size_curve = e.get("size_curve")
            size_min = e.get("size_curve_min")
            size_sep = e.get("size_sep")
            # velocityOverLifetime curve axes (local), world via vol_m.
            vol_curves = [(i, a) for i, a in enumerate(e.get("vol", ())) if a and a[0] == "curve"]
            vm = e.get("vol_m")
            qs = e.get("quad_scale")
            pivot = pivot_now
            stretch = e.get("stretch")
            asset, cols, rows = e["tex"]
            fr = e.get("frame", ("const", 0))
            n_tiles = cols if e.get("uv_row") is not None else cols * rows
            cycles = e.get("uv_cycles", 1.0)
            colors = e.get("colors")
            alphas = e.get("alphas")
            # FGO_EF shaders: out.a = tex.a * vcol.a * _oneValue, clamped to
            # [0, 1] by the LDR target before blending (verified from the
            # game build, tools/fgo_fx_ngui.py _FGO_SHADERS). Up to 1 that is
            # just the particle alpha x one; above 1 the clamp must come
            # after the texture alpha, which fgo.fx_matmul does.
            mat_mul = mul_now if mul_now is not None else e.get("mat_mul")
            if one > 1.0 and mat_mul is None:
                mat_mul = (1.0, 1.0, 1.0, 1.0)
            mt_ = e.get("mtex")
            if mt_ is not None:
                # an mtex material: every quad through the geometry leaf / mesh draw (fgo.fx_mt), the
                # current _MainTex_ST and wrap from the uv fields, _Time.y = play start + effect clock
                mat_mul = mat_mul or (1.0, 1.0, 1.0, 1.0)
                if uvst is not None:
                    mt_ = dict(mt_, st0=tuple(uvst[:4]), w0=tuple(uvst[4:6]))
                mt_ = _fgo_fx_mt_key(mt_, self.shader_t0 + st, one)
            # Local alignment / mesh particles (converter _render_fields):
            # G = e["align_m"]; pivot3 = the renderer pivot incl. Z;
            # cap_ox = |own.x| under Local scaling (the maxParticleSize test
            # is |own.x| * max(size_x, size_y)); mesh = indices into
            # spec["meshes"]. Absent from every older spec.
            G = e.get("align_m")
            if G is not None:
                G = tuple(tuple(r) for r in G)
            is_mesh_ = "mesh" in e
            pv3 = e.get("pivot3")
            cap_ox = e.get("cap_ox", 1.0)
            size_k = e.get("size_k", 1.0)
            gpx_ = e.get("gpx")
            if gpx_ is None and G is not None and _fgo_fx_gpx_all:
                # every plain billboard through a scaled G (Hierarchy scale, e.g.
                # 288): size x scale px through G / scale, not the pre-G size
                # truncated to whole pixels (2026-09-29, the user's decision;
                # AGENTS.md "Scaled-frame clamps" -> found on the way)
                gs_ = max(abs(G[0][0]), abs(G[0][1]), abs(G[1][0]), abs(G[1][1]))
                gpx_ = gs_ if gs_ > 1.5 else None
            G_gpx = tuple(tuple(v / gpx_ for v in r_) for r_ in G) if (gpx_ and G is not None) else None
            # Moving emitter, lane xform 2026-09-27 (_fgo_fx_dyn_frame): Local
            # simulation space -> the cloud is drawn about the CURRENT origin
            # and through J(t); a changing quad matrix -> G / cap now.
            Js = None
            ag_ = e.get("anim_geo") if mv is not None else None
            if ag_ is not None and (ag_.get("local") or "dyn" in ag_):
                O_, Js, G_, capx_ = _fgo_fx_dyn_frame(mv[0], e, mv[1], st)
                if G_ is not None:
                    G = G_
                    if not is_mesh_:
                        cap_ox = capx_
                if ag_.get("local"):
                    ex = cx + O_[0] * spread
                    ey = cy - O_[1] * spread
            reader_G = G
            reader_G_gpx = G_gpx
            reader_origin = O_ if ag_ is not None and ag_.get("local") else (e.get("dx", 0.0), e["dy"], e.get("dz", 0.0))
            is_mesh = "mesh" in e
            mesh_parts = [] if is_mesh else None
            partial_scope_ = (_fgo_fx_near_clip_partial and self.scale == 1.0 and view_ is None
                and getattr(self, "anchor", None) in _FGO_FX_NEAR_CAMERAS
                and _fgo_fx_near_partial_scope(e))

            for (birth, life, ox, oy, vx, vy, vcx, vcy, grav, size, size_y, rot0, spin,
                 tint_rgb, tint_a, frame, size_mix, vel_mix, track, vz, row_base, bxy, tilt, oz, mx, fz, lx) in parts:
                _FGO_FX_ROT_STEP = 3.0
                c2t = lx.get("c2") if lx else None
                if stop_tau is not None and birth > stop_tau:
                    continue
                age = tau - birth
                if type(birth) is _FGOFXClockBirth:
                    # Unity's float32 clock (_fgo_fx_fclock): drawn from its emission frame,
                    # gone from the frame whose step takes its percent past 100
                    if tau < birth.bk - 1e-9 or (birth.dk is not None and tau >= birth.dk - 1e-9):
                        continue
                    age = min(max(age, 0.0), life)
                elif age < 0.0 or age > life:
                    continue
                if _fgo_fx_burst_frames and tau < getattr(birth, "se", -1.0) - 1e-9:
                    continue       # a step-schedule burst exists from its step's END (_fgo_fx_bsched)
                u = age / life

                # Parent rig is reader-owned; preserve the converter emitter animation.
                parent = getattr(self, "parent_particle", None)
                root = None
                if parent is not None and getattr(self, "_view", None) is None:
                    gx, gy, parent_scale, root = parent(ei, e, st - tau + birth, st)
                    spread = up * parent_scale
                    u_sz = spread if hier else unit
                    u_shape = unit if e.get("scaling_mode") == 1 else spread
                    g_u = (unit / u_sz) if (hier and _fgo_fx_grav_units and u_sz) else 1.0
                    ex = cx + reader_origin[0] * spread
                    ey = cy - reader_origin[1] * spread
                    G, G_gpx = reader_G, reader_G_gpx
                    if sub_ is not None:
                        pu_sz = spread if ep_.get("scaling_mode") == 0 else unit
                        pu_shape = unit if ep_.get("scaling_mode") == 1 else spread
                        pdx_ = (ep_.get("dx", 0.0) - e.get("dx", 0.0)) * spread
                        pdy_ = -(ep_["dy"] - e["dy"]) * spread

                col = _fgo_grad(colors, u) if colors else (1.0, 1.0, 1.0)
                alpha = (_fgo_grad(alphas, u) if alphas else 1.0) * tint_a * one
                cm_ = e.get("col_mode")
                if cm_ is not None and colors:
                    # Fixed / PerceptualBlend gradients (converter _col2_fields)
                    col = _fgo_fx_gradm(colors, u, cm_[0], False)
                    alpha = _fgo_fx_gradm(alphas, u, cm_[0], True) * tint_a * one
                if c2t is not None:
                    # ColorOverLifetime TwoGradients: lerp(min, max, t), one t
                    # per particle (converter _col2_fields, Unity-verified)
                    m0_ = cm_[1] if cm_ is not None else 0
                    c0_ = _fgo_fx_gradm(e["col2"][0], u, m0_, False)
                    col = tuple(a + (b - a) * c2t for a, b in zip(c0_, col))
                    a0_ = _fgo_fx_gradm(e["col2"][1], u, m0_, True)
                    a1_ = _fgo_fx_gradm(alphas, u, cm_[0], True) if (cm_ is not None and alphas) else                         (_fgo_grad(alphas, u) if alphas else 1.0)
                    alpha = (a0_ + (a1_ - a0_) * c2t) * tint_a * one
                byte_col_ = _fgo_fx_bytecolor(e, u, tint_rgb, tint_a, colors, alphas, c2t) if (_fgo_fx_color_bytes or e.get("color_bytes")) else None
                alpha = _fgo_fx_shader_alpha(e, alpha)
                if byte_col_ is not None:
                    col, alpha = byte_col_[0], _fgo_fx_shader_alpha(e, byte_col_[1] * one)
                    if alpha <= 0.0 and _fgo_fx_debug is None:
                        continue
                else:
                    if alpha <= 0.004 and _fgo_fx_debug is None:
                        continue
                    col = (col[0] * tint_rgb[0], col[1] * tint_rgb[1], col[2] * tint_rgb[2])


                grow = 1.0
                if size_curve:
                    hi = _fgo_curve_at(size_curve[1], u)
                    if size_min:
                        hi = _fgo_curve_at(size_min, u) + (hi - _fgo_curve_at(size_min, u)) * size_mix
                    grow = size_curve[0] * hi
                raw_x = size * grow * u_sz
                raw_y = (size_y if size_y is not None else size) * grow * u_sz
                if size_sep is not None:
                    # sizeOverLifetime separateAxes (lane shape): X and Y on
                    # their own curves, one shared random fraction (size_mix)
                    # [Unity-verified 2026-09-26, LaneShapeProbe.cs Size()]
                    raw_x = size * _fgo_fx_size_axis(size_sep[0], u, size_mix) * u_sz
                    raw_y = (size_y if size_y is not None else size) * _fgo_fx_size_axis(size_sep[1], u, size_mix) * u_sz
                # (merge wave 1: separate-axes size first, THEN the scale
                # factor -- the two multiply, neither replaces the other.)
                if size_k != 1.0:
                    # stretched billboard on a uniformly scaled emitter
                    # (converter _scaled_modules): width and lengthScale term
                    raw_x *= size_k
                    raw_y *= size_k

                disp_x, disp_y, dzz, cvx, cvy, cvz = _fgo_fx_disp(
                    e, (birth, life, ox, oy, vx, vy, vcx, vcy, grav, size, size_y, rot0, spin, tint_rgb, tint_a,
                        frame, size_mix, vel_mix, track, vz, row_base, bxy, tilt, oz, mx, fz, lx), age, g_u, vol_curves, vm)
                if persp_:
                    # (converter: every item of a perspective view is near-scope, so z is exact)
                    d_ = (e.get("dz", 0.0) * spread + oz * u_shape + dzz * u_sz) / unit - camz_
                    if Rc_ is None and (d_ < view_["near"] or d_ > view_["far"]):
                        continue
                elif (_fgo_fx_near_clip and self.scale == 1.0 and _fgo_fx_near_scope(e)):
                    z_world = (e.get("dz", 0.0) * spread + oz * u_shape + dzz * u_sz) / unit
                    if view_ is not None:
                        # a picture camera's own orthographic near / far (identity rotation)
                        d_ = z_world - view_["p"][2] / fgo_px_per_unit
                        if d_ < view_["near"] or d_ > view_["far"]:
                            continue
                    elif _fgo_fx_near_hidden(z_world, getattr(self, "anchor", None)):
                        continue
                if "vel_align" in e:
                    G = _fgo_fx_velocity_G(e["vel_align"], cvx, cvy, cvz)
                    G_gpx = tuple(tuple(v / gpx_ for v in r_) for r_ in G) if gpx_ else None
                if "size_speed" in e:
                    ss_grow = _fgo_fx_sizespeed(e, cvx, cvy, cvz, lx.get("ss", 0.0) if lx else 0.0, g_u)
                    raw_x *= ss_grow
                    raw_y *= ss_grow
                reader_dz = oz * u_shape + dzz * u_sz
                if bxy is None:
                    px = ex + ox * u_shape + disp_x * u_sz
                    py = ey + oy * u_shape + disp_y * u_sz
                    if lx and "pa" in lx:
                        # sub-emitter child: + where its parent particle was
                        # (the parent's own drawing units)
                        pa_ = lx["pa"]
                        px += pdx_ + pa_[0] * pu_shape + pa_[2] * pu_sz
                        py += pdy_ + pa_[1] * pu_shape + pa_[3] * pu_sz
                    if Js is not None:
                        # Local simulation space on a rotating / scaling
                        # emitter: rest-frame offset D -> J(t) D (screen
                        # orientation), velocity likewise (stretch direction)
                        Dx, Dy, Dz = px - ex, py - ey, oz * u_shape + dzz * u_sz
                        reader_dz = Js[2][0] * Dx + Js[2][1] * Dy + Js[2][2] * Dz
                        px = ex + Js[0][0] * Dx + Js[0][1] * Dy + Js[0][2] * Dz
                        py = ey + Js[1][0] * Dx + Js[1][1] * Dy + Js[1][2] * Dz
                        cvx, cvy, cvz = (Js[0][0] * cvx + Js[0][1] * cvy + Js[0][2] * cvz,
                                         Js[1][0] * cvx + Js[1][1] * cvy + Js[1][2] * cvz,
                                         Js[2][0] * cvx + Js[2][1] * cvy + Js[2][2] * cvz)
                else:
                    # moving emitter: the particle's birth origin
                    px = cx + bxy[0] * spread + ox * u_shape + disp_x * u_sz
                    py = cy - bxy[1] * spread + oy * u_shape + disp_y * u_sz

                if root is not None:
                    z0 = bxy[2] if bxy is not None and len(bxy) > 2 else reader_origin[2]
                    p = _fgo_fx_mv(root, (px - cx, cy - py, z0 * spread + reader_dz))
                    px = rv.width * 0.5 + gx * up + p[0]
                    py = rv.height * 0.5 - gy * up - p[1]
                    v = _fgo_fx_mv(root, (cvx, -cvy, cvz))
                    cvx, cvy, cvz = v[0], -v[1], v[2]

                    # Compose after velocity alignment and pixel-scaled frame selection.
                    G = _fgo_fx_m3mul(root, G) if G is not None else None
                    G_gpx = _fgo_fx_m3mul(root, G_gpx) if G_gpx is not None else None

                if type(frame) is float:
                    # a startFrame offset over a frameOverTime curve (lane
                    # shape): floor(((fot + startFrame) mod 1) * n)
                    ph = (u * cycles) % 1.0 if cycles != 1.0 else u
                    if _fgo_fx_sf_two_curves and lx and "fr" in lx:
                        # TwoCurves: scalar * lerp(min, max, r) + startFrame
                        # [Unity-verified 2026-10-17, small_rules_2 probe]
                        lo_ = _fgo_curve_at(e["frame_min"], ph)
                        v_ = fr[1] * (lo_ + (_fgo_curve_at(fr[2], ph) - lo_) * lx["fr"])
                    else:
                        v_ = fr[1] * _fgo_curve_at(fr[2], ph)
                    frame = row_base + min(n_tiles - 1, max(0, int(((v_ + frame) % 1.0) * n_tiles)))
                if frame is None and lx and "fr" in lx:
                    # frameOverTime TwoCurves: one mix per particle (converter
                    # _col2_fields frame_min, Unity-verified)
                    ph = (u * cycles) % 1.0 if cycles != 1.0 else u
                    lo_ = _fgo_curve_at(e["frame_min"], ph)
                    v_ = fr[1] * (lo_ + (_fgo_curve_at(fr[2], ph) - lo_) * lx["fr"])
                    frame = row_base + min(n_tiles - 1, max(0, int(v_ * n_tiles)))
                if frame is None:
                    ph = (u * cycles) % 1.0 if cycles != 1.0 else u
                    v_ = fr[1] * _fgo_curve_at(fr[2], ph)
                    if _fgo_fx_frame_wrap and v_ > 1.0:
                        # frameOverTime above 1 wraps the whole sheet [Unity-verified 2026-10-20,
                        # small_rules_3: UV tiles of 5 curves, 445/445 frames; the clamp matched 143]
                        v_ %= 1.0
                    frame = row_base + min(n_tiles - 1, max(0, int(v_ * n_tiles)))
                cell = (asset, cols, rows, frame) if uvst is None else (asset, cols, rows, frame, uvst)
                fl_ = lx.get("fl", 0) if lx else 0
                if fl_ & 3:
                    cell = (asset, cols, rows, frame, uvst, fl_)
                if mt_ is not None:
                    cell = (asset, cols, rows, frame, uvst, fl_ & 3, mt_)
                rgb = col if byte_col_ is not None else (round(col[0], 2), round(col[1], 2), round(col[2], 2))
                # (particle alpha <= 1 unless _oneValue > 1; then the clamp is
                # fgo.fx_matmul's, after the texture alpha)
                a2 = (alpha if mat_mul is not None else min(alpha, 1.0)) if byte_col_ is not None else round(alpha if mat_mul is not None else min(alpha, 1.0), 2)

                if is_mesh:
                    # Mesh particle: vertex = centre + G * Ry Rx Rz(+z) *
                    # diag(sx, sy, sz) * m; never capped by maxParticleSize;
                    # pivot in units of size x the mesh's bounds size, Z
                    # negated [Unity-verified 2026-09-26, ProbeMeshParticles.cs].
                    rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                    raw_z = _fgo_fx_mesh_zsize(e, mx[0], size, grow, u_sz, u, size_mix)
                    T3 = _fgo_fx_m3mul(G, _fgo_fx_rot3(rx_, ry_, rot0 + spin * age + _fgo_fx_spin_at(lx, age), True))
                    fl_ = lx.get("fl", 0) if lx else 0
                    if fl_:
                        # renderer flip: the mesh and its pivot offset mirrored
                        # along the flipped mesh axes [Unity-verified
                        # 2026-09-27, SG2Flip.cs mesh_*: 3e-4 = probe drift]
                        raw_x = -raw_x if fl_ & 1 else raw_x
                        raw_y = -raw_y if fl_ & 2 else raw_y
                        raw_z = -raw_z if fl_ & 4 else raw_z
                    if pv3 is not None:
                        b_ = self.spec["meshes"][mx[1]]["b"]
                        lp = (pv3[0] * raw_x * b_[0], pv3[1] * raw_y * b_[1], -pv3[2] * raw_z * b_[2])
                        px += T3[0][0] * lp[0] + T3[0][1] * lp[1] + T3[0][2] * lp[2]
                        py -= T3[1][0] * lp[0] + T3[1][1] * lp[1] + T3[1][2] * lp[2]
                    if persp_:
                        # a perspective view: every mesh vertex projected (_fgo_fx_mesh_draw)
                        zl_ = (T3[2][0] * lp[0] + T3[2][1] * lp[1] + T3[2][2] * lp[2]) if pv3 is not None else 0.0
                        mesh_parts.append((px, py, (T3[0][0] * raw_x, T3[0][1] * raw_y, T3[0][2] * raw_z,
                                                    T3[1][0] * raw_x, T3[1][1] * raw_y, T3[1][2] * raw_z),
                                           mx[1], frame, col, alpha if mat_mul is not None else min(alpha, 1.0),
                                           (d_ + zl_ / unit, (T3[2][0] * raw_x, T3[2][1] * raw_y, T3[2][2] * raw_z),
                                            unit, W2_, H2_, f_, Rc_, view_["near"], view_["far"])))
                        continue
                    mesh_parts.append((px, py, (T3[0][0] * raw_x, T3[0][1] * raw_y, T3[0][2] * raw_z,
                                                T3[1][0] * raw_x, T3[1][1] * raw_y, T3[1][2] * raw_z),
                                       mx[1], frame, col, alpha if mat_mul is not None else min(alpha, 1.0)))
                    if _fgo_fx_debug is not None:
                        _fgo_fx_debug.append((ei, e["name"], px, py, int(abs(raw_x)), int(abs(raw_y)), a2))
                    continue

                if persp_ and stretch is not None:
                    # a perspective view of a stretched billboard: the quad is
                    # built in 3D (u along the signed 3D velocity axis from the
                    # particle, width across cross(particle - camera, axis), image
                    # top on the + side; the orthographic rules of the stretch
                    # branch below) and every grid vertex projected
                    # (_fgo_fx_pquad). The width cap acts on the projected width.
                    v3_ = math.sqrt(cvx * cvx + cvy * cvy + cvz * cvz)
                    if v3_ < 1e-6:
                        continue
                    sv = e.get("stretch_sv")
                    if sv is not None:
                        L_ = raw_y * sv[0] + sv[1] * v3_ * u_sz
                    else:
                        L_ = raw_y * stretch + e.get("stretch_vel", 0.0) * v3_ * u_sz
                        if e.get("stretch_neg"):
                            L_ = -L_
                    k_ = L_ / (v3_ * unit)
                    P_ = ((px - W2_) / unit, -(py - H2_) / unit, d_)
                    A_ = (-k_ * cvx, k_ * cvy, -k_ * cvz)
                    Sx_ = P_[1] * A_[2] - P_[2] * A_[1]
                    Sy_ = P_[2] * A_[0] - P_[0] * A_[2]
                    Sz_ = P_[0] * A_[1] - P_[1] * A_[0]
                    n_ = math.sqrt(Sx_ * Sx_ + Sy_ * Sy_ + Sz_ * Sz_)
                    if n_ < 1e-12:
                        continue
                    cap_d_ = _fgo_fx_rt(Rc_, P_)[2] if _fgo_fx_camera_depth_cap and Rc_ is not None else d_
                    if cap_d_ <= 0.0:
                        continue
                    wpx_ = raw_x * d0s_ / cap_d_
                    hw_ = raw_x * (cap / wpx_ if wpx_ > cap > 0 else 1.0) / unit
                    B_ = (-hw_ * Sx_ / n_, -hw_ * Sy_ / n_, -hw_ * Sz_ / n_)
                    O_ = (P_[0] - 0.5 * B_[0], P_[1] - 0.5 * B_[1], P_[2] - 0.5 * B_[2])
                    if Rc_ is not None:
                        O_, A_, B_, P_ = (_fgo_fx_rt(Rc_, O_), _fgo_fx_rt(Rc_, A_), _fgo_fx_rt(Rc_, B_),
                                          _fgo_fx_rt(Rc_, P_))
                        if P_[2] < view_["near"] or P_[2] > view_["far"]:
                            continue
                    got = _fgo_fx_pquad(cell, (O_, A_, B_), f_, W2_, H2_,
                                        rgb, a2, e["blend"], mat_mul)
                    if got is None:
                        continue
                    if _fgo_fx_debug is not None:
                        _fgo_fx_debug.append((ei, e["name"], W2_ + f_ * P_[0] / P_[2], H2_ - f_ * P_[1] / P_[2],
                                              got[3], got[4], a2))
                    rv.blit(renpy.render(got[0], got[3], got[4], st, at), (got[1], got[2]))
                    continue
                if persp_ and Rc_ is not None:
                    c_ = _fgo_fx_rt(Rc_, ((px - W2_) / unit, -(py - H2_) / unit, d_))
                    if c_[2] < view_["near"] or c_[2] > view_["far"]:
                        continue
                    sg_ = f_ / (unit * c_[2])
                    px = W2_ + f_ * c_[0] / c_[2]
                    py = H2_ - f_ * c_[1] / c_[2]
                    raw_x *= sg_
                    raw_y *= sg_
                elif persp_:
                    sg_ = d0s_ / d_
                    px = W2_ + (px - W2_) * sg_
                    py = H2_ + (py - H2_) * sg_
                    raw_x *= sg_
                    raw_y *= sg_
                pcap = cap
                piv_x, piv_y = raw_x, raw_y       # renderer pivot: uncapped sizes
                if stretch is None:
                    # maxParticleSize on a billboard shrinks the WHOLE quad
                    # uniformly by cap / max(size_x * own.x, size_y): the
                    # size3D Y size counts, the Local-mode own Y scale doesn't,
                    # rotation doesn't matter. [Unity-verified 2026-09-24:
                    # ProbeInferred.cs Clamp() -- own (3,1) -> 512x170.7, own
                    # (1,3) -> 288x864 uncapped; ProbeBatch2.cs Size3D() --
                    # size3D (3,1) -> 512x170.7, (1,3) -> 170.7x512.] Own X
                    # scale < 1 is unprobed. Stretched billboards cap the width
                    # only (below).
                    if G is not None:
                        # general path: |own.x| * max(size_x, size_y)
                        # [Unity-verified 2026-09-26, ProbeMeshParticles.cs
                        # Orient(): own (2,1,1.5), size (0.3, 0.5) -> 1.0].
                        # (The older rule below agrees on every older spec:
                        # bit_talk_blood_splash_fs h3, the only one with
                        # both size_y and quad_scale, has size_x >= size_y.)
                        big = cap_ox * max(raw_x, raw_y)
                    else:
                        bx = raw_x * abs(qs[0]) if qs is not None else raw_x
                        big = max(bx, raw_y)
                    if big > cap > 0:
                        f = cap / big
                        raw_x *= f
                        raw_y *= f
                    pcap = float("inf")
                wxf = max(floor, min(raw_x, pcap))
                wx = int(wxf)
                wf = hf = None
                clip_poly_ = None
                partial_ = partial_scope_ and bxy is None and Js is None and not (lx and "pa" in lx)
                ss_ = e.get("stretch_s") if stretch is not None else None
                if ss_ is not None:
                    # non-uniformly scaled / mirrored emitter: a parallelogram
                    # (_fgo_fx_stretch_axes, lane stretchscale rule)
                    r_ = _fgo_fx_stretch_axes(cvx, cvy, cvz, raw_x, raw_y, e["stretch_sv"], ss_, cap, u_sz,
                                              pivot[1] if pivot is not None else 0.0)
                    if r_ is None:
                        continue
                    (Ax, Ay), (Wx, Wy), (Px, Py) = r_
                    if "pivot_x" in e:
                        ls_, vs_ = e["stretch_sv"]
                        len_ = raw_y * ls_ * ss_[0] + math.sqrt(cvx*cvx + cvy*cvy + cvz*cvz) * vs_ * u_sz
                        ox_, oy_ = _fgo_fx_stretch_pivot_x(cvx, cvy, raw_x, raw_y, ss_[0], cap, len_, e["pivot_x"])
                        Px += ox_
                        Py += oy_
                    if _fgo_fx_direct_motes and mat_mul is None and len(cell) <= 6:
                        cr = _fgo_fx_moteS_render(cell, (Ax, Ay), (-Wx, -Wy), rgb, a2, e["blend"], st, at)
                        if cr is None:
                            continue
                    else:
                        mote = _fgo_fx_moteS(cell, (Ax, Ay), (-Wx, -Wy), rgb, a2, e["blend"], mat_mul)
                        if mote is None:
                            continue
                        cr = renpy.render(mote, 1, 1, st, at)
                    qx, qy = px + Px + Ax * 0.5, py + Py + Ay * 0.5
                    cw, ch = cr.get_size()
                    if _fgo_fx_debug is not None:
                        # + the corners (u, v) = (0,0) (0,1) (1,0) (1,1), screen px
                        b0x, b0y = px + Px, py + Py
                        _fgo_fx_debug.append((ei, e["name"], qx, qy, int(math.hypot(Ax, Ay)),
                                              int(math.hypot(Wx, Wy)), a2,
                                              [b0x + uu * Ax + (vv - 0.5) * Wx for uu in (0, 1) for vv in (0, 1)] +
                                              [b0y + uu * Ay + (vv - 0.5) * Wy for uu in (0, 1) for vv in (0, 1)]))
                    _fgo_fx_blit(rv, cr, qx - cw * 0.5, qy - ch * 0.5)
                    continue
                if stretch is not None:
                    # The stretch vector lies along the 3D velocity, so the
                    # orthographic camera sees it foreshortened: the lengthScale
                    # term x |v_screen| / |v_3d|; the velocityScale term
                    # (|v| * scale along v) projects to scale * |v_screen|.
                    # [Unity renders 2026-09-24: bit_talk_34's camera-facing
                    # 30-degree cones draw short streaks only with this; the
                    # 2D runtime's full length was ~2-3x too long.]
                    spd = math.hypot(cvx, cvy)
                    if spd < 1e-6:
                        continue
                    fore = spd / math.sqrt(spd * spd + cvz * cvz)
                    # Signed length (stretch_sv = signed lengthScale,
                    # velocityScale): > 0 trails BEHIND the particle, < 0
                    # extends AHEAD; texture u=0 sits at the particle
                    # [Unity-verified 2026-09-24, ProbeOpen.cs Stretch()].
                    # Older specs without stretch_sv: |lengthScale| + stretch_neg.
                    # With size3D the lengthScale term uses size Y (raw_y), the
                    # width size X [Unity-verified 2026-09-25, ProbeRotation3D.cs
                    # StretchSize3D(): size (0.2, 0.5), lengthScale 2 -> 1.0
                    # along v, 0.2 across]. Without size_y, raw_y == raw_x.
                    sv = e.get("stretch_sv")
                    if sv is not None:
                        length = raw_y * sv[0] * fore + sv[1] * spd * u_sz
                    else:
                        length = raw_y * stretch * fore + e.get("stretch_vel", 0.0) * spd * u_sz
                        if e.get("stretch_neg"):
                            length = -length
                    if "pivot_x" in e:
                        # New pivot specs use the size3D cap denominator too;
                        # leave the older no-pivot draw path byte-identical.
                        big_ = max(raw_x, raw_y)
                        k_ = cap / big_ if big_ > cap > 0 else 1.0
                        wxf = max(floor, raw_x * k_)
                        wx = int(wxf)
                        ox_, oy_ = _fgo_fx_stretch_pivot_x(cvx, cvy, raw_x, raw_y, 1.0, cap, length, e["pivot_x"])
                        px += ox_
                        py += oy_
                        pvx_ = (ox_, oy_, e["pivot_x"] * raw_x * k_)
                    else:
                        pvx_ = None
                    w = int(abs(length))
                    h = max(1, wx)
                    wf, hf = abs(length), max(1.0, wxf)
                    if w < 1:
                        continue
                    ang = math.degrees(math.atan2(cvx, -cvy))
                    rot = ang - 90.0 if length < 0 else ang + 90.0
                    ux, uy = cvx / spd, cvy / spd
                    side_ = None
                    if _fgo_fx_stretch_side:
                        sg_ = _fgo_fx_side_gate(self, e, bxy, Js, lx, pivot, w, h)
                        if sg_ is None:
                            # camera-position ray side (notes/stretch_width.md)
                            W_, H_ = getattr(self, "_rwh", (1920, 1080))
                            zr_ = (e.get("swz", 0.0) * spread + oz * u_shape + dzz * u_sz) / unit
                            # the ray starts at the particle itself (before the pivot.x shift)
                            p0x_, p0y_ = (px - pvx_[0], py - pvx_[1]) if pvx_ else (px, py)
                            side_ = _fgo_fx_ray_side(cvx, cvy, cvz, length, h, (p0x_ - W_ * 0.5) / unit,
                                                     -(p0y_ - H_ * 0.5) / unit, zr_ - (
                                                         self._view["p"][2] / fgo_px_per_unit if getattr(self, "_view", None) is not None
                                                         else _FGO_FX_SIDE_CAM_Z[self.anchor]))
                            if side_ is not None:
                                c_ = (side_[0] * cvy - side_[1] * cvx) * (1.0 if length > 0 else -1.0) / (h * spd)
                                skew_ = math.degrees(math.acos(max(-1.0, min(1.0, c_))))
                                if _fgo_fx_side_census and _fgo_fx_debug is not None:
                                    _fgo_fx_debug.append(("SIDE", ei, e["name"], round(skew_, 3), w, h))
                                # the width axis turns by `skew_` about the length axis: half the
                                # width times the angle must stay under 0.5 px (1.5 degrees max)
                                sk_thr_ = (min(_FGO_FX_ROT_STEP * 0.5, math.degrees(1.0 / max(h, 1)))
                                           if _fgo_fx_rot_exact_all else _FGO_FX_ROT_STEP * 0.5)
                                if skew_ < sk_thr_:
                                    side_ = None        # same bucket as the old side
                                elif skew_ > 180.0 - sk_thr_:
                                    side_ = None        # the old side reversed: mirror the cell
                                    cell = (asset, cols, rows, frame, uvst, fl_ ^ 2)
                                    if pvx_:
                                        px, py = px - 2.0 * pvx_[0], py - 2.0 * pvx_[1]
                                elif pvx_:
                                    # pivot.x moves along the drawn side: -pivot.x * width * side
                                    px = p0x_ - pvx_[2] * side_[0] / h
                                    py = p0y_ - pvx_[2] * side_[1] / h
                        elif _fgo_fx_side_census and _fgo_fx_debug is not None:
                            _fgo_fx_debug.append(("SIDEGATE", ei, e["name"], sg_, w, h))
                    px -= ux * length * 0.5
                    py -= uy * length * 0.5
                    if pivot is not None:
                        # 2 * pivot.y * size_y along the 3D velocity, projected
                        # [Unity-verified 2026-09-25, see the converter].
                        kpv = 2.0 * pivot[1] * piv_y / math.sqrt(spd * spd + cvz * cvz)
                        px += cvx * kpv
                        py += cvy * kpv
                    if partial_ and _fgo_fx_near_clip_partial:
                        z0_ = (e["clip_z"] * spread + oz * u_shape + dzz * u_sz) / unit
                        az_ = -length * cvz / (spd * unit)
                        clip_poly_ = _fgo_fx_near_partial_poly((z0_, z0_ + az_, z0_ + az_, z0_), self.anchor)
                        if clip_poly_ == ():
                            continue
                    if side_ is not None:
                        if _fgo_fx_direct_motes and mat_mul is None and clip_poly_ is None and len(cell) <= 6:
                            cr = _fgo_fx_moteS_render(cell, (-length * ux, -length * uy), side_, rgb, a2, e["blend"], st, at)
                            if cr is None:
                                continue
                        else:
                            mote = _fgo_fx_moteS(cell, (-length * ux, -length * uy), side_, rgb, a2, e["blend"], mat_mul, clip_poly_)
                            if mote is None:
                                continue
                            cr = renpy.render(mote, 1, 1, st, at)
                        cw, ch = cr.get_size()
                        if _fgo_fx_debug is not None:
                            _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                        _fgo_fx_blit(rv, cr, px - cw * 0.5, py - ch * 0.5)
                        continue
                else:
                    w = wx
                    hfl_ = max(floor, min(raw_y, pcap))
                    h = int(hfl_)
                    wf, hf = wxf, hfl_
                    rot = rot0 + spin * age
                    if lx and "spt" in lx:
                        rot += _fgo_fx_spin_at(lx, age)     # RotationOverLifetime curve
                    elif _fgo_fx_rot_exact and not spin and rot_static_:
                        _FGO_FX_ROT_STEP = 0.1
                    if pv3 is not None:
                        # general path: G * Ry Rx Rz(-z) * (px*sx, py*sy,
                        # -pz*sx), uncapped sizes [Unity-verified 2026-09-26,
                        # ProbeMeshParticles.cs Orient() pivot cases].
                        rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                        T3 = _fgo_fx_m3mul(G, _fgo_fx_rot3(rx_, ry_, rot, False))
                        lp = (pv3[0] * piv_x, pv3[1] * piv_y, -pv3[2] * piv_x)
                        px += T3[0][0] * lp[0] + T3[0][1] * lp[1] + T3[0][2] * lp[2]
                        py -= T3[1][0] * lp[0] + T3[1][1] * lp[1] + T3[1][2] * lp[2]
                    if pivot is not None:
                        # (pivot.x * size_x, pivot.y * size_y) in the quad's
                        # axes (y up), turned with the particle's rotation,
                        # then the own-scale zoom [Unity-verified 2026-09-25].
                        ax_, ay_ = pivot[0] * piv_x, pivot[1] * piv_y
                        if tilt is not None:
                            rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                            r00, r01, r10, r11 = _fgo_fx_tilt2(rx_, ry_, rot)
                        else:
                            c_, s_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
                            r00, r01, r10, r11 = c_, s_, -s_, c_       # Rz(-rot)
                        ox_ = r00 * ax_ + r01 * ay_
                        oy_ = r10 * ax_ + r11 * ay_
                        if qs is not None:
                            ox_, oy_ = ox_ * qs[0], oy_ * qs[1]
                        px += ox_
                        py -= oy_
                if gpx_ and G is not None and stretch is None:
                    # `gpx` (converter): the quad at size x G's scale px through
                    # G / scale -- the same product without truncating a
                    # sub-pixel pre-G size to 0
                    # (the pre-G quad stays within ~2000 px: a bigger one hits the
                    # 4000 px texture limit's shrink, which G then can't undo --
                    # halve the scale until it fits and G by the same factor)
                    kk_, bg_ = gpx_, max(raw_x, raw_y)
                    n_ = 0
                    while bg_ * kk_ > 2000.0 and n_ < 24:
                        kk_ *= 0.5
                        n_ += 1
                    w = max(1, int(round(raw_x * kk_)))
                    h = max(1, int(round(raw_y * kk_)))
                    wf, hf = raw_x * kk_, raw_y * kk_
                    G_use_ = G_gpx if n_ == 0 else tuple(tuple(v * (2.0 ** n_) for v in r_) for r_ in G_gpx)
                if w < 1 or h < 1:
                    continue

                if partial_ and stretch is None and _fgo_fx_near_clip_partial:
                    step_ = _fgo_fx_step_for(w, h, qs, (G_use_ if gpx_ else G) if G is not None else None)
                    rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                    angles_ = [_fgo_fx_rb(a_, step_) for a_ in (rx_, ry_, rot)]
                    T_ = _fgo_fx_rot3(*angles_, False)
                    z0_ = (e["clip_z"] * spread + oz * u_shape + dzz * u_sz) / unit
                    zs_ = tuple(z0_ + (T_[2][0] * (u_ - .5) * w
                        + T_[2][1] * (.5 - v_) * h) / unit
                        for u_, v_ in ((0, 0), (1, 0), (1, 1), (0, 1)))
                    clip_poly_ = _fgo_fx_near_partial_poly(zs_, self.anchor)
                    if clip_poly_ == ():
                        continue
                if e.get("vbb"):
                    # VerticalBillboard: Q (1/sqrt 2) diag(sx, sy) Rz(-z) c --
                    # the size AFTER the rotation (a parallelogram), X / Y
                    # rotation ignored [Unity-verified 2026-09-27, small gates
                    # 2, SG2Vertical.cs, see the converter's _vertical_fields]
                    cz_, sz_ = math.cos(math.radians(rot)), math.sin(math.radians(rot))
                    k_ = 0.7071067811865476
                    qx_, qy_ = (qs[0], qs[1]) if qs is not None else (1.0, 1.0)
                    ux_, uy_ = k_ * raw_x * cz_ * qx_, -k_ * raw_y * sz_ * qy_      # world, y up
                    vx_, vy_ = k_ * raw_x * sz_ * qx_, k_ * raw_y * cz_ * qy_
                    if _fgo_fx_direct_motes and mat_mul is None and len(cell) <= 6:
                        cr = _fgo_fx_moteS_render(cell, (ux_, -uy_), (-vx_, vy_), rgb, a2, e["blend"], st, at)
                        if cr is None:
                            continue
                    else:
                        mote = _fgo_fx_moteS(cell, (ux_, -uy_), (-vx_, vy_), rgb, a2, e["blend"], mat_mul)
                        if mote is None:
                            continue
                        cr = renpy.render(mote, 1, 1, st, at)
                    cw, ch = cr.get_size()
                    if _fgo_fx_debug is not None:
                        _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                    _fgo_fx_blit(rv, cr, px - cw * 0.5, py - ch * 0.5)
                    continue
                if (_fgo_fx_big_crop_general and G is not None and stretch is None
                        and mat_mul is None and uvst is None
                        and e["blend"] == "add" and 0.0 <= a2 <= 1.0
                        and all(0.0 <= c_ <= 1.0 for c_ in rgb)
                        and _fgo_fx_tilt_at(tilt, lx, age) == (0, 0)
                        and _fgo_mote_safe_size(w, h, 1) != (w, h)):
                    G_crop_ = G_use_ if gpx_ else G
                    got_crop_ = _fgo_fx_crop_corners(w, h, rot, G_crop_)
                    if _fgo_fx_crop_fullframe(rv, got_crop_, px, py):
                        _fgo_fx_mote_crop_general(rv, cell, w, h, px, py, rot, rgb, a2,
                            e["blend"], st, at, G=G_crop_)
                        if _fgo_fx_debug is not None:
                            _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                        continue
                if G is not None and stretch is None:
                    rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                    # BigMesh: a quad the safe size would shrink -> geometry leaf
                    geo_ = _fgo_fx_big_mesh and (_fgo_fx_big_mesh_all or _fgo_mote_safe_size(w, h, 1) != (w, h))
                    if _fgo_fx_direct_motes and mat_mul is None and not geo_ and len(cell) <= 6:
                        cr = _fgo_fx_moteG_render(cell, *_fgo_fx_wh(wf, hf, w, h), G_use_ if gpx_ else G, rx_, ry_, rot, rgb, a2,
                                                  e["blend"], st, at)
                        if cr is None:
                            continue     # edge-on
                        cw, ch = cr.get_size()
                        if _fgo_fx_debug is not None:
                            _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                        _fgo_fx_blit(rv, cr, px - cw * 0.5, py - ch * 0.5)
                        continue
                    mote = _fgo_fx_moteG(cell, *_fgo_fx_wh(wf, hf, w, h), G_use_ if gpx_ else G, rx_, ry_, rot, rgb, a2, e["blend"],
                                         mat_mul, geo_)
                    if mote is None:
                        continue         # edge-on
                elif tilt is not None and stretch is None:
                    rx_, ry_ = _fgo_fx_tilt_at(tilt, lx, age)
                    geo_ = _fgo_fx_big_mesh and (_fgo_fx_big_mesh_all or _fgo_mote_safe_size(w, h, 1) != (w, h))
                    mote = _fgo_fx_mote3d(cell, *_fgo_fx_wh(wf, hf, w, h), rx_, ry_,
                                          rot, rgb, a2, e["blend"], mat_mul, e.get("quad_scale"), geo_, clip_poly_)
                    if mote is None:
                        continue         # edge-on
                else:
                    if (_fgo_fx_big_crop and not (_fgo_fx_crop_mesh and _fgo_fx_big_mesh)
                            and max(w, h) > _FGO_MOTE_SAFE_PX and mat_mul is None
                            and e.get("quad_scale") is None and len(cell) <= 4
                            and _fgo_fx_rb(rot, _fgo_fx_step_for(w, h, qs)) == 0):
                        got = _fgo_fx_mote_crop(cell, w, h, px, py, getattr(self, "_rwh", (1920, 1080)),
                                                rgb, a2, e["blend"])
                        if got is None:
                            continue                 # entirely outside the window
                        if _fgo_fx_debug is not None:
                            _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                        rv.blit(renpy.render(got[0], got[3], got[4], st, at), (got[1], got[2]))
                        continue
                    rb_ = _fgo_fx_rb(rot, _fgo_fx_step_for(w, h, qs))
                    geo_ = _fgo_fx_big_mesh and (_fgo_fx_big_mesh_all or _fgo_mote_safe_size(w, h, rb_) != (w, h))
                    if (_fgo_fx_direct_motes and mat_mul is None and e.get("quad_scale") is None and not geo_
                            and clip_poly_ is None and len(cell) <= 6):
                        cr = _fgo_fx_mote_render(cell, *_fgo_fx_wh(wf, hf, w, h), rot, rgb, a2, e["blend"], st, at)
                        cw, ch = cr.get_size()
                        if _fgo_fx_debug is not None:
                            _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                        _fgo_fx_blit(rv, cr, px - cw * 0.5, py - ch * 0.5)
                        continue
                    mote = _fgo_fx_mote(cell, *_fgo_fx_wh(wf, hf, w, h), rot, rgb, a2, e["blend"], mat_mul, e.get("quad_scale"), geo_, clip_poly_)
                cr = renpy.render(mote, w, h, st, at)
                cw, ch = cr.get_size()
                if _fgo_fx_debug is not None:
                    _fgo_fx_debug.append((ei, e["name"], px, py, w, h, a2))
                _fgo_fx_blit(rv, cr, px - cw * 0.5, py - ch * 0.5)

            if mesh_parts:
                _fgo_fx_mesh_draw(rv, self.spec, e, mesh_parts, st, at, mat_mul, uvst, mt_)

        def visit(self):
            return []

# --- plain containers in the render path (runtime_performance, see _fgo_fx_plain) -------------
init 999 python:

    import builtins as _fgo_fx_builtins
    _fgo_fx_py_range = _fgo_fx_builtins.range
    _fgo_fx_py_sorted = _fgo_fx_builtins.sorted
    # the store names Ren'Py's compiler emits or rebinds -> the plain built-ins
    _FGO_FX_PLAIN_NAMES = {"__renpy__list__": "python_list", "list": "python_list",
                           "__renpy__dict__": "python_dict", "dict": "python_dict",
                           "__renpy__set__": "python_set", "set": "python_set",
                           "range": "_fgo_fx_py_range", "sorted": "_fgo_fx_py_sorted"}
    # the public API keeps Ren'Py's rollback-aware types (it touches the scene and _fgo_fx_active)
    _FGO_FX_PLAIN_KEEP = ("fgo_fx_register", "fgo_fx_show", "fgo_fx_stop", "fgo_fx_hide", "fgo_fx_hide_all",
                          "_fgo_fx_plain_data", "_fgo_fx_plain_code", "_fgo_fx_plain_fn", "_fgo_fx_plain_patch")

    def _fgo_fx_plain_code(co):
        """co with the Revertable names in its global lookups (and its nested code) renamed."""
        names = tuple(_FGO_FX_PLAIN_NAMES.get(n, n) for n in co.co_names)
        consts = tuple(_fgo_fx_plain_code(c) if isinstance(c, type(co)) else c for c in co.co_consts)
        if names == co.co_names and consts == co.co_consts:
            return co
        return co.replace(co_names=names, co_consts=consts)

    def _fgo_fx_plain_fn(f):
        import types
        co = _fgo_fx_plain_code(f.__code__)
        if co is f.__code__:
            return f
        g = types.FunctionType(co, f.__globals__, f.__name__, f.__defaults__, f.__closure__)
        g.__kwdefaults__ = f.__kwdefaults__
        g.__dict__.update(f.__dict__)
        g.__qualname__ = f.__qualname__
        g.__doc__ = f.__doc__
        g.__module__ = f.__module__
        return g

    def _fgo_fx_plain_patch():
        import types
        def ours(f):
            return isinstance(f, types.FunctionType) and f.__code__.co_filename.replace("\\", "/").endswith("fgo_fx_runtime.rpy")
        n = 0
        for k, v in list(store.__dict__.items()):
            if k in _FGO_FX_PLAIN_KEEP:
                continue
            if ours(v):
                setattr(store, k, _fgo_fx_plain_fn(v))
                n += 1
            elif isinstance(v, type) and v.__module__ == "store":
                for mk, mv in list(v.__dict__.items()):
                    if ours(mv):
                        setattr(v, mk, _fgo_fx_plain_fn(mv))
                        n += 1
                    elif isinstance(mv, (staticmethod, classmethod)) and ours(mv.__func__):
                        setattr(v, mk, type(mv)(_fgo_fx_plain_fn(mv.__func__)))
                        n += 1
        return n

    # the runtime's own caches (a Revertable container logs itself for rollback on its first change
    # in every interaction and keeps a copy); the per-play clocks and _fgo_fx_active stay Revertable
    _FGO_FX_PLAIN_CACHES = (
        "_fgo_fx_sub_parents", "_fgo_fx_clip_last_cache", "_fgo_fx_destroy_totals", "_fgo_fx_mt_layout",
        "_fgo_fx_mt_tex", "_fgo_fx_int_tables", "_fgo_fx_rate_tabs", "_fgo_fx_dist_cache", "_fgo_fx_rate_cache",
        "_fgo_fx_oneshot_cache", "_fgo_fx_cycle_cache", "_fgo_fx_color_key_cache", "_fgo_fx_color_tint_cache",
        "_fgo_fx_fclock_cache", "_fgo_fx_loopoff_cache", "_fgo_fx_wrot_cache", "_fgo_fx_cap_state",
        "_fgo_fx_pw_sched_cache", "_fgo_fx_shape_pixels", "_fgo_fx_burst_cache", "_fgo_fx_bsched_cache",
        "_fgo_fx_sv_clocks", "_fgo_fx_sv_rate_frames", "_fgo_fx_sv_gravity_cache", "_fgo_fx_pw_ks_cache",
        "_fgo_fx_nz_inv", "_fgo_fx_nz_offsets", "_fgo_fx_chunk_cache", "_fgo_fx_mesh_layout", "_fgo_fx_sheet_disp",
        "_fgo_fx_cells", "_fgo_fx_motes", "_fgo_fx_quad_layout", "_fgo_fx_quad_tex", "_fgo_fx_crop_geometry",
        "_fgo_fx_crops", "_fgo_fx_sub_cache", "_fgo_fx_sub_state", "_fgo_fx_nz_G3", "_fgo_fx_nz_G2", "_fgo_fx_nz_G1")

    def _fgo_fx_plain_caches():
        for k in _FGO_FX_PLAIN_CACHES:
            v = store.__dict__.get(k)
            for base in (python_dict, python_list, python_set):
                if isinstance(v, base) and type(v) is not base:
                    setattr(store, k, base(v))       # shallow: the keys / values stay the same objects

    if _fgo_fx_plain:
        _fgo_fx_plain_count = _fgo_fx_plain_patch()
        _fgo_fx_plain_caches()
