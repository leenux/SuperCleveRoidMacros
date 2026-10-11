# -*- coding: utf-8 -*-
"""Generate SCRM BossList.lua from AtlasMaps.lua + zhCN locales + DB.

Sources:
  - addons/docs/Atlas/AtlasMaps.lua (boss pins)
  - addons/docs/Atlas/Locale/Atlas-zhCN.lua (Chinese display names)
  - tw_world.locales_creature.name_loc4 (Chinese names by creature ID)
  - tw_world.creature_template.name (resolve name-only EN -> ID/CN when possible)

Outdoor worldboss/rareelite still rely on UnitClassification at runtime.
"""
from __future__ import annotations

import re
from pathlib import Path

import pymysql

ROOT = Path(r"d:\twow\twow-diy")
ATLAS_MAPS = ROOT / "addons" / "docs" / "Atlas" / "AtlasMaps.lua"
ATLAS_LOCALE = ROOT / "addons" / "docs" / "Atlas" / "Locale" / "Atlas-enUS.lua"
ATLAS_ZHCN = ROOT / "addons" / "docs" / "Atlas" / "Locale" / "Atlas-zhCN.lua"
OUT = ROOT / "addons" / "SuperCleveRoidMacros" / "BossList.lua"

SKIP_MAPS = {
    "BlackrockMountainEnt",
    "GnomereganEnt",
    "MaraudonEnt",
    "TheDeadminesEnt",
    "TheSunkenTempleEnt",
    "UldamanEnt",
    "WailingCavernsEnt",
    "DireMaulEnt",
    "AlteracValleyNorth",
    "AlteracValleySouth",
    "ArathiBasin",
    "WarsongGulch",
    "FPAllianceEast",
    "FPAllianceWest",
    "FPHordeEast",
    "FPHordeWest",
    "FPNeutral",
    "DLEast",
    "DLWest",
    "TransportRoutes",
}

SKIP_NPC_IDS = {
    3678, 3679, 3981, 4444, 4508, 4510, 4787, 5767, 5768, 5783, 5784, 6729,
    6912, 7850, 7897, 7998, 8320, 8516, 8696, 8767, 8888, 9020, 9021, 9023,
    9046, 9500, 9503, 9676, 10257, 10296, 10740, 10776, 10799, 10917, 11073,
    12944, 14337, 14338, 14368, 14371, 14381, 14382, 14383, 14387, 14395,
    14401, 14566, 15042, 15362, 15378, 15379, 15380, 15471, 15473, 15481,
    15502, 15503, 15504, 15549, 15556, 15560, 15578, 15587, 15593, 15607,
    16031, 16032, 16033, 16381, 16998, 11834,
}

SKIP_NAME_SUBSTR = (
    "trash mobs",
    "set: ",
    "entrance",
    "shrine of",
    "equipment",
    "engrav",
    "pillow",
    "bracelet",
)

SKIP_NAMES = {
    "vault armory equipment",
}

EXTRA_BOSSES = {
    12259: ("Gehennas", "extra@MoltenCore"),
    # City/raid training dummies — treat as boss for rotation macros
    50514: ("Heroic Training Dummy", "extra@TrainingDummy"),      # 团队副本训练假人
    50515: ("Apprentice Training Dummy", "extra@TrainingDummy"),  # 学徒训练假人
    50516: ("Expert Training Dummy", "extra@TrainingDummy"),      # 训练假人
}

META_AL_KEYS = {
    "Rare", "Upper", "Lower", "Varies", "Random", "Summon",
    "Optional", "Wanders", "Patrols", "Mini",
}


def load_enus_locale(path: Path) -> dict[str, str]:
    """AL key -> English string (enUS uses ["Key"] = true)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    names: dict[str, str] = {}
    for m in re.finditer(r'\["([^"]+)"\]\s*=\s*true', text):
        names[m.group(1)] = m.group(1)
    for m in re.finditer(r'\["([^"]+)"\]\s*=\s*"([^"]*)"', text):
        names[m.group(1)] = m.group(2) or m.group(1)
    return names


def load_zhcn_locale(path: Path) -> dict[str, str]:
    """English AL key -> Chinese translation."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return dict(re.findall(r'\["([^"]+)"\]\s*=\s*"([^"]*)"', text))


