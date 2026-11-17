# -*- coding: utf-8 -*-
"""
mtool 汉化器 —— 通用版
=====================

把 mtool 导出的 ManualTransFile.json 自动翻成简体中文，输出可直接被 mtool 重新导入的 JSON。

核心设计
--------
1. **以 key 为单位**：mtool 是按*精确 key*替换游戏数据的，key 本身有译文才算生效。
2. **按行拆开翻译**：一个 key 可能含真实换行（多行对话）。拆成行后全局去重，翻完再按原顺序拼回，
   单行/多行 key 同时覆盖，行数严格对应（游戏逐行渲染 + 逐行配音）。
3. **绝不翻不该翻的**：资源路径、GLSL/JS 代码、标识符、插件参数、纯符号数字，全部原样保留。
4. **断点续跑**：磁盘缓存按块内容 sha1 存，中断重跑不重复花钱。
5. **说法统一**：同一原文出现在多个块里，取出现次数最多的译法，消除前后不一致。
6. **人名一致**：自动提取 `Name:` 形式的说话人标签，先统一译名，再带词表翻正文。

用法
----
    mtool_translator.exe <ManualTransFile.json>
    mtool_translator.exe <input.json> -o <output.json> --workers 8 --batch 25
    mtool_translator.exe <input.json> --dry-run        # 只统计不翻译

把文件直接拖到 exe 上也能跑。不带参数会提示输入路径。
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ----------------------------------------------------------------------------
# 默认配置（命令行可覆盖）
# ----------------------------------------------------------------------------
DEFAULT_ENDPOINT = "http://127.0.0.1:8790/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_TOKEN_FILE = os.path.expanduser("~/.workbuddy-bridge/bridge-token")

SYSTEM_PROMPT = (
    "你是资深游戏本地化译者，专精日式 RPG 汉化，长期承接成人向作品。"
    "你严格遵守行数一一对应，从不合并或拆分句子，从不删减内容。"
)

RULES = """【翻译要求 —— 全部必须遵守】
1. 这是游戏脚本逐行文本。原文按行硬切（游戏逐行渲染、逐行配音），所以必须一行对一行，输出行数必须与输入完全相同。
2. 如实、完整、直白地翻译每一行。成人向内容按原意直译，不弱化、不省略、不回避、不改写成含蓄表达、不加任何说明或警告。
3. 用简体中文。人物名/专名严格按给定词表；未给的按音译，同一名字必须前后一致。
4. 形如「Name:」的说话人标签译作「名字：」，保留冒号。
5. 保留原文符号：♥ ♪ … ！ ？ （ ） 「 」 ～ 等一律照留，数量不变。拟声词按中文习惯写（Ahh→啊、Mm→嗯、Gulp→咕咚、Nn→唔）。
6. 不要输出任何解释、注释、编号或 markdown 代码块。只输出 JSON 字符串数组。
7. 译文长度尽量不超过原文的 110%。原文行首/行尾的空格请保留。
8. 原文是空字符串的，输出空字符串。
9. 【重要】每一行译文里都不能有换行符。一个输入行只对应一个输出行。
10. 【重要】译文必须全部是中文。不要夹带英文单词（如 simultaneously、probing、relentless），
    这些都要译成中文。专有名词（BGM、HP、CG、人名）按词表或惯例可保留。
