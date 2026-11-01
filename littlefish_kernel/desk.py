"""她的桌面：写作本子的存储。

一个本子 = `data/desk/<slug>.md`，开头一段 front-matter，正文按 `## HH:MM` 分页。

规则（2026-11-01 定）：
- 私密本、公开本**同一套上限**：每本一个 `limit`（默认 6000 字，面板可改，硬顶 20000），
  写满就写不进去，要走 `request_more()` 申请，主人在「能力 → 写作」页改。
- 两种本子唯一的差别是**谁看得见**，不再决定能不能改。
- 改文章**默认用 `edit_book()` 查找替换**：只输出定位串和替换串，不吐全文，
  token 跟改了幾個字有关，跟文章多长无关。
- 整篇重写（`mode="replace"`）只在 ≤9000 字时允许：一次重写约 ¥0.10，
  再长就超过主人的底线，直接拒，让她改用查找替换。
- 没有 `[[ ]]` 标记、没有"主人帮忙删括号再拼起来"——那套人为删减已经去掉。

这个模块不依赖任何插件，内核和插件都能用；不碰 memory 表、不碰注入预算。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import caps, config
from .log import log

PLUGIN = "ability_writing"
DEFAULT_LIMIT = 6000          # 每本默认上限：整篇重写一次约 ¥0.07，留出余量
HARD_LIMIT = 20000            # 面板最多能调到这儿（再往上就不是"一本本子"了）
FULL_REWRITE_MAX = 7800       # 超过这个字数，禁止整篇重写（重写一次约 ¥0.10，是底线）
MAX_BOOK_BYTES = 200 * 1024
MAX_BOOKS = 50

# 计费口径跟 budget.py 的默认价一致：输入 2 元/M，输出 8 元/M
PRICE_IN = 2.0 / 1_000_000
PRICE_OUT = 8.0 / 1_000_000

PAGE_RE = re.compile(
    r"^## (?:(?P<day>\d{1,2}-\d{1,2})[ \t]+)?(?P<at>\d{1,2}:\d{2}|睡前|补记)"
    r"(?:[ \t]+(?P<note>.*?))?[ \t]*$",
    re.M,
)
BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]')


def rewrite_cost(chars: int) -> float:
    """整篇重写一次的粗略成本（元）：输出全文 + 上下文里再读一遍。"""
    n = max(0, int(chars))
    return n * PRICE_OUT + (n * 2 + 3000) * PRICE_IN


def desk_dir() -> Path:
    env = os.environ.get("LITTLEFISH_DESK_DIR")
    if env:
        return Path(env)
    return config.ROOT / "data" / "desk"


def _cap_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(caps.raw(PLUGIN, key, default))
    except Exception:
        n = default
    return max(lo, min(n, hi))


def book_limit_default() -> int:
    """新本子的默认上限（私密、公开共用）。"""
    return _cap_int("max_chars", DEFAULT_LIMIT, 200, HARD_LIMIT)


def max_books() -> int:
    return _cap_int("max_books", MAX_BOOKS, 1, 500)


# ---------------------------------------------------------------- 解析

def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end]
    body = text[end + 4:]
    if body.startswith("\n"):
        body = body[1:]
    meta: dict = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, body


def _render(meta: dict, body: str) -> str:
    order = ["title", "started", "visibility", "limit", "status"]
    lines = ["---"]
    for k in order:
        if k in meta:
            lines.append(f"{k}: {meta[k]}")
    for k, v in meta.items():
        if k not in order:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


def pages_of(body: str) -> list[dict]:
    """把正文切成页：[{n, at, note, text}]。

    页头认三种写法：`## 09:20`、`## 11-01 09:20`、`## 11-01 09:20 《书名》`
    （`睡前`/`补记` 也一样）。`at` 是时间（有日期就带上日期），`note` 是页头后
    面那句备注（读书本里是书名）。页 1 是第一个页头之前的内容。
    """
    idx = []
    for m in PAGE_RE.finditer(body):
        day = (m.group("day") or "").strip()
        clock = (m.group("at") or "").strip()
        note = (m.group("note") or "").strip()
        idx.append((m.start(), f"{day} {clock}" if day else clock, note))
    out = []
    if not idx:
        return [{"n": 1, "at": "", "note": "", "text": body.strip()}] if body.strip() else []
    head = body[: idx[0][0]].strip()
    if head:
        out.append({"n": 1, "at": "", "note": "", "text": head})
    for i, (pos, at, note) in enumerate(idx):
        end = idx[i + 1][0] if i + 1 < len(idx) else len(body)
        seg = body[pos:end]
        seg = PAGE_RE.sub("", seg, count=1).strip()
        out.append({"n": len(out) + 1, "at": at, "note": note, "text": seg})
    return out


def body_chars(body: str) -> int:
    """只数她写的字，不数 `## HH:MM` 页头。"""
    return sum(len(re.sub(r"\s", "", p["text"])) for p in pages_of(body or ""))


# ---------------------------------------------------------------- 书架

def _safe_name(title: str) -> str:
    name = BAD_NAME.sub("-", str(title or "").strip()) or "本子"
    name = re.sub(r"\s+", "-", name).strip("-")
    return name[:40]


def _visible(f: Path) -> bool:
    """老版本留下的成文版/历史和临时文件不算一个本子。"""
    stem = f.stem
    return not (stem.endswith("-成文") or stem.endswith(".history") or stem.endswith(".md"))


def find_book(ref: str) -> Path | None:
    """按 slug、标题、或 slug 前缀找本子，必须在桌面目录里。"""
    root = desk_dir()
    if not root.is_dir():
        return None
    ref = str(ref or "").strip()
    if not ref:
        return None
    safe = Path(ref).name
    direct = root / (safe if safe.endswith(".md") else safe + ".md")
    try:
        if direct.is_file() and direct.resolve().parent == root.resolve():
            return direct
    except Exception:
        pass
    lo = ref.lower()
    for f in sorted(root.glob("*.md")):
        if _visible(f) and (f.stem.lower() == lo or f.stem.lower().startswith(lo)):
            return f
    for f in sorted(root.glob("*.md")):
        if not _visible(f):
            continue
        meta, _ = _parse(_read(f))
        if str(meta.get("title") or "").strip() == ref:
            return f
    return None


def _read(f: Path) -> str:
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[desk] 读不了 {f}: {exc}")
        return ""


def _write(f: Path, text: str) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, f)


def list_books() -> list[dict]:
    root = desk_dir()
    if not root.is_dir():
        return []
    out = []
    for f in sorted(root.glob("*.md")):
        if not _visible(f):
            continue
        raw = _read(f)
        meta, body = _parse(raw)
        out.append({
            "slug": f.stem,
            "title": meta.get("title") or f.stem,
            "visibility": meta.get("visibility") or "private",
            "status": meta.get("status") or "open",
            "started": meta.get("started") or "",
            "chars": body_chars(body),
            "limit": _limit_of(meta),
            "pages": len(pages_of(body)),
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
        })
    return out


def _limit_of(meta: dict) -> int:
    try:
        n = int(str(meta.get("limit") or "").strip() or 0)
    except Exception:
        n = 0
    return n or book_limit_default()


def read_book(ref: str, from_page: int = 0, to_page: int = 0) -> dict:
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    pages = pages_of(body)
    if from_page or to_page:
        a = max(1, int(from_page or 1))
        b = min(len(pages), int(to_page or a))
        pages = [p for p in pages if a <= p["n"] <= b]
    return {
        "ok": True,
        "slug": f.stem,
        "title": meta.get("title") or f.stem,
        "visibility": meta.get("visibility") or "private",
        "status": meta.get("status") or "open",
        "chars": body_chars(body),
        "limit": _limit_of(meta),
        "pages": pages,
        "body": body,
        "rewrite_cost": round(rewrite_cost(body_chars(body)), 4),
    }


def new_book(title: str, visibility: str = "private", limit: int = 0,
             started: str = "") -> dict:
    vis = visibility if visibility in ("private", "shared", "letter") else "private"
    books = list_books()
    if len(books) >= max_books():
        return {"ok": False, "error": f"桌上已经有 {len(books)} 本了，先封几本再拿新的"}
    stamp = started or time.strftime("%Y-%m-%dT%H:%M")
    slug = f"{time.strftime('%Y-%m-%d')}-{_safe_name(title)}"
    root = desk_dir()
    f = root / f"{slug}.md"
    n = 2
    while f.exists():
        f = root / f"{slug}-{n}.md"
        n += 1
    want = int(limit or 0)
    meta = {
        "title": str(title or "").strip() or "没题",
        "started": stamp,
        "visibility": vis,
        "limit": max(200, min(want, HARD_LIMIT)) if want > 0 else book_limit_default(),
        "status": "open",
    }
    _write(f, _render(meta, ""))
    log.info(f"[desk] new {f.name} visibility={vis} limit={meta['limit']}")
    return {"ok": True, "slug": f.stem, "path": str(f), "visibility": vis,
            "limit": meta["limit"]}


def append_page(ref: str, text: str, at: str = "", note: str = "") -> dict:
    """另起一页接着写。纯追加，不重写旧内容。

    `at` 是页头时间（默认「几月几号 几点」，也可以传「睡前」这种说法）；
    `note` 是页头后面那句备注，读书本用它写书名（`## 11-01 21:10 《书名》`）。
    """
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    if (meta.get("status") or "open") == "sealed":
        return {"ok": False, "error": "这本封了，只能读"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "写什么？"}
    stamp = at or time.strftime("%m-%d %H:%M")
    note = str(note or "").strip()
    head = f"## {stamp}" + (f" {note}" if note else "")
    body = (body.rstrip() + "\n\n" if body.strip() else "") + f"{head}\n{text}\n"
    limit = _limit_of(meta)
    chars = body_chars(body)
    if chars > limit:
        return {"ok": False, "chars": chars, "limit": limit,
                "error": f"这本到 {limit} 字了（现在会到 {chars} 字）。要更多就申请：desk_ask_more"}
    if len(body.encode("utf-8")) > MAX_BOOK_BYTES:
        return {"ok": False, "error": "这本太大了（200KB 上限）"}
    _write(f, _render(meta, body))
    log.info(f"[desk] append {f.name} chars={chars}/{limit}")
    return {"ok": True, "slug": f.stem, "chars": chars, "limit": limit,
            "pages": len(pages_of(body))}


# ---------------------------------------------------------------- 读书本

def reading_title() -> str:
    """读书本按月一本：所有书混着写，翻回去能看到哪个月读了什么。"""
    return f"读书-{time.strftime('%Y-%m')}"


def _ensure_reading_book():
    ref = reading_title()
    f = find_book(ref)
    if f:
        return f, {"ok": True, "created": False, "slug": f.stem}
    made = new_book(ref, visibility="private", limit=HARD_LIMIT)
    if not made.get("ok"):
        return None, made
    f = find_book(ref)
    if not f:
        return None, {"ok": False, "error": "读书本没拿起来"}
    return f, {"ok": True, "created": True, "slug": f.stem}


def note_passage(book: str, line: str, why: str = "", question: str = "",
                 at: str = "") -> dict:
    """读一本书时记一页，就三样：戳到我的那句、我为什么停在这、还没想明白的。

    不写读后感、不总结全书——她写不出那种东西，也不该逼她写。
    """
    book = str(book or "").strip().strip("《》").strip()
    line = str(line or "").strip()
    if not line:
        return {"ok": False, "error": "那句戳到你的话是什么？哪怕只记半句"}
    f, how = _ensure_reading_book()
    if not f:
        return how
    parts = [f"> {line}"]
    why = str(why or "").strip()
    question = str(question or "").strip()
    if why:
        parts.append(f"我为什么停在这：{why}")
    if question:
        parts.append(f"还没想明白：{question}")
    wrote = append_page(f.stem, "\n\n".join(parts), at=at,
                        note=f"《{book}》" if book else "")
    if not wrote.get("ok"):
        return wrote
    out = dict(wrote)
    out.update({"created": how.get("created", False), "book": book})
    log.info(f"[desk] note {book or '-'} -> {f.name} pages={out.get('pages')}")
    return out


def by_book(book: str, limit: int = 40) -> dict:
    """把写过这本书的页都翻出来（跨月、跨本子），按时间倒序。

    先认"页头写了这本书"的读书页（`## MM-DD HH:MM 《书名》`）；只有一页都没有时，
    才退而找"正文里顺口提过这本书"的页（日记之类），并在结果里标 `matched=text`。
    这样日记里顺手提一句，不会混进读书笔记里冒充"那天读它时想的"。
    """
    q = str(book or "").strip().strip("《》").strip()
    if not q:
        return {"ok": False, "error": "翻哪本书？说个书名"}
    strict, loose = [], []
    for meta in list_books():
        f = desk_dir() / f"{meta.get('slug')}.md"
        if not f.exists():
            continue
        _, body = _parse(_read(f))
        for pg in pages_of(body):
            note = pg.get("note") or ""
            text = pg.get("text") or ""
            if q not in note and q not in text:
                continue
            row = {
                "book": q,
                "slug": meta.get("slug"),
                "title": meta.get("title") or "",
                "at": pg.get("at") or "",
                "note": note,
                "text": text,
            }
            if q in note:
                strict.append(dict(row, matched="note"))
            else:
                loose.append(dict(row, matched="text"))
    hits = strict or loose
    hits.sort(key=lambda h: (h["at"], h["slug"] or ""), reverse=True)
    n = max(1, min(int(limit or 40), 100))
    return {"ok": True, "book": q, "count": len(hits), "shown": len(hits[:n]),
            "matched": "note" if strict else ("text" if loose else ""),
            "hits": hits[:n]}


def edit_book(ref: str, find: str, replace: str, all_hits: bool = False) -> dict:
    """查找并替换：只动命中的那一段，不重写全文。

    - 找不到就报错，原文一个字不动；
    - 命中多处而没给 all_hits，也报错，让她给更长的定位串；
    - 替换后仍要过字数上限。
    """
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    if (meta.get("status") or "open") == "sealed":
        return {"ok": False, "error": "这本封了，只能读"}
    needle = str(find or "")
    new = str(replace if replace is not None else "")
    if not needle:
        return {"ok": False, "error": "要用哪句话定位？find 不能空"}
    hits = body.count(needle)
    if hits == 0:
        return {"ok": False, "error": "找不到这句话，一个字都没动（用 desk_read 看看原文）"}
    if hits > 1 and not all_hits:
        return {"ok": False, "hits": hits,
                "error": f"这句话在文里出现 {hits} 次，给长一点的定位串，或者 all=true 全换"}
    body2 = body.replace(needle, new) if all_hits else body.replace(needle, new, 1)
    limit = _limit_of(meta)
    chars = body_chars(body2)
    if chars > limit:
        return {"ok": False, "chars": chars, "limit": limit,
                "error": f"换完会到 {chars} 字，超过这本的 {limit} 字上限"}
    _write(f, _render(meta, body2))
    log.info(f"[desk] edit {f.name} hits={hits if all_hits else 1} chars={chars}/{limit}")
    return {"ok": True, "slug": f.stem, "replaced": hits if all_hits else 1,
            "chars": chars, "limit": limit}


def replace_all(ref: str, text: str, at: str = "") -> dict:
    """整篇重写：只在 ≤9000 字时允许（约 ¥0.10 底线）。"""
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, _ = _parse(_read(f))
    if (meta.get("status") or "open") == "sealed":
        return {"ok": False, "error": "这本封了，只能读"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "写什么？"}
    limit = _limit_of(meta)
    chars = body_chars(text)
    if chars > FULL_REWRITE_MAX:
        return {"ok": False, "chars": chars,
                "error": (f"整篇重写这一篇约 ¥{rewrite_cost(chars):.2f}，超过 ¥0.10 底线。"
                          "改局部用 desk_edit（查找替换），或者接着往后面加页")}
    if chars > limit:
        return {"ok": False, "chars": chars, "limit": limit,
                "error": f"这本上限 {limit} 字，你这篇 {chars} 字，写不进去"}
    stamp = at or time.strftime("%H:%M")
    body = f"## {stamp}\n{text}\n"
    _write(f, _render(meta, body))
    log.info(f"[desk] replace {f.name} chars={chars}/{limit}")
    return {"ok": True, "slug": f.stem, "chars": chars, "limit": limit}


def search(ref: str, query: str, limit: int = 5) -> dict:
    """在一本（`ref` 给 `*`/空 = 全部）本子里找一句话，返回前后一小段。"""
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "error": "找什么？"}
    want = max(1, min(int(limit or 5), 10))
    if str(ref or "").strip() in ("", "*", "all"):
        targets = [b["slug"] for b in list_books()]
    else:
        f = find_book(ref)
        if not f:
            return {"ok": False, "error": f"桌上没有这本：{ref}"}
        targets = [f.stem]
    hits: list[dict] = []
    for slug in targets:
        r = read_book(slug)
        if not r.get("ok"):
            continue
        for pg in r["pages"]:
            i = (pg.get("text") or "").find(q)
            if i < 0:
                continue
            a, z = max(0, i - 50), min(len(pg["text"]), i + len(q) + 90)
            hits.append({"book": r["title"], "slug": r["slug"], "page": pg["n"],
                         "at": pg["at"], "snippet": pg["text"][a:z].replace("\n", " ")})
            if len(hits) >= want:
                break
        if len(hits) >= want:
            break
    return {"ok": True, "query": q, "count": len(hits), "hits": hits}


def seal(ref: str) -> dict:
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    meta["status"] = "sealed"
    _write(f, _render(meta, body))
    return {"ok": True, "slug": f.stem}


# ---------------------------------------------------------------- 导出（照书架的"导入"反过来）

def plain_text(ref: str) -> dict:
    """把一本本子摊平：去掉 front-matter 和 `## HH:MM` 页头，段与段之间空一行。"""
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    paras = [(p.get("text") or "").strip() for p in pages_of(body)]
    text = "\n\n".join([p for p in paras if p])
    return {"ok": True, "slug": f.stem, "title": meta.get("title", f.stem),
            "text": text, "chars": len(text)}


def export_book(ref: str, dest: str = "", fmt: str = "md") -> dict:
    """导出成文件。

    - `md`：原样（带 front-matter 和 `## HH:MM` 页头），就是桌上的那份。
    - `txt`：摊平的纯文本，适合直接贴出去或给别人看。
    不给 `dest` 时落在 `data/desk/_exports/` 里，不污染桌面。
    """
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    fmt = (fmt or "md").lower().lstrip(".")
    if fmt not in ("md", "txt"):
        return {"ok": False, "error": "只能导出成 md 或 txt"}
    if dest:
        out = Path(dest).expanduser()
        if out.is_dir():
            out = out / f"{f.stem}.{fmt}"
    else:
        out = desk_dir() / "_exports" / f"{f.stem}.{fmt}"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"建不了目录：{exc}"}
    if fmt == "md":
        text = _read(f)
    else:
        r = plain_text(f.stem)
        if not r.get("ok"):
            return r
        text = r["text"] + "\n"
    try:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"写不进去：{exc}"}
    log.info(f"[desk] export {f.name} -> {out} fmt={fmt} chars={len(text)}")
    return {"ok": True, "path": str(out), "fmt": fmt, "chars": len(text)}


# ---------------------------------------------------------------- 提额

def _req_file() -> Path:
    return desk_dir() / "_requests.jsonl"


def pending() -> list[dict]:
    fh = _req_file()
    if not fh.is_file():
        return []
    out = []
    for line in fh.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("status", "pending") == "pending":
            out.append(rec)
    return out


def request_more(ref: str, want: int, why: str = "") -> dict:
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    now = _limit_of(meta)
    want = max(now + 100, min(int(want or 0), HARD_LIMIT))
    rec = {
        "book": f.stem,
        "title": meta.get("title") or f.stem,
        "now": now,
        "want": want,
        "why": str(why or "").strip()[:200],
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }
    fh = _req_file()
    fh.parent.mkdir(parents=True, exist_ok=True)
    with fh.open("a", encoding="utf-8") as h:
        h.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log.info(f"[desk] request {f.stem} {now}->{want}")
    return {"ok": True, "request": rec}


def resolve(ref: str, limit: int = 0, reject: bool = False) -> dict:
    f = find_book(ref)
    if not f:
        return {"ok": False, "error": f"桌上没有这本：{ref}"}
    meta, body = _parse(_read(f))
    if not reject and limit:
        meta["limit"] = max(200, min(int(limit), HARD_LIMIT))
        _write(f, _render(meta, body))
    fh = _req_file()
    if fh.is_file():
        lines, changed = [], False
        for line in fh.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                lines.append(line)
                continue
            if rec.get("book") == f.stem and rec.get("status", "pending") == "pending":
                rec["status"] = "rejected" if reject else "approved"
                rec["resolved_at"] = time.strftime("%Y-%m-%d %H:%M")
                changed = True
            lines.append(json.dumps(rec, ensure_ascii=False))
        if changed:
            tmp = fh.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, fh)
    return {"ok": True, "slug": f.stem, "limit": _limit_of(meta),
            "rejected": bool(reject)}
