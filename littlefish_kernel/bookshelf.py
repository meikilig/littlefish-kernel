"""书架：把书放上书架，也看书架上有什么。

一本书 = `data/books/<slug>/index.json` + `chapters/0001.txt`。
章节文件：第一行章名，空一行，正文段落（每段前面两个全角空格）。

约定（2026-11-01 定）：
- 只认 `.txt` / `.md`（纯文本）。epub / 漫画走 `/book 导入` 那个 node 导入器，这里不收。
- 上传的字幕稿、txt 都先过 `clean_text()`：去文件名播报行、去 `讲话人N` 标签、
  把被切给两个人的半句话接回去、删掉机翻幻觉的拉丁文串、按整段重排。
- 章节切法两条：
  1. 文里有「第X集/章/回」小标题（独占一段、不超过 40 字）→ 按标题切；
  2. 没有标题 → 按 `chapter_chars`（默认 6000）在段落边界打包。
- 删除**不真删**：挪到 `data/books/.trash/<slug>-<时间>/`，要找回直接挪回来。
- 这个模块不依赖任何插件；内核、插件、命令行工具都能用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from . import config
from .log import log

MAX_UPLOAD_CHARS = 3_000_000     # 单次上传正文上限（约 3 MB 纯文本）
MAX_CHAPTERS = 400
DEFAULT_CHAPTER_CHARS = 6000     # 一章大概 6000 字：她一次读 1200，读五段换章
MIN_CHAPTER_CHARS = 800
TRASH_DIR = ".trash"

SPK_RE = re.compile(r"^讲话人\s*(\d+)$")
BANNER_RE = re.compile(r'^\s*["“”]?\s*《.*》.*\.(mp3|m4a|wav|aac|flac|mp4|txt|srt|ass|vtt|md|json)', re.I)
MARK_RE = re.compile(r"^\s*\[\s*(music|applause|laughter|speaking|inaudible|noise)[^\]]*\]\s*$", re.I)
LATIN_RUN_RE = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\.,!?;:\-\s]{6,}")
HEAD_RE = re.compile(r"^第[一二三四五六七八九十百零〇0-9]{1,12}[章回节集篇部卷]")
CJK = r"\u4e00-\u9fff"
SPACE_BEFORE_CJK_RE = re.compile(rf"[\s\u3000]+(?=[{CJK}，。！？、；：“”‘’（）《》…—])")
SPACE_AFTER_CJK_RE = re.compile(rf"(?<=[{CJK}，。！？、；：“”‘’（）《》…—])[\s\u3000]+")


def books_dir() -> Path:
    """书架目录。测试用 LITTLEFISH_BOOKS_DIR 指到临时根，跟插件一致的读法。"""
    env = os.environ.get("LITTLEFISH_BOOKS_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return Path(config.ROOT) / "data" / "books"


def clean_text(raw: str, keep_speakers: bool = False) -> dict:
    """把字幕稿 / 乱糟糟的 txt 洗成能读的段落。返回 {"paragraphs","stats"}。"""
    text = (raw or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_UPLOAD_CHARS:
        return {"ok": False, "error": f"太长了（{len(text)} 字，上限 {MAX_UPLOAD_CHARS}）"}
    lines = [ln.rstrip() for ln in text.split("\n")]
    if lines and BANNER_RE.match(lines[0]):
        lines = lines[1:]

    turns, spk, buf = [], "", []
    for ln in lines:
        s = ln.strip()
        if not s or MARK_RE.match(s):
            continue
        m = SPK_RE.match(s)
        if m:
            if buf:
                turns.append((spk, "".join(buf)))
                buf = []
            spk = "讲话人" + m.group(1)
            continue
        buf.append(s)
    if buf:
        turns.append((spk, "".join(buf)))
    if not turns:
        return {"ok": False, "error": "这份里没有能读的字"}

    flow = "".join(t for _, t in turns)
    flow = re.sub(r"（内容由AI生成）|\(内容由AI生成\)", "", flow)
    flow = LATIN_RUN_RE.sub("", flow)
    flow = SPACE_BEFORE_CJK_RE.sub("", flow)
    flow = SPACE_AFTER_CJK_RE.sub("", flow)
    sents = [s.strip() for s in re.split(r"(?<=[。！？…])", flow) if s.strip()]

    fixed: list[str] = []
    for s in sents:
        if fixed and s[0] in "的地得":
            # 「为古。/籍续命。」这类：句号被 ASR 插在词中间
            fixed[-1] = fixed[-1].rstrip("。") + s
        elif fixed and len(s) <= 8:
            prev = fixed[-1]
            if len(prev) >= 12 and len(s) <= 5 and prev.endswith("。"):
                fixed[-1] = prev[:-1] + s
            else:
                fixed[-1] += s
        else:
            fixed.append(s)

    if keep_speakers:
        # 保留换人与说话人：一段 = 一个人说的话
        per = [(spk2, "".join(buf2)) for spk2, buf2 in _remerge(turns)]
        paras = [f"{spk2}：{t}" if spk2 else t for spk2, t in per if len(t) >= 3]
    else:
        paras, buf, seen = [], "", ""
        for s in fixed:
            if s == seen:
                continue
            seen = s
            buf += s
            if len(buf) >= 140:
                paras.append(buf)
                buf = ""
        if buf:
            if paras and len(buf) < 20:
                paras[-1] += buf
            else:
                paras.append(buf)
        paras = [p for p in paras if len(p) >= 10]

    chars = sum(len(p) for p in paras)
    return {
        "ok": True,
        "paragraphs": paras,
        "stats": {
            "paragraphs": len(paras),
            "chars": chars,
            "turns": len(turns),
            "speakers": len({s for s, _ in turns if s}),
            "raw_chars": len(text),
        },
    }


def _remerge(turns) -> list[tuple[str, str]]:
    """keep_speakers 用：断在半句的接回去 + 相邻同人合并。"""
    out: list[tuple[str, str]] = []
    for spk, t in turns:
        if out:
            prev_spk, prev_t = out[-1]
            if prev_t and prev_t[-1] not in "。！？…：；\"”』」》）":
                out[-1] = (prev_spk, prev_t + t)
                continue
            if prev_spk == spk:
                out[-1] = (prev_spk, prev_t + t)
                continue
        out.append((spk, t))
    return out


def split_chapters(paras: list[str], per: int = DEFAULT_CHAPTER_CHARS) -> list[tuple[str, list[str]]]:
    """切章：有「第X集/章」小标题就按标题切，没有就按字数打包。"""
    per = max(MIN_CHAPTER_CHARS, min(int(per or DEFAULT_CHAPTER_CHARS), 20000))
    heads = [i for i, p in enumerate(paras) if len(p) <= 40 and HEAD_RE.match(p.strip())]
    chaps: list[tuple[str, list[str]]] = []
    if len(heads) >= 2:
        if heads[0] > 0:
            front = paras[: heads[0]]
            if sum(len(x) for x in front) > 80:
                chaps.append(("开头", front))
        for j, idx in enumerate(heads):
            end = heads[j + 1] if j + 1 < len(heads) else len(paras)
            body = [p for p in paras[idx + 1:end]]
            if body:
                chaps.append((paras[idx].strip()[:40], body))
    if not chaps:
        buf: list[str] = []
        size = 0
        for p in paras:
            buf.append(p)
            size += len(p)
            if size >= per:
                chaps.append((f"第{len(chaps) + 1}章", buf))
                buf, size = [], 0
        if buf:
            if chaps and size < 200:
                chaps[-1][1].extend(buf)
            else:
                chaps.append((f"第{len(chaps) + 1}章", buf))
    return chaps[:MAX_CHAPTERS]


def suggest_slug(title: str) -> str:
    """书名 → 目录名。全中文就先给个 book-xxxx，面板里可以改。"""
    t = (title or "").strip().strip("《》")
    ascii_part = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    if ascii_part:
        return ascii_part[:40]
    import hashlib
    return "book-" + hashlib.md5(t.encode("utf-8")).hexdigest()[:8]


def _chapter_text(title: str, paras: list[str]) -> str:
    body = "\n\n".join("　　" + p for p in paras)
    return f"{title}\n\n{body}\n"


def write_book(slug: str, title: str, chapters: list[tuple[str, list[str]]],
               source: str = "", fmt: str = "txt") -> dict:
    """落盘。已有同名 slug 就整个覆盖（先挪旧的去 trash，不真删）。"""
    slug = _safe_slug(slug)
    if not slug:
        return {"ok": False, "error": "slug 不合法（只能字母数字和横线）"}
    if not chapters:
        return {"ok": False, "error": "一章都没有"}
    dest = books_dir() / slug
    replaced = dest.exists()
    if replaced:
        _move_to_trash(dest)
    (dest / "chapters").mkdir(parents=True, exist_ok=True)
    rows, total = [], 0
    for i, (ctitle, paras) in enumerate(chapters, 1):
        text = _chapter_text(ctitle, paras)
        fn = f"{i:04d}.txt"
        (dest / "chapters" / fn).write_text(text, encoding="utf-8", newline="\n")
        total += len(text)
        rows.append({"n": i, "title": ctitle, "chars": len(text), "file": fn})
    index = {
        "slug": slug,
        "title": (title or slug).strip().strip("《》"),
        "sourceFile": source,
        "format": fmt,
        "importedAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        "chapters": rows,
        "chars": total,
    }
    (dest / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                     encoding="utf-8", newline="\n")
    log.info(f"[bookshelf] 上了书架 {slug}：{len(rows)} 章 {total} 字")
    return {"ok": True, "slug": slug, "title": index["title"],
            "chapters": len(rows), "chars": total, "replaced": replaced}


def list_books() -> list[dict]:
    """书架上有哪些书。坏掉的 index.json 跳过，不让整个书架 500。"""
    root = books_dir()
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        f = d / "index.json"
        if not f.is_file():
            continue
        try:
            idx = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[bookshelf] index.json 坏了 {f}: {exc}")
            continue
        if not isinstance(idx.get("chapters"), list):
            continue
        out.append({
            "slug": d.name,
            "title": idx.get("title") or d.name,
            "chapters": len(idx["chapters"]),
            "chars": int(idx.get("chars") or 0),
            "format": idx.get("format") or "",
            "at": idx.get("importedAt") or "",
            "files": len(list((d / "chapters").glob("*.txt"))) if (d / "chapters").is_dir() else 0,
        })
    return out


def toc(slug: str) -> dict:
    idx = _read_index(slug)
    if not idx.get("ok"):
        return idx
    return {"ok": True, "slug": slug, "title": idx["title"],
            "chapters": idx["chapters"], "chars": idx["chars"], "format": idx["format"]}


def read(slug: str, n: int = 1) -> dict:
    idx = _read_index(slug)
    if not idx.get("ok"):
        return idx
    n = max(1, min(int(n or 1), len(idx["chapters"])))
    f = books_dir() / _safe_slug(slug) / "chapters" / f"{n:04d}.txt"
    if not f.is_file():
        return {"ok": False, "error": f"第 {n} 章的文件不见了"}
    text = f.read_text(encoding="utf-8", errors="replace")
    return {"ok": True, "slug": slug, "n": n, "total": len(idx["chapters"]),
            "title": idx["chapters"][n - 1].get("title", ""), "text": text, "chars": len(text)}


def delete_book(slug: str) -> dict:
    """不真删：挪进 .trash/，随时能挪回来。"""
    slug = _safe_slug(slug)
    d = books_dir() / slug
    if not d.is_dir():
        return {"ok": False, "error": "书架上没这本书"}
    dest = _move_to_trash(d)
    return {"ok": True, "slug": slug, "trash": str(dest)}


def rebuild_chapters(slug: str, per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    """重新切章：保留原来的章名，只在每章正文超过 per 字时才拆成「续」章。"""
    idx = _read_index(slug)
    if not idx.get("ok"):
        return idx
    chaps: list[tuple[str, list[str]]] = []
    for c in idx["chapters"]:
        f = books_dir() / _safe_slug(slug) / "chapters" / str(c.get("file") or "")
        if not f.is_file():
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").split("\n")
        title = (lines[0].strip() if lines else "") or c.get("title") or ""
        paras = [ln.strip().strip("　").strip() for ln in lines[1:]]
        paras = [p for p in paras if p]
        if not paras:
            continue
        for i, group in enumerate(_pack_paras(paras, per)):
            name = title if i == 0 else f"{title}（续{i}）"
            chaps.append((name, group))
    if not chaps:
        return {"ok": False, "error": "这本书读不出正文"}
    if len(chaps) > MAX_CHAPTERS:
        return {"ok": False, "error": f"切出来 {len(chaps)} 章，太多了"}
    return write_book(slug, idx["title"], chaps, source=idx.get("source") or "", fmt=idx["format"])


def _pack_paras(paras: list[str], per: int) -> list[list[str]]:
    """按字数上限把段落打包成若干组。"""
    per = max(MIN_CHAPTER_CHARS, int(per or DEFAULT_CHAPTER_CHARS))
    groups: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for p in paras:
        if cur and size + len(p) > per:
            groups.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += len(p)
    if cur:
        groups.append(cur)
    return groups or [[]]


def build_chapters(raw: str, keep_speakers: bool = False,
                   per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    """正文 → 章节列表。文里有「第X集/章」小标题就先按标题切，再洗每一节。"""
    secs = _heading_sections(raw)
    headed = [(t, body) for t, body in secs if t]
    chaps: list[tuple[str, list[str]]] = []
    if len(headed) >= 2:
        lead = "".join(body for t, body in secs if not t)
        if lead.strip() and len(lead.strip()) > 40:
            r = clean_text(lead, keep_speakers=keep_speakers)
            if r.get("ok") and r.get("paragraphs"):
                chaps.append(("开头", r["paragraphs"]))
        for t, body in headed:
            r = clean_text(body, keep_speakers=keep_speakers)
            if r.get("ok") and r.get("paragraphs"):
                chaps.append((t, r["paragraphs"]))
            elif body.strip():
                # 太短的一节会被「段落至少 10 字」的过滤器吃掉，原样留着
                chaps.append((t, [body.strip()]))
        if chaps:
            return {"ok": True, "chapters": chaps, "mode": "heading"}
    r = clean_text(raw, keep_speakers=keep_speakers)
    if not r.get("ok"):
        return r
    return {"ok": True, "chapters": split_chapters(r["paragraphs"], per),
            "mode": "packing", "stats": r["stats"]}


def _heading_sections(raw: str) -> list[tuple[str, str]]:
    """按「第X集/章/回」这样的独占行切段，返回 [(标题, 正文), …]。"""
    text = (raw or "").replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    secs: list[tuple[str, str]] = []
    title, buf = "", []
    for ln in text.split("\n"):
        s = ln.strip()
        if len(s) <= 40 and HEAD_RE.match(s) and not s.endswith(("。", "，", "？", "！", "；")):
            if buf or title:
                secs.append((title, "\n".join(buf)))
            title, buf = s, []
        else:
            buf.append(ln)
    if buf or title:
        secs.append((title, "\n".join(buf)))
    return secs


def preview(raw: str, keep_speakers: bool = False, per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    """只看不写：洗干净后长什么样、会切成几章。"""
    built = build_chapters(raw, keep_speakers=keep_speakers, per=per)
    if not built.get("ok"):
        return built
    stats = built.get("stats") or {}
    if not stats:
        allp = [p for _, ps in built["chapters"] for p in ps]
        stats = {"paragraphs": len(allp), "chars": sum(len(p) for p in allp),
                 "raw_chars": len(raw or "")}
    first = built["chapters"][0][1] if built["chapters"] else []
    last = built["chapters"][-1][1] if built["chapters"] else []
    return {
        "ok": True,
        "mode": built.get("mode"),
        "stats": stats,
        "chapters": [{"title": t, "chars": sum(len(p) for p in ps) + len(t),
                      "paragraphs": len(ps)} for t, ps in built["chapters"]],
        "head": (first[0][:600] if first else ""),
        "tail": (last[-1][:300] if last else ""),
    }


def clean_name(filename: str) -> str:
    """把文件名变成能当章名的短标题：去扩展名、去《》壳、去序号和平台后缀。"""
    s = Path(str(filename or "")).name
    for _ in range(3):                                   # 有的是 .mp3.txt 这种双层后缀
        s2 = re.sub(r"\.(txt|md|markdown|text|epub|mobi|azw3|mp3|m4a|wav|aac|flac|mp4|srt|ass|vtt|json)$",
                    "", s, flags=re.I)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"\(.*?\)|（.*?）|\[.*?\]", "", s)          # (Av123,P1) 之类
    s = re.sub(r"《([^》]*)》", r"\1", s)                   # 《书》只留里面的字
    s = re.sub(r"^\s*[^-]{1,20}?\s*-\s*\d{1,2}\s*[.、．]\s*", "", s)   # 「书名 - 1.」前缀
    for _ in range(3):                                   # 括号去掉后可能还挂着 .mp3
        s2 = re.sub(r"\.(txt|md|markdown|text|epub|mobi|azw3|mp3|m4a|wav|aac|flac|mp4|srt|ass|vtt|json)$",
                    "", s, flags=re.I)
        if s2 == s:
            break
        s = s2
    s = re.sub(r"^\s*-\s*", "", s)
    s = re.sub(r"^\s*\d{1,2}\s*[.、．]\s*", "", s)          # 开头的「1.」
    s = re.sub(r"\s+", " ", s).strip(" -_·.")
    return (s or "一章")[:40]


def preview_files(files: list[tuple[str, str]], keep_speakers: bool = False,
                  per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    """多文件预览：一个文件 = 一章（文件内有小标题的按小标题再切）。"""
    chapters: list[dict] = []
    total_paras = total_chars = raw_chars = 0
    turns = speakers = 0
    skipped: list[str] = []
    for name, raw in files:
        built = build_chapters(raw, keep_speakers=keep_speakers, per=per)
        if not built.get("ok"):
            skipped.append(f"{clean_name(name)}（{built.get('error')}）")
            continue
        st = built.get("stats") or {}
        total_paras += int(st.get("paragraphs") or 0)
        total_chars += int(st.get("chars") or 0)
        raw_chars += int(st.get("raw_chars") or len(raw or ""))
        turns += int(st.get("turns") or 0)
        speakers = max(speakers, int(st.get("speakers") or 0))
        if built.get("mode") == "heading":
            for t, ps in built["chapters"]:
                chapters.append({"title": t, "chars": sum(len(p) for p in ps) + len(t),
                                 "paragraphs": len(ps)})
        else:
            ps = built["chapters"][0][1] if built["chapters"] else []
            t = clean_name(name)
            chapters.append({"title": t, "chars": sum(len(p) for p in ps) + len(t),
                             "paragraphs": len(ps)})
    if not chapters:
        return {"ok": False, "error": "这几份里没有能读的字"}
    return {
        "ok": True,
        "mode": "files",
        "stats": {"paragraphs": total_paras, "chars": total_chars,
                  "raw_chars": raw_chars, "turns": turns, "speakers": speakers,
                  "files": len(files), "skipped": skipped},
        "chapters": chapters[:MAX_CHAPTERS],
        "head": "",
        "tail": "",
        "suggest_slug": suggest_slug(clean_name(files[0][0]) if files else "书"),
        "suggest_title": clean_name(files[0][0]) if files else "新书",
    }


def import_files(files: list[tuple[str, str]], slug: str, title: str, source: str = "",
                 keep_speakers: bool = False, per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    """多文件入库：一个文件 = 一章（文件内带「第X集/章」小标题的按小标题切）。"""
    chapters: list[tuple[str, list[str]]] = []
    raw_total = 0
    for name, raw in files:
        raw_total += len(raw or "")
        built = build_chapters(raw, keep_speakers=keep_speakers, per=per)
        if not built.get("ok"):
            continue
        if built.get("mode") == "heading":
            for t, ps in built["chapters"]:
                chapters.append((t, ps))
        else:
            ps = built["chapters"][0][1] if built["chapters"] else []
            if ps:
                chapters.append((clean_name(name), ps))
        if len(chapters) >= MAX_CHAPTERS:
            break
    if not chapters:
        return {"ok": False, "error": "这几份里没有能读的字"}
    out = write_book(slug, title, chapters[:MAX_CHAPTERS],
                     source=source or "、".join(clean_name(n) for n, _ in files[:3]), fmt="txt")
    if out.get("ok"):
        out["mode"] = "files"
        out["stats"] = {"paragraphs": sum(len(ps) for _, ps in chapters),
                        "chars": sum(len(p) for _, ps in chapters for p in ps),
                        "raw_chars": raw_total, "files": len(files)}
    return out


def import_text(raw: str, slug: str, title: str, source: str = "",
                keep_speakers: bool = False, per: int = DEFAULT_CHAPTER_CHARS) -> dict:
    built = build_chapters(raw, keep_speakers=keep_speakers, per=per)
    if not built.get("ok"):
        return built
    out = write_book(slug, title, built["chapters"], source=source, fmt="txt")
    if out.get("ok"):
        out["mode"] = built.get("mode")
        out["stats"] = built.get("stats") or {
            "paragraphs": sum(len(ps) for _, ps in built["chapters"]),
            "chars": sum(len(p) for _, ps in built["chapters"] for p in ps),
            "raw_chars": len(raw or ""),
        }
    return out


# ---------- 内部 ----------

def _safe_slug(slug: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (slug or "").strip().lower()).strip("-")
    return s[:60]


def _read_index(slug: str) -> dict:
    d = books_dir() / _safe_slug(slug)
    f = d / "index.json"
    if not f.is_file():
        return {"ok": False, "error": "书架上没这本书"}
    try:
        idx = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"index.json 坏了：{exc}"}
    if not isinstance(idx.get("chapters"), list):
        return {"ok": False, "error": "index.json 里没有 chapters"}
    return {"ok": True, "title": idx.get("title") or slug,
            "chapters": idx["chapters"], "chars": int(idx.get("chars") or 0),
            "format": idx.get("format") or "", "source": idx.get("sourceFile") or ""}


def _move_to_trash(d: Path) -> Path:
    """挪进回收站。同一秒内多次覆盖会重名，所以撞了就加序号。"""
    trash = books_dir() / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    base = f"{d.name}-{time.strftime('%y%m%d-%H%M%S')}"
    dest = trash / base
    n = 2
    while dest.exists():
        dest = trash / f"{base}-{n}"
        n += 1
    shutil.move(str(d), str(dest))
    log.info(f"[bookshelf] {d.name} 挪进回收站")
    return dest


def trash_list() -> list[dict]:
    trash = books_dir() / TRASH_DIR
    if not trash.is_dir():
        return []
    out = []
    for p in sorted(trash.iterdir(), key=lambda x: x.name, reverse=True):
        if not p.is_dir():
            continue
        size = 0
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    size += f.stat().st_size
            except OSError:
                pass
        out.append({"name": p.name, "bytes": size})
    return out


def trash_clear(keep: int = 0) -> dict:
    """清空回收站（keep=N 时留最新 N 份）。"""
    trash = books_dir() / TRASH_DIR
    if not trash.is_dir():
        return {"ok": True, "removed": 0}
    items = sorted([p for p in trash.iterdir() if p.is_dir()],
                   key=lambda x: x.name, reverse=True)
    keep = max(0, int(keep or 0))
    removed = 0
    for p in items[keep:]:
        shutil.rmtree(p, ignore_errors=True)
        removed += 1
    return {"ok": True, "removed": removed, "kept": min(keep, len(items))}
