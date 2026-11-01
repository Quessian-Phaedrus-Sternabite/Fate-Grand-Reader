# Shared math dependencies used by the converted renderer (no hand-ported effects).
init -6 python:
    import math
    fgo_native_width = 1024
    fgo_px_per_unit = 288.0
    fgo_flash_fps = 30.0
    FGO_LAYER13_REF_FGOPX = 1344
    _FGO_MOTE_SAFE_PX = 4000
    _fgo_blend_names = {"add": "add", "alpha": "normal", "mul": "multiply", None: "normal"}

    def _fgo_curve_at(keys, u):
        """Sample a Unity AnimationCurve.

        4-tuple keys are (time, value, inSlope, outSlope) and evaluate as the
        cubic Hermite spline Unity uses. The tangents carry most of the
        character -- spirit05 accelerates from a flat start, spirit08 shoots up
        then levels off -- and straight lines between endpoints lose all of it.
        """
        if not keys:
            return 1.0
        if len(keys[0]) == 4:
            if u <= keys[0][0]:
                return keys[0][1]
            for k0, k1 in zip(keys, keys[1:]):
                t0, v0, _, m0 = k0
                t1, v1, m1, _ = k1
                if u <= t1:
                    if m0 is None or m1 is None:
                        # an infinite tangent (Unity YAML "Infinity", written
                        # as None by the converter): the segment HOLDS the left
                        # key's value -- particles, MinMaxCurve.Evaluate and
                        # AnimationCurve.Evaluate alike [Unity-verified
                        # 2026-09-27, small gates 2, SG2StepCurve.cs ->
                        # probe_sg2_step/step.csv]
                        return v0
                    dt = t1 - t0
                    if dt <= 0:
                        return v1
                    x = (u - t0) / dt
                    x2 = x * x
                    x3 = x2 * x
                    return ((2 * x3 - 3 * x2 + 1) * v0
                            + (x3 - 2 * x2 + x) * dt * m0
                            + (-2 * x3 + 3 * x2) * v1
                            + (x3 - x2) * dt * m1)
            return keys[-1][1]
        if u <= keys[0][0]:
            return keys[0][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if u <= t1:
                f = 0.0 if t1 == t0 else (u - t0) / (t1 - t0)
                return v0 + (v1 - v0) * f
        return keys[-1][1]


    def _fgo_grad(keys, t):
        """Sample colorOverLifetime / alpha keys at normalised age."""
        if t <= keys[0][0]:
            return keys[0][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t <= t1:
                f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                if isinstance(v0, tuple):
                    return tuple(a + (b - a) * f for a, b in zip(v0, v1))
                return v0 + (v1 - v0) * f
        return keys[-1][1]


    def _fgo_mote_safe_size(w, h, rot_b):
        """Scale (w, h) down (preserving aspect) if a rotated Transform's own
        `rotate_pad` (hypot(w,h)-square) would exceed Ren'Py/GL's real
        texture-size limit, or if either raw axis alone would (rot_b==0:
        no padding, so each axis is its own limit). Returns (w, h), both
        >= 1."""
        if rot_b:
            diag = math.hypot(w, h)
            if diag > _FGO_MOTE_SAFE_PX:
                f = _FGO_MOTE_SAFE_PX / diag
                return max(1, int(w * f)), max(1, int(h * f))
            return w, h
        return min(w, _FGO_MOTE_SAFE_PX), min(h, _FGO_MOTE_SAFE_PX)


