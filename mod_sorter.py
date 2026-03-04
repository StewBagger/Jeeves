"""
mod_sorter.py — Python-side mod load order sorter.

Reads mod.info files directly from the Workshop content folder to build a
dependency graph, detect categories, and produce a sorted mod list.

Category priority (lower = loads earlier):
    coreRequirement(0), tweaks(1), resource(2), map(3), vehicle(4),
    code(5), clothes(6), ui(7), other(8), translation(9), undefined(10)

Sort algorithm:
    1. Stable sort by preorder → loadFirst → category → loadLast → alpha
    2. Topological sort over require + loadAfter dependencies
    3. loadBefore rules converted to loadAfter on target mods
"""

import os
import re
from pathlib import Path
from functools import cmp_to_key
from typing import Optional


# =========================================================================
# Category system
# =========================================================================

CATEGORY_ORDER = {
    "coreRequirement": 0,
    "tweaks": 1,
    "resource": 2,
    "map": 3,
    "vehicle": 4,
    "code": 5,
    "clothes": 6,
    "ui": 7,
    "other": 8,
    "translation": 9,
    "undefined": 10,
}

PREORDER = {
    "ModManager": 1,
    "ModManagerServer": 2,
    "modoptions": 3,
}

# Tag → category mapping (case-insensitive)
TAG_CATEGORY_MAP = {
    "translation": "translation",
    "interface": "ui",
    "ui": "ui",
    "clothing": "clothes",
    "clothes": "clothes",
    "vehicle": "vehicle",
    "vehicles": "vehicle",
    "map": "map",
    "maps": "map",
    "framework": "tweaks",
    "tweak": "tweaks",
    "library": "tweaks",
    "utility": "tweaks",
    "api": "tweaks",
}

# Name substrings that suggest "tweaks" category
TWEAK_NAME_HINTS = [
    "framework", " api", "_api", "tweak", "interface",
    "utilit", "bugfix", "library", "lib ",
]


# =========================================================================
# mod.info parser
# =========================================================================

def parse_mod_info(mod_info_path: Path) -> Optional[dict]:
    """Parse a mod.info file and return a dict of fields."""
    try:
        text = mod_info_path.read_text(encoding='utf-8', errors='replace')
    except Exception:
        return None

    info = {
        "id": None,
        "name": None,
        "category": None,
        "tags": [],
        "require": [],
        "loadAfter": [],
        "loadBefore": [],
        "incompatibleMods": [],
        "loadFirst": False,
        "loadLast": False,
    }

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()

        if key == "id":
            info["id"] = value
        elif key == "name":
            info["name"] = value
        elif key == "category":
            info["category"] = value
        elif key == "tags":
            info["tags"] = [t.strip() for t in re.split(r'[,;]+', value) if t.strip()]
        elif key == "require":
            info["require"] = [r.strip() for r in re.split(r'[,;]+', value) if r.strip()]
        elif key == "loadafter":
            info["loadAfter"] = [r.strip() for r in re.split(r'[,;]+', value) if r.strip()]
        elif key == "loadbefore":
            info["loadBefore"] = [r.strip() for r in re.split(r'[,;]+', value) if r.strip()]
        elif key == "incompatiblemods":
            info["incompatibleMods"] = [r.strip() for r in re.split(r'[,;]+', value) if r.strip()]
        elif key == "loadfirst":
            info["loadFirst"] = value.lower() == "true"
        elif key == "loadlast":
            info["loadLast"] = value.lower() == "true"

    return info if info["id"] else None


# =========================================================================
# Category detection
# =========================================================================

def _dir_exists(path: Path) -> bool:
    """Check if a directory exists (case-insensitive fallback for Windows)."""
    try:
        return path.exists() and path.is_dir()
    except Exception:
        return False


def _scan_for_subdir(base: Path, target: str) -> bool:
    """Recursively check if any directory named `target` exists under base.
    This handles cases where the folder structure varies between mods."""
    target_lower = target.lower()
    try:
        for item in base.rglob("*"):
            if item.is_dir() and item.name.lower() == target_lower:
                return True
    except Exception:
        pass
    return False


