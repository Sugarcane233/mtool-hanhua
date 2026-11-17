# mtool 汉化器

把 mtool 导出的 `ManualTransFile.json` 自动翻成简体中文，输出可直接被 mtool 重新导入的 JSON，并顺手把游戏字体换成能显示中文的字体。

适用于 RPG Maker MV / MZ 等能用 mtool 导出文本的游戏。

## 为什么不能「直接翻译每条文本」

mtool 是**按精确 key 在游戏数据里查表替换**的。key 有两种形态：

- 单行 key：一条对话就是一行。
- **多行 key**：连续几行对话用真实换行 `\n` 拼成的一整段。

如果按「事件指令」或「段落」切分翻译，大量 key 会对不上，导入后大部分文本还是原文。

RPG Maker 对话是**逐行渲染、逐行配音**的，所以译文必须**严格一行对一行**：少一行或多一行，后面的排版和语音全部错位。

所以本工具的做法是：

```
以 key 为单位 → 按 \n 拆成行 → 全局去重 → 翻译 → 按原顺序拼回原 key
```

这样单行 key 和多行 key 同时覆盖，行数天然对齐。

## 哪些翻，哪些绝不翻

只看文本形态容易误判：`Sword`、`Fighting`、`Miu` 既是英文单词也是技能名/人名，而 `chara09b`、`Outside_B` 是图片名。所以优先用**游戏数据本身**当权威判据：

- 遍历 `www/data/*.json`，按白名单字段提取玩家真正能看到的文本：
  - 事件指令 code `401/405/102/402/105/108/408` 的 parameters
  - Actors 的 name/nickname/profile
  - Items/Skills/Weapons/Armors/States 的 name/description/message1..4
  - System.json 的 gameTitle/currencyUnit/terms.*
  - MapInfos.json 的 name
- 命中白名单 → 一定翻。
- 没命中 → 再按形态判：代码（GLSL/JS/正则）、文件路径、图片名、插件参数、纯数字、十六进制色、单 token 标识符 → 原样保留。
- 含假名 / 含汉字 / ≥2 个英文单词 → 翻。

实测在一个 13,390 条的项目上：需翻译 8,343 条、保留 5,047 条，「该保留却翻了」和「该翻却保留了」都是 0 条。

## 防拒译

成人向游戏最怕模型中途开始「这段内容不适合」然后把整行改成省略号或者干脆不返回。做法是：

- system prompt 明确身份是长期承接成人向作品的本地化译者；
- 要求「如实、完整、直白，不弱化不省略不回避，不加说明或警告」；
- 批量带上下文翻译（每块 25 行），而不是一句一句问——单句、极短、极露骨的句子单独反复请求，最容易触发上游内容过滤。

实测 252 次调用 0 失败，露骨内容按原意直译。

## 模型不听话的地方用规则兜

模型偶发三种毛病，全都不回头问模型，直接用确定性规则修：

| 毛病 | 修法 |
|---|---|
| 单行译文里塞了个 `\n` | 压平（多一个换行就整段错位） |
| `♥` `♪` 数量少了或多了 | 多的删掉，缺的按原文行位置补回 |
| 行首凭空加了说话人名 | 剥掉（说话人由游戏名字框单独显示） |

早期版本把这些当硬校验、不通过就单行重试，结果反复请求短露骨句，直接触发上游 `content_filter` 把整轮对话终止。**结论：永远不要对失败行做单行重试。**

## 字体

RPG Maker MV 自带的 `mplus-1m-regular-80.ttf` 只有 7,508 个码位，**缺 400 多个简体字**（这、说、边等），中文直接显示空白。`GameFont-zh.ttf` 是从 Noto Sans SC 实例化 + 子集化的字体，覆盖 GB2312 全集 + ASCII + 假名 + `♥♪` 等符号，2.4 MB。

工具会把字体复制进 `www/fonts/` 并改写 `gamefont.css`（原文件留 `.bak` 备份）。

## 用法

```bash
# 直接翻译
mtool汉化器.exe <游戏目录>\ManualTransFile.json

# 指定输出 / 并发
mtool汉化器.exe input.json -o output.json --workers 8 --batch 25

# 只统计不调模型
mtool汉化器.exe input.json --dry-run

# 不改字体、不写自动加载
mtool汉化器.exe input.json --no-font --no-autoload
```

把文件拖到 exe 上也能跑；不带参数会提示输入路径。

跑完会：

1. 输出 `*.zhCN.json`（4 空格缩进，可直接导入 mtool）；
2. 输出 `*.report.txt` 质量报告（条目数、行数、API 调用、空译文、中英混排、未译条目）；
3. 给游戏装上中文字体；
4. 写 `lastLoadedTrsFile`，让 mtool 启动时自动加载这份译文。

之后用游戏目录里的「与工具一同启动.bat」启动即可。

## 断点续跑

缓存按**单行内容的 sha1** 存（`tr_cache.json`），中断重跑不会重复花钱。同一原文出现在多处时取出现次数最多的译法，保证前后一致。

## 模型配置

默认指向本机 WorkBuddy 桥：

```
--endpoint http://127.0.0.1:8790/v1/chat/completions
--model    deepseek-v4.1-flash
--token-file ~/.workbuddy-bridge/bridge-token
```

任何 OpenAI 兼容接口都能用：

```bash
mtool汉化器.exe input.json --endpoint https://api.openai.com/v1/chat/completions \
  --model gpt-4o-mini --api-key sk-xxx
```

也认环境变量 `WORKBUDDY_TOKEN` / `OPENAI_API_KEY`。

## 人名统一

从 `Actors.json` 取人名（游戏名字框显示的就是这个字段），先让模型统一译名，再带词表翻正文，保证同一个人名全文一致。也可以用 `--glossary names.txt` 自己给词表（每行 `原文=译名`）。

## 自己打包

```bash
python -m pip install pyinstaller fonttools brotli
python build_asset_font.py    # 生成 assets/GameFont-zh.ttf（需要系统里有 Noto Sans SC）
python build_exe.py           # 生成 dist/mtool汉化器.exe
```

## 目录

```
mtool_translator.py    主程序（分类 / 拆行 / 并发翻译 / 拼回 / 装字体）
fontfix.py             单独给游戏换字体
build_exe.py           PyInstaller 打包脚本
build_asset_font.py    生成内嵌中文字体
assets/GameFont-zh.ttf 子集化中文字体（GB2312 + 符号）
```

## 已知边界

- 只处理 mtool 导出格式（`key: value` 且 key 是原文）。Ren'Py/其他引擎的 mtool 导出字段不同，需要另写提取器。
- 多行 key 的第一行如果是角色名（如 `ルース\n（Go in here?）`），人名首行会保留——因为剥掉会导致行数错位。
- 纯日文的作者署名、插件注释会被当成可译文本翻掉，无副作用（玩家看不到）。
