"""AI File Search - find any file or folder on your computer (C:, D:, anywhere).

Step 1  index    scan your drives once and save every file/folder name in a small database
Step 2  find     search by name (exact, partial or typo-tolerant)
Step 3  ask      describe the file in plain English; an AI model turns it into a search
Step 4  content  search inside documents (txt, md, csv, pdf, docx)

If you type a full path that exists, it simply confirms it.
Your files are never uploaded. In 'ask' mode only the sentence you type is sent to the AI
(and not at all with --provider mock or ollama running on your own laptop).
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
import string
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

DB_DEFAULT = str(Path.home() / ".ai_file_search.db")

# Folders skipped by default (system/cache folders that slow scanning). Use --include-system to scan them.
SKIP_DIRS = {"$recycle.bin", "system volume information", "windows", "program files",
             "program files (x86)", "programdata", "node_modules", ".git", "__pycache__",
             "venv", ".venv", "proc", "sys", "dev"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".html", ".xml", ".sql"}
CONTENT_EXT = TEXT_EXT | {".pdf", ".docx"}
STOPWORDS = {"my", "the", "a", "an", "file", "files", "document", "documents", "folder", "directory",
             "find", "search", "where", "is", "are", "named", "called", "please", "in", "on", "from",
             "of", "for", "to", "me", "i", "with", "name", "that", "this", "year", "last"}
EXT_WORDS = {"pdf": [".pdf"], "word": [".docx", ".doc"], "docx": [".docx"], "excel": [".xlsx", ".xls", ".csv"],
             "xlsx": [".xlsx"], "csv": [".csv"], "powerpoint": [".pptx", ".ppt"], "ppt": [".pptx", ".ppt"],
             "pptx": [".pptx"], "text": [".txt"], "txt": [".txt"], "image": [".jpg", ".jpeg", ".png"],
             "photo": [".jpg", ".jpeg", ".png"], "zip": [".zip"], "python": [".py"]}


# ------------------------------------------------------------------ drives and index
def find_roots() -> list[str]:
    if os.name == "nt":  # Windows: C:\, D:\, E:\ ...
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    return [str(Path.home())]  # Mac/Linux: your home folder (use --roots / for everything)


def connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        name TEXT, name_lower TEXT, path TEXT, is_dir INTEGER, ext TEXT, size INTEGER, mtime REAL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_name ON files(name_lower)")
    return conn


def build_index(db: str, roots: list[str], include_system: bool = False) -> None:
    conn = connect(db)
    conn.execute("DELETE FROM files")
    total, batch, started = 0, [], time.time()
    stack = list(roots)
    print("Scanning:", ", ".join(roots))
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if is_dir and not include_system and entry.name.lower() in SKIP_DIRS:
                            continue
                        st = entry.stat(follow_symlinks=False)
                        batch.append((entry.name, entry.name.lower(), entry.path, int(is_dir),
                                      os.path.splitext(entry.name)[1].lower(), st.st_size, st.st_mtime))
                        if is_dir:
                            stack.append(entry.path)
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            continue  # folder we are not allowed to open - skip it
        if len(batch) >= 5000:
            conn.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?)", batch)
            total += len(batch)
            batch.clear()
            print(f"  indexed {total:,} items...", end="\r")
    conn.executemany("INSERT INTO files VALUES (?,?,?,?,?,?,?)", batch)
    total += len(batch)
    conn.commit()
    conn.close()
    print(f"\nDone. {total:,} items indexed in {time.time() - started:.0f}s -> {db}")


# ------------------------------------------------------------------ name search
def score(query: str, name: str) -> float:
    q, n = query.lower(), name.lower()
    tokens = [t for t in re.split(r"[\s_\-\.]+", q) if t]
    if n == q:
        return 1.0
    best = difflib.SequenceMatcher(None, q, n).ratio()
    if q in n:
        best = max(best, 0.9)
    if tokens:
        best = max(best, 0.85 * sum(t in n for t in tokens) / len(tokens))
    return best


def search_names(conn, query: str, kind: str = "any", exts=None, min_score: float = 0.6, limit: int = 20):
    tokens = [t for t in re.split(r"[\s_\-]+", query.lower()) if t]
    where, params = [], []
    if kind == "file":
        where.append("is_dir = 0")
    elif kind == "folder":
        where.append("is_dir = 1")
    if exts:
        where.append("ext IN (%s)" % ",".join("?" * len(exts)))
        params += list(exts)
    base = ("WHERE " + " AND ".join(where) + " AND ") if where else "WHERE "
    like = " OR ".join("name_lower LIKE ?" for _ in tokens) or "1=1"
    rows = conn.execute(f"SELECT name,path,is_dir,size,mtime FROM files {base}({like}) LIMIT 20000",
                        params + [f"%{t}%" for t in tokens]).fetchall()
    if not rows:  # typo-tolerant fallback
        pool = [r[0] for r in conn.execute("SELECT DISTINCT name_lower FROM files")]
        close = difflib.get_close_matches(query.lower(), pool, n=10, cutoff=0.7)
        if close:
            rows = conn.execute(f"SELECT name,path,is_dir,size,mtime FROM files {base}name_lower IN ({','.join('?' * len(close))})",
                                params + close).fetchall()
    scored = sorted(((score(query, r[0]), r) for r in rows), key=lambda x: -x[0])
    return [(s, r) for s, r in scored if s >= min_score][:limit]


def show(results) -> bool:
    shown = 0
    for s, (name, path, is_dir, size, mtime) in results:
        if not os.path.exists(path):  # index may be old
            continue
        shown += 1
        kind = "FOLDER" if is_dir else "FILE  "
        info = "" if is_dir else f" | {size / 1024:,.0f} KB"
        when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  [{kind}] {path}\n           match {s:.0%}{info} | modified {when}")
    return shown > 0


def check_exact_path(text: str) -> bool:
    candidate = text.strip().strip('"')
    if os.path.sep in candidate or (os.name == "nt" and ":" in candidate):
        if os.path.exists(candidate):
            kind = "folder" if os.path.isdir(candidate) else "file"
            print(f"FOUND: that {kind} exists at {os.path.abspath(candidate)}")
            return True
    return False


def do_find(conn, query, kind="any", exts=None):
    if check_exact_path(query):
        return True
    results = search_names(conn, query, kind, exts)
    if results and show(results):
        print("RESULT: found.")
        return True
    print("RESULT: not found. (If the file is new, re-run the index command.)")
    return False


# ------------------------------------------------------------------ AI query understanding
PROMPT = """Turn the user's request into search settings for a file finder.
Return ONLY JSON: {{"keywords": ["..."], "extensions": [".pdf"], "type": "file|folder|any"}}
keywords = words likely to be in the file or folder name. extensions = file types, or [] if unknown.
Request: {q}"""


def mock_parse(q: str) -> dict:
    words = re.findall(r"[\w\.]+", q.lower())
    exts, kind = [], "any"
    for w in words:
        exts += EXT_WORDS.get(w, [])
    if any(w in {"folder", "directory"} for w in words):
        kind = "folder"
    keywords = [w for w in words if w not in STOPWORDS and w not in EXT_WORDS]
    return {"keywords": keywords, "extensions": sorted(set(exts)), "type": kind}


def ask_ollama(prompt: str) -> str:
    import requests
    r = requests.post(os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat"),
                      json={"model": os.getenv("OLLAMA_MODEL", "llama3.2"), "stream": False,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=300)
    r.raise_for_status()
    return r.json()["message"]["content"]


def ask_anthropic(prompt: str) -> str:
    from anthropic import Anthropic
    msg = Anthropic().messages.create(model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"), max_tokens=300,
                                      messages=[{"role": "user", "content": prompt}])
    return msg.content[0].text


def understand(q: str, provider: str) -> dict:
    if provider == "mock":
        return mock_parse(q)
    text = {"ollama": ask_ollama, "anthropic": ask_anthropic}[provider](PROMPT.format(q=q))
    try:
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return {"keywords": [str(k) for k in data.get("keywords", [])],
                "extensions": [str(e).lower() for e in data.get("extensions", [])],
                "type": data.get("type", "any") if data.get("type") in {"file", "folder", "any"} else "any"}
    except (ValueError, TypeError):
        print("AI reply was not valid JSON; using the simple built-in parser instead.")
        return mock_parse(q)


def do_ask(conn, q, provider):
    plan = understand(q, provider)
    print(f"Understood as: {plan}")
    if not plan["keywords"] and not plan["extensions"]:
        print("RESULT: I could not work out what to search for. Try adding a name.")
        return False
    return do_find(conn, " ".join(plan["keywords"]) or "*", plan["type"], plan["extensions"] or None) \
        if plan["keywords"] else do_ext_only(conn, plan)


def do_ext_only(conn, plan):
    rows = conn.execute("SELECT name,path,is_dir,size,mtime FROM files WHERE ext IN (%s) ORDER BY mtime DESC LIMIT 20"
                        % ",".join("?" * len(plan["extensions"])), plan["extensions"]).fetchall()
    ok = show([(1.0, r) for r in rows])
    print("RESULT: found (newest first)." if ok else "RESULT: not found.")
    return ok


# ------------------------------------------------------------------ content search
def read_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
        return re.sub(r"<[^>]+>", " ", xml)
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    return ""


def do_content(conn, phrase: str, under: str | None, max_mb: int = 20):
    q = "SELECT path,size FROM files WHERE is_dir = 0 AND ext IN (%s)" % ",".join("?" * len(CONTENT_EXT))
    params = list(CONTENT_EXT)
    if under:
        q += " AND path LIKE ?"
        params.append(under.rstrip("\\/") + "%")
    hits, checked = 0, 0
    for path, size in conn.execute(q, params):
        if size > max_mb * 1024 * 1024 or not os.path.exists(path):
            continue
        checked += 1
        try:
            if phrase.lower() in read_text(path).lower():
                hits += 1
                print(f"  [CONTENT MATCH] {path}")
        except Exception:
            continue  # unreadable or protected file
    print(f"RESULT: {'found in ' + str(hits) + ' file(s)' if hits else 'not found'} ({checked:,} documents checked).")


# ------------------------------------------------------------------ command line
def interactive(conn, provider):
    print("AI File Search. Type a file/folder name, a full path, or 'ask <sentence>'.")
    print("Other commands: 'content <text>' to search inside documents, 'exit' to quit.")
    while True:
        text = input("\nSearch> ").strip()
        if text.lower() in {"exit", "quit", ""}:
            break
        if text.lower().startswith("ask "):
            do_ask(conn, text[4:], provider)
        elif text.lower().startswith("content "):
            do_content(conn, text[8:], None)
        else:
            do_find(conn, text)


def main():
    ap = argparse.ArgumentParser(description="Find files and folders anywhere on your computer.")
    ap.add_argument("--db", default=DB_DEFAULT, help="index database location")
    ap.add_argument("--provider", choices=["mock", "ollama", "anthropic"], default="mock")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("index", help="scan drives and build the index")
    p.add_argument("--roots", nargs="*", help='e.g. C:\\ D:\\  (default: all drives)')
    p.add_argument("--include-system", action="store_true")
    p = sub.add_parser("find", help="search by file or folder name")
    p.add_argument("query")
    p.add_argument("--type", choices=["any", "file", "folder"], default="any")
    p.add_argument("--ext", nargs="*", help="limit to extensions, e.g. .pdf .docx")
    p = sub.add_parser("ask", help="describe it in plain English")
    p.add_argument("sentence")
    p = sub.add_parser("content", help="search inside documents")
    p.add_argument("phrase")
    p.add_argument("--under", help="only search under this folder")
    args = ap.parse_args()

    if args.cmd == "index":
        build_index(args.db, args.roots or find_roots(), args.include_system)
        return
    if not os.path.exists(args.db):
        sys.exit("No index yet. First run:  python ai_file_search.py index")
    conn = connect(args.db)
    if args.cmd == "find":
        sys.exit(0 if do_find(conn, args.query, args.type, args.ext) else 1)
    elif args.cmd == "ask":
        sys.exit(0 if do_ask(conn, args.sentence, args.provider) else 1)
    elif args.cmd == "content":
        do_content(conn, args.phrase, args.under)
    else:
        interactive(conn, args.provider)


if __name__ == "__main__":
    main()