def _probe_content_dirs(mod_dir: Path) -> dict[str, bool]:
    """Probe a mod's content directory for categorization signals.
    Checks mod_dir itself AND common sub-structures."""
    probes = {
        "has_code": False,
        "has_textures": False,
        "has_models": False,
        "has_vehicle_models": False,
        "has_skinned": False,
        "has_ui": False,
        "has_translate": False,
        "has_resource": False,
        "has_map": False,
    }

    if not mod_dir or not mod_dir.is_dir():
        return probes

    # Check multiple possible content root locations:
    # mod_dir itself, mod_dir/42/, mod_dir/42.0/, mod_dir/Contents/
    content_roots = [mod_dir]
    for sub in ("42", "42.0", "Contents"):
        candidate = mod_dir / sub
        if candidate.is_dir():
            content_roots.append(candidate)

    for root in content_roots:
        media = root / "media"
        if not media.is_dir():
            continue

        # Check each signal
        for d in ("lua/client", "lua/server", "scripts", "shared"):
            if (media / d).is_dir():
                probes["has_code"] = True

        for d in ("textures", "texturepacks"):
            if (media / d).is_dir():
                probes["has_textures"] = True

        for d in ("models_X", "models"):
            models_path = media / d
            if models_path.is_dir():
                probes["has_models"] = True
                if (models_path / "vehicles").is_dir():
                    probes["has_vehicle_models"] = True
                if (models_path / "Skinned").is_dir() and probes["has_textures"]:
                    probes["has_skinned"] = True

        for d in ("textures/ui", "ui"):
            if (media / d).is_dir():
                probes["has_ui"] = True

        if (media / "lua" / "shared" / "Translate").is_dir():
            probes["has_translate"] = True

        if (media / "resource").is_dir():
            probes["has_resource"] = True

        if (media / "maps").is_dir():
            probes["has_map"] = True

    return probes


def detect_category(info: dict, mod_dir: Path) -> str:
    """Detect the mod's category using tags, name heuristics, and filesystem probing."""

    # If mod.info already has a valid category, use it
    if info.get("category") and info["category"] in CATEGORY_ORDER:
        return info["category"]

    category = "other"

    # Phase 1: Tag-based detection
    tags_lower = {t.lower() for t in info.get("tags", [])}
    for tag, cat in TAG_CATEGORY_MAP.items():
        if tag in tags_lower:
            if CATEGORY_ORDER.get(cat, 99) < CATEGORY_ORDER.get(category, 99):
                category = cat

    if category != "other":
        return category

    # Phase 2: Name heuristic
    name_lower = (info.get("name") or "").lower()
    is_tweak = any(hint in name_lower for hint in TWEAK_NAME_HINTS)

    # Phase 3: Filesystem probing
    probes = _probe_content_dirs(mod_dir)

    if any(probes.values()):
        # At least one probe succeeded — filesystem is accessible
        def upgrade(new_cat, condition):
            nonlocal category
            if condition and CATEGORY_ORDER.get(new_cat, 99) < CATEGORY_ORDER.get(category, 99):
                category = new_cat

        upgrade("translation", probes["has_translate"] and not (probes["has_code"] or probes["has_models"] or probes["has_textures"]))
        upgrade("ui", probes["has_ui"])
        upgrade("clothes", probes["has_skinned"])
        upgrade("code", probes["has_code"] and not (probes["has_models"] or probes["has_ui"] or probes["has_resource"] or probes["has_map"]))
        upgrade("tweaks", is_tweak)
        upgrade("vehicle", probes["has_vehicle_models"] and probes["has_textures"])
        upgrade("map", probes["has_map"])
        upgrade("resource", (probes["has_textures"] or probes["has_resource"]) and not (probes["has_code"] or probes["has_models"] or probes["has_map"] or probes["has_ui"]))
    elif is_tweak:
        category = "tweaks"

    return category


