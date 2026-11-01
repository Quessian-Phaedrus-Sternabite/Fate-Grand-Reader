# Native ProgramEffectManager figure effects; see docs/native-figure-shader-probe.md.
init 10 python:
    VN_SPECIAL_FIGURE_EFFECTS = {
        "appearance": (1.0,2.0,None,None), "appearancereverse": (1.0,2.0,None,None),
        "erasure": (0.0,2.0,None,None), "erasurereverse": (0.0,2.0,None,None),
        "enemyerasure": (0.0,2.0,None,None), "flasherasure": (0.0,2.0,None,"bit_talk_17"),
        "darkenemyerasure": (0.0,2.0,None,"bit_talk_19"), "wipe": (0.0,0.0,5.0,None),
        "darkwipe": (0.0,0.0,5.0,"bit_talk_19"), "wipetime": (0.0,0.0,None,None),
        "wipetimere": (1.0,0.0,None,None), "flash": (0.0,0.0,5.0,"bit_talk_17"),
    }
    VN_SPECIAL_GRADATION = 0.2
    VN_SPECIAL_MAX_VOLUME = 1.2039
    VN_SPECIAL_BODY_HEIGHT = 766.0  # UIStandFigureRender cctor, rodata 0xE93C70.

    def vn_special_volume(rec, now=None):
        # UITweener.Sample method 1, ARM64 0x35B10E0: 1+sin((1-factor)*(-pi/2)).
        now = vn_now() if now is None else now
        f = 0.0 if rec["paused"] else (1.0 if rec["duration"] <= 0.0 else
                                      min(1.0, max(0.0, (now-rec["start"])/rec["duration"])))
        eased = 1.0 - math.cos(f * math.pi * 0.5)
        return rec["volume_from"] + (rec["volume_to"]-rec["volume_from"])*eased

    def vn_special_end(rec):
        return None if rec["paused"] else rec["end"]

    def vn_special_alive(rec, now=None):
        now = vn_now() if now is None else now
        return rec["paused"] or now < rec["end"]

    def vn_special_active(data, now=None):
        # Keep the terminal material until the lifecycle timer destroys it. Dropping
        # it in render before that store update could briefly reveal the original sprite.
        records = data.get("special_effects", [])
        return records[-1] if records else None

    def vn_special_select(args):
        slot = args[0] if args else None
        name = vn_convert_name(args[1]).lower() if len(args)>1 else None
        return [(data, rec) for key, data in store.current_chara_defs.items()
                if slot is None or key == slot
                for rec in data.get("special_effects", [])
                if (name is None or rec["name"] == name) and vn_special_alive(rec)]

    def vn_special_finish(data, rec):
        data["alpha"], data["alpha_anim"] = rec["target"], None
        child = rec.get("child")
        store.vn_effects = [fx for fx in store.vn_effects if fx is not child]
        records = data.get("special_effects", [])
        records[:] = [r for r in records if r is not rec]

    def vn_special_tick():
        # Store writes occur in commands/the screen timer, never in render callbacks.
        now = vn_now()
        changed = False
        for data in store.current_chara_defs.values():
            for rec in list(data.get("special_effects", [])):
                if rec["paused"]:
                    continue
                if rec["name"] == "flash" and not rec["flash_fired"] and now >= rec["start"]+rec["duration"]:
                    rec["flash_fired"] = True
                    when = rec["start"]+rec["duration"]
                    store.vn_fade = {"start":when,"dur":1.0,"from":0.0,"to":1.0,"color":"#ffffff"}
                    store.vn_busy["fade"] = when+1.0
                    store.vn_phase_fade_seen = True
                    changed = True
                if rec["name"] == "flash" and rec["flash_fired"]:
                    fade = getattr(store,"vn_fade",None)
                    if fade:
                        rec["end"] = max(rec["end"],fade.get("start",now)+fade.get("dur",0.0))
                if now >= rec["end"]:
                    vn_special_finish(data,rec)
                    changed = True
        return changed

    def vn_special_pulse():
        if vn_special_tick():
            renpy.restart_interaction()

    def vn_special_pending():
        return any(data.get("special_effects") for data in store.current_chara_defs.values())

    def vn_special_deadline():
        # Rendering drives the volume; only lifecycle deadlines need store updates.
        deadlines = []
        for data in store.current_chara_defs.values():
            for rec in data.get("special_effects",[]):
                if not rec["paused"]:
                    deadlines.append(rec["end"])
                    if rec["name"] == "flash" and not rec["flash_fired"]:
                        deadlines.append(rec["start"]+rec["duration"])
        return min(deadlines) if deadlines else None

    def vn_special_delay():
        deadline = vn_special_deadline()
        return max(0.001,deadline-vn_now()) if deadline is not None else None

    def vn_special_resume(data, rec):
        rec["paused"] = False
        rec["start"] = vn_now()
        rec["end"] = rec["start"]+rec["duration"]
        child = rec.get("child")
        if vn_skipping():
            if rec["name"] == "wipetimere":
                rec["target"] = 0.0
            if rec["name"] == "flash":
                vn_start_fade("white",False,0.0)
            vn_special_finish(data,rec)
            return
        if child is not None:
            child["paused"],child["start"],child["stop"] = False,rec["start"],rec["duration"]
            tail = _fgo_fx_done_time(fgo_fx_defs[child["name"]],child["stop"])
            if tail is not None:
                rec["end"] = max(rec["end"],child["start"]+tail)
        if rec["name"] == "flash":
            rec["end"] = max(rec["end"],rec["start"]+rec["duration"]+1.0)

    def vn_special_child_offset(child, now):
        rec = child.get("special")
        if rec is None or rec["mode"] >= 2:
            return (0.0,0.0)
        v = max(0.0,vn_special_volume(rec,now)-0.2)
        y = -v*VN_SPECIAL_BODY_HEIGHT if rec["mode"] == 1 else (v-1.0)*VN_SPECIAL_BODY_HEIGHT
        return (0.0,y)

    def vn_special_child_visible(child, now=None):
        rec = child.get("special")
        return rec is None or rec["mode"] >= 2 or vn_special_volume(rec,now)>0.2

    def vn_special_stage_items(group):
        result = []
        for slot,data in store.current_chara_defs.items():
            if data.get("sub_group") != group:
                continue
            for rec in data.get("special_effects",[]):
                if rec["name"] == "flash" and vn_special_alive(rec):
                    result.append((20.0+10.0*vn_effective_depth(data)-2.0,
                                   1,rec["serial"],"fx",{"display":rec["back_display"]}))
        return result

    def vn_special_command(name,args):
        if name not in ("charaspecialeffect","charaspecialeffectpause","charaspecialeffectstart","charaspecialeffectstop"):
            return False
        vn_special_tick()
        if name.endswith("start") or name.endswith("stop"):
            for data,rec in vn_special_select(args):
                if name.endswith("start"):
                    if rec["paused"]:
                        vn_special_resume(data,rec)
                else:
                    vn_special_finish(data,rec)
            return True
        if len(args)<2:
            return True
        slot,effect = args[0],vn_convert_name(args[1]).lower()
        data = store.current_chara_defs.get(slot)
        if data is None or effect not in VN_SPECIAL_FIGURE_EFFECTS:
            return True
        if data.get("kind") in ("image","script_image") or data.get("communication_chara"):
            return True
        target,default,forced,child_name = VN_SPECIAL_FIGURE_EFFECTS[effect]
        duration = vn_float(args[3],0.0) if len(args)>3 else 0.0
        duration = forced if forced is not None else (duration if duration>0.0 else default)
        mode = 3 if effect == "flash" else (2 if "wipe" in effect else (1 if "reverse" in effect else 0))
        color = ((1.,1.,1.,1.) if effect == "flasherasure" else
                 ((60./255.,0.,0.,1.) if effect in ("enemyerasure","darkenemyerasure","darkwipe") else
                  ((1.,1.,200./255.,1.) if effect == "flash" else (0.,0.,0.,0.))))
        range_value = vn_float(args[5],0.0) if len(args)>5 else 0.0
        maximum = VN_SPECIAL_MAX_VOLUME-range_value if effect in ("wipetime","wipetimere") else VN_SPECIAL_MAX_VOLUME
        initial,final = (0.2,1.0) if effect.startswith("appearance") else ((0.,maximum) if effect == "wipetimere" else (maximum,0.))
        store.vn_fx_serial += 1
        rec = dict(name=effect,target=target,duration=duration,start=vn_now(),end=0.,paused=True,
                   child=None,mode=mode,color=color,volume_from=initial,volume_to=final,
                   mask="circleIn" if effect == "flash" else "circleOut",flash_fired=False,
                   serial=store.vn_fx_serial,offset=vn_parse_position(args[2]) if len(args)>2 and "," in args[2] else (0.,0.))
        data.setdefault("special_effects",[]).append(rec)
        if target == 1.0:
            data["alpha"],data["alpha_anim"] = 1.0,None
        # StopShadowEffect stops particles, not the logical isShadow/tint flag.
        if child_name in fgo_fx_defs and not vn_skipping():
            serial = store.vn_fx_serial
            vn_fx_command("charaeffectpause",[slot,child_name,args[2] if len(args)>2 else "1"])
            rec["child"] = next((fx for fx in store.vn_effects if fx["serial"]>serial),None)
            if rec["child"] is not None:
                rec["child"]["anchor"],rec["child"]["special"] = "specialeffect",rec
        if effect == "flash" and not vn_skipping():
            rec["back_display"] = vn_special_back_display(slot,data,rec)
        if not name.endswith("pause") or vn_skipping():
            vn_special_resume(data,rec)
        return True

    def vn_special_wait(tag,args):
        if tag.lower() not in ("charaspecialeffect","charaspecialeffectstart"):
            return False
        if tag.lower().endswith("start") or vn_skipping():
            return True
        vn_special_tick()
        while vn_special_select(args) and not vn_skipping():
            renpy.pause(0.03,hard=True)
            vn_special_tick()
        vn_fx_prune()
        return True