def extract_display_name(line: str, locale: dict[str, str]) -> str | None:
    parts = re.findall(r'AL\["([^"]+)"\]|"([^"\\]+)"', line)
    chunks: list[str] = []
    al_keys: list[str] = []
    for al_key, lit in parts:
        if al_key:
            if al_key in META_AL_KEYS:
                continue
            chunks.append(locale.get(al_key, al_key))
            al_keys.append(al_key)
        elif lit and not lit.startswith("(") and lit not in ("", " "):
            if re.fullmatch(r"\d+\)\s*", lit):
                continue
            chunks.append(lit)
    if not chunks:
        return None
    name = chunks[0].strip()
    if not name:
        return None
    low = name.lower()
    if low in SKIP_NAMES:
        return None
    for bad in SKIP_NAME_SUBSTR:
        if bad in low:
            return None
    # Prefer original AL key for zhCN lookup (same as English display for enUS)
    return name


def extract_al_key(line: str) -> str | None:
    for al_key in re.findall(r'AL\["([^"]+)"\]', line):
        if al_key not in META_AL_KEYS:
            return al_key
    return None


def parse_atlas(maps_path: Path, locale: dict[str, str]):
    text = maps_path.read_text(encoding="utf-8", errors="replace")
    bosses: dict[int, tuple[str, str]] = {}
    # english_lower -> reason
    name_only: dict[str, str] = {}
    # english_lower -> original AL key / English casing for zh lookup
    en_keys: dict[str, str] = {}

    map_key_re = re.compile(r"^(?:\t|[ ]{4})(\w+)\s*=\s*\{")

    maps_with_npc_ids: set[str] = set()
    current = None
    for line in text.splitlines():
        if re.match(r"^\s*--", line):
            continue
        m = map_key_re.match(line)
        if m:
            current = m.group(1)
            continue
        if not current or current in SKIP_MAPS:
            continue
        for nid in re.findall(r"NPC,\s*(-?\d+)", line):
            if int(nid) > 0:
                maps_with_npc_ids.add(current)

    current = None
    for line in text.splitlines():
        if re.match(r"^\s*--", line):
            continue
        m = map_key_re.match(line)
        if m:
            current = m.group(1)
            continue
        if not current or current in SKIP_MAPS:
            continue
        if "GREY" not in line and "GREN" not in line:
            continue
        if re.search(r",\s*(ITEM|OBJECT|FACTION|QUEST|ZONE)\s*,", line):
            continue

        numbered = bool(re.search(r'(?:GREY|GREN)\.\."\d+\)', line))
        is_rare = 'AL["Rare"]' in line
        is_indent = "INDENT" in line
        has_npc = "NPC," in line

        if numbered:
            keep = True
        elif has_npc and (is_indent or is_rare or "GREN" in line):
            keep = True
        else:
            keep = False
        if not keep:
            continue

        npc_ids = [int(x) for x in re.findall(r"NPC,\s*(-?\d+)", line)]
        name = extract_display_name(line, locale)
        if not name:
            continue
        al_key = extract_al_key(line) or name

        if npc_ids:
            for nid in npc_ids:
                if nid <= 0:
                    name_only[name.lower()] = f"atlas@{current}:name"
                    en_keys[name.lower()] = al_key
                    continue
                if nid in SKIP_NPC_IDS:
                    continue
                bosses[nid] = (name, f"atlas@{current}")
                en_keys[name.lower()] = al_key
        elif numbered and current not in maps_with_npc_ids:
            name_only[name.lower()] = f"atlas@{current}:name"
            en_keys[name.lower()] = al_key

    bosses.update(EXTRA_BOSSES)
    for entry, (name, _reason) in EXTRA_BOSSES.items():
        en_keys.setdefault(name.lower(), name)
    return bosses, name_only, en_keys


