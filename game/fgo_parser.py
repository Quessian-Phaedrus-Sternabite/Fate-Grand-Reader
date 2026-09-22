import re
import shlex

TAG_REGEX = re.compile(r"\[([^\]]+)\]")
PAGE_TOKENS = {"page", "k", "nopage", "newline"}
LINE_BREAK_TAGS = {"r", "sr"}
SPEAKER_MARKERS = ("\uff20", "@", "\xef\xbc\xa0")
CHOICE_MARKERS = ("\uff1f", "?", "\xef\xbc\x9f")
FULLWIDTH_COLONS = ("\uff1a", "\xef\xbc\x9a")
MUSIC_MARKERS = ("\u266a", "\u266b", "\xe2\x99\xaa", "\xe2\x99\xac")
CHOICE_END_TEXT = ("\uff01", "!")

# Player-name / gender substitutions used by FGO text ([%1], [&him:her]).
PLAYER_NAME = "Fujimaru"
PLAYER_GENDER_INDEX = 1  # 0 = first alternative (male), 1 = second (female)

# FGO font-size presets in NGUI units (ScriptMessageLabel: default 29); Ren'Py {size=} deltas.
FONT_SIZE_DELTAS = {"x-small": 19 - 29, "small": 24 - 29, "medium": 0, "large": 48 - 29, "x-large": 64 - 29}
COLOR_TAG_REGEX = re.compile(r"^[0-9a-fA-F]{6}$")
RENPY_MARKUP_RE = re.compile(r'\{[^}]*\}')


def _clean_speaker_name(name):
    """Remove Ren'Py text markup from a speaker name so it matches slot names."""
    return RENPY_MARKUP_RE.sub('', name).strip()


def _inline_tag_markup(token: str):
    """Convert an in-text FGO tag into Ren'Py text markup.

    Returns None when the tag is not an inline-display tag, so it is recorded
    in the node tags instead.
    """
    stripped = token.strip()
    if COLOR_TAG_REGEX.match(stripped):
        return "{color=#%s}" % stripped.lower()
    if stripped == "-":
        return "{/color}"
    parts = stripped.split()
    head = parts[0].lower() if parts else ""
    if head == "line":
        try:
            n = max(1, int(parts[1]))
        except Exception:
            n = 1
        return "―" * n  # [line N] = horizontal dash rule N units long (used as an em-dash)
    if head.startswith("%"):
        return PLAYER_NAME
    if stripped.startswith("&") and ":" in stripped:
        options = stripped[1:].split(":")
        return options[min(PLAYER_GENDER_INDEX, len(options) - 1)]
    if head == "f":
        if len(parts) >= 2 and parts[1] == "-":
            return "{/size}"
        delta = FONT_SIZE_DELTAS.get(parts[1] if len(parts) >= 2 else "", 0)
        return "{size=%+d}" % delta
    return None

