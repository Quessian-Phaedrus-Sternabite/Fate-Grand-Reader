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


def _strip_script_tags(text: str) -> tuple[str, dict[str, str]]:
    tags: dict[str, str] = {}

    def replace_tag(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        if not token:
            return ""

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
        return {
            "type": "chara_set",
            "slot": token_parts[1],
            "chara_id": token_parts[2],
            "position": token_parts[3],
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
    if command == "charafilter" and len(token_parts) >= 2:
        return {
            "type": "chara_filter",
            "slot": token_parts[1],
            "filter": token_parts[2],
            "tags": {"charaFilter": token},
        }
    if command == "communicationchara" and len(token_parts) >= 5:
        return {
            "type": "communication_chara",
            "chara_id": token_parts[1],
            "position": token_parts[2],
            "depth": token_parts[3],
            "noisekind": token_parts[4],
            "face": token_parts[5],
            "tags": {"communicationChara": token},
        }
    if command == "communicationcharaface" and len(token_parts) >= 1:
        return {
            "type": "communication_charaface",
            "face": token_parts[1],
            "tags": {"communicationCharaFace": token},
        }
    if command == "communicationcharaloop" and len(token_parts) >= 5:
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
    return None



def _is_choice_end_marker(line: str) -> bool:
    return line[1:].strip() in CHOICE_END_TEXT


def _collect_choice_branches(block_lines: list[str]) -> list[dict]:
    """Returns a list of dicts::
        {"text": str, "tags": dict, "branch_lines": list[str]}
    """
    branches: list[dict] = []
    current_text: str | None = None
    current_tags: dict = {}
    current_branch: list[str] = []

    for line in block_lines:
        if line.startswith(CHOICE_MARKERS) and not _is_choice_end_marker(line):
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
        if line.startswith(CHOICE_MARKERS):
            block_end = len(lines)
            for j in range(i, len(lines)):
                if lines[j].startswith(CHOICE_MARKERS) and _is_choice_end_marker(lines[j]):
                    block_end = j
                    break

            block_lines = lines[i:block_end]
            branches = _collect_choice_branches(block_lines)
            has_branch_content = any(any(l for l in b["branch_lines"]) for b in branches)

            if has_branch_content:
                parsed_choices = []
                for branch in branches:
                    branch_nodes, _ = _parse_lines(branch["branch_lines"], current_speaker)
                    parsed_choices.append(
                        {"text": branch["text"], "tags": branch["tags"], "nodes": branch_nodes}
                    )
                nodes.append({"type": "choice_block", "choices": parsed_choices})
            else:
                for branch in branches:
                    nodes.append(
                        {"type": "choice", "text": branch["text"], "tags": branch["tags"]}
                    )

            i = block_end + 1
            continue

        if line.startswith("[") and line.endswith("]"):
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
            speaker_marker, text = _parse_dialogue_line(line)
            current_speaker = text or speaker_marker or ""
            i += 1
            continue

        if line.startswith(MUSIC_MARKERS):
            line = line.lstrip("".join(MUSIC_MARKERS)).strip()

        nodes.append(
            {
                "type": "dialogue",
                "speaker": current_speaker,
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