#!/usr/bin/env python3
"""
python3 main.py ./pdf ./md
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ENGINES = ("pymupdf4llm", "pymupdf", "pdfplumber", "pdftotext")

# ────────────────────────────── утилиты ──────────────────────────────


def human_size(n: float) -> str:
    """1234567 -> '1.2 MB'"""
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024.0
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


class C:
    """ANSI-цвета; гаснут, если вывод не в терминал или задан NO_COLOR."""

    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s

    @classmethod
    def ok(cls, s): return cls._w("32", s)

    @classmethod
    def skip(cls, s): return cls._w("90", s)

    @classmethod
    def warn(cls, s): return cls._w("33", s)

    @classmethod
    def fail(cls, s): return cls._w("31", s)

    @classmethod
    def dim(cls, s): return cls._w("2", s)

    @classmethod
    def bold(cls, s): return cls._w("1", s)


def engine_available(name: str) -> bool:
    if name == "pdftotext":
        return shutil.which("pdftotext") is not None
    mod = "fitz" if name == "pymupdf" else name
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def pick_engine(preferred: str) -> str:
    if preferred != "auto":
        if not engine_available(preferred):
            raise SystemExit(f"Движок '{preferred}' не установлен.")
        return preferred
    for name in ENGINES:
        if engine_available(name):
            return name
    raise SystemExit("Не найден ни один движок. Установите:  pip install pymupdf4llm")


# ──────────────────── сборка markdown из строк ────────────────────
#
# Общий формат для всех движков со шрифтовой информацией — список страниц,
# каждая страница — список строк:
#   {"text": str, "size": float, "bold": bool, "top": float, "bottom": float}

CID_RE = re.compile(r"\(cid:\d+\)")
BULLET_RE = re.compile(r"^\s*(?:[•‣◦▪·○●□■*]|-(?=\s)|\(cid:\d+\))\s*")
NUM_RE = re.compile(r"^\s*(\d{1,2}[.)]|[a-zа-яA-ZА-Я][.)])\s+")
HYPHEN_END_RE = re.compile(r"(\w)[-‐‑–]$")
END_PUNCT = ".!?:;,»\"')"
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
             "\ufb03": "ffi", "\ufb04": "ffl", "\u00ad": ""}


def clean_text(s: str) -> str:
    """Убирает мусор сломанных кодировок и лигатуры."""
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    s = s.lstrip()
    # нераспознанный глиф в начале строки + пробел — почти всегда маркер списка
    m = re.match(r"\(cid:\d+\)\s+", s)
    if m:
        s = "• " + s[m.end():]
    s = CID_RE.sub("", s)
    return re.sub(r"[ \t\u00a0]{2,}", " ", s).strip()


def round_size(x: float) -> float:
    """Кластеризуем кегли по 0.5 pt, иначе 11.96 и 12.0 считаются разными."""
    return round(x * 2) / 2


def size_level_map(counter: Counter, body: float) -> dict[float, int]:
    """
    Размеры крупнее основного текста ранжируем по убыванию → уровни H1..H4.
    Размер, которым набрано много текста, заголовком не считаем.
    """
    total = sum(counter.values()) or 1
    big = sorted((s for s, cnt in counter.items()
                  if s >= body * 1.10 and cnt < total * 0.25), reverse=True)
    return {s: i + 1 for i, s in enumerate(big[:4])}


def escape_md(s: str) -> str:
    """Минимальное экранирование, чтобы текст не превратился в разметку."""
    if s[:1] in "#>|":
        return "\\" + s
    return s


def heading_level(size: float, bold: bool, txt: str,
                  levels: dict[float, int]) -> int | None:
    """Уровень заголовка: сперва по кеглю, иначе короткая жирная строка → H4."""
    if size in levels:
        return levels[size]
    if bold and len(txt) <= 80 and txt[-1] not in END_PUNCT:
        return min(len(levels) + 1, 4)
    return None


def join_lines(lines: list[str]) -> str:
    """Склеивает строки в абзац, восстанавливая слова, разбитые переносом."""
    para = ""
    for ln in lines:
        if not para:
            para = ln
            continue
        m = HYPHEN_END_RE.search(para)
        para = para[: m.start(0) + 1] + ln if m else para + " " + ln
    return re.sub(r"\s{2,}", " ", para).strip()


def build_markdown(pages: list[list[dict]], counter: Counter) -> str:
    """Строки со шрифтовой информацией → markdown."""
    body = counter.most_common(1)[0][0] if counter else 10.0
    levels = size_level_map(counter, body)
    out: list[str] = []

    for lines in pages:
        buf: list[str] = []
        prev: dict | None = None
        last_bullet = False

        def flush():
            nonlocal buf
            if buf:
                out.append(escape_md(join_lines(buf)))
                buf = []

        for ln in lines:
            txt = clean_text(ln["text"])
            if not txt:
                continue

            # разрыв блока по вертикальному отступу между строками
            gap = ln["top"] - prev["bottom"] if prev else 0.0
            new_block = prev is None or gap > body * 0.55
            prev = ln

            level = heading_level(ln["size"], ln["bold"], txt, levels)

            if level:
                flush()
                out.append("#" * level + " " + txt.rstrip(" .:"))
                last_bullet = False
            elif BULLET_RE.match(txt):
                flush()
                out.append("- " + BULLET_RE.sub("", txt))
                last_bullet = True
            elif NUM_RE.match(txt):
                flush()
                out.append(txt)
                last_bullet = True
            elif last_bullet and not new_block and out:
                # «хвост» длинного пункта списка — дописываем к нему
                out[-1] = join_lines([out[-1], txt])
            else:
                if new_block:
                    flush()
                    last_bullet = False
                buf.append(txt)

        flush()

    return blocks_to_md(out)


def blocks_to_md(blocks: list[str]) -> str:
    """Готовые блоки → текст: пункты списка слитно, остальное через пустую строку."""
    def is_item(s: str) -> bool:
        return s.startswith("- ") or bool(NUM_RE.match(s))

    lines: list[str] = []
    for i, part in enumerate(blocks):
        lines.append(part)
        nxt = blocks[i + 1] if i + 1 < len(blocks) else ""
        if not (is_item(part) and is_item(nxt)):
            lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def is_heading_line(txt: str, nxt: str) -> bool:
    """Заголовок без шрифтовых подсказок: короткая строка, начинающая блок."""
    if not (2 < len(txt) <= 70) or len(txt.split()) > 9:
        return False
    if txt[-1] in END_PUNCT:
        return False
    # следующая строка должна начинать новую мысль, а не продолжать эту
    return (not nxt or nxt[:1].isupper() or nxt[:1].isdigit()
            or bool(BULLET_RE.match(nxt)))


def plain_text_to_markdown(pages: list[str]) -> str:
    """
    Фолбэк для pdftotext: шрифтов нет, поэтому заголовки ищем по форме строки —
    и обязательно ДО склейки абзацев, иначе они прилипнут к тексту.
    """
    out: list[str] = []

    for text in pages:
        lines = [clean_text(ln) for ln in text.splitlines()]
        buf: list[str] = []
        block_start = True          # «здесь допустим заголовок»

        def flush():
            nonlocal buf
            if buf:
                out.append(escape_md(join_lines(buf)))
                buf = []

        for idx, ln in enumerate(lines):
            if not ln:
                flush()
                block_start = True
                continue
            if BULLET_RE.match(ln):
                flush()
                out.append("- " + BULLET_RE.sub("", ln))
                block_start = True
                continue
            if NUM_RE.match(ln):
                flush()
                out.append(ln)
                block_start = True
                continue

            nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
            if block_start and is_heading_line(ln, nxt):
                flush()
                letters = [ch for ch in ln if ch.isalpha()]
                caps = bool(letters) and sum(
                    ch.isupper() for ch in letters) / len(letters) > 0.6
                out.append(("## " if caps else "### ") + ln.rstrip(" .:"))
                block_start = True
                continue

            buf.append(ln)
            # конец предложения — дальше снова может стоять заголовок
            block_start = ln[-1] in ".!?"

        flush()

    return blocks_to_md(out)


# ───────────────────────────── движки ─────────────────────────────


def conv_pymupdf4llm(path: Path) -> tuple[str, int]:
    import fitz
    import pymupdf4llm

    with fitz.open(path) as doc:
        n = doc.page_count
    md = pymupdf4llm.to_markdown(str(path), show_progress=False)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n", n


def conv_pymupdf(path: Path) -> tuple[str, int]:
    import fitz

    pages: list[list[dict]] = []
    counter: Counter = Counter()

    with fitz.open(path) as doc:
        n = doc.page_count
        for page in doc:
            try:
                blocks = page.get_text("dict", sort=True)["blocks"]
            except TypeError:                       # старые версии PyMuPDF
                blocks = page.get_text("dict")["blocks"]

            plines: list[dict] = []
            for blk in blocks:
                for line in blk.get("lines", []):
                    spans = line.get("spans", [])
                    txt = "".join(sp["text"] for sp in spans)
                    if not txt.strip():
                        continue
                    for sp in spans:
                        counter[round_size(sp["size"])] += len(sp["text"])
                    x0, y0, x1, y1 = line["bbox"]
                    plines.append({
                        "text": txt,
                        "size": round_size(max(sp["size"] for sp in spans)),
                        "bold": any(sp.get("flags", 0) & 16
                                    or "bold" in sp.get("font", "").lower()
                                    for sp in spans),
                        "top": y0,
                        "bottom": y1,
                    })
            pages.append(plines)

    return build_markdown(pages, counter), n


def conv_pdfplumber(path: Path) -> tuple[str, int]:
    import pdfplumber

    pages: list[list[dict]] = []
    counter: Counter = Counter()

    with pdfplumber.open(path) as pdf:
        n = len(pdf.pages)
        for page in pdf.pages:
            plines: list[dict] = []
            for ln in page.extract_text_lines(layout=False):
                chars = ln.get("chars") or []
                sizes = [c["size"] for c in chars if c.get("size")]
                for sz in sizes:
                    counter[round_size(sz)] += 1
                plines.append({
                    "text": ln["text"],
                    "size": round_size(max(sizes)) if sizes else 10.0,
                    "bold": any("bold" in (c.get("fontname") or "").lower()
                                for c in chars),
                    "top": ln["top"],
                    "bottom": ln["bottom"],
                })
            pages.append(plines)
            try:
                page.flush_cache()                  # не раздуваем память
            except AttributeError:
                pass

    return build_markdown(pages, counter), n


def conv_pdftotext(path: Path) -> tuple[str, int]:
    res = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", str(path), "-"],
        capture_output=True, timeout=900,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode("utf-8", "replace").strip()[:200])
    text = res.stdout.decode("utf-8", "replace")
    pages = text.split("\f")
    return plain_text_to_markdown(pages), max(1, len(pages) - 1)


CONVERTERS = {
    "pymupdf4llm": conv_pymupdf4llm,
    "pymupdf": conv_pymupdf,
    "pdfplumber": conv_pdfplumber,
    "pdftotext": conv_pdftotext,
}


# ──────────────────────────── воркер ────────────────────────────


def convert_one(job: tuple) -> dict:
    """Выполняется в отдельном процессе. Возвращает только picklable-данные."""
    src_s, dst_s, engine, front_matter = job
    src, dst = Path(src_s), Path(dst_s)
    t0 = time.perf_counter()
    res = {"src": src_s, "dst": dst_s, "in_size": src.stat().st_size,
           "out_size": 0, "pages": 0, "secs": 0.0, "status": "ok", "note": ""}
    try:
        md, pages = CONVERTERS[engine](src)
        res["pages"] = pages

        if len(md.strip()) < 16:
            # пустой .md только мешал бы: на следующем запуске файл бы пропустился
            res["status"] = "warn"
            res["note"] = "нет текстового слоя — скан, нужен OCR; .md не создан"
            res["secs"] = time.perf_counter() - t0
            return res

        if front_matter:
            title = src.stem.replace("_", " ").replace('"', "'").strip()
            md = ("---\n"
                  f'title: "{title}"\n'
                  f'source: "{src.name}"\n'
                  f"pages: {pages}\n"
                  "---\n\n") + md

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(dst)                    # атомарно: битых .md не остаётся
        res["out_size"] = dst.stat().st_size
    except Exception as e:                  # одна битая PDF не валит весь батч
        res["status"] = "fail"
        res["note"] = f"{type(e).__name__}: {e}".replace("\n", " ")[:150]
    res["secs"] = time.perf_counter() - t0
    return res


# ──────────────────────────── лог и main ────────────────────────────

LABELS = {"ok": ("ok  ", C.ok), "skip": ("skip", C.skip),
          "warn": ("warn", C.warn), "fail": ("fail", C.fail)}


def log_line(i: int, total: int, res: dict, width: int) -> None:
    label, paint = LABELS[res["status"]]
    counter = C.dim(f"[{i:>{len(str(total))}}/{total}]")
    name = Path(res["src"]).name
    name = name if len(name) <= width else name[: width - 1] + "…"
    row = f"{counter} {paint(label)}  {name:<{width}}"

    if res["status"] == "skip":
        print(f"{row}  {C.dim(res['note'])}")
        return
    if res["status"] == "fail":
        print(f"{row}  {C.fail(res['note'])}")
        return

    sizes = f"{human_size(res['in_size']):>9} → {human_size(res['out_size']):>9}"
    meta = f"{res['pages']:>4} стр  {res['secs']:>6.2f} с"
    tail = f"  {C.warn(res['note'])}" if res["note"] else ""
    print(f"{row}  {sizes}  {C.dim(meta)}{tail}")


def tally(stats: dict, res: dict) -> None:
    stats[res["status"]] = stats.get(res["status"], 0) + 1
    if res["status"] != "fail":
        stats["in"] += res["in_size"]
        stats["out"] += res["out_size"]


def main() -> int:
    p = argparse.ArgumentParser(description="Батч-конвертер PDF → Markdown.")
    p.add_argument("src", type=Path, help="папка с PDF")
    p.add_argument("dst", type=Path, help="папка для .md")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="обходить подпапки, сохраняя структуру")
    p.add_argument("-f", "--force", action="store_true",
                   help="перезаписывать существующие .md")
    p.add_argument("--newer", action="store_true",
                   help="переконвертировать, если PDF новее .md")
    p.add_argument("-j", "--workers", type=int, default=0,
                   help="число процессов (0 = авто)")
    p.add_argument("-e", "--engine", default="auto", choices=("auto",) + ENGINES,
                   help="движок извлечения текста")
    p.add_argument("--front-matter", action="store_true",
                   help="добавлять YAML-шапку (title/source/pages)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="только показать, что будет сделано")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args()

    # чтобы `pdf2md ... | head` не падал с BrokenPipeError
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass                                    # Windows / не главный поток

    if args.no_color:
        C.enabled = False

    if not args.src.is_dir():
        print(C.fail(f"Нет такой папки: {args.src}"), file=sys.stderr)
        return 2

    pdfs = sorted(f for f in args.src.glob("**/*" if args.recursive else "*")
                  if f.is_file() and f.suffix.lower() == ".pdf")
    if not pdfs:
        print(C.warn(f"PDF не найдены в {args.src}"))
        return 0

    engine = pick_engine(args.engine)

    jobs: list[tuple] = []
    skipped: list[tuple[Path, Path]] = []
    for src in pdfs:
        rel = src.relative_to(args.src) if args.recursive else Path(src.name)
        dst = (args.dst / rel).with_suffix(".md")
        stale = args.newer and dst.exists() and src.stat().st_mtime > dst.stat().st_mtime
        if dst.exists() and not args.force and not stale:
            skipped.append((src, dst))
        else:
            jobs.append((str(src), str(dst), engine, args.front_matter))

    total = len(pdfs)
    width = min(46, max([len(f.name) for f in pdfs] + [14]))

    print(f"{C.bold('pdf2md')}  {args.src} → {args.dst}  {C.dim('движок: ' + engine)}")
    print(C.dim(f"найдено: {total}   к конвертации: {len(jobs)}   "
                f"пропуск: {len(skipped)}"))
    print()

    i = 0
    for src, dst in skipped:
        i += 1
        log_line(i, total, {"src": str(src), "status": "skip",
                            "note": f"{dst.name} уже есть"}, width)

    if args.dry_run:
        for src, dst, _, _ in jobs:
            i += 1
            print(f"{C.dim(f'[{i}/{total}]')} {C.warn('plan')}  "
                  f"{Path(src).name:<{width}}  → {dst}")
        return 0

    args.dst.mkdir(parents=True, exist_ok=True)
    workers = args.workers or min(8, os.cpu_count() or 2)
    if len(jobs) <= 1:
        workers = 1

    t0 = time.perf_counter()
    stats = {"ok": 0, "warn": 0, "fail": 0, "in": 0, "out": 0}

    try:
        if workers == 1:
            for job in jobs:
                res = convert_one(job)
                i += 1
                tally(stats, res)
                log_line(i, total, res, width)
        else:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(convert_one, job) for job in jobs]
                for fut in as_completed(futures):
                    res = fut.result()
                    i += 1
                    tally(stats, res)
                    log_line(i, total, res, width)
    except KeyboardInterrupt:
        print(C.warn("\nПрервано пользователем."))
        return 130

    elapsed = time.perf_counter() - t0
    parts = [C.ok(f"{stats['ok']} готово")]
    if stats["warn"]:
        parts.append(C.warn(f"{stats['warn']} без текста"))
    if skipped:
        parts.append(C.skip(f"{len(skipped)} пропущено"))
    if stats["fail"]:
        parts.append(C.fail(f"{stats['fail']} с ошибкой"))

    print()
    print(f"{C.bold('Итого')} за {elapsed:.2f} с: " + ", ".join(parts))
    if stats["in"]:
        print(C.dim(f"PDF {human_size(stats['in'])} → MD {human_size(stats['out'])}"))
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())