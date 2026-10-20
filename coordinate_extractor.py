# -*- coding: utf-8 -*-
"""
Koikatu 人物卡 → 服装卡（Coordinate）提取器  —— 终局版

与游戏内「另存服装卡」行为逐项对齐（已用 9 张游戏原生卡 测试1..9 全 slot 验证）：
  1. 卡头：int32 100 + 7bit 标记/版本/名字 + int32 块长 + 块。
  2. 衣服/饰品：按 UAR 把动态 Slot ID 改写成 LocalSlot ID 后重新打包
     （use_single_float=True，实测与原生卡逐字节一致，不会损失精度）。
  3. KKEx：7bit "KKEx" + int32 版本 3 + int32 长度 + msgpack。
  4. UAR：只保留 outfit{slot}. 项，并去掉该前缀（原生卡就是无前缀的）。
  5. ME：9 个列表按 CoordinateIndex==slot 且 ObjectType∈{0,1,2} 过滤
     （源码排除 Hair=3 / Character=4）；
     TextureDictionary 只保留被这些列表引用的 TexID（原生卡行为，防白板且不臃肿）。
  6. ABM：只保留服装级骨骼（CoordinateModifiers.Length > 1），取第 slot 组；版本 3。
  7. moreAccessories / Accessory_States / madevil.kk.ass / Additional_Card_Info /
     ClothingBlendShape / anal / dbde / DynamicBoneEditor / ObjImport /
     hairAccessoryCustomizer / pushup / KCOX 均按 slot 取出原生卡对应的结构。
  8. KSOX 及所有人物级插件（脸/身体/怀孕/皮肤等）全部丢弃。
  9. 可选 skip_accessories：强制清空指定饰品槽（用于人物卡比服装卡多出饰品的情况）。

用法：
    from coordinate_extractor import extract_slot, extract_all
    extract_slot("NTR Onna.png", 5, "输出目录")
"""

import io
import copy
import json
import struct
from pathlib import Path

import msgpack
import lz4.block
import xml.etree.ElementTree as ET

PNG_SIG = b'\x89PNG\r\n\x1a\n'

# 工具放在子目录里，数据（plugin_catalog / 规则库 / 测试卡）仍在项目根目录
ROOT = Path(__file__).resolve().parent.parent

# 0..6 是原版 7 套；7/8 来自 MoreOutfits 等扩展
CLOTHES_NAMES = ["校内", "下校", "体操", "水着", "部活", "私服", "睡衣"]

# ChaFileClothes.parts 的下标 → UAR Property 里的部件名
CLOTHES_PART_NAMES = [
    "ClothesTop", "ClothesBot", "ClothesBra", "ClothesShorts", "ClothesGloves",
    "ClothesPants", "ClothesSocks", "ClothesShoesInner", "ClothesShoesOuter",
]

# subPartsId 的 3 个槽位可能由 Jacket 或 Sailor 两组属性名之一提供
SUBPART_NAMES = [
    ("ClothesJacketSubA", "ClothesSailorSubA"),
    ("ClothesJacketSubB", "ClothesSailorSubB"),
    ("ClothesJacketSubC", "ClothesSailorSubC"),
]

ME_LIST_KEYS = [
    "RendererPropertyList", "ProjectorPropertyList", "MaterialNamePropertyList",
    "MaterialFloatPropertyList", "MaterialKeywordPropertyList", "MaterialColorPropertyList",
    "MaterialTexturePropertyList", "MaterialShaderList", "MaterialCopyList",
]

# MaterialEditor 的 ObjectType：0=Unknown 1=Clothing 2=Accessory 3=Hair 4=Character。
# 源码 OnCoordinateBeingSaved 的条件是：CoordinateIndex == 当前slot
# 且 ObjectType != Hair && != Character，所以保留 0/1/2。
ME_KEEP_OBJECT_TYPES = (0, 1, 2)

