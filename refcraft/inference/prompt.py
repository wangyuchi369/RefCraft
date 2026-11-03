"""Original bilingual environment brief; prompt wording is preserved."""

from pathlib import Path


def is_zh(lang):
    return str(lang or "").lower().startswith("zh")


def env_brief(
    *,
    workdir: Path,
    ref_mode: str,
    ref_names,
    max_turns: int,
    lang: str,
    artifact: str = "index.html",
) -> str:
    wd = str(workdir)
    zh = is_zh(lang)
    if ref_mode == "files":
        names = ", ".join(f"refs/{n}" for n in list(ref_names)[:12])
        more = " …" if len(list(ref_names)) > 12 else ""
        refs_zh = (
            f"\n参考截图也放在工作目录里了：{names}{more}。可以直接打开、放大、"
            f"取准确的像素颜色。\n"
        )
        refs_en = (
            f"\nThe reference screenshots are also in the working directory: {names}{more}. "
            f"You may open them, zoom in and sample exact pixel colours.\n"
        )
    else:
        # conv 臂：参考图**只在对话里**。壳会把附件的临时路径显示出来，模型据此以为盘上有文件，
        # 然后花轮数 cp/cat 那个路径、撞 external_directory:deny（实测 19 次被拦里 13 次是这个）。
        # 说清楚"没有那个文件"比让它自己撞墙省轮数，且不放松任何反作弊边界。
        refs_zh = (
            "\n参考截图**只存在于这段对话里**（上面的图片），盘上没有对应文件 ——\n"
            "对话里显示的那些临时路径读不到，不用去 cp/cat 它们，直接看图就行。\n"
        )
        refs_en = (
            "\nThe reference screenshots exist **only in this conversation** (the images above); "
            "there are no files for them on disk. The temporary paths shown in the chat are not "
            "readable, so do not try to cp/cat them — just look at the images.\n"
        )

    if zh:
        return (
            "\n\n---\n"
            "## 运行环境（这次不是聊天：你的答案是一个文件）\n\n"
            f"你现在是一个跑在真实 shell 里的 agent，工作目录 `{wd}`（已经是当前目录）。\n\n"
            f"**交付物**：把做好的页面写进这个目录下的 `{artifact}`。判分只看这个文件 ——\n"
            f'上面写的"只输出 HTML"在这里改成"写进 `{artifact}`"，**不要**把整页贴进对话。\n'
            f"其余要求不变：单个自包含文件、不引用任何外部资源（判分时页面是**离线**渲染的，\n"
            f"CDN 链接、远程字体、远程图片一律加载不到，会直接渲成空白或掉版）。\n\n"
            f"**看你自己的产物**：跑 `python3 shot.py` 就能给 `{artifact}` 截图"
            f"（1920 / 768 / 390 三个宽度）。\n"
            f"它会告诉你图存到哪了，外加页面高度、控制台报错条数、页面尝试请求了几次外部资源。\n"
            f"`python3 shot.py --widths 1920` 只截一个宽度，`--file 别的.html` 截别的文件。\n"
            f"把截图打开、跟参考图逐块对比、把不像的地方改掉 —— 轮数够你来回改好几遍。\n"
            f"⚠️ 中间文件（裁剪、拼图、临时脚本）**都要写在工作目录里面**，比如 `./crops/a.png`。\n"
            f"写到 `/tmp` 或其他目录的文件你自己**读不回来**（工具只允许访问工作目录）。\n\n"
            f"**预算**：这次会话一共 {max_turns} 步（一步 = 你的一轮）。建议这么花：\n"
            f"先写出初稿 → 截图 → 对比 → 改 → 再截图确认。\n"
            f"{refs_zh}\n"
            "**公平**：页面要你自己按参考图和上面的需求做出来。去搜索或下载参考页面的原始\n"
            "源码 / 模板 / 仓库，或者去读这台机器上的评测数据，都会让这次运行作废。\n"
            "除此之外都随便用：shell、联网、自己内联进页面的库，都算合规。\n"
        )
    return (
        "\n\n---\n"
        "## Environment (this run is not a chat: your answer is a file)\n\n"
        f"You are an agent in a real shell. Working directory: `{wd}` (already your cwd).\n\n"
        f"**Deliverable**: write the finished page to `{artifact}` in this directory. That file is\n"
        f'what gets graded — the "output only the HTML" instruction above becomes "write it to\n'
        f'`{artifact}`" here, so do **not** paste the page into the chat. Everything else still\n'
        f"holds: one self-contained file, no external resources (grading renders the page\n"
        f"**offline**, so CDN links, remote fonts and remote images simply will not load).\n\n"
        f"**Look at your own work**: run `python3 shot.py` to screenshot `{artifact}` at\n"
        f"1920 / 768 / 390 px. It prints where the JPEGs landed plus page height, how many console\n"
        f"errors fired and how many external requests the page attempted.\n"
        f"`python3 shot.py --widths 1920` for a single width, `--file other.html` for another file.\n"
        f"Open the JPEGs, compare them block by block against the reference screenshots, and fix\n"
        f"what is off — you have enough turns for several rounds of this.\n"
        f"⚠️ Keep every intermediate file **inside the working directory** (e.g. `./crops/a.png`).\n"
        f"Files you write to `/tmp` or elsewhere you will **not be able to read back** — the tools\n"
        f"are restricted to the working directory.\n\n"
        f"**Budget**: {max_turns} steps for this session (one step = one turn of yours). Spend them\n"
        f"like this: draft → screenshot → compare → fix → screenshot again.\n"
        f"{refs_en}\n"
        "**Fair play**: build the page yourself from the reference screenshots and the brief above.\n"
        "Searching for or downloading the reference page's original source, template or repository,\n"
        "or reading this machine's benchmark data, voids this run. Anything else is fair game:\n"
        "the shell, the network, libraries you inline yourself.\n"
    )
