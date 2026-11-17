# -*- coding: utf-8 -*-
"""做一个「通用中文字体」放进 exe：覆盖 GB2312 全集 + ASCII + 假名 + 常用符号。

这样 exe 换到别的游戏上也不缺字，运行时直接复制进游戏的 fonts 目录即可，
不需要目标机器装 fontTools。
"""
import os
import sys

SRC_FONT = r"C:\Windows\Fonts\NotoSansSC-VF.ttf"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "GameFont-zh.ttf")

EXTRA = (
    "！？。，、；：（）「」『』【】《》…—～·×÷±＝％＋－*/\\|@#$&<>[]{}_^~`\"'"
    "♥♪★☆●○◆◇■□▲△▼▽※→←↑↓①②③④⑤⑥⑦⑧⑨⑩"
    "\u3000\u00a0"
)


def gb2312_all():
    """GB2312 全部汉字 + 全角符号（一级 3755 + 二级 3008）。"""
    out = []
    for hi in range(0xA1, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                ch = bytes([hi, lo]).decode("gb2312")
            except Exception:
                continue
            out.append(ch)
    return "".join(out)


def main():
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont
    from fontTools import subset

    chars = set(gb2312_all())
    chars.update(EXTRA)
    chars.update(chr(c) for c in range(0x20, 0x7F))
    chars.update(chr(c) for c in range(0x3041, 0x30FF + 1))  # 假名，日文残留也能显示
    chars = {c for c in chars if c.isprintable() or c in "\u3000\u00a0"}
    text = "".join(sorted(chars))
    print("字符数:", len(text))

    font = TTFont(SRC_FONT)
    axes = {a.axisTag: a.defaultValue for a in font["fvar"].axes}
    axes["wght"] = 400
    font = instantiateVariableFont(font, axes, inplace=False, updateFontNames=False)

    opts = subset.Options()
    opts.desubroutinize = True
    opts.drop_tables += ["DSIG"]
    opts.name_IDs = ["*"]
    opts.name_legacy = True
    opts.layout_features = ["*"]
    sub = subset.Subsetter(options=opts)
    sub.populate(text=text)
    sub.subset(font)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    font.save(OUT)
    print("输出:", OUT, os.path.getsize(OUT), "字节")

    f2 = TTFont(OUT)
    cm = set()
    for t in f2["cmap"].tables:
        cm.update(t.cmap.keys())
    lost = [c for c in chars if ord(c) not in cm]
    print("缺失:", len(lost), "".join(lost[:30]))


if __name__ == "__main__":
    main()
