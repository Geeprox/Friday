"""用于浏览器回归的合成名字和选择题。"""

DEMO_NAMES = (
    ("沈听澜", "scene", "action", "shěn tīng lán"),
    ("沈知遥", "character", "action", "shěn zhī yáo"),
    ("沈弦月", "object", "modifier", "shěn xián yuè"),
    ("沈雨初", "scene", "parallel", "shěn yǔ chū"),
    ("沈汀然", "sound", "parallel", "shěn tīng rán"),
    ("沈禾宁", "life", "image", "shěn hé níng"),
    ("沈行远", "character", "action", "shěn xíng yuǎn"),
)


def populate(book):
    book.import_history({"surname": "沈", "avoided": [], "history": [], "simulation": True})
    with book.transaction() as db:
        for direction in ("girl", "boy"):
            keys = []
            for name, branch, form, pinyin in DEMO_NAMES:
                key = book.insert_name(db, direction, {"name": name, "branch": branch, "form": form,
                                                      "pinyin": pinyin, "quality": 2,
                                                      "card_note": ("像听见水面上卷起的一阵微澜。" if name == "沈听澜"
                                                                    else "可以留意两个字连读时的抑扬。"),
                                                      "detail": "合成示例，仅用于验证操作。"})
                keys.append(key)
            if all(keys[:2]):
                db.execute("INSERT INTO comparisons(direction,first,second,axis) VALUES(?,?,?,'image')",
                           (direction, keys[0], keys[1]))