# 当前插件版本写出的数据版本（来自原生卡实测）
ABM_SAVE_VERSION = 3
KCOX_SAVE_VERSION = 2
MORE_ACC_SAVE_VERSION = 2
ACI_SAVE_VERSION = 1
ABM_EXT_CODE = 99
# 「胸骨归一化」用的 identity 矩阵
ABM_IDENTITY = [[1.0, 1.0, 1.0], 1.0, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
BUST_BONE_PREFIXES = ("cf_d_bust",)

UAR_KEY = "com.bepis.sideloader.universalautoresolver"
ME_KEY = "com.deathweasel.bepinex.materialeditor"
ABM_KEY = "KKABMPlugin.ABMData"
MORE_ACC_KEY = "moreAccessories"
ACI_KEY = "Additional_Card_Info"
KCOX_KEY = "KCOX"
CLOTHING_UNLOCKER_KEY = "com.deathweasel.bepinex.clothingunlocker"
ANAL_KEY = "org.njaecha.plugins.anal"
DBDE_KEY = "org.njaecha.plugins.dbde"
HAIR_ACC_KEY = "com.deathweasel.bepinex.hairaccessorycustomizer"
PUSHUP_KEY = "com.deathweasel.bepinex.pushup"
ACC_STATES_KEY = "Accessory_States"
MADEVIL_ASS_KEY = "madevil.kk.ass"
CLOTHING_BLEND_SHAPE_KEY = "nakay.kk.ClothingBlendShape"
DYNAMIC_BONE_KEY = "com.deathweasel.bepinex.dynamicboneeditor"
OBJ_IMPORT_KEY = "org.njaecha.plugins.objimport"
AAAPK_KEY = "madevil.kk.AAAPK"
MR_KEY = "madevil.kk.mr"
PPCAM_KEY = "madevil.kk.PPCAM"
CETOL_PERSIST_KEY = "cetol.cardpersist"
CETOL_ANTICLIP_KEY = "com.cetol.kks.anticlipping"

# 服装级插件白名单。_build_kkex 只可能写出这些 key；
# KSOX / marco.authordata / Pregnancy / SkinEffects 等人物级插件永远不会进入服装卡。
COORDINATE_PLUGIN_KEYS = frozenset({
    UAR_KEY, ME_KEY, ABM_KEY, MORE_ACC_KEY, ACI_KEY, KCOX_KEY,
    CLOTHING_UNLOCKER_KEY,
    ANAL_KEY, DBDE_KEY, HAIR_ACC_KEY, PUSHUP_KEY, ACC_STATES_KEY,
    MADEVIL_ASS_KEY, CLOTHING_BLEND_SHAPE_KEY, DYNAMIC_BONE_KEY, OBJ_IMPORT_KEY,
    AAAPK_KEY, MR_KEY, PPCAM_KEY,
    CETOL_PERSIST_KEY, CETOL_ANTICLIP_KEY,
})

# 已知的人物级插件：无论源卡有没有，都绝不写入服装卡。
CHARACTER_LEVEL_PLUGIN_KEYS = frozenset({
    "KSOX", "marco.authordata", "KK_Profile", "KK_Pregnancy", "KK_PregnancyPlus",
    "Marco.SkinEffects", "com.deathweasel.bepinex.uncensorselector",
    "com.deathweasel.bepinex.eyecontrol", "com.deathweasel.bepinex.colliders",
    "com.deathweasel.bepinex.eyeshaking", "orange.spork.fkheightadjustuiplugin",
    "orange.spork.advikplugin", "AAAAAAAAAAAA_Data",
    "com.deathweasel.bepinex.moreoutfits",
    "Sauceke.SexFaces", "org.njaecha.plugins.editfangs",
    "com.shallty.blendshapecreator", "KK_InvisibleBody",
    "com.snw.bepinex.breastphysicscontroller",
    # Cetol Super Plugin Pack：这些插件没有坐标保存钩子（只有人物级/加载钩子）
    "Cetol.HairFaceShadow", "Cetol.HeadRuntime.Pupil",
    "cetol.outfitalign", "cetol.bonecollidervisualizer.BoneTweaks",
    "com.cetol.runtime_trust.card_owner_proof",
})

# 明确不写入服装卡的迁移/工具类插件
DROP_PLUGIN_KEYS = frozenset({
    "KK_COBOCMigration",
    "MigrationHelper",
    "PluginListTool",
})

# 结构差异大、暂不做通用提取的坐标插件（避免写出插件不认的字段）
GENERIC_UNSUPPORTED = frozenset({
    "Accessory_Parents",
    "nakay.kk.ChaAlphaMask",
    "com.jim60105.kk.charaoverlaysbasedoncoordinate",
})

# 内置分类兜底（2026-10-20 扫描结果固化）。
# plugin_catalog.json / known_plugins.json 被删时，提取器仍能判断插件作用域。
DEFAULT_PLUGIN_CATEGORIES = {
    "AAAAAAAAAAAA_Data": "both",
    "Accessory_Parents": "both",
    "Accessory_States": "both",
    "Accessory_Themes": "both",
    "Additional_Card_Info": "both",
    "BendUrAcc": "both",
    "BetterPenetrationController": "character",
    "BonerStateSync": "both",
    "Boop": "character",
    "BreastDynamicBoneController": "character",
    "Bulge": "character",
    "cetol.bonecollidervisualizer.BoneTweaks": "character",
    "cetol.cardpersist": "both",
    "Cetol.HairFaceShadow": "character",
    "Cetol.HeadRuntime.Pupil": "character",
    "cetol.outfitalign": "character",
    "com.bepis.sideloader.universalautoresolver": "both",
    "com.cetol.kks.anticlipping": "both",
    "com.cetol.runtime_trust.card_owner_proof": "character",
    "com.deathweasel.bepinex.animationcontroller": "character",
    "com.deathweasel.bepinex.autosave": "character",
    "com.deathweasel.bepinex.bodyshaders": "character",
    "com.deathweasel.bepinex.clothingunlocker": "both",
    "com.deathweasel.bepinex.colliders": "character",
    "com.deathweasel.bepinex.dynamicboneeditor": "both",
    "com.deathweasel.bepinex.eyecontrol": "character",
    "com.deathweasel.bepinex.eyeshaking": "character",
    "com.deathweasel.bepinex.hairaccessorycustomizer": "both",
    "com.deathweasel.bepinex.hcharaadjustment": "character",
    "com.deathweasel.bepinex.malejuice": "character",
    "com.deathweasel.bepinex.materialeditor": "both",
    "com.deathweasel.bepinex.moreoutfits": "character",
    "com.deathweasel.bepinex.pushup": "both",
    "com.deathweasel.bepinex.studiocolliders": "character",
    "com.deathweasel.bepinex.uncensorselector": "character",
    "com.gebo.BepInEx.GameDialogHelper": "character",
    "com.gebo.bepinex.translationhelper.chara": "character",
    "com.ghostendsky.kkbonegen": "both",
    "com.ghostendsky.kkshapeeditor": "character",
    "com.jim60105.kk.charaoverlaysbasedoncoordinate": "both",
    "com.jim60105.kk.irisoverlaybycoordinate": "character",
    "com.rikkibalboa.bepinex.buttEditor": "character",
    "com.rikkibalboa.bepinex.studioPseudoMaker": "character",
    "com.shallty.blendshapecreator": "character",
    "com.snw.bepinex.breastphysicscontroller": "character",
    "CreamAssWipe": "character",
    "IrisPlus": "character",
    "KCOX": "both",
    "Keitaro.SkinAssEffects": "character",
    "Keitaro.UncensorDetails": "character",
    "KK_COBOCMigration": "both",
    "KK_FutaMod": "character",
    "KK_InvisibleBody": "character",
    "KK_Pregnancy": "character",
    "KK_PregnancyPlus": "character",
    "KK_Profile": "character",
    "KKABMPlugin.ABMData": "both",
    "kkpe": "character",
    "kkpeCord": "character",
    "koik.blink": "character",
    "koik.kokyu": "character",
    "koikdaisy.kkvrheadsizeadjust": "character",
    "kokaiinum.KKExpandMaleMaker": "character",
    "KSOX": "character",
    "LewdCrestX": "character",
    "madevil.kk.AAAPK": "both",
    "madevil.kk.ass": "both",
    "madevil.kk.BandOver": "both",
    "madevil.kk.BoobWork": "character",
    "madevil.kk.ca": "character",
    "madevil.kk.DynamicBoner": "both",
    "madevil.kk.iSpliter": "character",
    "madevil.kk.mr": "both",
    "madevil.kk.PPCAM": "both",
    "madevil.kk.ppcbs": "character",
    "madevil.kk.wtf": "both",
    "marco.authordata": "character",
    "marco.becometrap": "character",
    "Marco.SkinEffects": "character",
    "MaskRemover": "both",
    "MigrationHelper": "both",
    "moreAccessories": "both",
    "nakay.kk.ChaAlphaMask": "both",
    "nakay.kk.ClothingBlendShape": "both",
    "orange.spork.advikplugin": "character",
    "orange.spork.fkheightadjustuiplugin": "character",
    "org.njaecha.plugins.anal": "both",
    "org.njaecha.plugins.assetimport": "character",
    "org.njaecha.plugins.dbde": "both",
    "org.njaecha.plugins.editfangs": "character",
    "org.njaecha.plugins.objimport": "both",
    "OverlayLayers": "character",
    "picolet21.koikatsu.EroStatus": "character",
    "picolet21.koikatsu.LewdCrest": "character",
    "picolet21.koikatsu.PantyRobber": "character",
    "PluginListTool": "both",
    "prolo.chararmorpher": "character",
    "Pussy.blood": "character",
    "Sauceke.SexFaces": "character",
    "TestCharaCustomFunctionController": "character",
    "怀孕指标\nKK_Pregnancy": "character",
    "怀孕状态\n(KK_PregnancyPlus)": "character",
}

_PLUGIN_CATALOG = None


def _load_plugin_catalog():
    """读取 plugin_catalog.json（或规则库），用于判断源卡插件的坐标/人物作用域。"""
    global _PLUGIN_CATALOG
    if _PLUGIN_CATALOG is not None:
        return _PLUGIN_CATALOG
    candidates = [
        ROOT / "插件规则库合并" / "plugin_catalog.json",
        ROOT / "plugin_catalog.json",
        Path(__file__).with_name("plugin_catalog.json"),
        Path("plugin_catalog.json"),
        ROOT / "人物卡插件删除工具" / "known_plugins.json",
        Path("人物卡插件删除工具/known_plugins.json"),
    ]
    catalog = dict(DEFAULT_PLUGIN_CATEGORIES)
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        found = 0
        for key, rec in (data.get("plugins") or {}).items():
            if not isinstance(rec, dict):
                continue
            cat = rec.get("category")
            if cat in ("both", "character", "unknown"):
                catalog[str(key)] = cat
                found += 1
            elif cat == "coordinate":
                catalog[str(key)] = "both"
                found += 1
        if found:
            break
    _PLUGIN_CATALOG = catalog
    return catalog


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------

def find_png_end(data):
    if len(data) < 8 or data[:8] != PNG_SIG:
        return -1
    pos = 8
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        pos += 8 + length + 4
        if ctype == b'IEND':
            return pos
    return -1


def read_7bit_int(data, pos):
    result = 0
    shift = 0
    for _ in range(5):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("7bit 长度溢出")


def read_7bit_str(data, pos):
    n, pos = read_7bit_int(data, pos)
    return data[pos:pos + n].decode("utf-8", "replace"), pos + n


def write_7bit_int(value):
    buf = bytearray()
    while value >= 0x80:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    buf.append(value & 0x7F)
    return bytes(buf)


def write_7bit_str(text):
    raw = text.encode("utf-8")
    return write_7bit_int(len(raw)) + raw


def unpack(data):
    return msgpack.unpackb(data, raw=False, strict_map_key=False)


def pack(obj):
    # 原生卡所有浮点都是 float32；用 use_single_float 重新打包可逐字节还原。
    return msgpack.packb(obj, use_single_float=True)


# ---------------------------------------------------------------------------
# 卡片解析
# ---------------------------------------------------------------------------

def _get_chara_block(data, chara, name):
    for block in chara["lst"]["lstInfo"]:
        if block["name"] == name:
            start = chara["data_start"] + block["pos"]
            return bytes(data[start:start + block["size"]])
    return None


def _get_character_name(data, chara):
    param_block = _get_chara_block(data, chara, "Parameter")
    if not param_block:
        return ""
    try:
        unpacker = msgpack.Unpacker(io.BytesIO(param_block), raw=False, strict_map_key=False)
        while True:
            try:
                obj = unpacker.unpack()
            except msgpack.exceptions.OutOfData:
                break
            if isinstance(obj, dict) and "firstname" in obj:
                return f"{obj.get('lastname', '')}{obj.get('firstname', '')}"
    except Exception:
        pass
    return ""


def parse_character_card(path):
    """解析人物卡（Koikatu 人物卡，PNG 尾部）。"""
    data = Path(path).read_bytes()
    png_end = find_png_end(data)
    if png_end < 0:
        raise ValueError("不是有效的 PNG/人物卡")
    pos = png_end + 4
    _, pos = read_7bit_str(data, pos)          # 标记
    _, pos = read_7bit_str(data, pos)          # 版本
    face_len = struct.unpack("<i", data[pos:pos + 4])[0]
    pos += 4 + face_len
    lst_len = struct.unpack("<i", data[pos:pos + 4])[0]
    pos += 4
    lst_raw = data[pos:pos + lst_len]
    pos += lst_len
    data_start = pos + 8
    chara = {"data_start": data_start, "lst": unpack(lst_raw)}
    kkex_raw = _get_chara_block(data, chara, "KKEx")
    kkex = unpack(kkex_raw) if kkex_raw else {}
    return {
        "data": data,
        "png_end": png_end,
        "data_start": data_start,
        "lst": chara["lst"],
        "kkex": kkex,
        "name": _get_character_name(data, chara),
    }


def get_coordinate_count(card):
    block = _get_chara_block(card["data"], card, "Coordinate")
    if not block:
        raise ValueError("卡内没有 Coordinate 块")
    coords = msgpack.Unpacker(io.BytesIO(block), raw=False, strict_map_key=False).unpack()
    return len(coords)


def _read_coordinate_block(card, slot):
    block = _get_chara_block(card["data"], card, "Coordinate")
    if not block:
        raise ValueError("卡内没有 Coordinate 块")
    coords = msgpack.Unpacker(io.BytesIO(block), raw=False, strict_map_key=False).unpack()
    if slot < 0 or slot >= len(coords):
        raise ValueError(f"slot {slot} 超出范围（0..{len(coords) - 1}）")
    raw = coords[slot]
    pos = 0
    c_len = struct.unpack("<i", raw[pos:pos + 4])[0]; pos += 4
    c_data = raw[pos:pos + c_len]; pos += c_len
    a_len = struct.unpack("<i", raw[pos:pos + 4])[0]; pos += 4
    a_data = raw[pos:pos + a_len]; pos += a_len
    enable_mk = raw[pos]; pos += 1
    m_len = struct.unpack("<i", raw[pos:pos + 4])[0]; pos += 4
    m_data = raw[pos:pos + m_len]
    return c_data, a_data, enable_mk, m_data, len(coords)


# ---------------------------------------------------------------------------
# UAR / ID 改写
# ---------------------------------------------------------------------------

def _uar_info_list(kkex):
    uar = kkex.get(UAR_KEY)
    if not isinstance(uar, list) or len(uar) < 2 or not isinstance(uar[1], dict):
        return None, None
    info = uar[1].get("info", [])
    items = []
    for entry in info:
        if isinstance(entry, bytes):
            try:
                items.append(unpack(entry))
            except Exception:
                pass
        elif isinstance(entry, dict):
            items.append(entry)
    return uar[0], items


def _build_id_maps(kkex, slot):
    """返回 (按 Property 的映射, 按 Slot 的映射)。"""
    _, items = _uar_info_list(kkex)
    by_property = {}
    by_slot = {}
    if not items:
        return by_property, by_slot
    prefix = f"outfit{slot}."
    for item in items:
        prop = item.get("Property", "")
        if not isinstance(prop, str) or not prop.startswith(prefix):
            continue
        by_property[prop] = item.get("LocalSlot")
        slot_id = item.get("Slot")
        local_id = item.get("LocalSlot")
        if isinstance(slot_id, int) and isinstance(local_id, int):
            by_slot[slot_id] = local_id
    return by_property, by_slot


def _rewrite_clothes_ids(doc, slot, by_property):
    """按 UAR 的 ChaFileClothesProperties 全部字段改写衣服 ID。"""
    def prop(name):
        return by_property.get(f"outfit{slot}.ChaFileClothes.{name}")

    parts = doc.get("parts")
    if isinstance(parts, list):
        for i, part in enumerate(parts):
            if not isinstance(part, dict) or i >= len(CLOTHES_PART_NAMES):
                continue
            name = CLOTHES_PART_NAMES[i]
            old_id = part.get("id")
            new_id = prop(name)
            if isinstance(old_id, int) and old_id != 0 and isinstance(new_id, int):
                part["id"] = new_id

            # 徽章（两个槽位）
            for field, suffix in (("emblemeId", "Emblem"), ("emblemeId2", "Emblem2")):
                old = part.get(field)
                new = prop(f"{name}{suffix}")
                if isinstance(old, int) and old != 0 and isinstance(new, int):
                    part[field] = new

            # 4 组花纹
            color_info = part.get("colorInfo")
            if isinstance(color_info, list):
                for j, color in enumerate(color_info[:4]):
                    if not isinstance(color, dict):
                        continue
                    old = color.get("pattern")
                    new = prop(f"{name}Pattern{j}")
                    if isinstance(old, int) and old != 0 and isinstance(new, int):
                        color["pattern"] = new

    # 外套 / 水手服子部件
    sub_parts = doc.get("subPartsId")
    if isinstance(sub_parts, list):
        for i, old in enumerate(sub_parts[:len(SUBPART_NAMES)]):
            if not isinstance(old, int) or old == 0:
                continue
            new_id = None
            for name in SUBPART_NAMES[i]:
                candidate = prop(name)
                if isinstance(candidate, int):
                    new_id = candidate
                    break
            if isinstance(new_id, int):
                sub_parts[i] = new_id
    return doc


def _rewrite_accessory_ids(doc, slot, by_property):
    """改写 ChaFileAccessory.parts[i].id。"""
    parts = doc.get("parts")
    if not isinstance(parts, list):
        return doc
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        old_id = part.get("id")
        if not isinstance(old_id, int) or old_id == 0:
            continue
        new_id = by_property.get(f"outfit{slot}.accessory{i}.ChaFileAccessory.PartsInfo.id")
        if isinstance(new_id, int):
            part["id"] = new_id
    return doc


def _clear_accessory_parts(doc, indices):
    """把指定饰品槽清空为游戏标准的空饰品结构（type=120）。"""
    parts = doc.get("parts")
    if not isinstance(parts, list) or not indices:
        return doc
    template = None
    for part in parts:
        if isinstance(part, dict) and part.get("type") == 120:
            template = copy.deepcopy(part)
            break
    if template is None:
        template = {
            "type": 120, "id": 0, "parentKey": "",
            "addMove": [2, 3, [
                [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0],
                [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 1.0, 1.0],
            ]],
            "color": [[1.0, 1.0, 1.0, 1.0] for _ in range(4)],
            "hideCategory": 0, "noShake": False, "ExtendedSaveData": None,
        }
    for i in indices:
        if isinstance(i, int) and 0 <= i < len(parts):
            parts[i] = copy.deepcopy(template)
    return doc


# ---------------------------------------------------------------------------
# 各插件裁剪
# ---------------------------------------------------------------------------

def _build_uar(kkex, slot, skip_accessories=()):
    version, items = _uar_info_list(kkex)
    if items is None:
        return None
    prefix = f"outfit{slot}."
    acc_prefix = f"outfit{slot}.accessory"
    skip = set(skip_accessories)
    kept = []
    for item in items:
        prop = item.get("Property", "")
        if not isinstance(prop, str) or not prop.startswith(prefix):
            continue
        if prop.startswith(acc_prefix):
            head = prop[len(acc_prefix):].split(".", 1)[0]
            if head.isdigit() and int(head) in skip:
                continue
        new_item = dict(item)
        new_item["Property"] = prop[len(prefix):]
        kept.append(pack(new_item))
    if not kept:
        return None
    return [version, {"info": kept}]


def _build_me(kkex, slot, skip_accessories=()):
    me = kkex.get(ME_KEY)
    if not isinstance(me, list) or len(me) < 2 or not isinstance(me[1], dict):
        return None
    version, data = me[0], me[1]
    skip = set(skip_accessories)
    out = {}

    # 先过滤列表，同时收集仍在使用的 TexID
    used_tex_ids = []
    for key in ME_LIST_KEYS:
        value = data.get(key)
        if isinstance(value, bytes):
            try:
                value = unpack(value)
            except Exception:
                out[key] = None
                continue
        if not isinstance(value, list):
            out[key] = None
            continue
        kept = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("CoordinateIndex") != slot:
                continue
            object_type = item.get("ObjectType")
            if object_type is not None and object_type not in ME_KEEP_OBJECT_TYPES:
                continue
            # 被强制清空的饰品，其材质/贴图也不要带
            if object_type == 2 and item.get("Slot") in skip:
                continue
            kept.append(item)
            tex_id = item.get("TexID")
            if isinstance(tex_id, int) and tex_id not in used_tex_ids:
                used_tex_ids.append(tex_id)
            # 贴图动画的每一帧也会引用额外贴图
            anim = item.get("TexAnimationDef")
            if isinstance(anim, dict):
                for frame in anim.get("frames") or []:
                    if isinstance(frame, dict):
                        frame_tex = frame.get("texID")
                        if isinstance(frame_tex, int) and frame_tex not in used_tex_ids:
                            used_tex_ids.append(frame_tex)
        out[key] = pack(kept) if kept else None

    # 贴图字典：保留原顺序，只裁掉没人引用的贴图
    tex_dict = data.get("TextureDictionary")
    if isinstance(tex_dict, bytes):
        try:
            tex_dict = unpack(tex_dict)
        except Exception:
            tex_dict = None
    if isinstance(tex_dict, dict):
        used = set(used_tex_ids)
        pruned = {k: v for k, v in tex_dict.items() if k in used}
        out["TextureDictionary"] = pack(pruned) if pruned else None
    else:
        out["TextureDictionary"] = None

    # 所有列表和贴图都为空时，游戏根本不会写 ME 数据
    if all(out.get(key) is None for key in ME_LIST_KEYS) and out.get("TextureDictionary") is None:
        return None

    # 保证键顺序与原生卡一致
    ordered = {"TextureDictionary": out.pop("TextureDictionary", None)}
    for key in ME_LIST_KEYS:
        ordered[key] = out.get(key)
    return [version, ordered]


def _build_abm(kkex, slot, coordinate_count, bust_abm="keep"):
    """bust_abm: keep=游戏原版行为；reset=胸骨缩放改为 1.0；remove=删除胸骨。"""
    abm = kkex.get(ABM_KEY)
    if not isinstance(abm, list) or len(abm) < 2 or not isinstance(abm[1], dict):
        return None
    bone_bytes = abm[1].get("boneData")
    if not isinstance(bone_bytes, bytes):
        return None
    try:
        ext = unpack(bone_bytes)
        if not isinstance(ext, msgpack.ExtType) or ext.code != ABM_EXT_CODE:
            return None
        # 长度前缀按 msgpack 整数读取，兼容老卡的 3 字节/5 字节编码
        unpacker = msgpack.Unpacker(io.BytesIO(ext.data), raw=False, strict_map_key=False)
        raw_size = unpacker.unpack()
        prefix_len = unpacker.tell()
        raw = lz4.block.decompress(ext.data[prefix_len:], uncompressed_size=raw_size)
        bones = unpack(raw)
    except Exception:
        return None
    if not isinstance(bones, list):
        return None

    filtered = []
    for bone in bones:
        # 老版本 ABM 是 [骨骼名, 矩阵组]；新版本多一个 BoneLocation
        if not isinstance(bone, list) or len(bone) < 2:
            continue
        groups = bone[1]
        if not isinstance(groups, list) or len(groups) <= 1:
            continue
        name = bone[0] if isinstance(bone[0], str) else ""
        is_bust = any(name.startswith(p) for p in BUST_BONE_PREFIXES)
        if is_bust and bust_abm == "remove":
            continue
        # KKABMX 的 IsCoordinateSpecific() == (CoordinateModifiers.Length > 1)
        value = groups[slot] if slot < len(groups) else None
        if is_bust and bust_abm == "reset":
            value = ABM_IDENTITY
        new_bone = [bone[0], [value]]
        if len(bone) >= 3:
            new_bone.append(bone[2])
        filtered.append(new_bone)

    # 没有服装级骨骼时，插件不会写 ABM 数据
    if not filtered:
        return None

    new_raw = pack(filtered)
    new_comp = lz4.block.compress(new_raw, store_size=False)
    # 原生卡这里固定是 5 字节 int32（0xd2 + 4 字节大端），不是最小长度编码
    new_prefix = b"\xd2" + struct.pack(">i", len(new_raw))
    new_bone_data = pack(msgpack.ExtType(code=ABM_EXT_CODE, data=new_prefix + new_comp))
    return [ABM_SAVE_VERSION, {"boneData": new_bone_data}]


def _build_more_accessories(kkex, slot, by_slot, accessory_parts=None):
    ma = kkex.get(MORE_ACC_KEY)
    if not isinstance(ma, list) or len(ma) < 2 or not isinstance(ma[1], dict):
        return None
    xml_text = ma[1].get("additionalAccessories")
    if not isinstance(xml_text, str):
        return None
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None

    version = root.get("version", "2.0.21.1")
    target = None
    for child in root:
        if child.tag == "accessorySet" and child.get("type") == str(slot):
            target = child
            break

    if target is not None:
        children = list(target)
    elif all(child.tag == "accessory" for child in root):
        children = list(root)          # 已经是扁平结构
    else:
        children = []

    pieces = []
    for k, child in enumerate(children):
        node = copy.deepcopy(child)

        # 优先用同一张卡 a.parts 里已经 UAR 解析过的 ID。
        # 人物卡 XML 偶尔会残留旧 ID，直接复制会导致饰品错位/换模型。
        part = None
        if isinstance(accessory_parts, list) and 20 + k < len(accessory_parts):
            part = accessory_parts[20 + k]
        synced = False
        if isinstance(part, dict) and part.get("type") not in (None, 120) and part.get("id"):
            node_type = node.get("type")
            node_parent = node.get("parentKey")
            type_match = (node_type is None or str(node_type) == str(part.get("type")))
            parent_match = (node_parent is None or node_parent == (part.get("parentKey") or ""))
            if type_match and parent_match:
                node.set("id", str(part["id"]))
                synced = True

        if not synced:
            raw_id = node.get("id")
            if raw_id is not None:
                try:
                    old_id = int(raw_id)
                except ValueError:
                    old_id = None
                if old_id is not None and old_id in by_slot:
                    node.set("id", str(by_slot[old_id]))
        pieces.append(ET.tostring(node, encoding="unicode"))
    if pieces:
        new_xml = f'<additionalAccessories version="{version}">' + "".join(pieces) + "</additionalAccessories>"
    else:
        # 原生卡空元素写成自闭合；保持字节形态一致
        new_xml = f'<additionalAccessories version="{version}" />'
    return [ma[0] if isinstance(ma[0], int) else MORE_ACC_SAVE_VERSION,
            {"additionalAccessories": new_xml}]


def _build_additional_card_info(kkex, slot):
    """只使用人物卡里实际存在的 Additional_Card_Info 数据；不凭空补默认值。"""
    aci = kkex.get(ACI_KEY)
    if not isinstance(aci, list) or len(aci) < 2 or not isinstance(aci[1], dict):
        return None
    version = aci[0] if isinstance(aci[0], int) else ACI_SAVE_VERSION
    coord_info = aci[1].get("CoordinateInfo")
    if isinstance(coord_info, bytes):
        try:
            coord_info = unpack(coord_info)
        except Exception:
            return None
    if not isinstance(coord_info, dict):
        return None
    value = coord_info.get(slot)
    if not isinstance(value, dict):
        return None
    return [version, {"CoordinateInfo": pack(value)}]


def _build_anal(kkex, slot):
    anal = kkex.get(ANAL_KEY)
    if not isinstance(anal, list) or len(anal) < 2 or not isinstance(anal[1], dict):
        return None
    data = anal[1]
    graphs = data.get("Graphs")
    graph_data = data.get("GraphData")
    for name, value in (("Graphs", graphs), ("GraphData", graph_data)):
        if isinstance(value, bytes):
            try:
                if name == "Graphs":
                    graphs = unpack(value)
                else:
                    graph_data = unpack(value)
            except Exception:
                return None
    if not isinstance(graphs, dict) or not isinstance(graph_data, dict):
        return None
    if slot not in graphs or slot not in graph_data:
        return None
    return [anal[0] if isinstance(anal[0], int) else 0, {
        "Graph": pack(graphs[slot]),
        "GraphData": pack(graph_data[slot]),
        "Version": data.get("Version", 2),
    }]


def _build_hair_accessory(kkex, slot, skip_accessories=()):
    hair = kkex.get(HAIR_ACC_KEY)
    if not isinstance(hair, list) or len(hair) < 2 or not isinstance(hair[1], dict):
        return None
    value = hair[1].get("HairAccessories")
    if isinstance(value, bytes):
        try:
            value = unpack(value)
        except Exception:
            return None
    if not isinstance(value, dict) or slot not in value:
        return None
    data = value[slot]
    skip = {str(i) for i in skip_accessories}
    if skip and isinstance(data, dict):
        data = {k: v for k, v in data.items() if str(k) not in skip}
    # 该坐标没有发饰数据时，原生卡不会写 HAC 插件
    if not data:
        return None
    return [hair[0] if isinstance(hair[0], int) else 0,
            {"CoordinateHairAccessories": pack(data)}]


def _build_obj_import(kkex, slot):
    """ObjImport：人物卡是 slots{n}/meshes{n}，服装卡写成 slots/meshes。"""
    plugin = kkex.get(OBJ_IMPORT_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    slots = plugin[1].get(f"slots{slot}")
    meshes = plugin[1].get(f"meshes{slot}")
    if not isinstance(slots, bytes) or not isinstance(meshes, bytes):
        return None
    try:
        slot_list = unpack(slots)
    except Exception:
        return None
    if not isinstance(slot_list, list) or not slot_list:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 0,
            {"slots": slots, "meshes": meshes}]


def _build_pushup(kkex, slot):
    pushup = kkex.get(PUSHUP_KEY)
    if not isinstance(pushup, list) or len(pushup) < 2 or not isinstance(pushup[1], dict):
        return None
    data = pushup[1]
    bra = data.get("Pushup_BraData")
    top = data.get("Pushup_TopData")
    if isinstance(bra, bytes):
        try:
            bra = unpack(bra)
        except Exception:
            return None
    if isinstance(top, bytes):
        try:
            top = unpack(top)
        except Exception:
            return None
    if not isinstance(bra, dict) or not isinstance(top, dict):
        return None
    bra_slot = bra.get(slot)
    top_slot = top.get(slot)
    if not isinstance(bra_slot, dict) or not isinstance(top_slot, dict):
        return None

    # 状态数据（Cetol 版 Pushup 会按坐标写 Top/BotStateData）
    state_top = _as_value(data.get("Pushup_TopStateData"))
    state_bot = _as_value(data.get("Pushup_BotStateData"))
    top_state = state_top.get(slot, state_top.get(str(slot))) if isinstance(state_top, dict) else None
    bot_state = state_bot.get(slot, state_bot.get(str(slot))) if isinstance(state_bot, dict) else None

    # 原版 Pushup：胸/上衣都没开、也没有状态数据时，插件根本不写入
    if (not bra_slot.get("EnablePushup") and not top_slot.get("EnablePushup")
            and not isinstance(top_state, dict) and not isinstance(bot_state, dict)):
        return None
    out = {
        "PushupCoordinate_BraData": pack(bra_slot),
        "PushupCoordinate_TopData": pack(top_slot),
    }
    if isinstance(top_state, dict):
        out["PushupCoordinate_TopStateData"] = pack(top_state)
    if isinstance(bot_state, dict):
        out["PushupCoordinate_BotStateData"] = pack(bot_state)
    return [pushup[0] if isinstance(pushup[0], int) else 0, out]


def _build_dbde(kkex):
    dbde = kkex.get(DBDE_KEY)
    if not isinstance(dbde, list):
        return None
    return [dbde[0] if len(dbde) and isinstance(dbde[0], int) else 0, {
        "AccessoryEdits": pack([]),
        "NormalEdits": pack([]),
    }]


def _build_clothing_unlocker(kkex, slot):
    """ClothingUnlocker：人物卡按坐标存 bool，服装卡只在需要解锁时写当前坐标。"""
    plugin = kkex.get(CLOTHING_UNLOCKER_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    unlocked = _as_value(plugin[1].get("ClothingUnlocked"))
    if not isinstance(unlocked, dict):
        return None
    value = unlocked.get(slot, unlocked.get(str(slot), False))
    if not value:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 0,
            {"ClothingUnlockedCoordinate": True}]


def _build_aaapk(kkex, slot):
    """AAAPK：人物卡 ParentRules 是带 Coordinate 的列表，服装卡只保留当前坐标。"""
    plugin = kkex.get(AAAPK_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    rules = _as_value(plugin[1].get("ParentRules"))
    if not isinstance(rules, list):
        return None
    kept = []
    for x in rules:
        if not isinstance(x, dict) or x.get("Coordinate") != slot:
            continue
        rule = dict(x)
        # 插件 OnCoordinateBeingSaved 会把所有 Coordinate 改写为 -1
        rule["Coordinate"] = -1
        kept.append(rule)
    if not kept:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 1,
            {"ParentRules": pack(kept)}]


def _build_mr(kkex, slot):
    """MaterialRouter：人物卡 OutfitTriggers 按坐标存，服装卡字段名是 OutfitTrigger。"""
    plugin = kkex.get(MR_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    data = plugin[1]
    out = {}
    triggers = _as_value(data.get("OutfitTriggers"))
    if isinstance(triggers, dict):
        sub = triggers.get(slot, triggers.get(str(slot)))
        if isinstance(sub, list) and sub:
            out["OutfitTrigger"] = pack(sub)
    body = _as_value(data.get("BodyTrigger"))
    if isinstance(body, list) and body:
        out["BodyTrigger"] = pack(body)
    if not out:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 1, out]


def _build_ppcam(kkex, slot):
    """PPCAM：BodyMaskList 按坐标存；TextureDictionary 是共享贴图表，整表保留。"""
    plugin = kkex.get(PPCAM_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    data = plugin[1]
    masks = _as_value(data.get("BodyMaskList"))
    if not isinstance(masks, dict):
        return None
    sub = masks.get(slot, masks.get(str(slot)))
    if not isinstance(sub, list):
        return None
    out = {"BodyMaskList": pack(sub)}
    textures = _as_value(data.get("TextureDictionary"))
    if isinstance(textures, dict) and textures:
        out["TextureDictionary"] = pack(textures)
    return [plugin[0] if isinstance(plugin[0], int) else 1, out]


def _filter_spcr_section(spcr, slot):
    """SPCR = [controllers, sectionVersion, engineForkVersion]；按控制器里的坐标过滤。"""
    if not isinstance(spcr, list) or not spcr:
        return spcr
    controllers = spcr[0]
    if not isinstance(controllers, list):
        return spcr
    kept = [c for c in controllers if isinstance(c, list) and c and c[0] == slot]
    out = list(spcr)
    out[0] = kept
    return out


def _build_cetol_cardpersist(kkex, slot):
    """Cetol 的 CardPersist：doc = [version, 骨骼[], 鞋跟[], SPCR]。按当前坐标过滤。

    卡片里的每条记录都带 Coordinate 字段；游戏保存服装卡时会为当前坐标重新采集一份，
    所以这里只保留 Coordinate == slot 的条目（值原样复制，不做归一化）。
    """
    plugin = kkex.get(CETOL_PERSIST_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    raw = plugin[1].get("doc")
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        doc = unpack(bytes(raw))
    except Exception:
        return None
    if not isinstance(doc, list) or len(doc) < 3:
        return None
    bones = doc[1] if isinstance(doc[1], list) else []
    heels = doc[2] if isinstance(doc[2], list) else []
    out = list(doc)
    out[1] = [b for b in bones if isinstance(b, list) and b and b[0] == slot]
    out[2] = [h for h in heels if isinstance(h, list) and h and h[0] == slot]
    if len(out) > 3:
        out[3] = _filter_spcr_section(out[3], slot)

    spcr = out[3] if len(out) > 3 else None
    spcr_empty = True
    if isinstance(spcr, list) and spcr and isinstance(spcr[0], list):
        spcr_empty = not spcr[0]
    if not out[1] and not out[2] and spcr_empty:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 1, {"doc": pack(out)}]


def _build_cetol_anticlipping(kkex, slot):
    """Koikatsu AntiClipping：字段名形如 MaskData_<part>_<坐标>，只保留当前坐标。

    AC_StorageSchema 是结构标记（无坐标后缀），必须保留。
    """
    plugin = kkex.get(CETOL_ANTICLIP_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    data = plugin[1]
    out = {}
    for key, value in data.items():
        tail = key.rsplit("_", 1)[-1]
        if tail.isdigit():
            if int(tail) == slot:
                out[key] = value
        else:
            out[key] = value
    if not any(k != "AC_StorageSchema" for k in out):
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 0, out]


def _build_kcox(kkex, slot):
    """OverlayMods 的衣服覆盖层：只使用人物卡里实际存在的 KCOX 数据。"""
    kcox = kkex.get(KCOX_KEY)
    if not isinstance(kcox, list) or len(kcox) < 2 or not isinstance(kcox[1], dict):
        return None
    data = kcox[1]
    out = {}
    for key in ("Overlays", "TextureSizeOverride"):
        value = _as_value(data.get(key))
        if not isinstance(value, dict):
            continue
        group = value.get(slot, value.get(str(slot)))
        if group:
            out[key] = pack(group)
    version = kcox[0] if isinstance(kcox[0], int) else KCOX_SAVE_VERSION
    return [version, out]


def _as_value(value):
    """把 PluginData 里的 bytes 解成对象。"""
    if isinstance(value, bytes):
        try:
            return unpack(value)
        except Exception:
            return None
    return value


def _generic_coordinate_extract(value, slot, coordinate_count):
    """对未显式实现的服装级插件做通用按槽提取。

    支持两种常见结构：
      - {字段: {"0": ..., "1": ...}}  -> 取 [slot]
      - {字段: [{"Coordinate": n, ...}, ...]} -> 过滤 Coordinate == slot
    返回 ([version, 新数据], 说明) 或 (None, 失败原因)。
    """
    if not (isinstance(value, list) and len(value) >= 2 and isinstance(value[1], dict)):
        return None, "不是 [version, dict] 格式"

    version = value[0] if isinstance(value[0], int) else 0
    data = value[1]
    out = {}
    details = []

    for key, raw in data.items():
        parsed = _as_value(raw)
        if isinstance(parsed, dict):
            subkey = slot if slot in parsed else (str(slot) if str(slot) in parsed else None)
            if subkey is None:
                continue
            sub = parsed[subkey]
            if not isinstance(sub, (dict, list)):
                continue
            # 至少要有多个“坐标键”，避免把普通 dict 误当坐标表
            numkeys = [k for k in parsed
                       if isinstance(k, int) or (isinstance(k, str) and k.isdigit())]
            if len(numkeys) >= min(2, coordinate_count):
                out[key] = pack(sub)
                details.append(f"{key}=dict[{slot}]")
        elif isinstance(parsed, list):
            kept = []
            coord_seen = False
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                coord = item.get("Coordinate", item.get("CoordinateIndex"))
                if coord is None:
                    continue
                coord_seen = True
                if coord == slot or str(coord) == str(slot) or coord == -1 or str(coord) == "-1":
                    kept.append(item)
            if coord_seen and kept:
                out[key] = pack(kept)
                details.append(f"{key}=list(Coordinate=={slot})")

    if not details:
        return None, "未找到按坐标分组的结构"

    # 保留标量字段（例如 Version）
    for key, raw in data.items():
        if key in out:
            continue
        if raw is None or isinstance(raw, (int, float, bool, str)):
            out[key] = raw

    return [version, out], ", ".join(details)


def _accessory_states_nonempty(data):
    if not isinstance(data, dict):
        return False
    # 插件的判定是 this.Slotinfo.Count == 0；对应序列化后的 _slotinfo / _names。
    # _clothnotdata 只是个 3 元素占位列表，不能当作“有数据”的依据。
    for key in ("_slotinfo", "_names"):
        value = data.get(key)
        if isinstance(value, (dict, list)) and value:
            return True
    return False


def _build_accessory_states(kkex, slot, skip_accessories=()):
    """Accessory States（GUID: Accessory_States）只保存当前坐标的状态。"""
    states = kkex.get(ACC_STATES_KEY)
    if not isinstance(states, list) or len(states) < 2 or not isinstance(states[1], dict):
        return None
    coord_data = _as_value(states[1].get("CoordinateData"))
    if not isinstance(coord_data, dict) or slot not in coord_data:
        return None
    value = coord_data[slot]
    if not _accessory_states_nonempty(value):
        return None
    if skip_accessories and isinstance(value, dict):
        value = copy.deepcopy(value)
        slotinfo = value.get("_slotinfo")
        if isinstance(slotinfo, dict):
            for i in skip_accessories:
                slotinfo.pop(i, None)
                slotinfo.pop(str(i), None)
        if not _accessory_states_nonempty(value):
            return None
    return [states[0] if isinstance(states[0], int) else 1,
            {"CoordinateData": pack(value)}]


def _build_madevil_ass(kkex, slot, skip_accessories=()):
    """Accessory States 的旧 GUID 兼容数据：按 Coordinate 过滤，并改写成 -1。"""
    ass = kkex.get(MADEVIL_ASS_KEY)
    if not isinstance(ass, list) or len(ass) < 2 or not isinstance(ass[1], dict):
        return None
    data = ass[1]
    props = _as_value(data.get("TriggerPropertyList"))
    groups = _as_value(data.get("TriggerGroupList"))
    skip = set(skip_accessories)

    def keep(items):
        kept = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("Coordinate") != slot:
                    continue
                if item.get("Slot") in skip:
                    continue
                copied = dict(item)
                # 插件写坐标卡时调用 Accstatesyncconvert(-1, ...)
                copied["Coordinate"] = -1
                kept.append(copied)
        return kept

    kept_props = keep(props)
    kept_groups = keep(groups)
    if not kept_props and not kept_groups:
        return None
    return [ass[0] if isinstance(ass[0], int) else 6, {
        "TriggerPropertyList": pack(kept_props),
        "TriggerGroupList": pack(kept_groups),
    }]


def _build_clothing_blend_shape(kkex, slot):
    """Clothing BlendShape：每项带 Coordinate 字段。"""
    plugin = kkex.get(CLOTHING_BLEND_SHAPE_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    data_set = _as_value(plugin[1].get("ClothesDataSet"))
    if not isinstance(data_set, list):
        return None
    kept = [item for item in data_set
            if isinstance(item, dict) and item.get("Coordinate") == slot]
    if not kept:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 1,
            {"ClothesDataSet": pack(kept)}]


def _build_dynamic_bone_editor(kkex, slot, skip_accessories=()):
    """Dynamic Bone Editor：每项带 CoordinateIndex 字段。"""
    plugin = kkex.get(DYNAMIC_BONE_KEY)
    if not isinstance(plugin, list) or len(plugin) < 2 or not isinstance(plugin[1], dict):
        return None
    data = _as_value(plugin[1].get("AccessoryDynamicBoneData"))
    if not isinstance(data, list):
        return None
    skip = set(skip_accessories)
    kept = [item for item in data
            if isinstance(item, dict) and item.get("CoordinateIndex") == slot
            and item.get("Slot") not in skip]
    if not kept:
        return None
    return [plugin[0] if isinstance(plugin[0], int) else 0,
            {"AccessoryDynamicBoneData": pack(kept)}]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _build_kkex(card, slot, skip_accessories=(), bust_abm="keep", accessory_parts=None,
                generic_plugins=True):
    kkex = card["kkex"]
    coordinate_count = get_coordinate_count(card)
    by_property, by_slot = _build_id_maps(kkex, slot)

    plugins = {}
    notes = []

    uar = _build_uar(kkex, slot, skip_accessories)
    if uar:
        plugins[UAR_KEY] = uar

    more_acc = _build_more_accessories(kkex, slot, by_slot, accessory_parts)
    if more_acc:
        plugins[MORE_ACC_KEY] = more_acc

    accessory_states = _build_accessory_states(kkex, slot, skip_accessories)
    if accessory_states:
        plugins[ACC_STATES_KEY] = accessory_states

    madevil_ass = _build_madevil_ass(kkex, slot, skip_accessories)
    if madevil_ass:
        plugins[MADEVIL_ASS_KEY] = madevil_ass

    aci = _build_additional_card_info(kkex, slot)
    if aci:
        plugins[ACI_KEY] = aci

    # KCOX：只在人物卡本身有 OverlayMods 数据时写入
    kcox = _build_kcox(kkex, slot)
    if kcox:
        plugins[KCOX_KEY] = kcox

    clothing_unlocker = _build_clothing_unlocker(kkex, slot)
    if clothing_unlocker:
        plugins[CLOTHING_UNLOCKER_KEY] = clothing_unlocker

    aaapk = _build_aaapk(kkex, slot)
    if aaapk:
        plugins[AAAPK_KEY] = aaapk

    mr = _build_mr(kkex, slot)
    if mr:
        plugins[MR_KEY] = mr

    ppcam = _build_ppcam(kkex, slot)
    if ppcam:
        plugins[PPCAM_KEY] = ppcam

    clothing_blend_shape = _build_clothing_blend_shape(kkex, slot)
    if clothing_blend_shape:
        plugins[CLOTHING_BLEND_SHAPE_KEY] = clothing_blend_shape

    anal = _build_anal(kkex, slot)
    if anal:
        plugins[ANAL_KEY] = anal

    dbde = _build_dbde(kkex)
    if dbde:
        plugins[DBDE_KEY] = dbde

    abm = _build_abm(kkex, slot, coordinate_count, bust_abm=bust_abm)
    if abm:
        plugins[ABM_KEY] = abm

    dynamic_bone = _build_dynamic_bone_editor(kkex, slot, skip_accessories)
    if dynamic_bone:
        plugins[DYNAMIC_BONE_KEY] = dynamic_bone

    obj_import = _build_obj_import(kkex, slot)
    if obj_import:
        plugins[OBJ_IMPORT_KEY] = obj_import

    hair = _build_hair_accessory(kkex, slot, skip_accessories)
    if hair:
        plugins[HAIR_ACC_KEY] = hair

    me = _build_me(kkex, slot, skip_accessories)
    if me:
        plugins[ME_KEY] = me

    pushup = _build_pushup(kkex, slot)
    if pushup:
        plugins[PUSHUP_KEY] = pushup

    cetol_persist = _build_cetol_cardpersist(kkex, slot)
    if cetol_persist:
        plugins[CETOL_PERSIST_KEY] = cetol_persist

    cetol_anticlip = _build_cetol_anticlipping(kkex, slot)
    if cetol_anticlip:
        plugins[CETOL_ANTICLIP_KEY] = cetol_anticlip

    # 未显式实现、但规则库判定为“人物级/服装级”的插件：尝试通用按槽提取。
    # 显式白名单里的 key 即使 builder 返回 None，也不走通用逻辑（尊重槽内无数据的语义）。
    generic_added = set()
    catalog = _load_plugin_catalog()
    for key, value in kkex.items():
        if key in COORDINATE_PLUGIN_KEYS:
            continue
        if key in DROP_PLUGIN_KEYS:
            notes.append(f"{key}: 迁移/工具插件，已丢弃")
            continue
        if key in CHARACTER_LEVEL_PLUGIN_KEYS:
            # 规则库可能因为个别旧服装卡把它标成 both，但游戏保存时不会写入坐标数据
            notes.append(f"{key}: 人物级白名单，已丢弃")
            continue
        if key in GENERIC_UNSUPPORTED:
            notes.append(f"{key}: 服装级，但字段结构需显式适配，已丢弃")
            continue
        cat = catalog.get(key)
        if cat in ("both", "coordinate"):
            if not generic_plugins:
                notes.append(f"{key}: 服装级，但未启用通用提取，已丢弃")
                continue
            extracted, detail = _generic_coordinate_extract(value, slot, coordinate_count)
            if extracted is not None:
                plugins[key] = extracted
                generic_added.add(key)
                notes.append(f"{key}: 通用坐标提取（{detail}），请进游戏验证")
            else:
                notes.append(f"{key}: 服装级，通用提取失败（{detail}），已丢弃")
        elif cat in ("character", "unknown"):
            notes.append(f"{key}: {cat}，已丢弃")
        else:
            notes.append(f"{key}: 不在规则库，已丢弃")

    # 护栏：显式白名单 + 本次确认可提取的通用坐标插件
    allowed = set(COORDINATE_PLUGIN_KEYS) | generic_added
    plugins = {k: v for k, v in plugins.items() if k in allowed}
    return plugins, by_property, notes


def _safe_filename(name):
    """把人物名里 Windows 不允许的字符替换掉，避免写文件失败。"""
    bad = '<>:"/\\|?*'
    cleaned = "".join(("_" if (ch in bad or ord(ch) < 32) else ch) for ch in str(name))
    cleaned = cleaned.rstrip(" .")
    if not cleaned:
        cleaned = "Clothes"
    reserved = {"CON", "PRN", "AUX", "NUL",
                *[f"COM{i}" for i in range(1, 10)],
                *[f"LPT{i}" for i in range(1, 10)]}
    if cleaned.upper() in reserved:
        cleaned = "_" + cleaned
    return cleaned


LAST_WARNINGS = []


def extract_slot(character_path, slot, output_dir=None, skip_accessories=(), bust_abm="keep",
                 generic_plugins=True, warnings=None):
    """从人物卡提取指定 slot，生成游戏可直接读取的服装卡，返回输出路径。

    skip_accessories: 需要强制清空的饰品槽下标（例如 (0,)）。
    bust_abm: keep=原版行为（默认）；reset=胸骨缩放改为 1.0；remove=删除胸骨。
    generic_plugins: 对规则库里的新服装级插件尝试通用按槽提取。
    warnings: 可选列表，收集插件处理说明。
    """
    character_path = str(character_path).replace('\x00', '').strip()
    output_dir = str(output_dir or Path(character_path).parent).replace('\x00', '').strip()
    skip_accessories = tuple(int(i) for i in skip_accessories)
    if bust_abm not in ("keep", "reset", "remove"):
        raise ValueError(f"bust_abm 只能是 keep/reset/remove，收到 {bust_abm!r}")

    card = parse_character_card(character_path)
    c_data, a_data, enable_mk, m_data, _ = _read_coordinate_block(card, slot)

    # --- 衣服 / 饰品：ID 改写后重新打包 ---
    by_property, _ = _build_id_maps(card["kkex"], slot)
    try:
        clothes = unpack(c_data)
    except Exception as exc:
        raise ValueError(f"衣服数据解析失败: {exc}")
    try:
        accessory = unpack(a_data)
    except Exception as exc:
        raise ValueError(f"饰品数据解析失败: {exc}")

    _rewrite_clothes_ids(clothes, slot, by_property)
    _rewrite_accessory_ids(accessory, slot, by_property)
    if skip_accessories:
        _clear_accessory_parts(accessory, skip_accessories)
    c_data = pack(clothes)
    a_data = pack(accessory)

    # --- KKEx ---
    plugins, _, notes = _build_kkex(card, slot, skip_accessories, bust_abm=bust_abm,
                                    accessory_parts=accessory.get("parts"),
                                    generic_plugins=generic_plugins)
    LAST_WARNINGS[:] = notes
    if warnings is not None:
        warnings.extend(notes)
    for note in notes:
        print(f"[插件] {note}")
    kkex_packed = pack(plugins)

    # --- 卡头与数据块 ---
    clothes_name = CLOTHES_NAMES[slot] if slot < len(CLOTHES_NAMES) else f"Slot{slot}"
    full_name = f"{card['name']}{clothes_name}" if card["name"] else clothes_name
    if skip_accessories:
        full_name += "_noAcc" + "-".join(str(i) for i in skip_accessories)
    if bust_abm == "reset":
        full_name += "_bust1"
    elif bust_abm == "remove":
        full_name += "_noBust"

    save_bytes = struct.pack("<i", len(c_data)) + c_data
    save_bytes += struct.pack("<i", len(a_data)) + a_data
    save_bytes += struct.pack("<B", enable_mk)
    save_bytes += struct.pack("<i", len(m_data)) + m_data

    output = card["data"][:card["png_end"]]
    output += struct.pack("<i", 100)                  # ProductNo（KK 固定 100，非卡版本）
    output += write_7bit_str("【KoiKatuClothes】")
    output += write_7bit_str("0.0.0")
    output += write_7bit_str(full_name)
    output += struct.pack("<i", len(save_bytes))
    output += save_bytes

    # 原生服装卡的 KKEx 头：7bit "KKEx" + int32 3 + int32 长度 + msgpack
    output += write_7bit_str("KKEx")
    output += struct.pack("<i", 3)
    output += struct.pack("<i", len(kkex_packed))
    output += kkex_packed

    out_path = Path(output_dir) / f"{_safe_filename(full_name)}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(output)
    return str(out_path)


def extract_all(character_path, output_dir=None, skip_accessories=(), bust_abm="keep",
                generic_plugins=True, warnings=None):
    """提取卡内全部 slot，返回 [(slot, 路径或异常), ...]。"""
    card = parse_character_card(character_path)
    count = get_coordinate_count(card)
    results = []
    for slot in range(count):
        try:
            results.append((slot, extract_slot(character_path, slot, output_dir,
                                               skip_accessories=skip_accessories,
                                               bust_abm=bust_abm,
                                               generic_plugins=generic_plugins,
                                               warnings=warnings)))
        except Exception as exc:
            results.append((slot, exc))
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Koikatu 人物卡 → 服装卡")
    parser.add_argument("src", help="人物卡 .png")
    parser.add_argument("out", nargs="?", help="输出目录（默认与人物卡同目录）")
    parser.add_argument("slots", nargs="*", type=int, help="只提取指定 slot（默认全部）")
    parser.add_argument("--skip-acc", default="",
                        help="强制清空的饰品槽，逗号分隔，例如 0 或 0,3")
    parser.add_argument("--bust-abm", choices=("keep", "reset", "remove"), default="keep",
                        help="胸骨 ABM：keep=游戏原版（默认）；reset=缩放改 1.0；remove=删除胸骨")
    parser.add_argument("--no-generic-plugins", action="store_true",
                        help="关闭规则库中新服装级插件的通用按槽提取")
    args = parser.parse_args()

    skip = tuple(int(x) for x in args.skip_acc.split(",") if x.strip().isdigit())
    card = parse_character_card(args.src)
    total = get_coordinate_count(card)
    print(f"人物: {card['name']!r}，共 {total} 套服装")
    slots = args.slots if args.slots else list(range(total))
    for slot in slots:
        path = extract_slot(args.src, slot, args.out, skip_accessories=skip,
                            bust_abm=args.bust_abm,
                            generic_plugins=not args.no_generic_plugins)
        print(f"  slot {slot}: {path}  ({Path(path).stat().st_size} 字节)")