def _strip_script_tags(text: str) -> tuple[str, dict[str, str]]:
    tags: dict[str, str] = {}

    def replace_tag(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return ""

        markup = _inline_tag_markup(token)
        if markup is not None:
            return markup

        if "=" in token:
            key, value = token.split("=", 1)
            tag_key = key.strip().lower()
            tags[tag_key] = value.strip()
        else:
            tag_key = token.strip().lower()
            # Keep the full token so scene-time flags like "bgm BGM_EVENT_2 0.1" can be replayed.
            tags[tag_key] = token

        if tag_key in LINE_BREAK_TAGS:
            return "\n"

        return ""

    # Escape literal braces first so raw text can never be mistaken for Ren'Py markup.
    text = text.replace("{", "{{")
    cleaned = TAG_REGEX.sub(replace_tag, text)
    return cleaned.strip(), tags


def _parse_dialogue_line(line: str) -> tuple[str, str]:
    content = line[1:].strip()
    speaker = content
    message = ""
    for colon in FULLWIDTH_COLONS + (":",):
        if colon in content:
            speaker, message = content.split(colon, 1)
            break
    return speaker.strip(), message.strip()


def _parse_speaker_line(line: str) -> tuple[str, str | None]:
    """FGO speaker lines: ``＠Name`` (name only), ``＠C：Name`` (slot letter + name), bare ``＠``.
    Returns (display_name, slot_letter_or_None). Mirrors ScriptMessageLabel.GetTalkName: a single
    A-Z token before the colon is the chara slot that gets the talk highlight."""
    marker, text = _parse_dialogue_line(line)
    if text or (marker and len(marker) == 1 and marker.isalpha() and marker.isupper()):
        if len(marker) == 1 and marker.isalpha() and marker.isupper():
            return text, marker
        return (text or marker), None
    return marker, None


def _parse_command_node(token: str):
    # Whole-line commands change reader state; inline tags stay attached to dialogue nodes.
    try:
        token_parts = shlex.split(token)
    except ValueError:
        token_parts = token.split()
    command = token_parts[0].lower() if token_parts else ""
    if command == "scene" and len(token_parts) >= 2:
        return {
            "type": "scene",
            "scene_id": token_parts[1],
            "tags": {"scene": token},
        }
    if command == "bgm" and len(token_parts) >= 2:
        return {"type": "bgm", "name": token_parts[1], "tags": {"bgm": token}}
    if command in ("bgmstop", "soundstopall"):
        return {"type": "audio_stop", "tags": {command: token}}
    if command == "charaset" and len(token_parts) >= 4:
        # [charaSet SLOT CHARA_ID FORM NAME] -- token_parts[3] is the FORM/limitCount (ascension/costume),
        # NOT a screen position. Proven by the same id appearing with different values (98002000 0/1/2 Fou,
        # 98001000 0/1 Mash). Placement comes from a later charaFadein/charaPut, so charaSet sets no pos.
        return {
            "type": "chara_set",
            "slot": token_parts[1],
            "chara_id": token_parts[2],
            "form": token_parts[3],
            "name": token_parts[4] if len(token_parts) >= 5 else "",
            "tags": {"charaSet": token},
        }
    if command == "charaface" and len(token_parts) >= 3:
        return {
            "type": "chara_face",
            "slot": token_parts[1],
            "face": token_parts[2],
            "tags": {"charaFace": token},
        }
    if command == "charafadein" and len(token_parts) >= 2:
        return {
            "type": "chara_show",
            "slot": token_parts[1],
            "position": token_parts[3] if len(token_parts) >= 4 else None,
            "tags": {"charaFadein": token},
        }
    if command == "charafadeout" and len(token_parts) >= 2:
        return {
            "type": "chara_hide",
            "slot": token_parts[1],
            "tags": {"charaFadeout": token},
        }
    if command == "charafilter" and len(token_parts) >= 3:
        return {
            "type": "chara_filter",
            "slot": token_parts[1],
            "filter": token_parts[2],
            "tags": {"charaFilter": token},
        }
    if command == "communicationchara" and len(token_parts) >= 6:
        return {
            "type": "communication_chara",
            "chara_id": token_parts[1],
            "position": token_parts[2],
            "depth": token_parts[3],
            "noisekind": token_parts[4],
            "face": token_parts[5],
            "tags": {"communicationChara": token},
        }
    if command == "communicationcharaface" and len(token_parts) >= 2:
        return {
            "type": "communication_charaface",
            "face": token_parts[1],
            "tags": {"communicationCharaFace": token},
        }
    if command == "communicationcharaloop" and len(token_parts) >= 6:
        return {
            "type": "communication_charaloop",
            "chara_id": token_parts[1],
            "position": token_parts[2],
            "depth": token_parts[3],
            "noisekind": token_parts[4],
            "face": token_parts[5],
            "tags": {"communicationCharaLoop": token},
        }
    if command == "communicationcharaclear":
        return {
            "type": "communication_characlear",
            "tags": {"communicationCharaClear": token},
            }
    if command and command not in PAGE_TOKENS:
        # Everything else (charaTalk, messageOff, wt, wait, fadein, charaMove, ...) becomes a generic
        # command node so the reader can dispatch it by name without re-parsing the raw token.
        return {"type": "command", "name": token_parts[0], "args": token_parts[1:], "tags": {token_parts[0]: token}}
    return None

def _is_choice_end_marker(line: str) -> bool:
    return line[1:].strip() in CHOICE_END_TEXT


def _is_choice_start_marker(line: str) -> bool:
    """True only for FGO's numbered option syntax (``？1：...`` / ``?1:...``).

    Dialogue frequently consists of a bare ``?`` or ``???``. Treating every question-mark-prefixed
    line as a menu opener made the parser consume all content up to the next unrelated ``？！``.
    """
    return bool(re.match(r"^[？?]\d+[：:]", line))


def _collect_choice_branches(block_lines: list[str]) -> list[dict]:
    """Returns a list of dicts::
        {"text": str, "tags": dict, "branch_lines": list[str]}
    """
    branches: list[dict] = []
    current_text: str | None = None
    current_tags: dict = {}
    current_branch: list[str] = []

    for line in block_lines:
        if _is_choice_start_marker(line):
            if current_text is not None:
                branches.append(
                    {"text": current_text, "tags": current_tags, "branch_lines": current_branch}
                )
            stripped, tags = _strip_script_tags(line)
            current_text = stripped[1:].strip()
            current_tags = tags
            current_branch = []
        else:
            if current_text is not None:
                current_branch.append(line)

    if current_text is not None:
        branches.append(
            {"text": current_text, "tags": current_tags, "branch_lines": current_branch}
        )
    return branches


def _parse_lines(lines: list[str], initial_speaker: str = "") -> tuple[list[dict], str]:
    nodes: list[dict] = []
    current_speaker = initial_speaker
    current_speaker_slot = None
    i = 0

    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue

        # ？！ end-of-choices marker appearing outside a block — skip.
        if line.startswith(CHOICE_MARKERS) and _is_choice_end_marker(line):
            i += 1
            continue

        # Choice start: collect the entire block up to the ？！ end marker.
        if _is_choice_start_marker(line):
            block_end = len(lines)
            for j in range(i, len(lines)):
                if lines[j].startswith(CHOICE_MARKERS) and _is_choice_end_marker(lines[j]):
                    block_end = j
                    break

            block_lines = lines[i:block_end]
            branches = _collect_choice_branches(block_lines)

            # Always emit a choice_block so playback uses one branch-aware path: the reader shows the
            # menu, then plays only the selected option's nodes (an option with no inline content just
            # resumes the common script after the ？！ marker). Emitting flat "choice" nodes here would
            # drop the player's selection, so real branches (option A vs option B lead to different
            # dialogue) would never diverge.
            if branches:
                parsed_choices = []
                for branch in branches:
                    branch_nodes, _ = _parse_lines(branch["branch_lines"], current_speaker)
                    parsed_choices.append(
                        {"text": branch["text"], "tags": branch["tags"], "nodes": branch_nodes}
                    )
                nodes.append({"type": "choice_block", "choices": parsed_choices})

            i = block_end + 1
            continue

        if line.startswith("[") and line.endswith("]") and "]" not in line[1:-1]:
            token = line[1:-1].strip()
            token_lower = token.lower()
            command_node = _parse_command_node(token)
            if command_node:
                nodes.append(command_node)
                i += 1
                continue
            if token_lower in PAGE_TOKENS:
                i += 1
                continue

        # A small number of official scripts contain unterminated charaSet lines, for example
        # ``[charaSet J 1098191900 1 "Olympus Official Guard"``.  They are commands in FGO, but
        # treating only well-bracketed lines as commands made the reader print them as dialogue and
        # left all subsequent operations on those slots as no-ops.  Recover this one known malformed
        # command without interpreting ordinary text that begins with an inline tag (``[f large]...``).
        if line.startswith("[") and "]" not in line and line[1:].lstrip().lower().startswith("charaset "):
            command_node = _parse_command_node(line[1:].strip())
            if command_node and command_node.get("type") == "chara_set":
                nodes.append(command_node)
                i += 1
                continue

        line, tags = _strip_script_tags(line)
        if not line:
            if tags:
                nodes.append({"type": "metadata", "tags": tags})
            i += 1
            continue
        if line.startswith(("$", "\uff04")):
            i += 1
            continue

        if line.startswith(SPEAKER_MARKERS):
            name, slot = _parse_speaker_line(line)
            # Speaker names may carry inline tags too ([51d4ff]Announcement[-], [%1]).
            current_speaker, _ = _strip_script_tags(name or "")
            current_speaker = _clean_speaker_name(current_speaker)
            current_speaker_slot = slot
            i += 1
            continue

        if line.startswith(MUSIC_MARKERS):
            line = line.lstrip("".join(MUSIC_MARKERS)).strip()

        nodes.append(
            {
                "type": "dialogue",
                "speaker": current_speaker,
                "speaker_slot": current_speaker_slot,
                "text": line,
                "tags": tags,
            }
        )
        i += 1

    return nodes, current_speaker


def parse_script_text(raw_text: str) -> list[dict[str, str]]:
    if not raw_text:
        return []
    lines = [line.strip() for line in raw_text.splitlines()]
    nodes, _ = _parse_lines(lines)
    return nodes
