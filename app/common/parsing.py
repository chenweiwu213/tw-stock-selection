"""
資料解析共用工具
"""


def find_field_index(fields: list, candidates: list) -> int:
    """在 fields 中依 candidates 候選名稱找出欄位索引(先找完全相符，再找部分包含)，找不到則丟出 ValueError(內含實際欄位清單，方便官方格式改版時校正)"""
    for cand in candidates:
        if cand in fields:
            return fields.index(cand)
    for i, f in enumerate(fields):
        if any(cand in f for cand in candidates):
            return i
    raise ValueError(f"找不到欄位：{candidates}(實際欄位：{fields})")