# -*- coding: utf-8 -*-
"""Generate SCRM NpcTraits.lua from tw_world (danger + npcimmune lists).

Sources:
  - creature_template.spell_list_id -> creature_spells
  - creature_template.spell_id1..4
  - creature_ai_events + creature_ai_scripts (command 15 = cast spell)
  - creature_template.mechanic_immune_mask / school_immune_mask / type
  - locales_creature.name_loc4 for zhCN name fallback

Danger kinds (spell mechanic on castable NPC spells):
  fear   = mechanic 5 (Fear) or 24 (Horror)
  disarm = mechanic 3

Npcimmune kinds:
  disarm/fear/bleed = mechanic_immune_mask bits (1 << (mech-1))
  poison = school Nature immune OR creature type Undead(6)/Mechanical(9)
           (poison has no DBC mechanic; do NOT use sleep=10)
  taunt  = flags_extra bit discovered below; if unset on this core, table stays
           empty aside from EXTRA_TAUNT_IMMUNE

Re-run after DB updates:
  python addons/SuperCleveRoidMacros/gen_npc_traits.py
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "NpcTraits.lua"

DB = dict(
    host="127.0.0.1",
    port=3306,
    user="mangos",
    password="mangos",
    database="tw_world",
    charset="utf8",
    connect_timeout=10,
)

# DBC SpellMechanic ids
MECH_DISARM = 3
MECH_FEAR = 5
MECH_HORROR = 24
MECH_BLEED = 15

DANGER_KINDS = {
    "fear": (MECH_FEAR, MECH_HORROR),
    "disarm": (MECH_DISARM,),
}

# mechanic_immune_mask bit = 1 << (mechanicId - 1)
IMMUNE_MECHANIC = {
    "disarm": MECH_DISARM,
    "fear": MECH_FEAR,
    "bleed": MECH_BLEED,
}

# SPELL_SCHOOL_MASK_NATURE = 0x8 (bit 3)
SCHOOL_NATURE = 0x8

# creature_template.type
TYPE_UNDEAD = 6
TYPE_MECHANICAL = 9

# AI cast-spell command (this core)
AI_COMMAND_CAST = 15

# Hand-curated taunt-immune creature IDs when flags_extra bit is unavailable.
# Keep empty unless verified in-game / docs for this fork.
EXTRA_TAUNT_IMMUNE: dict[int, str] = {}

# Candidate flags_extra bits for NOT_TAUNTABLE across cores (tried in order).
# Prefer a bit that hits a small non-empty set and is not the generic "raid boss"
# cluster (0x200000 / 0x8000 / 0x1).
TAUNT_FLAG_CANDIDATES = (
    0x00000100,  # classic CREATURE_FLAG_EXTRA_NOT_TAUNTABLE
    0x00000200,
    0x00001000,
    0x00002000,
)


def mech_bit(mech: int) -> int:
    return 1 << (mech - 1)


def name_key(name: str) -> str:
    key = (name or "").strip()
    if not key:
        return ""
    if all(ord(ch) < 128 for ch in key):
        return key.lower()
    return key


def connect():
    return pymysql.connect(**DB)


def load_spell_mechanics(cur) -> dict[int, set[int]]:
    """spell_id -> set of mechanic ids (spell-level + effect-level)."""
    cur.execute(
        """
        SELECT entry, mechanic, effectMechanic1, effectMechanic2, effectMechanic3
        FROM spell_template
        WHERE mechanic IN (3,5,24)
           OR effectMechanic1 IN (3,5,24)
           OR effectMechanic2 IN (3,5,24)
           OR effectMechanic3 IN (3,5,24)
        """
    )
    out: dict[int, set[int]] = {}
    for entry, m, e1, e2, e3 in cur.fetchall():
        mechs = {int(x) for x in (m, e1, e2, e3) if x and int(x) > 0}
        if mechs:
            out[int(entry)] = mechs
    return out


def danger_kind_for(mechs: set[int]) -> set[str]:
    kinds: set[str] = set()
    for kind, want in DANGER_KINDS.items():
        if mechs.intersection(want):
            kinds.add(kind)
    return kinds


def collect_danger(cur, spell_mechs: dict[int, set[int]]) -> dict[str, set[int]]:
    danger: dict[str, set[int]] = {k: set() for k in DANGER_KINDS}

    # 1) creature_spells via spell_list_id
    cur.execute(
        """
        SELECT ct.entry,
               cs.spellId_1, cs.spellId_2, cs.spellId_3, cs.spellId_4,
               cs.spellId_5, cs.spellId_6, cs.spellId_7, cs.spellId_8
        FROM creature_template ct
        JOIN creature_spells cs ON cs.entry = ct.spell_list_id
        WHERE ct.spell_list_id <> 0
        """
    )
    for row in cur.fetchall():
        cid = int(row[0])
        for sid in row[1:]:
            if not sid:
                continue
            mechs = spell_mechs.get(int(sid))
            if not mechs:
                continue
            for kind in danger_kind_for(mechs):
                danger[kind].add(cid)

    # 2) spell_id1..4 on template
    cur.execute(
        """
        SELECT entry, spell_id1, spell_id2, spell_id3, spell_id4
        FROM creature_template
        WHERE spell_id1 <> 0 OR spell_id2 <> 0 OR spell_id3 <> 0 OR spell_id4 <> 0
        """
    )
    for row in cur.fetchall():
        cid = int(row[0])
        for sid in row[1:]:
            if not sid:
                continue
            mechs = spell_mechs.get(int(sid))
            if not mechs:
                continue
            for kind in danger_kind_for(mechs):
                danger[kind].add(cid)

    # 3) creature AI cast scripts (command 15, datalong = spell)
    cur.execute(
        """
        SELECT DISTINCT e.creature_id, s.datalong
        FROM creature_ai_events e
        JOIN creature_ai_scripts s
          ON s.id IN (e.action1_script, e.action2_script, e.action3_script)
        WHERE s.command = %s AND s.datalong <> 0
        """,
        (AI_COMMAND_CAST,),
    )
    for creature_id, spell_id in cur.fetchall():
        mechs = spell_mechs.get(int(spell_id))
        if not mechs:
            continue
        for kind in danger_kind_for(mechs):
            danger[kind].add(int(creature_id))

    return danger


def discover_taunt_flag(cur) -> int | None:
    """Return a flags_extra bit for NOT_TAUNTABLE, or None if none look safe."""
    for bit in TAUNT_FLAG_CANDIDATES:
        cur.execute(
            "SELECT COUNT(*) FROM creature_template WHERE (flags_extra & %s) <> 0",
            (bit,),
        )
        n = int(cur.fetchone()[0])
        # Prefer small curated sets; skip empty and huge "raid boilerplate" masks
        if 1 <= n <= 80:
            return bit
    return None


def collect_immune(cur, taunt_bit: int | None) -> dict[str, set[int]]:
    immune: dict[str, set[int]] = {
        "disarm": set(),
        "fear": set(),
        "bleed": set(),
        "poison": set(),
        "taunt": set(),
    }

    # Mechanic mask immunities
    for kind, mech in IMMUNE_MECHANIC.items():
        bit = mech_bit(mech)
        cur.execute(
            "SELECT entry FROM creature_template WHERE (mechanic_immune_mask & %s) <> 0",
            (bit,),
        )
        for (entry,) in cur.fetchall():
            immune[kind].add(int(entry))

    # Poison: Nature school immune OR Undead/Mechanical type
    cur.execute(
        """
        SELECT entry FROM creature_template
        WHERE (school_immune_mask & %s) <> 0
           OR type IN (%s, %s)
        """,
        (SCHOOL_NATURE, TYPE_UNDEAD, TYPE_MECHANICAL),
    )
    for (entry,) in cur.fetchall():
        immune["poison"].add(int(entry))

    if taunt_bit is not None:
        cur.execute(
            "SELECT entry FROM creature_template WHERE (flags_extra & %s) <> 0",
            (taunt_bit,),
        )
        for (entry,) in cur.fetchall():
            immune["taunt"].add(int(entry))

    for entry, _reason in EXTRA_TAUNT_IMMUNE.items():
        immune["taunt"].add(int(entry))

    return immune


def fetch_names(cur, ids: set[int]) -> dict[int, tuple[str, str | None]]:
    """entry -> (en_name, cn_name|None)."""
    out: dict[int, tuple[str, str | None]] = {}
    id_list = sorted(ids)
    for i in range(0, len(id_list), 400):
        chunk = id_list[i : i + 400]
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
            cn_s = cn.strip() if cn and str(cn).strip() else None
            out[int(entry)] = (en or "", cn_s)
    return out


def lua_id_table(kind: str, ids: set[int], names: dict[int, tuple[str, str | None]]) -> str:
    lines = [f"    {kind} = {{"]
    for cid in sorted(ids):
        en, cn = names.get(cid, ("", None))
        label = cn or en or ""
        label = label.replace("--", "- -")
        lines.append(f"        [{cid}] = true, -- {label}")
    lines.append("    },")
    return "\n".join(lines)


def lua_name_table(kind: str, ids: set[int], names: dict[int, tuple[str, str | None]]) -> str:
    # name -> true (en lower + zhCN exact)
    keys: dict[str, int] = {}
    for cid in sorted(ids):
        en, cn = names.get(cid, ("", None))
        ek = name_key(en)
        if ek:
            keys.setdefault(ek, cid)
        if cn:
            ck = name_key(cn)
            if ck:
                keys.setdefault(ck, cid)
    lines = [f"    {kind} = {{"]
    for key in sorted(keys.keys(), key=lambda s: (s.lower(), s)):
        cid = keys[key]
        # Escape Lua string
        esc = key.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'        ["{esc}"] = true, -- {cid}')
    lines.append("    },")
    return "\n".join(lines)


def write_lua(
    danger: dict[str, set[int]],
    immune: dict[str, set[int]],
    names: dict[int, tuple[str, str | None]],
    taunt_bit: int | None,
) -> None:
    all_ids: set[int] = set()
    for s in danger.values():
        all_ids |= s
    for s in immune.values():
        all_ids |= s

    danger_counts = ", ".join(f"{k}={len(v)}" for k, v in sorted(danger.items()))
    immune_counts = ", ".join(f"{k}={len(v)}" for k, v in sorted(immune.items()))
    taunt_note = (
        f"0x{taunt_bit:X}" if taunt_bit is not None else "none (EXTRA_TAUNT_IMMUNE only)"
    )

    parts = [
        "-- Auto-generated by gen_npc_traits.py from tw_world. Do not hand-edit.",
        f"-- danger: {danger_counts}",
        f"-- npcimmune: {immune_counts}",
        f"-- taunt flags_extra bit: {taunt_note}",
        "-- poison = Nature school immune OR type Undead/Mechanical (not sleep mechanic).",
        "local _G = _G or getfenv(0)",
        "local CleveRoids = _G.CleveRoids or {}",
        "_G.CleveRoids = CleveRoids",
        "",
        "-- [kind][creatureID] = true",
        "CleveRoids.NpcDangerIDs = {",
    ]
    for kind in ("fear", "disarm"):
        parts.append(lua_id_table(kind, danger.get(kind, set()), names))
    parts.append("}")
    parts.append("")
    parts.append("-- [kind][name] = true  (en lower / zhCN exact)")
    parts.append("CleveRoids.NpcDangerNames = {")
    for kind in ("fear", "disarm"):
        parts.append(lua_name_table(kind, danger.get(kind, set()), names))
    parts.append("}")
    parts.append("")
    parts.append("CleveRoids.NpcImmuneIDs = {")
    for kind in ("disarm", "fear", "bleed", "poison", "taunt"):
        parts.append(lua_id_table(kind, immune.get(kind, set()), names))
    parts.append("}")
    parts.append("")
    parts.append("CleveRoids.NpcImmuneNames = {")
    for kind in ("disarm", "fear", "bleed", "poison", "taunt"):
        parts.append(lua_name_table(kind, immune.get(kind, set()), names))
    parts.append("}")
    parts.append("")
    parts.append(
        """
