"""Read Workshop update state from Steam's own local manifest.

WHY THIS EXISTS
---------------
The bot detected mod updates through the keyless Steam Web API endpoint
``ISteamRemoteStorage/GetPublishedFileDetails``. That endpoint **cannot see an
unlisted Workshop item**: it answers ``result: 9`` with no ``time_updated`` and
no ``title`` at all.

The caller never checked ``result``. It took the entry anyway and substituted
defaults -- ``time_updated`` became ``0`` and the title became "Unknown Mod" --
so the comparison ``info['time'] > previous['time']`` was ``0 > 0``, which is
false forever. An unlisted mod could therefore **never** report an update, in
silence, and the poisoned ``0`` was then written back over the saved baseline.

That matters because the mods now distributed as unlisted items are the ones
changing fastest, and the same check drives the automatic restart pipeline. The
server would quietly keep running a stale build indefinitely.

WHAT THIS READS INSTEAD
-----------------------
``steamapps/workshop/appworkshop_<appid>.acf`` is Steam's own bookkeeping, and it
records per item BOTH what is installed and what Steam last saw as available::

    "3749697135"
    {
        "manifest"            "2050755134397703812"
        "timeupdated"         "1787445576"
        "latest_manifest"     "2050755134397703812"
        "latest_timeupdated"  "1787445576"
    }

So ``latest_timeupdated`` is the update signal, it needs no API, no credentials
and no key, and visibility does not restrict it -- an unlisted item is as
readable as a public one.

THE LIMIT, STATED
-----------------
``latest_*`` is refreshed when something asks Steam, not continuously. If nothing
polls, these values can lag. This is therefore an accurate report of *what Steam
last told this machine*, which is not the same as *what is on Steam right now*.
For the auto-restart path that is usually what you want anyway -- it answers "is
the installed copy behind?", which is the question that decides a restart -- but
do not read it as a live query.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

APPID = "108600"

# One flat "<id> { ... }" block. Non-greedy and brace-free inside, so it matches
# each leaf block rather than swallowing the enclosing section.
_BLOCK = r'"%s"\s*\{([^{}]*)\}'
_KV = re.compile(r'"([A-Za-z_]+)"\s+"([^"]*)"')


def acf_path_from_mods_folder(mods_folder: str) -> Optional[Path]:
    """MODS_FOLDER_PATH is .../steamapps/workshop/content/<appid>.

    The manifest sits two levels up, beside `content/`.
    """
    if not mods_folder:
        return None
    content_dir = Path(mods_folder)
    workshop_dir = content_dir.parent.parent          # .../steamapps/workshop
    candidate = workshop_dir / ("appworkshop_%s.acf" % APPID)
    return candidate if candidate.is_file() else None


def _blocks_for(text: str, item_id: str) -> List[Dict[str, str]]:
    """Every flat block keyed by this id. The id appears more than once -- once
    under WorkshopItemsInstalled and once under WorkshopItemDetails -- and only
    the latter carries the `latest_*` keys."""
    out = []
    for body in re.findall(_BLOCK % re.escape(item_id), text):
        out.append({k: v for k, v in _KV.findall(body)})
    return out


def read_item_times(acf_file: Path, item_ids: List[str]) -> Dict[str, int]:
    """Return {item_id: latest_timeupdated} for the ids present in the manifest.

    Falls back to `timeupdated` when `latest_timeupdated` is absent, which is the
    honest reading: with nothing newer recorded, installed IS the latest Steam
    told us about. Ids the manifest does not mention are omitted rather than
    reported as 0 -- a missing entry must not look like "never updated", which is
    the exact mistake this module exists to correct.
    """
    try:
        text = acf_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    times: Dict[str, int] = {}
    for item_id in item_ids:
        best = None
        for block in _blocks_for(text, item_id):
            for key in ("latest_timeupdated", "timeupdated"):
                raw = block.get(key)
                if raw and raw.isdigit():
                    value = int(raw)
                    if best is None or value > best:
                        best = value
                    break
        if best is not None:
            times[item_id] = best
    return times


def read_item_title(mods_folder: str, item_id: str) -> Optional[str]:
    """Best-effort human name for an item, from the mod.info it shipped.

    The manifest has no titles, and the Web API cannot supply one for an unlisted
    item, so without this an unlisted update would be announced as "Unknown Mod"
    -- true, and useless to whoever reads the message.
    """
    try:
        base = Path(mods_folder) / item_id / "mods"
        if not base.is_dir():
            return None
        for mod_dir in sorted(base.iterdir()):
            info = mod_dir / "mod.info"
            if not info.is_file():
                continue
            for line in info.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.lower().startswith("name="):
                    name = line.split("=", 1)[1].strip()
                    if name:
                        return name
    except OSError:
        pass
    return None