# =========================================================================
# Build mod cache from workshop folder
# =========================================================================

def build_mod_cache(
    mods_folder: Path,
    mod_ids: list[str],
    workshop_ids: list[str],
    map_mod_ids: Optional[set[str]] = None,
) -> dict[str, dict]:
    """Build a cache of mod metadata by scanning mod.info files in the workshop folder.

    Args:
        mods_folder: Path to steamapps/workshop/content/108600/
        mod_ids: List of mod IDs from the Mods= INI line
        workshop_ids: List of Workshop IDs from the WorkshopItems= INI line
        map_mod_ids: Optional set of mod IDs known to be map mods (from Map Folder: input)

    Returns:
        Dict mapping mod_id -> mod info dict with category, dependencies, etc.
    """
    cache = {}
    mod_id_set = set(mod_ids)
    map_mod_ids = map_mod_ids or set()

    # First pass: scan for map folders in each workshop item
    # This catches map mods even when detect_category filesystem probing fails
    ws_has_maps: dict[str, bool] = {}
    for ws_id in workshop_ids:
        ws_path = mods_folder / ws_id
        if not ws_path.is_dir():
            continue
        # Quick scan: does any path under this workshop item contain "media/maps"?
        has_maps = False
        try:
            for p in ws_path.rglob("maps"):
                if p.is_dir() and p.parent.name.lower() == "media":
                    has_maps = True
                    break
        except Exception:
            pass
        ws_has_maps[ws_id] = has_maps

    # Second pass: parse mod.info and detect categories
    for ws_id in workshop_ids:
        ws_path = mods_folder / ws_id
        if not ws_path.is_dir():
            continue

        for mod_info_path in ws_path.rglob("mod.info"):
            info = parse_mod_info(mod_info_path)
            if not info or info["id"] not in mod_id_set:
                continue
            if info["id"] in cache:
                continue

            # Use the mod.info's parent directory as the mod root for probing
            # Walk upward to find the mod root (parent of media/)
            mod_dir = mod_info_path.parent

            info["category"] = detect_category(info, mod_dir)

            # Override: if this workshop item contains media/maps/ and
            # detect_category missed it, force "map"
            if info["category"] == "other" and ws_has_maps.get(ws_id, False):
                info["category"] = "map"
                print(f"[ModSorter] Forced category 'map' for {info['id']} (workshop {ws_id} has media/maps/)")

            # Override: if caller explicitly told us this is a map mod
            if info["id"] in map_mod_ids:
                info["category"] = "map"

            info["_workshop_id"] = ws_id
            cache[info["id"]] = info

    # Add placeholder entries for any mod IDs not found on disk
    for mod_id in mod_ids:
        if mod_id not in cache:
            # If caller flagged this as a map mod, respect that
            cat = "map" if mod_id in map_mod_ids else "other"
            cache[mod_id] = {
                "id": mod_id,
                "name": mod_id,
                "category": cat,
                "tags": [],
                "require": [],
                "loadAfter": [],
                "loadBefore": [],
                "incompatibleMods": [],
                "loadFirst": False,
                "loadLast": False,
                "_workshop_id": None,
                "_unresolved": True,
            }

    return cache


# =========================================================================
# Sorting algorithm
# =========================================================================

def _resolve_load_before(cache: dict) -> None:
    """Convert loadBefore rules into loadAfter on target mods."""
    for mod_id, info in cache.items():
        for target_id in info.get("loadBefore", []):
            if target_id in cache:
                target_after = cache[target_id].setdefault("loadAfter", [])
                if mod_id not in target_after:
                    target_after.append(mod_id)


