# VN message, typewriter, gating, choice, and playback engine.
init -30 python:
    # ------------------------------------------------------------------
    # Message window
    # ------------------------------------------------------------------

    def vn_text_cps():
        """Ren'Py text speed preference: characters per second, 0 = instant (FGO default ~30)."""
        try:
            cps = int(round(float(preferences.text_cps)))
        except Exception:
            cps = 30
        return max(0, cps)

    def vn_text_speed_setting():
        """FGO-equivalent speed setting (cps / 10, as 3.0 = 30 cps) for the auto-wait formula."""
        cps = vn_text_cps()
        if cps <= 0:
            return 5.0
        return max(0.5, min(5.0, cps / 10.0))

    def vn_auto_wait_seconds():
        """InitMessageManager / GetAutoWaitTime: autoWaitSetting * 0.2 + 0.03125 * chars / (textSpeed / 3)."""
        setting = max(0.0, min(3.0, vn_float(renpy.store.vn_auto_wait, 0.0)))
        chars = len(renpy.store.vn_msg_text or "")
        speed = max(0.5, vn_text_speed_setting())
        return setting * 0.2 + 0.03125 * chars / (speed / 3.0)

    def vn_apply_player_settings():
        """Push the master name / gender into the script parser (used when a phase script is parsed)."""
        import fgo_parser
        fgo_parser.PLAYER_NAME = str(renpy.store.vn_player_name or "Fujimaru")
        fgo_parser.PLAYER_GENDER_INDEX = 1 if int(renpy.store.vn_player_gender or 0) else 0

    def vn_settings_changed():
        """Persist the reader settings to config.json and apply them."""
        try:
            renpy.store.vn_auto_wait = float(int(round(vn_float(renpy.store.vn_auto_wait, 0.0))))
            config_update("autoWait", renpy.store.vn_auto_wait)
            config_update("masterName", str(renpy.store.vn_player_name or ""))
            config_update("masterGender", int(renpy.store.vn_player_gender or 0))
            config_update("requireInput", bool(renpy.store.vn_require_input))
            config_update("offline", bool(renpy.store.vn_offline))
            api = getattr(renpy.store, "_atlas_api", None)
            if api is not None:
                api.offline = bool(renpy.store.vn_offline)
        except Exception:
            pass
        vn_apply_player_settings()

    def vn_set_language(region):
        """Switch Atlas region immediately and discard UI data from the old region."""
        region = str(region or "NA").upper()
        if region not in ("NA", "JP") or region == renpy.store.lang:
            return
        renpy.store.lang = region
        renpy.store.vn_language_changed = True
        config_update("language", region)
        renpy.store._atlas_api = AtlasAPI(region=region)
        renpy.store._atlas_api.offline = bool(renpy.store.vn_offline)
        renpy.store.war_list = []
        renpy.store.war_load_error = None
        renpy.store.selected_war_id = 0
        renpy.store.resume_marker = None
        vn_clear_chapter_select()
        renpy.restart_interaction()

    def vn_scroll_time():
        setting = max(0.5, float(VN_SCROLL_SPEED_SETTING))
        if setting >= 5.0:
            return 0.1
        return max(0.1, 0.3 / (setting / 3.0))

    def vn_text_width():
        return vn_px(VN_DISP_W)

    def vn_line_pitch():
        return vn_px(VN_LINE_PITCH)

    def vn_visible_height():
        return vn_px(VN_DISP_H)

    def vn_text_top_offset():
        """Y of the first label inside the text rect: startPosition (ruby + 2) minus the leading Ren'Py adds
        above the line, so the glyph box lands where NGUI's label top is."""
        return vn_px(VN_LINE_START_Y + VN_GLYPH_ADJUST) - vn_ensure_metrics()

    _vn_metrics = {"leading": None}

    def vn_ensure_metrics():
        """Measure the font's natural line height once and derive the leading that gives FGO's pitch."""
        if _vn_metrics["leading"] is not None:
            return _vn_metrics["leading"]
        leading = int(round((VN_LINE_PITCH - VN_FONT_SIZE) * upscale_ratio)) - int(VN_LINE_SPACING * upscale_ratio)
        try:
            probe = Text("Ag\nAg", style="vn_msg_text", slow=False, substitute=False, line_leading=0)
            renpy.render(probe, vn_text_width(), 4000, 0, 0)
            lines = vn_layout_lines(probe)
            if len(lines) >= 2:
                natural = lines[1][0] - lines[0][0]
                leading = max(0, int(math.ceil(vn_line_pitch() - natural)))
        except Exception:
            pass
        _vn_metrics["leading"] = leading
        return leading

    def vn_layout_lines(text_disp):
        """(y, height, max_time) per laid-out line, in virtual (1280x720) pixels. Text.get_layout() is in drawable
        pixels (window size dependent); the size-only virtual layout carries no line data, so scale instead."""
        layout = text_disp.get_layout()
        if layout is None:
            return []
        ratio = getattr(renpy.display.draw, "draw_per_virt", None) or 1.0
        return [(float(l.y) / ratio, float(l.height) / ratio, float(l.max_time)) for l in layout.lines]

    def vn_on_slow_done():
        if renpy.store.vn_text_done_at is not None:
            return   # a duplicate render's child can fire this again; act once per message
        renpy.store.vn_text_done_at = vn_now()
        renpy.store.vn_msg_forced = True   # ReturnScroll snaps to the newest line (fastScrollTime 0)
        body = renpy.store.vn_msg_body
        if body is not None:
            body.force_complete()
        renpy.restart_interaction()   # re-evaluate vn_message so the next-mark shows

    def vn_measure_lines(text_disp):
        """Lay the text out once to learn where each wrapped line sits and when it finishes typing."""
        try:
            renpy.render(text_disp, vn_text_width(), 4000, 0, 0)
            return vn_layout_lines(text_disp)
        except Exception:
            return []

    def vn_gate_eff_and_hold(steps, now=None):
        """Manual scroll: the effective typewriter time and whether the message is frozen at a gate.

        A "gate" is the bottom of the visible window (the start of the next unreleased roll). Time flows
        normally until it reaches a gate, then freezes there until the reader releases it; each release
        resumes the clock from that gate's boundary. This is a *pure* function of the store plus the wall
        clock, so it is safe to call from render() even when a screen renders a duplicate of the body
        (the reason the whole-message fast-forward flag also lives in the store, as vn_msg_forced)."""
        if now is None:
            now = vn_now()
        released = int(renpy.store.vn_msg_gates_released)
        if released <= 0:
            eff = now - float(renpy.store.vn_msg_start_wall)
        else:
            prev_gate = steps[released - 1][0]
            eff = prev_gate + (now - float(renpy.store.vn_msg_last_release_wall))
        holding = False
        if released < len(steps):
            gate = steps[released][0]
            if eff >= gate:
                eff = gate
                holding = True
        return eff, holding

    def vn_gate_active():
        """True while a manual-scroll message is mid-flight (gated, AUTO off, not finished)."""
        body = renpy.store.vn_msg_body
        return (body is not None and getattr(body, "gated", False)
                and not renpy.store.vn_auto_mode and renpy.store.vn_text_done_at is None)

    def vn_msg_is_holding():
        """True while the typewriter is frozen at a gate waiting for the reader (drives the next-mark)."""
        if not vn_gate_active():
            return False
        _, holding = vn_gate_eff_and_hold(renpy.store.vn_msg_body.steps)
        return holding

    class VNTextBody(renpy.display.layout.Container):
        """Message body: blits the text with a vertical scroll offset. `steps` is a list of
        (start_time, distance) rolls; each takes `roll_t` seconds with NGUI EaseInOut. `clock` is
        "st" (the AdjustTimes-rebased clock the typewriter uses) or "wall" (wall clock since creation).

        `base` is a constant scroll applied before the steps: the roll-out (vn_page_roll_out) uses it to
        start the outgoing text at the offset it already had on screen, so a scrolled message rolls away
        from where it sits instead of snapping back to its first lines.

        `gated` is FGO's manual scroll: when set, the typewriter and the roll both freeze at each roll
        boundary (the bottom of the visible window) until the reader taps. It is only honoured while AUTO
        mode is off, so AUTO and manual scroll never fight.

        render() is side-effect free: all gate state (release count, timestamps, the whole-message
        fast-forward flag) lives in the store and is written only by VNAdvance, so a duplicated render of
        this displayable behaves identically to the real one."""

        def __init__(self, child, steps, roll_t, clock="st", top=0, base=0, gated=False, pauses=(), **properties):
            super(VNTextBody, self).__init__(**properties)
            self.add(child)
            self.steps = list(steps)
            # Mid-line [wt] holds (vn_split_inline_waits): steps and gates are in elapsed time, the
            # typewriter child gets vn_text_time of it.
            self.pauses = list(pauses)
            self.roll_t = float(roll_t)
            self.clock = clock
            self.top = int(top)
            self.base = int(base)
            self.gated = bool(gated)
            self.t0 = vn_now()
            self.forced = False

        def force_complete(self):
            """isFastMessageRequest: a tap during typing shows the whole message and ReturnScroll snaps to
            the newest line (fastScrollTime is 0 for the main window)."""
            self.forced = True
            renpy.redraw(self, 0)

        def _offset_at(self, t):
            """Returns (scroll offset, still animating, offset of the rolls that have fully completed)."""
            # Screens may render a duplicate of this displayable, so the fast-forward flag lives in the store.
            if self.forced or (self.clock == "st" and renpy.store.vn_msg_forced):
                total = int(round(sum(dy for _, dy in self.steps)))
                return self.base + total, False, total
            offset = 0.0
            done = 0.0
            running = False
            for start, dy in self.steps:
                if t < start:
                    running = True
                    break
                f = (t - start) / self.roll_t if self.roll_t > 0 else 1.0
                if f >= 1.0:
                    offset += dy
                    done += dy
                else:
                    running = True
                    f = f * f * (3 - 2 * f)
                    offset += dy * f
            return self.base + int(round(offset)), running, int(round(done))

        def render(self, width, height, st, at):
            if self.forced or (self.clock == "st" and renpy.store.vn_msg_forced):
                # Reveal the whole message at once (feed the typewriter a time far past its end).
                child_st = 1.0e7
                offset, running, done = self._offset_at(0.0)
            elif self.gated and not renpy.store.vn_auto_mode:
                eff, holding = vn_gate_eff_and_hold(self.steps)
                child_st = vn_text_time(eff, self.pauses)
                offset, running, done = self._offset_at(eff)
                running = (running and not holding) or (bool(self.pauses) and eff < self.pauses[-1][1])
            else:
                base_t = st if self.clock == "st" else (vn_now() - self.t0)
                if self.pauses:
                    # Wall clock, so a tap can seek to the next [wt] (vn_msg_seek).
                    base_t = vn_now() - float(renpy.store.vn_msg_start_wall)
                child_st = vn_text_time(base_t, self.pauses)
                offset, running, done = self._offset_at(base_t)
                running = running or (bool(self.pauses) and base_t < self.pauses[-1][1])

            cr = renpy.render(self.child, width, height, child_st, at)
            # Lines whose roll has finished are dropped entirely (their descenders would otherwise peek
            # in at the top of the window, which NGUI's tighter label boxes never do).
            if 0 < done < cr.height:
                # Also eat most of the leading gap above the first visible line: glyph descenders overhang
                # the layout's line box by a few pixels.
                cut = min(cr.height - 1, done + max(0, vn_ensure_metrics() - 6))
                cr = cr.subsurface((0, cut, cr.width, cr.height - cut))
                offset -= cut
            rv = renpy.Render(cr.width, max(1, cr.height + self.top))
            rv.blit(cr, (0, self.top - offset))
            self.offsets = [(0, self.top - offset)]
            if running:
                renpy.redraw(self, 0)
            return rv

    import re
    _VN_MARKUP_RE = re.compile(r"\{\{|\{[^{}]*\}")

    def vn_inline_command_times(text, inline, cps):
        """[[typewriter_time, token], ...] for inline commands at character offsets into `text`: the time the
        character just before each one is typed (0 at the start of the line, or with instant text). Timing
        only depends on the glyphs, so the prefix is laid out with its markup removed."""
        timed = []
        for off, token in inline or []:
            t = 0.0
            prefix = text[:int(off)]
            if cps > 0 and prefix:
                plain = _VN_MARKUP_RE.sub(lambda m: m.group(0) if m.group(0) == "{{" else "", prefix)
                if plain:
                    probe = Text(plain, style="vn_msg_text", substitute=False, line_leading=vn_ensure_metrics(),
                                 slow_cps=cps, slow=True)
                    lines = vn_measure_lines(probe)
                    if lines:
                        t = max(l[2] for l in lines)
            timed.append([t, token])
        timed.sort(key=lambda e: e[0])
        return timed

    def vn_inline_wt_seconds(token):
        """A mid-line [wt N] becomes [wait time N] on a message line: waitCount drops by delta * GetTextSpeed
        (max(speed, ScenarioTextSpeedLow) / ScenarioTextSpeedDefault), so it lasts N at the default speed.
        At the top text speed the wait is zeroed."""
        parts = token.split()
        secs = max(0.0, vn_float(parts[1], 0.0)) if len(parts) >= 2 else 0.0
        speed = vn_text_speed_setting()
        if vn_text_cps() <= 0 or speed >= 5.0:
            return 0.0
        return secs / (speed / 3.0)

    def vn_split_inline_waits(timed):
        """Splits typewriter-timed inline tokens into commands and mid-line [wt] pauses. Returns
        ([[elapsed_time, token], ...], [(pause_start, pause_end, text_time), ...]) where elapsed time counts
        the pauses before each entry; a command written before a [wt] runs before it, one after runs after."""
        commands = []
        pauses = []
        held = 0.0
        for t, token in timed:
            if token.split()[0].lower() == "wt":
                d = vn_inline_wt_seconds(token)
                if d > 0.0:
                    pauses.append((t + held, t + held + d, t))
                    held += d
            else:
                commands.append([t + held, token])
        return commands, pauses

    def vn_text_time(elapsed, pauses):
        """Typewriter time after `elapsed` seconds of the message: the clock stands still inside a pause."""
        t = elapsed
        for start, end, text_t in pauses:
            if elapsed < start:
                break
            if elapsed < end:
                # Ren'Py shows the next glyph at exactly text_t, so hold just short of it.
                return text_t - 0.001
            t -= end - start
        return t

    def vn_elapsed_time(text_t, pauses):
        """Inverse of vn_text_time for a line start: a pause at or before `text_t` delays it."""
        return text_t + sum(end - start for start, end, t in pauses if t <= text_t)

    def vn_msg_pause_at(eff):
        """(inside a pause, start of the next pause or None) for the current message at elapsed time eff."""
        for start, end, _ in renpy.store.vn_msg_pauses or []:
            if start <= eff < end:
                return True, None
            if eff < start:
                return False, start
        return False, None

    def vn_msg_seek(eff, steps):
        """Moves the current message's elapsed-time anchor so its clock reads `eff` now."""
        released = int(renpy.store.vn_msg_gates_released)
        if released <= 0:
            renpy.store.vn_msg_start_wall = vn_now() - eff
        else:
            renpy.store.vn_msg_last_release_wall = vn_now() - (eff - steps[released - 1][0])

    def vn_build_message_disp(text, inline=None):
        leading = vn_ensure_metrics()
        cps = vn_text_cps()
        # slow_abortable is off: every tap goes through VNAdvance so completion, manual-scroll gates and
        # node advance are all driven from one place (Ren'Py's own click-to-finish would ignore the gates).
        text_disp = Text(text, style="vn_msg_text", substitute=False, line_leading=leading,
                         slow_cps=cps if cps > 0 else 0, slow_done=vn_on_slow_done,
                         slow=(cps > 0), slow_abortable=False)
        lines = vn_measure_lines(text_disp)
        renpy.store.vn_msg_last_line_y = int(lines[-1][0]) if lines else 0

        # ReturnScroll: once a line beyond the visible ones starts typing, roll up one pitch
        # (0.3 s EaseInOut) so the newest line sits at the bottom.
        pitch = vn_line_pitch()
        roll_t = vn_scroll_time()
        # Inline [messageShake]s fire as the typewriter reaches them (vn_msg_shake_promote, driven by the
        # vn_message shake transform); mid-line [wt]s hold the typewriter, so everything after them is later.
        commands, pauses = vn_split_inline_waits(vn_inline_command_times(text, inline, cps))
        schedule = []
        for i in range(VN_VISIBLE_LINES, len(lines)):
            start = lines[i - 1][2] if cps > 0 else 0.0
            schedule.append((vn_elapsed_time(start, pauses), pitch))

        renpy.store.vn_msg_forced = False
        renpy.store.vn_msg_gates_released = 0
        renpy.store.vn_msg_start_wall = vn_now()
        renpy.store.vn_msg_last_release_wall = vn_now()
        renpy.store.vn_msg_shake_pending = commands
        renpy.store.vn_msg_pauses = pauses
        # Final scroll once every line has rolled up: where the roll-out for the next same-speaker line
        # must begin so it continues from the on-screen position instead of jumping to the top.
        renpy.store.vn_msg_scroll = int(round(sum(dy for _, dy in schedule)))
        # Manual scroll only kicks in when there is something to scroll and AUTO mode is off.
        gated = bool(renpy.store.vn_require_input) and not renpy.store.vn_auto_mode and len(schedule) > 0
        body = VNTextBody(text_disp, schedule, roll_t, clock="st", top=vn_text_top_offset(), gated=gated,
                          pauses=pauses)
        renpy.store.vn_msg_body = body
        return renpy.display.layout.AdjustTimes(body, None, None)

    def vn_message_off():
        """[messageOff] / ClearText: instant hide, text cleared, talk name forgotten. The MessageShake node is
        not touched by ClearText, so a running [messageShake] carries on."""
        vn_msg_shake_promote(flush=True)
        renpy.store.vn_msg_visible = False
        renpy.store.vn_msg_disp = None
        renpy.store.vn_msg_body = None
        renpy.store.vn_msg_pauses = []
        renpy.store.vn_msg_text = ""
        renpy.store.vn_msg_speaker = ""
        renpy.store.vn_msg_last_name = ""
        renpy.store.vn_msg_last_slot = None
        renpy.store.vn_text_done_at = None
        renpy.store.vn_msg_scroll = 0
        renpy.store.vn_msg_gates_released = 0
        renpy.store.vn_msg_start_wall = vn_now()
        renpy.store.vn_msg_last_release_wall = vn_now()

    def vn_page_roll_out():
        """Same speaker: PageScroll the old text up and out, then the new text starts fresh."""
        if vn_skipping():
            return
        dur = vn_scroll_time()
        dy = renpy.store.vn_msg_last_line_y + vn_visible_height() // 2   # (startPos - dispPos) + dispSize.y / 2
        # The message may already be scrolled (it ran past the visible window); begin the roll-out from
        # that offset so it slides away from where it sits instead of snapping back to the first lines.
        base = int(renpy.store.vn_msg_scroll or 0)
        remaining = max(0, dy - base)
        old = Text(renpy.store.vn_msg_text, style="vn_msg_text", slow=False, substitute=False,
                   line_leading=vn_ensure_metrics())
        renpy.store.vn_msg_disp = VNTextBody(old, [(0.0, remaining)], dur, clock="wall",
                                             top=vn_text_top_offset(), base=base)
        renpy.store.vn_wait_until = vn_now() + dur
        renpy.pause(dur, hard=True)

    def vn_mask_is_opaque():
        anim = renpy.store.vn_mask
        if not anim:
            return False
        value, running = vn_anim_value(anim)
        return (not running) and value >= 1.0

    def vn_show_message(name, slot, text, inline=None):
        # Every command of the previous line has run by the time it was dismissed.
        vn_msg_shake_promote(flush=True)
        # [maskin] is used around FGO's name-entry dialog ([input name] ... [label inputName]); the game
        # lifts the CommonUI mask when that dialog closes. The reader has no dialog, so lift it before
        # the next message (MaskFade fade-in, DEFAULT_FADE_TIME).
        if vn_mask_is_opaque():
            vn_start_mask((renpy.store.vn_mask or {}).get("color", "black"), False, VN_DEFAULT_FADE_TIME)
            vn_wait_for("mask")
        # talkName3 waits for in-flight loads/fades (isWaitTalkMoveAlpha) before switching the highlight.
        vn_wait_for("charaFade")
        vn_apply_talk_line(name, slot)
        if renpy.store.vn_msg_visible and renpy.store.vn_msg_text and vn_is_equal_talk_name(name, slot):
            vn_page_roll_out()
        # ClearText + SetTalkName + AddText: all instant.
        renpy.store.vn_msg_speaker = name or ""
        renpy.store.vn_msg_text = text
        renpy.store.vn_msg_last_name = name or ""
        renpy.store.vn_msg_last_slot = slot
        renpy.store.vn_text_done_at = None
        renpy.store.vn_msg_disp = vn_build_message_disp(text, inline)
        renpy.store.vn_msg_visible = True
        # A script that never fades in would otherwise stay black.
        if renpy.store.vn_fade and not renpy.store.vn_phase_fade_seen:
            renpy.store.vn_fade = None

    def vn_complete_message():
        """Instantly reveal the whole message (auto-scroll tap, or a manual-scroll tap with no gate left).
        The flag lives in the store so a duplicated render honours it too; force_complete covers the
        real instance."""
        renpy.store.vn_msg_forced = True
        body = renpy.store.vn_msg_body
        if body is not None:
            body.force_complete()

    class VNAutoAdvance(Action):
        """AUTO: the wait has elapsed; FGO still holds the line while a voice is playing."""
        def __call__(self):
            if vn_voice_busy():
                if not renpy.store.vn_auto_voice_hold:
                    renpy.store.vn_auto_voice_hold = True
                    renpy.restart_interaction()
                return None
            return True

    class VNAdvance(Action):
        """Tap to continue. One tap handles, in order: finishing the typewriter (or filling the visible
        window in manual scroll), releasing a manual-scroll gate, and advancing to the next line. Ignored
        for defaultKeyDelayTime after the text finished."""
        def __call__(self):
            # Skip mode: instantly complete and advance
            if vn_skipping():
                vn_complete_message()
                if renpy.store.vn_text_done_at is None:
                    renpy.store.vn_text_done_at = vn_now()
                return True

            done = renpy.store.vn_text_done_at
            if done is not None:
                # Text fully shown: the next tap advances the line (after the key-delay grace period).
                if vn_now() - done < VN_KEY_DELAY:
                    return None
                return True

            # Text is still being revealed.
            body = renpy.store.vn_msg_body
            gated = body is not None and body.gated and not renpy.store.vn_auto_mode
            steps = body.steps if body is not None else []
            released = int(renpy.store.vn_msg_gates_released)
            if gated:
                eff, holding = vn_gate_eff_and_hold(steps)
                if holding:
                    # Frozen at the bottom of the visible window: release one roll and resume typing
                    # from that boundary.
                    renpy.store.vn_msg_gates_released = released + 1
                    renpy.store.vn_msg_last_release_wall = vn_now()
                    renpy.restart_interaction()
                    return None
            else:
                eff = vn_now() - float(renpy.store.vn_msg_start_wall)

            # A mid-line [wt] is [wait time] (not skippableTime): a tap never shortens it, and a tap while
            # typing toward one only fills the text up to it.
            in_pause, next_pause = vn_msg_pause_at(eff)
            if in_pause:
                return None
            gate = steps[released][0] if gated and released < len(steps) else None
            if next_pause is not None and (gate is None or next_pause < gate):
                vn_msg_seek(next_pause, steps)
                renpy.restart_interaction()
                return None

            if gated:
                if gate is not None:
                    # Still typing toward a gate: fast-forward to fill the visible window, then hold there
                    # (do NOT auto-advance to the next line). Rewind the relevant time anchor so the pure
                    # eff computation lands exactly on the gate and stays clamped.
                    vn_msg_seek(gate, steps)
                    renpy.restart_interaction()
                    return None
                # No gate left: reveal the rest of the (last) line at once.
                vn_complete_message()
                return None

            # Auto-scroll (default) mode: a tap completes the whole message instantly.
            vn_complete_message()
            return None


    def vn_say(node, api):
        speaker = node.get("speaker") or ""
        text = node.get("text") or ""
        if not text:
            return
        # An unrendered effect's timing never leaks past the dialogue it introduced.
        renpy.store.vn_unrendered_wait = False
        renpy.store.vn_unrendered_movie = False
        vn_apply_music_flag(get_scene_music_flag(node, api))
        vn_apply_inline_tags(node, api)
        record_dialogue(speaker, text)
        vn_show_message(speaker, node.get("speaker_slot"), text, node.get("inline_cmds"))
        # Commands before the first character ([charaFace A 3]Hello) apply before the line is first drawn.
        vn_msg_shake_promote()
        if vn_skipping():
            # Skip mode: complete text instantly and advance after a brief display pause
            vn_complete_message()
            vn_msg_shake_promote(flush=True)
            if renpy.store.vn_text_done_at is None:
                renpy.store.vn_text_done_at = vn_now()
            renpy.pause(0.02, hard=True)
            return
        renpy.store.vn_show_next_mark = True
        renpy.store.vn_auto_voice_hold = False
        renpy.call_screen("reader_advance")
        # The line's remaining inline commands have all run before the next script command does.
        vn_msg_shake_promote(flush=True)
        renpy.store.vn_show_next_mark = False
        renpy.store.vn_auto_voice_hold = False
        # The [k] touch wait ends: if isBusyVoice, stopVoiceAll(0) (a voice never outlives its line).
        vn_voice_stop()

    def vn_choose(options):
        # The choice screen waits for input even when skip is enabled. Keep the
        # toggle live so its indicator stays visible and Tab can change it.
        return renpy.call_screen("reader_choice", options)

    def vn_record_choice(selected):
        """JumpScriptSwitchCase: a menu after [input selectBranch] stores its 0-based index in the static
        select-branch index and the INPUT_SELECT_BRANCH flag (later scripts branch on it)."""
        if renpy.store.vn_select_branch_pending and selected is not None:
            renpy.store.vn_select_branch_pending = False
            renpy.store.vn_select_branch_index = int(selected)
            renpy.store.vn_script_flags["INPUT_SELECT_BRANCH"] = str(int(selected))

    def vn_flow_command(node):
        """Script flow control. Returns (handled, jump label or None).
        [label X]: jump target. [branch X]: jump. [branch X FLAG V]: jump when flag FLAG == V.
        [flag FLAG V]: set a flag. [masterBranch MALE FEMALE]: jump by master gender.
        [input selectBranch]: the next menu records its index (see vn_record_choice)."""
        if node.get("type") != "command":
            return False, None
        name = str(node.get("name") or "").lower()
        args = [str(a) for a in (node.get("args") or [])]
        if name == "label":
            return True, None
        if name == "branch":
            if not args:
                return True, None
            if len(args) >= 3:
                if renpy.store.vn_script_flags.get(args[1]) != args[2]:
                    return True, None
            return True, args[0]
        if name == "flag":
            if len(args) >= 2:
                renpy.store.vn_script_flags[args[0]] = args[1]
            return True, None
        if name == "masterbranch":
            if not args:
                return True, None
            gender = 1 if int(renpy.store.vn_player_gender or 0) else 0
            return True, args[min(gender, len(args) - 1)]
        if name == "input" and args and args[0] == "selectBranch":
            renpy.store.vn_select_branch_pending = True
            return True, None
        if name == "selectionuse":
            # [selectionUse masterMale|masterFemale]: the next menu only exists for that master gender.
            renpy.store.vn_selection_use = args[0] if args else None
            return True, None
        return False, None

    def vn_selection_allowed():
        """Consumes a pending [selectionUse]; False when the next menu belongs to the other gender."""
        cond = renpy.store.vn_selection_use
        renpy.store.vn_selection_use = None
        gender = 1 if int(renpy.store.vn_player_gender or 0) else 0
        if cond == "masterMale":
            return gender == 0
        if cond == "masterFemale":
            return gender == 1
        return True

    def vn_find_label(nodes, label, start):
        """SearchBranchLabel: forward from the current command first, then from the top."""
        order = list(range(start, len(nodes))) + list(range(0, start))
        for i in order:
            n = nodes[i]
            if n.get("type") == "command" and str(n.get("name") or "").lower() == "label":
                if [str(a) for a in (n.get("args") or [])][:1] == [label]:
                    return i
        return None

    def vn_play_nodes(nodes, api, nested=False):
        """Plays nodes in order. A [branch]/[masterBranch] jump whose [label] is not in this list (e.g. a
        [branch] inside a menu option) is returned to the enclosing call, which owns the label."""
        idx = 0
        while idx < len(nodes):
            node = nodes[idx]
            node_type = node.get("type")
            jump = None
            if node_type == "dialogue":
                vn_say(node, api)
            elif node_type == "choice":
                vn_apply_music_flag(get_scene_music_flag(node, api))
                choice_options, next_idx = collect_choice_options(nodes, idx)
                if choice_options and vn_selection_allowed():
                    vn_record_choice(vn_choose(choice_options))
                idx = next_idx - 1
            elif node_type == "choice_block":
                cb_choices = node.get("choices", [])
                cb_texts = [normalize_choice_text(c.get("text", "")) for c in cb_choices]
                if cb_texts and vn_selection_allowed():
                    selected = vn_choose(cb_texts)
                    vn_record_choice(selected)
                    if selected is not None and selected < len(cb_choices):
                        jump = vn_play_nodes(cb_choices[selected].get("nodes", []), api, nested=True)
            else:
                handled, jump = vn_flow_command(node)
                if not handled:
                    flag = get_scene_music_flag(node, api)
                    if flag:
                        vn_apply_music_flag(flag)
                    else:
                        apply_reader_node(node, api)
            if jump:
                target = vn_find_label(nodes, jump, idx)
                if target is not None:
                    idx = target
                elif nested:
                    return jump
                # A missing label at the top level: keep reading in order.
            idx += 1
        return None

    def vn_reset_stage():
        vn_fx_reset()
        renpy.store.current_background_path = None
        renpy.store.current_scene_id = None
        renpy.store.current_chara_defs = {}
        renpy.store.vn_bg = {}
        renpy.store.vn_busy = {}
        renpy.store.vn_mask = None
        renpy.store.vn_wipe = None
        renpy.store.vn_camera = None
        renpy.store.vn_blur = None               # InitSetting: StopGaussianBlur(0); fresh material (and filter)
        renpy.store.vn_back_camera_color = None  # ExitExecuteScript restores the MargeCamera clear
        vn_flash_reset()                         # InitSetting: InitFlashCommand
        renpy.store.vn_shake = None
        renpy.store.vn_msg_shake = None          # ProcessScript/ExitExecuteScript: messageManager.Shake(0,0,0,0)
        renpy.store.vn_msg_shake_pending = []
        renpy.store.vn_pictureframe = None
        renpy.store.vn_sublayers = {}
        renpy.store.vn_substretch_enabled = True
        renpy.store.vn_skip_allowed = True
        renpy.store.vn_unrendered_wait = False
        renpy.store.vn_unrendered_movie = False
        renpy.store.vn_talk_mask_enabled = True
        renpy.store.vn_talk_depth_enabled = True
        renpy.store.vn_talk_mask_name = ""
        renpy.store.vn_talk_index = None
        vn_se_stop_all()
        vn_voice_stop()
        vn_message_off()

    def vn_begin_phase(nodes=None):
        """Each script starts behind the black mask (ScriptManager DEFAULT_FADE_TIME); the script's own
        [fadein] reveals the stage."""
        vn_reset_stage()
        # EndFirstLoadAsset sizes the PictureFrame for this script (isPictureFrameForceChangeSize is static and
        # is not reset here).
        renpy.store.vn_script_full_screen = vn_nodes_enable_full_screen(nodes)
        renpy.store.vn_pictureframe_width = vn_picture_frame_script_width()
        renpy.store.vn_fade = {"from": 1.0, "to": 1.0, "start": vn_now(), "dur": 0.0, "color": "#000000"}
        renpy.store.vn_phase_fade_seen = False
        # Each script's flag list starts with INPUT_SELECT_BRANCH = the static select-branch index.
        renpy.store.vn_script_flags = {"INPUT_SELECT_BRANCH": str(int(renpy.store.vn_select_branch_index or 0))}
        renpy.store.vn_select_branch_pending = False
        renpy.store.vn_selection_use = None