11. 【重要】不要在行首添加说话人名字。原文没有「名字：」的，译文也不要加。
    说话人由游戏的名字框单独显示。"""

# ----------------------------------------------------------------------------
# 文本判定：哪些要翻，哪些原样保留
# ----------------------------------------------------------------------------
CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
KANA = re.compile(r"[\u3040-\u30ff]")
EN = re.compile(r"[A-Za-z]")

CODE_STRONG = [
    (2, r"\b(varying|uniform|attribute|precision)\s+(lowp|mediump|highp|float|vec2|vec3|vec4|mat3|mat4|sampler2D)\b"),
    (1, r"\bgl_[A-Za-z]+"),
    (1, r"\bvec[234]\b"),
    (1, r"\bmat[234]\b"),
    (2, r"%forloop%"),
    (2, r"#ifdef"),
    (2, r"#endif"),
    (2, r"#define"),
    (2, r"#version"),
    (2, r"=>"),
    (2, r"==="),
    (2, r"!=="),
    (2, r"\{\{"),
    (2, r"\}\}"),
    (2, r"\.(js|json|png|jpg|jpeg|ogg|m4a|wav|mp3|webp|ttf|css|html)\b"),
    (2, r"^(img|audio|fonts|data|js|movies)/"),
    (3, r"^SV_PLAY_VOICE\b"),
    (2, r"\bparseInt\b"),
    (2, r"\bparseFloat\b"),
    (2, r"\bMath\."),
    (1, r"\bundefined\b"),
    (2, r"\bprototype\b"),
    (2, r"\bthis\.[A-Za-z_$]"),
    (2, r"\bwindow\.[A-Za-z_$]"),
    (2, r"\bdocument\.[A-Za-z_$]"),
    # 插件命令：全大写词组 + 空格 + 参数（MP_SET_MOVIE 02_J / P_CALL_CE 20 160）
    (3, r"^[A-Z][A-Z0-9]*(_[A-Z0-9]*){2,}(\s|$)"),
    (3, r"^[A-Z][A-Z0-9]*_[A-Z0-9]+\s"),
    # JS 赋值/表达式
    (2, r"[A-Za-z_$\]\)]\s*[+\-*/]?=\s*[^=]"),
    (2, r"\bfunction\s*[A-Za-z_$]*\s*\("),
]
CODE_WEAK = [
    (1, r"[;{}]\s*$"),
    (1, r"^\s*(var|let|const)\s+[A-Za-z_$]"),
    (1, r"^\s*return\s+[A-Za-z_$.]"),
    (1, r"^\s*(if|for|while)\s*\("),
    (1, r"^\s*new\s+[A-Z]"),
    (1, r"//"),
    (1, r"/\*"),
    (1, r"\*/"),
]
STRONG_RE = [(w, re.compile(p)) for w, p in CODE_STRONG]
WEAK_RE = [(w, re.compile(p)) for w, p in CODE_WEAK]

# 只有这些「全大写标识符 + 参数」和「纯装饰线」单独成立就判代码
DECISIVE = [
    re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]*){1,}(\s|$)"),
    re.compile(r"^[\u30fc\u2500-\u257f=―─ー]{3,}$"),
    re.compile(r"^-?\d+(\.\d+)?$"),
    re.compile(r"^#[0-9a-fA-F]{3,8}$"),
    re.compile(r"^[\d\s,.\-+*/()=<>!&|%^~\[\]]+$"),
]


def looks_like_code(s):
    for r in DECISIVE:
        if r.search(s):
            return True
    score = 0
    for w, r in STRONG_RE:
        if r.search(s):
            score += w
    for w, r in WEAK_RE:
        if r.search(s):
            score += w
    return score >= 2

FLAGS = {"off", "on", "true", "false", "auto", "none", "null", ""}


def classify(key, value, visible=None):
    """判定一条 key 该翻还是该留。返回 ('trans'|'keep', reason)。

    visible: 可选，从游戏数据里精确提取的「玩家可见文本」集合（strip 后比对）。
             给了它就当权威：命中必翻；单 token 命中也要翻（数据库里的技能名/人名
             常是短单词，光看文本形态会被误判成资源名）。
    """
    if key.startswith("MTool_Meta"):
        return "keep", "meta"
    if key != value:
        return "keep", "key!=value"
    s = key.strip()
    if s.lower() in FLAGS:
        return "keep", "flag"

    hint = False
    if visible is not None:
        if s in visible:
            hint = True
        elif "\n" in key and any(p.strip() in visible for p in key.split("\n")):
            hint = True
    if hint:
        return "trans", "visible"
    if looks_like_code(s):
        return "keep", "code"

    # 库/框架的诊断字符串：翻译会破坏调用方匹配，也从不显示给玩家
    if any(t in s for t in ("deprecated", "has been renamed", "has been replaced",
                            "please use", "PIXI.", "is not available")):
        return "keep", "lib_comment"
    # 占位符模板 %name%
    if re.fullmatch(r"[^%]*%[A-Za-z_]+%[^%]*", s):
        return "keep", "template"

    has_cjk = bool(CJK.search(s))
    has_kana = bool(KANA.search(s))
    has_en = bool(EN.search(s))

    # 无空格且含斜杠 → 一定是路径（含日文名的语音目录也算），即使带假名
    if " " not in s and "\t" not in s and re.search(r"[/\\]", s):
        return "keep", "path"
    if not has_cjk and " " not in s:
        if re.match(r"^[\w.-]+\.[A-Za-z]{2,5}$", s):
            return "keep", "filename"

    if " " not in s and "\t" not in s and not has_cjk:
        if re.match(r"^[A-Za-z_$][A-Za-z0-9_$.]*$", s):
            if re.match(r"^[A-Z][A-Z0-9_]*$", s):
                return "keep", "ident"
            if re.search(r"[a-z][A-Z]", s) or "_" in s or "." in s:
                return "keep", "ident"
            if len(s) <= 6:
                return "keep", "ident"
            return "keep", "single_word"

    if not has_en and not has_cjk:
        return "keep", "symbol"

    # 说话人标签，如 "Ruth:" / "Mysterious Voice:" —— 需要翻（译名靠词表统一）
    if re.match(r"^[A-Z][A-Za-z .'\-]{0,23}:\s*$", s):
        return "trans", "speaker"

    # 短资源码：09_D / 11_E / chara09b / * c.r 之类
    if not has_cjk and len(s) <= 12 and " " not in s and re.search(r"\d", s) \
            and not re.search(r"[a-z]{3,}", s):
        return "keep", "asset_code"
    if not has_cjk and len(s) <= 6 and re.match(r"^[*#@]", s):
        return "keep", "asset_code"

    if has_kana:
        return "trans", "japanese"
    if has_cjk and not has_en:
        return "trans", "cjk"

    words = re.findall(r"[A-Za-z][A-Za-z']*", s)
    if len(words) >= 2:
        return "trans", "english"
    if len(words) == 1:
        w = words[0]
        # 单个词但带句末标点/感叹/爱心 → 是台词或拟声词
        if re.search(r"[,.!?…♥♪～~、。「」（）]", s) or " " in s:
            return "trans", "english"
        if len(s) > len(w) + 1:
            return "trans", "english"
    return "keep", "other"


# ----------------------------------------------------------------------------
# 模型调用
# ----------------------------------------------------------------------------
class Client:
    def __init__(self, endpoint, model, token_file, timeout=300, api_key=""):
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.token = ""
        # 优先级：命令行 > 环境变量 > 令牌文件
        for cand in (api_key, os.environ.get("WORKBUDDY_TOKEN", ""),
                     os.environ.get("OPENAI_API_KEY", "")):
            if cand and cand.strip():
                self.token = cand.strip()
                break
        if not self.token:
            for p in (token_file, DEFAULT_TOKEN_FILE):
                if not p:
                    continue
                try:
                    t = open(p, encoding="utf-8").read().strip()
                    if t:
                        self.token = t
                        break
                except Exception:
                    continue

    def chat(self, messages, temperature=0.3, max_tokens=8000):
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            obj = json.loads(r.read().decode("utf-8"))
        return obj["choices"][0]["message"]["content"]


def parse_array(text, n):
    """从模型返回里抠出一个字符串数组。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        seg = t[i:j + 1]
        for cand in (seg, seg.replace("\n", "\\n")):
            try:
                v = json.loads(cand)
            except Exception:
                continue
            if isinstance(v, list):
                out = []
                for x in v:
                    if isinstance(x, str):
                        out.append(x)
                    elif isinstance(x, (int, float)):
                        out.append(str(x))
                    elif isinstance(x, dict):
                        for kk in ("translation", "text", "zh", "value", "cn"):
                            if isinstance(x.get(kk), str):
                                out.append(x[kk])
                                break
                        else:
                            out.append("")
                    else:
                        out.append("")
                return out
    lines = []
    for ln in t.split("\n"):
        m = re.match(r"^\s*\d+[.、)]\s*(.*)$", ln)
        if m:
            s = m.group(1).strip().strip('",')
            if s:
                lines.append(s)
    return lines or None