def fetch_db_names(boss_ids: list[int], name_only_en: list[str]):
    """Return (id->cn, en_lower->(id, cn|None), en_lower->template_en)."""
    id_cn: dict[int, str] = {}
    en_to_row: dict[str, tuple[int, str | None]] = {}
    conn = pymysql.connect(
        host="127.0.0.1",
        port=13306,
        user="mangos",
        password="mangos",
        database="tw_world",
        charset="utf8",
        connect_timeout=5,
    )
    try:
        cur = conn.cursor()
        if boss_ids:
            # chunk to avoid huge IN lists
            for i in range(0, len(boss_ids), 400):
                chunk = boss_ids[i : i + 400]
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT ct.entry, ct.name, lc.name_loc4
                    FROM creature_template ct
                    LEFT JOIN locales_creature lc ON lc.entry = ct.entry
                    WHERE ct.entry IN ({ph})
                    """,
                    chunk,
                )
                for entry, en, cn in cur.fetchall():
                    if cn and cn.strip():
                        id_cn[int(entry)] = cn.strip()
                    if en:
                        en_to_row[en.lower()] = (int(entry), (cn.strip() if cn and cn.strip() else None))

        # Resolve name-only English labels via template name (exact, case-insensitive)
        unresolved = [n for n in name_only_en if n not in en_to_row]
        for n in unresolved:
            cur.execute(
                """
                SELECT ct.entry, ct.name, lc.name_loc4
                FROM creature_template ct
                LEFT JOIN locales_creature lc ON lc.entry = ct.entry
                WHERE LOWER(ct.name) = %s
                LIMIT 3
                """,
                (n,),
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                entry, en, cn = rows[0]
                en_to_row[en.lower()] = (
                    int(entry),
                    (cn.strip() if cn and cn.strip() else None),
                )
    finally:
        conn.close()
    return id_cn, en_to_row


def add_name(dest: dict[str, str], name: str, reason: str) -> None:
    if not name:
        return
    key = name.strip()
    if not key:
        return
    # ASCII English -> lowercase; keep CJK / mixed as-is for UnitName match
    if all(ord(ch) < 128 for ch in key):
        key = key.lower()
    dest.setdefault(key, reason)


def main() -> None:
    locale = load_enus_locale(ATLAS_LOCALE)
    zhcn = load_zhcn_locale(ATLAS_ZHCN)
    bosses, name_only, en_keys = parse_atlas(ATLAS_MAPS, locale)

    for entry, (name, _reason) in bosses.items():
        name_only.setdefault(name.lower(), f"id:{entry}")
        en_keys.setdefault(name.lower(), name)

    id_cn, en_to_row = fetch_db_names(sorted(bosses.keys()), sorted(name_only.keys()))

    # Promote name-only entries that uniquely resolve to a creature ID
    promoted = 0
    for en_low, reason in list(name_only.items()):
        if ":name" not in reason:
            continue
        row = en_to_row.get(en_low)
        if not row:
            continue
        entry, cn = row
        if entry in SKIP_NPC_IDS:
            continue
        if entry not in bosses:
            # Prefer Atlas English casing
            en_disp = en_keys.get(en_low, en_low)
            bosses[entry] = (en_disp, f"dbresolve@{reason}")
            promoted += 1
            name_only[en_low] = f"id:{entry}"

    # Build bilingual name table
    names: dict[str, str] = {}
    atlas_zh_hits = 0
    db_zh_hits = 0

    for entry, (en_name, reason) in bosses.items():
        add_name(names, en_name, f"id:{entry}")
        # Atlas-zhCN via English key
        al = en_keys.get(en_name.lower(), en_name)
        zh = zhcn.get(al) or zhcn.get(en_name)
        if zh:
            add_name(names, zh, f"atlas-zhCN:{entry}")
            atlas_zh_hits += 1
        cn = id_cn.get(entry)
        if cn:
            add_name(names, cn, f"db-zhCN:{entry}")
            db_zh_hits += 1

    for en_low, reason in name_only.items():
        en_disp = en_keys.get(en_low, en_low)
        add_name(names, en_disp, reason)
        al = en_keys.get(en_low, en_disp)
        zh = zhcn.get(al) or zhcn.get(en_disp)
        if zh:
            add_name(names, zh, f"atlas-zhCN:{reason}")
            atlas_zh_hits += 1
        row = en_to_row.get(en_low)
        if row and row[1]:
            add_name(names, row[1], f"db-zhCN:{reason}")
            db_zh_hits += 1

    lines: list[str] = []
    lines.append("-- Auto-generated from Atlas + Atlas-zhCN + locales_creature.name_loc4.")
    lines.append("-- Used by [type:boss] / CleveRoids.IsBossUnit. Do not hand-edit large sections;")
    lines.append("-- re-run gen_bosslist.py after Atlas/DB updates.")
    lines.append("-- Outdoor worldboss/rareelite still matched via UnitClassification.")
    lines.append("local _G = _G or getfenv(0)")
    lines.append("local CleveRoids = _G.CleveRoids or {}")
    lines.append("_G.CleveRoids = CleveRoids")
    lines.append("")
    lines.append("-- [creatureID] = true")
    lines.append("CleveRoids.BossCreatureIDs = {")
    for entry in sorted(bosses.keys()):
        name, reason = bosses[entry]
        safe = name.replace("--", "- -")
        lines.append(f"    [{entry}] = true, -- {safe} ({reason})")
    lines.append("}")
    lines.append("")
    lines.append("-- EN (lowercased) + zhCN names for clients without UnitCreatureID / name-only bosses")
    lines.append("CleveRoids.BossCreatureNames = {")
    for key in sorted(names.keys(), key=lambda s: (s.lower(), s)):
        lua_name = key.replace("\\", "\\\\").replace('"', '\\"')
        reason = names[key]
        lines.append(f'    ["{lua_name}"] = true, -- {reason}')
    lines.append("}")
    lines.append("")
    lines.append("function CleveRoids.GetUnitCreatureID(unit)")
    lines.append("    if not unit or not UnitExists(unit) then return nil end")
    lines.append("    if UnitCreatureID then")
    lines.append("        local id = UnitCreatureID(unit)")
    lines.append("        if id and id > 0 then return id end")
    lines.append("    end")
    lines.append("    if C_CreatureInfo and C_CreatureInfo.GetCreatureID and UnitGUID then")
    lines.append("        local guid = UnitGUID(unit)")
    lines.append("        if guid then")
    lines.append("            local id = C_CreatureInfo.GetCreatureID(guid)")
    lines.append("            if id and id > 0 then return id end")
    lines.append("        end")
    lines.append("    end")
    lines.append("    return nil")
    lines.append("end")
    lines.append("")
    lines.append("--- True for worldboss/rareelite, listed dungeon/raid bosses, or training dummies.")
    lines.append("function CleveRoids.IsBossUnit(unit)")
    lines.append("    if not unit or not UnitExists(unit) then return false end")
    lines.append("    if UnitIsPlayer and UnitIsPlayer(unit) then return false end")
    lines.append("    local cl = UnitClassification(unit)")
    lines.append('    if cl == "worldboss" or cl == "boss" or cl == "rareelite" then')
    lines.append("        return true")
    lines.append("    end")
    lines.append("    local id = CleveRoids.GetUnitCreatureID(unit)")
    lines.append("    if id and CleveRoids.BossCreatureIDs[id] then")
    lines.append("        return true")
    lines.append("    end")
    lines.append("    local name = UnitName(unit)")
    lines.append("    if name then")
    lines.append("        -- Exact match (zhCN) then lowercased (enUS); avoid lower-only on CJK.")
    lines.append("        if CleveRoids.BossCreatureNames[name]")
    lines.append("            or CleveRoids.BossCreatureNames[string.lower(name)] then")
    lines.append("            return true")
    lines.append("        end")
    lines.append("    end")
    lines.append("    return false")
    lines.append("end")
    lines.append("")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"  creature IDs: {len(bosses)} (promoted from name-only: {promoted})")
    print(f"  name keys: {len(names)} (atlas-zh hits~{atlas_zh_hits}, db-zh hits~{db_zh_hits})")
    # samples
    for sample in ("艾德温·范克里夫", "奥妮克希亚", "因辛迪斯", "哨兵卡什", "edwin vancleef", "onyxia"):
        print(f"  name[{sample}] = {names.get(sample)}")
    still_name_only = [k for k, r in name_only.items() if ":name" in r and k not in {n.lower() for n in ()}]
    # recount pure name-only remaining (no id)
    remaining = [k for k, r in name_only.items() if r.endswith(":name") or ":name" in r and not r.startswith("id:")]
    remaining = [k for k, r in name_only.items() if ":name" in r]
    print(f"  remaining name-only labels: {len(remaining)}")
    for k in sorted(remaining)[:20]:
        zh = zhcn.get(en_keys.get(k, k)) or zhcn.get(k)
        print(f"    {k} -> zh={zh!r} db={en_to_row.get(k)}")


if __name__ == "__main__":
    main()
