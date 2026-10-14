import hashlib
import json
import math
import re
from typing import Any

from app.geojson import json_safe

_HINDI_NAME = re.compile(r"\n[ \t]*नाम[ \t]*:")
_EN_NAME = re.compile(r"\n[ \t]*Name[ \t]*:", re.I)


def _value(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text or "", re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).strip().strip(",")
    return value or None


def _owner_from_block(block: str, lang: str) -> dict[str, str | None]:
    lines = (block or "").strip()
    name = lines.split("\n", 1)[0].strip() if lines else None
    if lang == "hi":
        father = _value(r"पिता का नाम\s*:\s*(.+)", block)
        husband = _value(r"पति का नाम\s*:\s*(.+)", block)
        address = _value(r"पता\s*:\s*(.+)", block)
    else:
        father = _value(r"Father'?s name\s*:\s*(.+)", block)
        husband = _value(r"Husband'?s name\s*:\s*(.+)", block)
        address = _value(r"Address\s*:\s*(.+)", block)
    relation = "father" if father else ("husband" if husband else None)
    return {
        "name": name or None,
        "relation": relation,
        "relation_name": father or husband,
        "address": address,
    }


def _parse_text(text: str, lang: str) -> dict[str, Any]:
    raw = (text or "").replace("\r\n", "\n")
    splitter = _HINDI_NAME if lang == "hi" else _EN_NAME
    chunks = splitter.split(raw)
    header = chunks[0] if chunks else ""
    owners = [_owner_from_block(chunk, lang) for chunk in chunks[1:] if chunk.strip()]
    if lang == "hi":
        tenure = _value(r"धारणाधिकार\s*:\s*(.+)", header)
        khasra = _value(r"खसरा नंबर\s*:\s*(.+)", header)
        land_use = _value(r"प्रयोजन \(भूमि उपयोग\)\s*:\s*(.+)", header)
        included = _value(r"शामिल खसरा\s*:\s*(.+)", raw)
    else:
        tenure = _value(r"Tenure rights\s*:\s*(.+)", header)
        khasra = _value(r"Khasra number\s*:\s*(.+)", header)
        land_use = _value(r"Purpose \(land use\)\s*:\s*(.+)", raw)
        included = _value(r"Included khasra\s*:\s*(.+)", raw)
    return {
        "tenure": tenure,
        "khasra": khasra,
        "land_use": land_use,
        "included_khasra": included,
        "owners": [owner for owner in owners if owner.get("name")],
    }


def owner_color(name: str) -> str:
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    hue = int(digest[:2], 16) * 360 // 255
    sat = 0.52 + (int(digest[2:4], 16) % 20) / 100
    light = 0.46 + (int(digest[4:6], 16) % 12) / 100

    def channel(n: int) -> int:
        k = (n + hue / 30) % 12
        a = sat * min(light, 1 - light)
        color = light - a * max(-1, min(k - 3, 9 - k, 1))
        return int(round(255 * color))

    return f"#{channel(0):02x}{channel(8):02x}{channel(4):02x}"


