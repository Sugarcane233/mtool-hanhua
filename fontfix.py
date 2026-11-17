# -*- coding: utf-8 -*-
"""给 RPG Maker MV/MZ 游戏换上能显示中文的字体。

原版游戏自带 mplus 字体，缺几百个简体字，直接塞中文会显示空白方块。
这里把内置的 Noto Sans SC 子集字体复制进游戏并改写 gamefont.css。

所有改动都留 .bak 备份，可随时还原。
"""
import os
import shutil
import sys

FONT_NAME = "GameFont-zh.ttf"
CSS_TMPL = """@font-face {
    font-family: GameFont;
    src: url("%s");
}

.IIV::-webkit-media-controls-play-button,
video::-webkit-media-controls-start-playback-button {
    opacity: 0;
    pointer-events: none;
    width: 5px;
}
"""


def resource_path(rel):
    """PyInstaller 解包目录或源码目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def find_font_dir(game_root):
    for sub in ("www/fonts", "fonts"):
        p = os.path.join(game_root, sub.replace("/", os.sep))
        if os.path.isdir(p):
            return p
    return None


def find_css(game_root):
    fd = find_font_dir(game_root)
    if not fd:
        return None
    for name in ("gamefont.css", "GameFont.css"):
        p = os.path.join(fd, name)
        if os.path.exists(p):
            return p
    return None


def backup(path):
    b = path + ".bak"
    if os.path.exists(path) and not os.path.exists(b):
        shutil.copy2(path, b)
    return b


def install(game_root, verbose=True):
    """把中文字体装进游戏。返回 (成功, 说明)。"""
    if not game_root or not os.path.isdir(game_root):
        return False, "游戏目录不存在"

    fd = find_font_dir(game_root)
    if not fd:
        return False, "没找到 fonts 目录（这大概是 RPG Maker MV/MZ 游戏吗？）"

    asset = resource_path(os.path.join("assets", FONT_NAME))
    if not os.path.exists(asset):
        return False, "内置字体缺失，无法安装"

    dst = os.path.join(fd, FONT_NAME)
    try:
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copy2(asset, dst)
    except Exception as e:
        return False, "复制字体失败：%s" % e

    css = find_css(game_root)
    if css:
        backup(css)
        try:
            with open(css, "w", encoding="utf-8", newline="\n") as f:
                f.write(CSS_TMPL % FONT_NAME)
        except Exception as e:
            return False, "改写 gamefont.css 失败：%s" % e
        if verbose:
            print("   字体已装: %s" % dst)
            print("   css 已改: %s（备份 %s.bak）" % (css, os.path.basename(css)))
    else:
        # 没有 css 就自己造一个，并确保 index.html 引用它
        css = os.path.join(fd, "gamefont.css")
        with open(css, "w", encoding="utf-8", newline="\n") as f:
            f.write(CSS_TMPL % FONT_NAME)
        idx = os.path.join(game_root, "www", "index.html")
        if os.path.exists(idx):
            html = open(idx, encoding="utf-8").read()
            if "gamefont.css" not in html:
                html = html.replace("</head>",
                                    '<link rel="stylesheet" type="text/css" href="fonts/gamefont.css">\n</head>')
                backup(idx)
                open(idx, "w", encoding="utf-8", newline="\n").write(html)
        if verbose:
            print("   字体已装: %s（新建了 gamefont.css）" % dst)

    return True, dst


def revert(game_root):
    """还原成原版字体。"""
    css = find_css(game_root)
    if css and os.path.exists(css + ".bak"):
        shutil.copy2(css + ".bak", css)
        os.remove(css + ".bak")
    fd = find_font_dir(game_root)
    if fd:
        p = os.path.join(fd, FONT_NAME)
        if os.path.exists(p):
            os.remove(p)
    return True


if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else input("游戏根目录: ").strip('" ')
    print(install(root))
