# -*- coding: utf-8 -*-
"""把汉化器打包成单文件 exe。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile",
    "--console",
    "--name", "mtool汉化器",
    # 有些机器/安全软件禁止在 %TEMP% 里解包并执行 DLL，
    # 改成解包到 exe 同目录下的 _runtime，避免「Could not create temporary directory」。
    "--runtime-tmpdir", ".",
    "--add-data", "%s;assets" % ASSETS,
    "--hidden-import", "concurrent.futures",
    os.path.join(HERE, "mtool_translator.py"),
]
print(" ".join(cmd))
r = subprocess.run(cmd, cwd=HERE)
sys.exit(r.returncode)
