#!/usr/bin/env python3
"""
PDF → Markdown: настольное приложение (Windows / macOS).

    python gui.py

Конвертация — движками из main.py, интерфейс — HTML в нативном окне (pywebview).
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

# В оконной сборке PyInstaller stdout/stderr = None — библиотеки, которые пишут
# в консоль напрямую, иначе падают.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w", encoding="utf-8"))

import main as engine_mod  # noqa: E402  (движки конвертации)

APP_NAME = "PDF → MD"


def resource(rel: str) -> Path:
    """Путь к файлу ресурсов и в исходниках, и внутри сборки PyInstaller."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


# ───────────────────────── воркер (отдельный процесс) ─────────────────────────


def convert_worker(src: str, engine: str, front_matter: bool) -> dict:
    t0 = time.perf_counter()
    path = Path(src)
    md, pages = engine_mod.CONVERTERS[engine](path)
    if len(md.strip()) < 16:
        raise RuntimeError("В PDF нет текстового слоя (скан) — нужен OCR")
    if front_matter:
        title = path.stem.replace("_", " ").replace('"', "'").strip()
        md = (f'---\ntitle: "{title}"\nsource: "{path.name}"\n'
              f"pages: {pages}\n---\n\n") + md
    return {"md": md, "pages": pages, "secs": time.perf_counter() - t0}


# ─────────────────────────────── API для JS ───────────────────────────────