local function _trait_lookup(id_table, name_table, unit, kind)
    if not unit or not kind or not UnitExists or not UnitExists(unit) then
        return false
    end
    if UnitIsPlayer and UnitIsPlayer(unit) then
        return false
    end
    local ids = id_table and id_table[kind]
    local names = name_table and name_table[kind]
    if not ids and not names then
        return false
    end
    local id = CleveRoids.GetUnitCreatureID and CleveRoids.GetUnitCreatureID(unit)
    if id and ids and ids[id] then
        return true
    end
    local name = UnitName(unit)
    if name and names then
        if names[name] or names[string.lower(name)] then
            return true
        end
    end
    return false
end

function CleveRoids.UnitHasNpcDanger(unit, kind)
    return _trait_lookup(CleveRoids.NpcDangerIDs, CleveRoids.NpcDangerNames, unit, kind)
end

function CleveRoids.UnitHasNpcImmune(unit, kind)
    return _trait_lookup(CleveRoids.NpcImmuneIDs, CleveRoids.NpcImmuneNames, unit, kind)
end
""".strip()
    )
    parts.append("")

    OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> None:
    conn = connect()
    try:
        cur = conn.cursor()
        spell_mechs = load_spell_mechanics(cur)
        print(f"spell rows with fear/disarm/horror mechanic: {len(spell_mechs)}")

        danger = collect_danger(cur, spell_mechs)
        taunt_bit = discover_taunt_flag(cur)
        print(f"taunt flags_extra bit: {hex(taunt_bit) if taunt_bit else None}")
        immune = collect_immune(cur, taunt_bit)

        for k, v in danger.items():
            print(f"danger:{k} = {len(v)}")
        for k, v in immune.items():
            print(f"npcimmune:{k} = {len(v)}")

        all_ids: set[int] = set()
        for s in danger.values():
            all_ids |= s
        for s in immune.values():
            all_ids |= s
        names = fetch_names(cur, all_ids)
        write_lua(danger, immune, names, taunt_bit)
        print(f"wrote {OUT} ({len(all_ids)} unique creature ids)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