# ----------------------------------------------------------------------------
# 翻译引擎
# ----------------------------------------------------------------------------
class Translator:
    def __init__(self, client, cache_path, glossary=None, workers=8, batch=25, verbose=True):
        self.client = client
        self.cache_path = cache_path
        self.workers = workers
        self.batch = batch
        self.verbose = verbose
        self.glossary = glossary or {}
        self.lock = threading.Lock()
        self.cache = {}
        self.stats = {"calls": 0, "failed_blocks": 0, "failed_lines": 0}
        self._load()

    def _load(self):
        if os.path.exists(self.cache_path):
            try:
                self.cache = json.load(open(self.cache_path, encoding="utf-8"))
            except Exception:
                self.cache = {}

    def save(self):
        with self.lock:
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(self.cache, f, ensure_ascii=False)
            os.replace(tmp, self.cache_path)

    @staticmethod
    def block_key(lines):
        h = hashlib.sha1()
        for s in lines:
            h.update(s.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def _glossary_text(self):
        if not self.glossary:
            return ""
        items = "、".join("%s=%s" % (k, v) for k, v in list(self.glossary.items())[:200])
        return "\n【固定译名】" + items

    def _prompt(self, lines):
        budget = max(12, int(max(len(s) for s in lines) * 1.15))
        numbered = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(lines))
        user = (
            "%s\n下面是 %d 行游戏脚本，请翻译成简体中文。\n"
            "只输出一个 JSON 字符串数组，元素个数必须正好 %d，顺序与输入一一对应，不要编号、不要解释。\n"
            "单行译文建议不超过 %d 个汉字。%s\n\n%s"
            % (RULES, len(lines), len(lines), budget, self._glossary_text(), numbered)
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _try_block(self, lines, attempt=0):
        try:
            txt = self.client.chat(self._prompt(lines), temperature=0.25 + 0.2 * attempt)
            with self.lock:
                self.stats["calls"] += 1
        except Exception as e:
            if self.verbose:
                print("      ! 调用失败 %s: %s" % (type(e).__name__, str(e)[:70]), flush=True)
            return None
        arr = parse_array(txt, len(lines))
        if not arr or len(arr) != len(lines):
            return None
        arr = [str(x) for x in arr]
        arr = [self._clean(x) for x in arr]
        for a, o in zip(arr, lines):
            if not a.strip() and o.strip():
                return None
        return arr

    @staticmethod
    def _clean(t):
        """一行译文里绝不能带换行——游戏逐行渲染，多一个 \\n 就整段错位。"""
        if "\n" in t or "\r" in t:
            t = re.sub(r"[\r\n]+", "", t)
        return t

    def translate_lines(self, lines):
        """翻一批行；失败就二分降级，最差逐行。返回 list 或 None。"""
        key = self.block_key(lines)
        if key in self.cache:
            return self.cache[key]
        for attempt in range(3):
            r = self._try_block(lines, attempt)
            if r:
                with self.lock:
                    self.cache[key] = r
                return r
        if len(lines) > 1:
            mid = len(lines) // 2
            a = self.translate_lines(lines[:mid])
            b = self.translate_lines(lines[mid:])
            if a is not None and b is not None:
                out = a + b
                with self.lock:
                    self.cache[key] = out
                return out
            return None
        with self.lock:
            self.stats["failed_lines"] += 1
            self.stats["failed_blocks"] += 1
        return None

    def run(self, lines, progress_every=20):
        # 先吃掉已有的逐行缓存（单行 key），能省大量调用
        line_tr = {}
        for s in lines:
            k = self.block_key([s])
            if k in self.cache:
                t = self.cache[k][0]
                if t and t.strip():
                    line_tr[s] = t
        todo = [s for s in lines if s not in line_tr]
        print("逐行缓存命中 %d，仍需翻译 %d 行" % (len(line_tr), len(todo)), flush=True)
        if not todo:
            return line_tr

        blocks = [todo[i:i + self.batch] for i in range(0, len(todo), self.batch)]
        total = len(blocks)
        print("分 %d 块（每块最多 %d 行，并发 %d）" % (total, self.batch, self.workers), flush=True)

        pair_counts = collections.defaultdict(collections.Counter)  # 原行 -> {译文: 次数}
        ok_blocks = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = {ex.submit(self.translate_lines, b): b for b in blocks}
            for n, fut in enumerate(as_completed(futs), 1):
                b = futs[fut]
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    ok_blocks += 1
                    for s, t in zip(b, r):
                        if t and t.strip():
                            pair_counts[s][t] += 1
                if self.verbose and (n % progress_every == 0 or n == total):
                    el = time.time() - t0
                    print("  进度 %d/%d  成功块 %d  失败行 %d  用时 %.0fs"
                          % (n, total, ok_blocks, self.stats["failed_lines"], el), flush=True)
                if n % 40 == 0:
                    self.save()
        self.save()

        conflicts = 0
        for s, c in pair_counts.items():
            top = c.most_common(1)[0][0]
            line_tr[s] = top
            if len(c) > 1:
                conflicts += 1
        if conflicts:
            print("  同原文多译法已统一: %d 行" % conflicts, flush=True)
        return line_tr


# ----------------------------------------------------------------------------
# 说话人 / 专名
# ----------------------------------------------------------------------------
SPEAKER_RE = re.compile(r"^([A-Z][A-Za-z .'\-]{1,24}):\s*$")
SPEAKER_INLINE_RE = re.compile(r"^([A-Z][A-Za-z .'\-]{1,24}):\s+\S")
# 译文里可能被模型硬加的人名前缀，如「Ruone 这…」「Lisa：」——判断正文有没有被污染
SPEAKER_PREFIX_RE = re.compile(r"^[A-Z][A-Za-z]{2,14}[\s：]")


def find_speakers(lines, min_count=2):
    c = collections.Counter()
    for s in lines:
        t = s.rstrip()
        m = SPEAKER_RE.match(t) or SPEAKER_INLINE_RE.match(t)
        if m:
            c[m.group(1).strip()] += 1
    return [n for n, k in c.most_common() if k >= min_count]


def names_from_actors(game_root):
    """从 Actors.json 取角色名（游戏里名字框显示的就是这个字段）。"""
    out = []
    if not game_root:
        return out
    for sub in ("www/data", "data"):
        p = os.path.join(game_root, sub, "Actors.json")
        if not os.path.exists(p):
            continue
        try:
            d = json.loads(open(p, "rb").read().decode("utf-8-sig"))
        except Exception:
            return out
        for row in d if isinstance(d, list) else []:
            if isinstance(row, dict):
                n = row.get("name")
                if isinstance(n, str) and n.strip() and n.strip() not in out:
                    out.append(n.strip())
        break
    return out


def build_glossary(client, names, verbose=True):
    if not names:
        return {}
    if verbose:
        print("提取到说话人/专名 %d 个，正在统一译名..." % len(names), flush=True)
    numbered = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(names))
    user = (
        "下面是游戏角色名/专名列表，请给出统一的简体中文译名（音译优先，符合日式奇幻风格）。\n"
        "只输出 JSON 字符串数组，元素个数必须正好 %d。\n\n%s" % (len(names), numbered)
    )
    try:
        txt = client.chat([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ], temperature=0.2)
        arr = parse_array(txt, len(names))
        if arr and len(arr) == len(names):
            g = {}
            for a, b in zip(names, arr):
                b = b.strip().strip("：:")
                if b:
                    g[a] = b
            if verbose:
                print("  译名表: " + "、".join("%s=%s" % (k, v) for k, v in list(g.items())[:24]), flush=True)
            return g
    except Exception as e:
        if verbose:
            print("  译名统一失败（继续）:", str(e)[:80], flush=True)
    return {}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def find_game_root(path):
    """从 mtool 文件位置往上找游戏根目录（含 www/data 或 data 的目录）。"""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(4):
        for cand in (os.path.join(d, "www", "data"), os.path.join(d, "data")):
            if os.path.isdir(cand) and glob.glob(os.path.join(cand, "System.json")):
                return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def build_visible(game_root):
    """从游戏数据里精确提取玩家可见文本（白名单字段）。找不到就返回 None。"""
    if not game_root:
        return None
    if os.path.basename(game_root.rstrip("\\/")).lower() == "data":
        data_dir = game_root
    elif os.path.isdir(os.path.join(game_root, "www", "data")):
        data_dir = os.path.join(game_root, "www", "data")
    elif os.path.isdir(os.path.join(game_root, "data")):
        data_dir = os.path.join(game_root, "data")
    else:
        return None

    TALK_CODES = {401, 405, 102, 402, 105, 108, 408}
    DB_VIEW = {
        "Actors.json": ("name", "nickname", "profile"),
        "Items.json": ("name", "description"),
        "Skills.json": ("name", "description", "message1", "message2"),
        "Weapons.json": ("name", "description"),
        "Armors.json": ("name", "description"),
        "States.json": ("name", "description", "message1", "message2", "message3", "message4"),
        "Classes.json": ("name",),
        "Enemies.json": ("name",),
        "Troops.json": ("name",),
    }

    def walk_cmds(cmds, out):
        if not isinstance(cmds, list):
            return
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            if cmd.get("code") in TALK_CODES and isinstance(cmd.get("parameters"), list):
                for p in cmd["parameters"]:
                    if isinstance(p, str) and p.strip():
                        out.add(p)
                    elif isinstance(p, list):
                        for q in p:
                            if isinstance(q, str) and q.strip():
                                out.add(q)
            ps = cmd.get("parameters")
            if isinstance(ps, list):
                for p in ps:
                    if isinstance(p, dict):
                        walk_cmds(p.get("list"), out)
                    elif isinstance(p, list) and p and isinstance(p[0], dict):
                        walk_cmds(p, out)

    def walk_any(node, out):
        if isinstance(node, list):
            for x in node:
                walk_any(x, out)
        elif isinstance(node, dict):
            if "list" in node:
                walk_cmds(node["list"], out)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk_any(v, out)

    seen = set()
    for f in sorted(glob.glob(os.path.join(data_dir, "*.json"))):
        base = os.path.basename(f)
        try:
            d = json.loads(open(f, "rb").read().decode("utf-8-sig"))
        except Exception:
            continue
        if base.startswith("Map") or base == "CommonEvents.json":
            walk_any(d, seen)
        if base in DB_VIEW and isinstance(d, list):
            for row in d:
                if isinstance(row, dict):
                    for k in DB_VIEW[base]:
                        v = row.get(k)
                        if isinstance(v, str) and v.strip():
                            seen.add(v)
        if base == "System.json" and isinstance(d, dict):
            for k in ("gameTitle", "currencyUnit"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    seen.add(v)
            for grp, val in (d.get("terms") or {}).items():
                if isinstance(val, dict):
                    seen.update(v for v in val.values() if isinstance(v, str) and v.strip())
                elif isinstance(val, list):
                    seen.update(v for v in val if isinstance(v, str) and v.strip())
        if base == "MapInfos.json" and isinstance(d, list):
            for row in d:
                if isinstance(row, dict):
                    v = row.get("name")
                    if isinstance(v, str) and v.strip():
                        seen.add(v)
    return {s.strip() for s in seen} or None


SPK = re.compile(r"^(?:[A-Z][A-Za-z]{2,14}|[\u4e00-\u9fff]{2,6})[\s：:]")


def polish(src, dst, zh_names=()):
    """确定性修补，不回头请求模型。

    修三件事（都是模型偶发、且能靠规则修好的）：
      * 译文里多出来的换行 → 压平（游戏逐行渲染，多一个 \\n 就整段错位）
      * ♥/♪ 数量与原文不一致 → 多的删、缺的补
      * 行首被塞了说话人名 → 剥掉（说话人由游戏名字框单独显示）
    返回 (修好的译文, 修过的项目名列表)。
    """
    tags = []
    o = dst

    if o.count("\n") > src.count("\n"):
        o = re.sub(r"[\r\n]+", "", o)
        tags.append("换行")

    for ch in "♥♪":
        want, have = src.count(ch), o.count(ch)
        if want == have:
            continue
        if have > want:
            parts = o.split(ch)
            o = ch.join(parts[:want + 1]) + "".join(parts[want + 1:])
            tags.append("符号")
            continue
        sl, dl = src.split("\n"), o.split("\n")
        for i, line in enumerate(sl):
            if i >= len(dl):
                break
            need = line.count(ch) - dl[i].count(ch)
            if need > 0:
                dl[i] = dl[i].rstrip() + ch * need
        o = "\n".join(dl)
        tags.append("符号")

    if not SPK.match(src.strip()) and "\n" not in src:
        # 只在单行 key 上剥名字前缀：多行 key 的第一行本来就可能是角色名，
        # 剥掉会导致行数错位、整段对话排版和配音全乱。
        stripped = None
        for n in zh_names:
            if o.startswith(n):
                rest = re.sub(r"^[\s:：]+", "", o[len(n):])
                if rest.strip():
                    stripped = rest
                    break
        if stripped is None:
            m = re.match(r"^([A-Z][A-Za-z]{2,14})[\s：:]+(.+)$", o, re.S)
            if m and not src.startswith(m.group(1)):
                stripped = m.group(2)
        if stripped is not None:
            o = stripped
            tags.append("名字")

    return o, tags


FONT_NAME = "GameFont-zh.ttf"

FONT_CSS = """@font-face {
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
    """打包成 exe 后的资源目录，或源码运行时的当前目录。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def install_font(game_root, verbose=True):
    """给 RPG Maker MV/MZ 游戏换上内置中文字体（原字体缺简体字，会显示空白）。"""
    if not game_root or not os.path.isdir(game_root):
        return False, "没有游戏目录"
    fdir = None
    for sub in (("www", "fonts"), ("fonts",)):
        p = os.path.join(game_root, *sub)
        if os.path.isdir(p):
            fdir = p
            break
    if not fdir:
        return False, "没找到 fonts 目录"

    asset = resource_path(os.path.join("assets", FONT_NAME))
    if not os.path.exists(asset):
        return False, "内置字体缺失"

    dst = os.path.join(fdir, FONT_NAME)
    try:
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copy2(asset, dst)
    except Exception as e:
        return False, "复制字体失败：%s" % e

    css = None
    for name in ("gamefont.css", "GameFont.css"):
        p = os.path.join(fdir, name)
        if os.path.exists(p):
            css = p
            break
    if css is None:
        css = os.path.join(fdir, "gamefont.css")
    else:
        bak = css + ".bak"
        if not os.path.exists(bak):
            try:
                shutil.copy2(css, bak)
            except Exception:
                pass
    try:
        with open(css, "w", encoding="utf-8", newline="\n") as f:
            f.write(FONT_CSS % FONT_NAME)
    except Exception as e:
        return False, "改写 gamefont.css 失败：%s" % e

    if verbose:
        print("  字体已装: %s" % dst)
    return True, dst


def write_autoload(game_root, out_path, verbose=True):
    """写 lastLoadedTrsFile，mtool 启动时会自动加载这个译文文件。"""
    if not game_root:
        return False, "没有游戏目录"
    try:
        p = os.path.join(game_root, "lastLoadedTrsFile")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(os.path.basename(out_path))
        if verbose:
            print("  已设置自动加载: %s" % p)
        return True, p
    except Exception as e:
        return False, "写 lastLoadedTrsFile 失败：%s" % e


def load_json(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp932", "latin-1"):
        try:
            return json.loads(raw.decode(enc)), enc
        except Exception:
            continue
    raise SystemExit("无法解析这个 JSON 文件，编码不认识。")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="mtool 汉化器：把 ManualTransFile.json 自动翻成中文，输出可直接导入 mtool 的新 JSON。")
    ap.add_argument("input", nargs="?", help="mtool 导出的 ManualTransFile.json")
    ap.add_argument("-o", "--output", help="输出路径，默认在输入文件旁生成 *.zhCN.json")
    ap.add_argument("--cache", help="缓存路径，默认 <输出>.cache.json")
    ap.add_argument("--workers", type=int, default=8, help="并发数，默认 8")
    ap.add_argument("--batch", type=int, default=25, help="每块行数，默认 25")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="模型名")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OpenAI 兼容接口")
    ap.add_argument("--api-key", help="API Key（也可用环境变量 WORKBUDDY_TOKEN / OPENAI_API_KEY）")
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE, help="API Key 文件")
    ap.add_argument("--glossary", help="词表文件，每行 原文=译名")
    ap.add_argument("--game-dir", help="游戏根目录（含 www/data）；默认自动从输入文件位置向上查找")
    ap.add_argument("--no-visible", action="store_true", help="不读游戏数据，只用文本形态判定")
    ap.add_argument("--no-font", action="store_true", help="不改游戏字体（默认会装上内置中文字体）")
    ap.add_argument("--no-autoload", action="store_true", help="不写 lastLoadedTrsFile（默认写上，让 mtool 自动加载译文）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不调模型")
    ap.add_argument("--no-pause", action="store_true", help="跑完不等回车（脚本里调用时用）")
    ap.add_argument("--report", help="质量报告路径")
    args = ap.parse_args(argv)
    if args.no_pause:
        sys._NO_PAUSE = True

    path = args.input
    if not path:
        print("把 mtool 导出的 ManualTransFile.json 路径拖到这里，回车：")
        path = input().strip().strip('"').strip("'")
    if not os.path.exists(path):
        print("找不到文件:", path)
        return 1

    out_path = args.output or (os.path.splitext(path)[0] + ".zhCN.json")
    cache_path = args.cache or (out_path + ".cache.json")
    t0 = time.time()

    data, enc = load_json(path)
    print("载入 %s（%s 编码），条目总数 %d" % (os.path.basename(path), enc, len(data)))

    # ---------- 定位游戏数据，拿到「玩家可见文本」当权威判据 ----------
    visible = None
    root = args.game_dir or find_game_root(path)
    if not args.no_visible:
        if root:
            visible = build_visible(root)
        if visible:
            print("已定位游戏数据 %s，玩家可见文本 %d 条" % (root, len(visible)))
        else:
            print("未找到游戏数据，退回纯文本形态判定（准确率略低）")

    # ---------- 分类 ----------
    trans_keys, keep_n = [], 0
    reasons = collections.Counter()
    for k, v in data.items():
        d, why = classify(k, v, visible=visible)
        if d == "trans":
            trans_keys.append(k)
            reasons[why] += 1
        else:
            keep_n += 1
    print("需翻译 %d 条 / 原样保留 %d 条" % (len(trans_keys), keep_n))
    if reasons:
        print("  分类: " + "  ".join("%s=%d" % kv for kv in reasons.most_common()))

    # ---------- 拆行去重 ----------
    key_lines, ordered, seen = {}, [], set()
    for k in trans_keys:
        parts = k.split("\n")
        key_lines[k] = parts
        for p in parts:
            if p.strip() and p not in seen:
                seen.add(p)
                ordered.append(p)
    print("行槽位 %d，去重后需翻译 %d 行，共 %d 字符"
          % (sum(len(v) for v in key_lines.values()), len(ordered), sum(len(s) for s in ordered)))

    if args.dry_run:
        print("\n[dry-run] 不调用模型。前 30 行预览：")
        for s in ordered[:30]:
            print("   ", s[:100])
        return 0
    if not ordered:
        print("没有需要翻译的内容。")
        return 0

    # ---------- 词表 ----------
    glossary = {}
    if args.glossary and os.path.exists(args.glossary):
        for ln in open(args.glossary, encoding="utf-8"):
            if "=" in ln:
                a, b = ln.split("=", 1)
                if a.strip():
                    glossary[a.strip()] = b.strip()

    client = Client(args.endpoint, args.model, args.token_file, api_key=args.api_key or "")
    tr = Translator(client, cache_path, glossary=glossary, workers=args.workers, batch=args.batch)

    if not tr.glossary:
        names = names_from_actors(root if not args.no_visible else None) or find_speakers(ordered)
        tr.glossary = build_glossary(client, names)
        glossary = tr.glossary

    # ---------- 翻译 ----------
    line_tr = tr.run(ordered)

    # ---------- 拼回 ----------
    missing = 0
    out = {}
    zh_names = sorted(set(glossary.values()), key=len, reverse=True)
    fixed = collections.Counter()
    for k, v in data.items():
        if k not in key_lines:
            out[k] = v
            continue
        pieces = []
        for p in key_lines[k]:
            if not p.strip():
                pieces.append("")
            elif line_tr.get(p):
                pieces.append(line_tr[p])
            else:
                pieces.append(p)
                missing += 1
        t = "\n".join(pieces)
        # 说话人标签 → 中文冒号
        for en, zh in glossary.items():
            if t.startswith(en + ":"):
                t = zh + "：" + t[len(en) + 1:].lstrip()
                break
        # 确定性修补：模型偶尔会多塞换行 / 丢 ♥♪ / 在行首自造说话人
        t2 = polish(k, t, zh_names)
        for name_ in t2[1]:
            fixed[name_] += 1
        out[k] = t2[0]

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=4)
    if fixed:
        print("  已修补: " + "  ".join("%s=%d" % kv for kv in fixed.most_common()))

    # ---------- 报告 ----------
    rep = [
        "输入: %s" % path,
        "输出: %s" % out_path,
        "条目: %d（翻译 %d / 保留 %d）" % (len(data), len(trans_keys), keep_n),
        "唯一行: %d  字符: %d" % (len(ordered), sum(len(s) for s in ordered)),
        "未译回退原文的行槽: %d" % missing,
        "API 调用: %d  失败行: %d" % (tr.stats["calls"], tr.stats["failed_lines"]),
        "耗时: %.1f 秒" % (time.time() - t0),
    ]
    empties = [k for k in trans_keys if not str(out.get(k, "")).strip()]
    rep.append("\n空译文条目: %d" % len(empties))
    for k in empties[:20]:
        rep.append("   %r" % (k[:80],))

    mixed = [k for k in trans_keys
             if re.search(r"[A-Za-z]{3,}", str(out.get(k, ""))) and re.search(r"[\u4e00-\u9fff]", str(out.get(k, "")))]
    rep.append("\n中英混排嫌疑: %d" % len(mixed))
    for k in mixed[:25]:
        rep.append("   %r -> %r" % (k[:36], str(out.get(k))[:60]))

    same = [k for k in trans_keys if out.get(k) == k]
    rep.append("\n未译（译文与原文相同）: %d" % len(same))
    for k in same[:25]:
        rep.append("   %r" % (k[:70],))

    report = "\n".join(rep)
    rep_path = args.report or (out_path + ".report.txt")
    with open(rep_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(report + "\n")

    print("\n" + report)
    print("\n完成。\n  输出: %s\n  报告: %s" % (out_path, rep_path))

    # ---------- 收尾：装字体 + 让 mtool 自动加载 ----------
    if not args.no_font:
        ok, info = install_font(root)
        if not ok:
            print("  ! 字体没装上：%s（中文可能显示空白，可重新运行或手动换字体）" % info)
    if not args.no_autoload:
        ok, info = write_autoload(root, out_path)
        if not ok:
            print("  ! 自动加载没设置成功：%s（可在 mtool 里手动导入译文）" % info)

    print("\n下一步：用「与工具一同启动.bat」启动游戏，mtool 会自动载入这份译文。")
    return 0


def entry():
    """exe 入口：拖文件进来也能跑，出错不闪退。"""
    # Windows 控制台默认不是 UTF-8，中文会变乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = sys.argv[1:]
    try:
        rc = main(argv)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except KeyboardInterrupt:
        print("\n已中断。")
        rc = 130
    except Exception:
        import traceback
        print("\n出错了：")
        traceback.print_exc()
        rc = 1
    # 双击/拖拽运行时窗口会立刻关掉，看不到结果 —— 停一下
    if not getattr(sys, "_NO_PAUSE", False):
        try:
            input("\n按回车关闭…")
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(entry())