class Api:
    def __init__(self) -> None:
        self._window = None
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._busy = False
        self._last_dir: str = ""

    # служебное
    def _push(self, fn: str, payload) -> None:
        import json
        if self._window:
            self._window.evaluate_js(f"window.app && app.{fn}({json.dumps(payload, ensure_ascii=False)})")

    @staticmethod
    def _public(it: dict) -> dict:
        return {k: it[k] for k in ("id", "name", "size", "status", "pages", "secs", "note", "out_size")}

    def _add_paths(self, paths) -> list[dict]:
        found: list[Path] = []
        for p in paths or []:
            p = Path(p)
            if p.is_dir():
                found += sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() == ".pdf")
            elif p.is_file() and p.suffix.lower() == ".pdf":
                found.append(p)

        added = []
        with self._lock:
            known = {it["path"] for it in self._items.values()}
            for f in found:
                if str(f) in known:
                    continue
                known.add(str(f))
                it = {"id": uuid.uuid4().hex[:10], "path": str(f), "name": f.name,
                      "size": f.stat().st_size, "status": "queued", "pages": 0,
                      "secs": 0.0, "note": "", "md": None, "out_size": 0}
                self._items[it["id"]] = it
                added.append(self._public(it))
        if found:
            self._last_dir = str(found[-1].parent)
        return added

    def _done(self) -> list[dict]:
        return [it for it in self._items.values() if it["md"] is not None]

    def _dialog(self, kind, **kw):
        import webview
        res = self._window.create_file_dialog(kind, directory=self._last_dir, **kw)
        if not res:
            return None
        return res if isinstance(res, str) else res[0]

    # вызывается из JS
    def pick_files(self) -> list[dict]:
        import webview
        res = self._window.create_file_dialog(
            webview.FileDialog.OPEN, directory=self._last_dir, allow_multiple=True,
            file_types=("PDF (*.pdf)", "Все файлы (*.*)"))
        return self._add_paths(res)

    def pick_folder(self) -> list[dict]:
        import webview
        folder = self._dialog(webview.FileDialog.FOLDER)
        return self._add_paths([folder]) if folder else []

    def remove(self, item_id: str) -> None:
        with self._lock:
            it = self._items.get(item_id)
            if it and it["status"] != "working":
                del self._items[item_id]

    def clear(self) -> None:
        with self._lock:
            for k in [k for k, v in self._items.items() if v["status"] != "working"]:
                del self._items[k]

    def engines(self) -> list[str]:
        return [e for e in ("pymupdf4llm", "pymupdf") if engine_mod.engine_available(e)]

    def convert(self, engine: str, front_matter: bool, redo: bool = False) -> bool:
        if self._busy:
            return False
        with self._lock:
            todo = [it for it in self._items.values()
                    if it["status"] in ("queued", "fail") or (redo and it["status"] == "ok")]
            for it in todo:
                it.update(status="waiting", note="", md=None)
        if not todo:
            return False
        self._busy = True
        threading.Thread(target=self._run, args=(todo, engine, front_matter), daemon=True).start()
        return True

    def _run(self, todo: list[dict], engine: str, front_matter: bool) -> None:
        workers = max(1, min(len(todo), (os.cpu_count() or 2) - 1, 6))
        t0 = time.perf_counter()
        for it in todo:
            self._push("update", self._public(it))
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futs = {}
                for it in todo:
                    futs[ex.submit(convert_worker, it["path"], engine, front_matter)] = it
                for it in todo[:workers]:  # «в работе» — первые N, остальные ждут
                    it["status"] = "working"
                    self._push("update", self._public(it))
                pending = set(futs)
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in done:
                        it = futs[fut]
                        try:
                            r = fut.result()
                            it.update(status="ok", md=r["md"], pages=r["pages"], secs=r["secs"],
                                      out_size=len(r["md"].encode("utf-8")))
                        except Exception as e:  # одна битая PDF не валит весь список
                            msg = str(e) or type(e).__name__
                            it.update(status="fail", note=msg.replace("\n", " ")[:160])
                        self._push("update", self._public(it))
                        nxt = next((x for x in todo if x["status"] == "waiting"), None)
                        if nxt:
                            nxt["status"] = "working"
                            self._push("update", self._public(nxt))
        except Exception as e:
            for it in todo:
                if it["status"] in ("waiting", "working"):
                    it.update(status="fail", note=f"{type(e).__name__}: {e}"[:160])
                    self._push("update", self._public(it))
        finally:
            self._busy = False
            self._push("finished", {"secs": round(time.perf_counter() - t0, 1)})

    def preview(self, item_id: str) -> str:
        it = self._items.get(item_id)
        return it["md"] if it and it["md"] else ""

    def save_one(self, item_id: str) -> str | None:
        import webview
        it = self._items.get(item_id)
        if not it or it["md"] is None:
            return None
        dst = self._dialog(webview.FileDialog.SAVE, save_filename=Path(it["name"]).stem + ".md")
        if not dst:
            return None
        Path(dst).write_text(it["md"], encoding="utf-8")
        self._last_dir = str(Path(dst).parent)
        return dst

    def save_all(self) -> dict | None:
        import webview
        done = self._done()
        if not done:
            return None
        folder = self._dialog(webview.FileDialog.FOLDER)
        if not folder:
            return None
        used: set[str] = set()
        for it in done:
            name = unique_name(Path(it["name"]).stem, used)
            (Path(folder) / name).write_text(it["md"], encoding="utf-8")
        self._last_dir = folder
        return {"path": folder, "count": len(done)}

    def save_zip(self) -> dict | None:
        import webview
        done = self._done()
        if not done:
            return None
        dst = self._dialog(webview.FileDialog.SAVE, save_filename="markdown.zip")
        if not dst:
            return None
        used: set[str] = set()
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
            for it in done:
                z.writestr(unique_name(Path(it["name"]).stem, used), it["md"])
        self._last_dir = str(Path(dst).parent)
        return {"path": dst, "count": len(done)}

    def reveal(self, path: str) -> None:
        """Показать файл/папку в проводнике или Finder."""
        p = Path(path)
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(p)] if p.is_file() else ["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])


def unique_name(stem: str, used: set[str]) -> str:
    name, n = f"{stem}.md", 2
    while name.lower() in used:
        name, n = f"{stem} ({n}).md", n + 1
    used.add(name.lower())
    return name


# ─────────────────────────────── запуск ───────────────────────────────


def on_start(window, api: Api, argv_paths: list[str]) -> None:
    """Drag & drop файлов/папок: полный путь отдаёт только pywebview на стороне Python."""
    from webview.dom import DOMEventHandler

    if argv_paths:                      # файлы, брошенные на иконку exe / «Открыть с помощью»
        api._push("added", api._add_paths(argv_paths))
        if os.environ.get("PDF2MD_AUTOCONVERT"):
            api.convert("pymupdf4llm", False)
            api._push("started", None)

    def on_drop(e):
        files = e.get("dataTransfer", {}).get("files", [])
        paths = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
        added = api._add_paths(paths)
        api._push("added", added)

    def noop(_):
        pass

    doc = window.dom.document
    doc.events.dragenter += DOMEventHandler(noop, True, True)
    doc.events.dragover += DOMEventHandler(noop, True, True, debounce=500)
    doc.events.drop += DOMEventHandler(on_drop, True, True)


def run() -> None:
    import webview

    api = Api()
    html = resource("ui/index.html").read_text(encoding="utf-8")
    window = webview.create_window(
        APP_NAME, html=html, js_api=api, width=900, height=680,
        min_size=(640, 480))
    api._window = window
    argv = [a for a in sys.argv[1:] if not a.startswith("-")]
    webview.start(on_start, (window, api, argv), debug=bool(os.environ.get("PDF2MD_DEBUG")))


if __name__ == "__main__":
    multiprocessing.freeze_support()   # обязательно для ProcessPool в exe/.app
    run()