def _compare_mods(a: dict, b: dict) -> int:
    """Comparison function for initial stable sort."""
    a_id, b_id = a["id"], b["id"]

    # Preorder (ModManager etc.)
    a_pre = PREORDER.get(a_id, 10000)
    b_pre = PREORDER.get(b_id, 10000)
    if a_pre != b_pre:
        return -1 if a_pre < b_pre else 1

    # Global loadFirst
    a_first = a.get("loadFirst", False)
    b_first = b.get("loadFirst", False)
    if a_first != b_first:
        return -1 if a_first else 1

    # Global loadLast
    a_last = a.get("loadLast", False)
    b_last = b.get("loadLast", False)
    if a_last != b_last:
        return 1 if a_last else -1

    # Category priority
    a_cat = CATEGORY_ORDER.get(a.get("category", "other"), 99)
    b_cat = CATEGORY_ORDER.get(b.get("category", "other"), 99)
    if a_cat != b_cat:
        return -1 if a_cat < b_cat else 1

    # Alphabetical
    a_low = a_id.lower()
    b_low = b_id.lower()
    if a_low != b_low:
        return -1 if a_low < b_low else 1
    return 0


def _topological_sort(initial_order: list[str], cache: dict) -> list[str]:
    """Topological sort respecting require and loadAfter dependencies."""
    sorted_result = []
    visited = set()
    visiting = set()

    def visit(mod_id: str):
        if mod_id in visiting:
            return  # Cycle — break it
        if mod_id in visited:
            return

        info = cache.get(mod_id, {})
        visiting.add(mod_id)

        for dep in info.get("require", []):
            if dep in cache:
                visit(dep)

        for dep in info.get("loadAfter", []):
            if dep in cache:
                visit(dep)

        visiting.discard(mod_id)
        visited.add(mod_id)
        sorted_result.append(mod_id)

    for mod_id in initial_order:
        visit(mod_id)

    return sorted_result


def sort_mods(cache: dict, mod_ids: list[str]) -> list[str]:
    """Sort mod IDs using category priority + topological ordering."""
    _resolve_load_before(cache)

    mod_list = [cache[m] for m in mod_ids if m in cache]
    mod_list.sort(key=cmp_to_key(_compare_mods))
    initial_order = [m["id"] for m in mod_list]

    return _topological_sort(initial_order, cache)


# =========================================================================
# Validate order (find issues)
# =========================================================================

def validate_order(cache: dict, ordered_ids: list[str]) -> dict[str, dict]:
    """Validate a mod order and return any issues found."""
    issues = {}
    seen = set()

    for mod_id in ordered_ids:
        info = cache.get(mod_id, {})
        mod_issues = {"missing": [], "wrongOrder": [], "incompatible": []}

        for req in info.get("require", []):
            if req in cache and req not in seen:
                mod_issues["missing"].append(req)

        for dep in info.get("loadAfter", []):
            if dep in cache and dep not in seen:
                mod_issues["wrongOrder"].append(dep)

        for inc in info.get("incompatibleMods", []):
            if inc in cache:
                mod_issues["incompatible"].append(inc)

        if mod_issues["missing"] or mod_issues["wrongOrder"] or mod_issues["incompatible"]:
            issues[mod_id] = mod_issues

        seen.add(mod_id)

    return issues


# =========================================================================
# High-level convenience: sort an INI's mod list
# =========================================================================

def sort_ini_mods(
    mods_folder: Path,
    mod_ids: list[str],
    workshop_ids: list[str],
    map_mod_ids: Optional[set[str]] = None,
) -> tuple[list[str], dict, int]:
    """Build cache, sort, and return (sorted_ids, issues, change_count).

    Args:
        mods_folder: Path to steamapps/workshop/content/108600/
        mod_ids: Current Mods= list from INI
        workshop_ids: Current WorkshopItems= list from INI
        map_mod_ids: Optional set of mod IDs known to be map mods

    Returns:
        (sorted_mod_ids, issues_dict, number_of_position_changes)
    """
    cache = build_mod_cache(mods_folder, mod_ids, workshop_ids, map_mod_ids)
    sorted_ids = sort_mods(cache, mod_ids)
    issues = validate_order(cache, sorted_ids)

    change_count = sum(1 for i, (a, b) in enumerate(zip(mod_ids, sorted_ids)) if a != b)
    change_count += abs(len(mod_ids) - len(sorted_ids))

    return sorted_ids, issues, change_count