def to_multipolygon(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        geom = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    if not isinstance(geom, dict):
        return None
    if geom.get("type") == "Polygon":
        return {"type": "MultiPolygon", "coordinates": [geom.get("coordinates") or []]}
    if geom.get("type") == "MultiPolygon":
        return geom
    return geom


def parse_plot_info(info: str | None, info_en: str | None) -> dict[str, Any]:
    hindi_text = ""
    owner_plots: list[str] = []
    gisinfo = None
    try:
        payload = json.loads(info) if info else {}
        plot_info = payload.get("plot_info") or {}
        hindi_text = plot_info.get("info") or ""
        owner_plots = [str(item) for item in (plot_info.get("ownerplots") or []) if item]
        gisinfo = plot_info.get("gisinfo")
    except json.JSONDecodeError:
        hindi_text = info or ""

    hi = _parse_text(hindi_text, "hi")
    en = _parse_text(info_en or "", "en")
    hi_owners = hi["owners"]
    en_owners = en["owners"]
    owners = []
    for index in range(max(len(hi_owners), len(en_owners))):
        hindi = hi_owners[index] if index < len(hi_owners) else {}
        english = en_owners[index] if index < len(en_owners) else {}
        owners.append(
            {
                "name": hindi.get("name") or english.get("name"),
                "name_en": english.get("name"),
                "relation": hindi.get("relation") or english.get("relation"),
                "relation_name": hindi.get("relation_name") or english.get("relation_name"),
                "address": hindi.get("address") or english.get("address"),
            }
        )

    primary = owners[0] if owners else {}
    owner_name = primary.get("name")
    return {
        "tenure": hi.get("tenure") or en.get("tenure"),
        "khasra": hi.get("khasra") or en.get("khasra"),
        "land_use": hi.get("land_use") or en.get("land_use"),
        "included_khasra": hi.get("included_khasra") or en.get("included_khasra"),
        "gisinfo": gisinfo,
        "owner_plots": ", ".join(owner_plots),
        "owners": owners,
        "owner_count": len(owners),
        "owner_name": owner_name,
        "owner_name_en": primary.get("name_en"),
        "owner_relation": primary.get("relation"),
        "owner_relation_name": primary.get("relation_name"),
        "owner_address": primary.get("address"),
        "owners_text": ", ".join(owner.get("name") or "" for owner in owners if owner.get("name")),
        "owner_color": owner_color(owner_name) if owner_name else "#c6f54b",
    }


_INNER_RING = re.compile(r"\(([-\d.eE+\s,]+)\)")


def wkt_to_multipolygon(wkt: str | None) -> dict[str, Any] | None:
    if not wkt:
        return None
    rings = []
    for group in _INNER_RING.findall(wkt):
        nums = [float(part) for part in group.replace(",", " ").split()]
        if len(nums) < 6:
            continue
        ring = [[nums[i], nums[i + 1]] for i in range(0, len(nums) - 1, 2)]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        rings.append(ring)
    if not rings:
        return None
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}


def local_wkt_to_geojson(
    wkt: str | None,
    x0: float,
    y0: float,
    lon0: float,
    lat0: float,
) -> dict[str, Any] | None:
    geom = wkt_to_multipolygon(wkt)
    if not geom:
        return None
    meters_per_deg_lat = 110540.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))

    def convert(x: float, y: float) -> list[float]:
        return [lon0 + (x - x0) / meters_per_deg_lon, lat0 + (y - y0) / meters_per_deg_lat]

    return {
        "type": "MultiPolygon",
        "coordinates": [
            [[convert(x, y) for x, y in ring] for ring in polygon]
            for polygon in geom["coordinates"]
        ],
    }


def plot_to_feature(row: Any, local_origin: dict[str, float] | None = None) -> dict[str, Any]:
    data = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    geom_wkt = data.pop("geom_wkt", None)
    data.pop("x_min", None)
    data.pop("y_min", None)
    if local_origin:
        geometry = local_wkt_to_geojson(
            geom_wkt,
            local_origin["x0"],
            local_origin["y0"],
            local_origin["lon0"],
            local_origin["lat0"],
        )
        data.pop("geom_geojson", None)
    else:
        geometry = to_multipolygon(data.pop("geom_geojson", None))
        data.pop("geom_wkt", None)
    parsed = parse_plot_info(data.pop("info", None), data.pop("info_en", None))
    properties = {key: json_safe(value) for key, value in data.items() if key != "id"}
    properties.update(parsed)
    properties["geometry_type"] = geometry.get("type") if geometry else None
    properties["geo_status"] = "local_cad" if local_origin else "georeferenced"
    return {
        "type": "Feature",
        "id": data.get("id"),
        "geometry": geometry,
        "properties": properties,
    }
