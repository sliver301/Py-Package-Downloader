# Package Downloader - Python packages for PCs without internet
# Copyright (C) 2026 Package Downloader contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# This program is free software: you can redistribute it and/or modify it under the terms of
# the GNU General Public License as published by the Free Software Foundation, either version 3
# of the License, or (at your option) any later version. See the LICENSE file.

"""
Package Downloader - get Python packages onto a PC that has no internet.

Run it on a PC WITH internet. Pick packages (most downloaded, search PyPI, or scan a
.py / .bat / .ps1 / requirements file), and it downloads them with all dependencies for
the offline PC's Python and writes "<name> - Needed Packages Install.exe". Copy that exe to
the offline PC and run it: it finds Python there and installs everything without internet.

The logic uses only the Python standard library. The windows (package_downloader_ui.py) use
PySide6. pip and PySide6 are bundled into the .exe when it's built.

The same .exe works in three ways:
  * normal start              -> the downloader window
  * a copy with packages      -> the offline installer (the packages are appended to it)
  * "--pip ..."               -> runs the bundled pip (used internally)
"""

import ast
import datetime as _dt
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
# Python 3.15 imports concurrent.futures' classes lazily; in the built exe that left a stand-in object
# instead of the real class ('lazy_import' object is not callable). Importing the submodule avoids it.
from concurrent.futures.thread import ThreadPoolExecutor
if not isinstance(ThreadPoolExecutor, type):      # still a lazy stand-in: load the real class
    import importlib
    ThreadPoolExecutor = importlib.import_module("concurrent.futures.thread").ThreadPoolExecutor

APP_NAME = "Package Downloader"
APP_VERSION = "4.1.0"
INSTALL_SUFFIX = " - Needed Packages Install"
FROZEN = bool(getattr(sys, "frozen", False))
IS_WIN = os.name == "nt"

# Installer payload: <exe bytes><zip bytes XOR 0x5A><MAGIC 16 bytes><zip length uint64>.
# The XOR keeps PyInstaller's own archive marker from ever showing up inside the payload.
PAYLOAD_MAGIC = b"NPI-PAYLOAD-v1\x00\x00"
FOOTER_LEN = len(PAYLOAD_MAGIC) + 8
_XOR = bytes(b ^ 0x5A for b in range(256))
CHUNK = 4 * 1024 * 1024

UA = "%s/%s (offline package helper)" % (APP_NAME.replace(" ", "-"), APP_VERSION)
TOP_URLS = [
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json",
    "https://hugovk.github.io/top-pypi-packages/top-pypi-packages-30-days.min.json",
    "https://raw.githubusercontent.com/hugovk/top-pypi-packages/main/top-pypi-packages.min.json",
]


def app_dir():
    """Folder holding the .exe (or this .py)."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    base = getattr(sys, "_MEIPASS", None) or app_dir()
    return os.path.join(base, name)


def data_dir():
    if IS_WIN:
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        root = os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(root, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def canon(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def safe_filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "Python Packages"


def build_info():
    """Python version / platform of the PC the .exe was built on (the offline PC)."""
    info = {"python": "%d.%d" % sys.version_info[:2], "platform": "win_amd64", "from_build": False}
    for p in (resource_path("build_info.json"), os.path.join(app_dir(), "build_info.json")):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            info.update({k: d[k] for k in ("python", "platform") if d.get(k)})
            info["from_build"] = True
            break
        except Exception:
            pass
    return info


# =========================================================================================
#  Payload (appended zip) helpers
# =========================================================================================

def read_footer(path):
    """Return (payload_start, payload_len) if the file carries a payload, else None.
    The footer is normally the last 24 bytes; the last 1 MB is searched too, in case
    something (a signing tool, a copy program) added bytes after it."""
    try:
        size = os.path.getsize(path)
        if size < FOOTER_LEN:
            return None
        look = min(size, 1024 * 1024)
        with open(path, "rb") as f:
            f.seek(size - look)
            tail = f.read(look)
        pos = tail.rfind(PAYLOAD_MAGIC)
        while pos >= 0:
            if pos + FOOTER_LEN <= len(tail):
                (n,) = struct.unpack("<Q", tail[pos + len(PAYLOAD_MAGIC): pos + FOOTER_LEN])
                end = size - look + pos            # where the payload ends
                start = end - n
                if 0 < start < end:
                    return start, n
            pos = tail.rfind(PAYLOAD_MAGIC, 0, pos)
        return None
    except OSError:
        return None


def looks_like_installer(path):
    return INSTALL_SUFFIX.strip().lower() in os.path.basename(path).lower()


def write_installer(stub_path, zip_path, out_path, on_bytes=None):
    """Copy the stub exe (without any payload it may carry) and append the zip."""
    stub_len = os.path.getsize(stub_path)
    ft = read_footer(stub_path)
    if ft:
        stub_len = ft[0]
    tmp = out_path + ".part"
    with open(tmp, "wb") as out:
        with open(stub_path, "rb") as f:
            left = stub_len
            while left:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                out.write(b)
                left -= len(b)
        n = 0
        with open(zip_path, "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                out.write(b.translate(_XOR))
                n += len(b)
                if on_bytes:
                    on_bytes(len(b))
        out.write(PAYLOAD_MAGIC + struct.pack("<Q", n))
    if os.path.exists(out_path):
        os.remove(out_path)
    os.replace(tmp, out_path)


def extract_payload(path, dest):
    start, n = read_footer(path)
    zpath = os.path.join(dest, "payload.zip")
    with open(path, "rb") as f, open(zpath, "wb") as out:
        f.seek(start)
        left = n
        while left:
            b = f.read(min(CHUNK, left))
            if not b:
                raise IOError("The installer file is damaged (payload is cut short).")
            out.write(b.translate(_XOR))
            left -= len(b)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest)
    os.remove(zpath)


# =========================================================================================
#  pip runner
# =========================================================================================

def run_bundled_pip(args):
    """'Package Downloader.exe --pip ...' : run the pip that was bundled into the exe."""
    os.environ.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.stdout is None or sys.stderr is None:
        # windowed build started without usable pipes: write pip's output to a file instead
        out = os.environ.get("PD_PIP_OUT")
        f = open(out, "w", encoding="utf-8", buffering=1) if out else open(os.devnull, "w")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    try:
        from pip._internal.cli.main import main as pip_main
    except Exception as e:  # pragma: no cover
        print("pip is not available in this build: %s" % e)
        return 3
    return pip_main(args) or 0


def pip_base_cmd():
    if FROZEN:
        return [sys.executable, "--pip"]
    return [sys.executable, "-m", "pip"]


def pip_available():
    try:
        r = subprocess.run(pip_base_cmd() + ["--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=120, **_no_window())
        return r.returncode == 0, (r.stdout or r.stderr).strip()
    except Exception as e:
        return False, str(e)


def _no_window():
    if IS_WIN:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


# =========================================================================================
#  PyPI data (popular list, search, package info)
# =========================================================================================

FALLBACK_POPULAR = """boto3 urllib3 botocore requests setuptools certifi idna charset-normalizer
typing-extensions python-dateutil packaging s3transfer six aiobotocore numpy pyyaml grpcio-status
pip fsspec s3fs cryptography pydantic cffi attrs pycparser pandas google-api-core pydantic-core
jmespath protobuf markupsafe jinja2 rsa click pyasn1 importlib-metadata platformdirs zipp pytz
wheel pyjwt googleapis-common-protos colorama filelock pluggy cachetools tomli virtualenv
pyasn1-modules google-auth jsonschema wrapt pytest sqlalchemy pyarrow psutil openpyxl
pillow requests-oauthlib aiohttp tzdata docutils pygments exceptiongroup iniconfig lxml
beautifulsoup4 soupsieve tqdm scipy greenlet multidict yarl frozenlist aiosignal werkzeug
decorator isodate grpcio httpx httpcore h11 anyio sniffio matplotlib kiwisolver fonttools
contourpy cycler pyparsing python-dotenv flask itsdangerous blinker rich markdown-it-py mdurl
pyodbc xlsxwriter xlrd python-docx python-pptx pymupdf pypdf pdfplumber reportlab opencv-python
scikit-learn joblib threadpoolctl seaborn plotly selenium pyautogui pywin32 pyperclip
keyboard pynput pyinstaller pyserial paramiko pymysql psycopg2-binary redis pymongo
fastapi uvicorn starlette django streamlit openai anthropic tabulate termcolor schedule
watchdog ezdxf shapely pyproj networkx sympy xlwings pythonnet comtypes customtkinter
ttkbootstrap pystray qrcode pyttsx3 pytesseract""".split()


class PyPI:
    def __init__(self):
        self.lock = threading.Lock()
        self.top = None          # list of (name, downloads)
        self.top_rank = {}       # canon name -> (rank, downloads)
        self.top_date = ""
        self.all_names = None    # list of all project names on PyPI
        self.info_cache = {}
        self.cache = data_dir()

    # ---- http ----
    @staticmethod
    def get(url, accept=None, timeout=30):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        if accept:
            req.add_header("Accept", accept)
        req.add_header("Accept-Encoding", "gzip")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                data = gzip.decompress(data)
            return data

    def _cached(self, fname, max_age_h):
        p = os.path.join(self.cache, fname)
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < max_age_h * 3600:
            return p
        return None

    # ---- popular ----
    def load_top(self):
        with self.lock:
            if self.top is not None:
                return
            p = os.path.join(self.cache, "top-packages.json")
            data = None
            if not self._cached("top-packages.json", 72):
                for u in TOP_URLS:
                    try:
                        raw = self.get(u, timeout=40)
                        data = json.loads(raw)
                        with open(p, "wb") as f:
                            f.write(raw)
                        break
                    except Exception:
                        continue
            if data is None and os.path.exists(p):
                try:
                    with open(p, "rb") as f:
                        data = json.loads(f.read())
                except Exception:
                    data = None
            rows = []
            if data:
                for r in data.get("rows", []):
                    rows.append((r.get("project"), int(r.get("download_count") or 0)))
                self.top_date = data.get("last_update", "")
            if not rows:
                rows = [(n, 0) for n in FALLBACK_POPULAR]
                self.top_date = "built-in list (PyPI stats could not be reached)"
            self.top = rows
            self.top_rank = {canon(n): (i + 1, d) for i, (n, d) in enumerate(rows)}

    def popular(self, offset, limit, q):
        self.load_top()
        rows = self.top
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in r[0].lower()]
        out = [{"name": n, "downloads": d, "rank": self.top_rank[canon(n)][0]}
               for n, d in rows[offset: offset + limit]]
        return {"rows": out, "total": len(rows), "updated": self.top_date}

    # ---- search ----
    def load_all_names(self):
        with self.lock:
            if self.all_names is not None:
                return
            p = os.path.join(self.cache, "all-names.txt")
            if not self._cached("all-names.txt", 24 * 7):
                try:
                    raw = self.get("https://pypi.org/simple/",
                                   accept="application/vnd.pypi.simple.v1+json", timeout=120)
                    names = [x["name"] for x in json.loads(raw)["projects"]]
                    with open(p, "w", encoding="utf-8") as f:
                        f.write("\n".join(names))
                except Exception:
                    pass
            names = []
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    names = f.read().split("\n")
            self.all_names = names

    def search(self, q):
        q = q.strip()
        if not q:
            return []
        self.load_top()
        self.load_all_names()
        qc = canon(q)
        ql = q.lower()
        pool = self.all_names or [n for n, _ in self.top]
        hits = []
        for n in pool:
            nl = n.lower()
            if ql in nl or qc in canon(n):
                hits.append(n)
        def score(n):
            c = canon(n)
            rank = self.top_rank.get(c, (10 ** 7, 0))[0]
            exact = 0 if c == qc else (1 if c.startswith(qc) else 2)
            return (exact, rank, len(n))
        hits.sort(key=score)
        res = []
        for n in hits[:60]:
            r = self.top_rank.get(canon(n))
            res.append({"name": n, "downloads": r[1] if r else 0, "rank": r[0] if r else None})
        if not res or canon(res[0]["name"]) != qc:
            # the name list may be stale or missing - ask PyPI directly for an exact match
            info = self.info(q)
            if info.get("exists"):
                res.insert(0, {"name": info["name"], "downloads": 0, "rank": None})
        return res

    # ---- one package ----
    def info(self, name):
        key = canon(name)
        if key in self.info_cache:
            return self.info_cache[key]
        try:
            raw = self.get("https://pypi.org/pypi/%s/json" % urllib.parse.quote(name), timeout=30)
            d = json.loads(raw)
        except urllib.error.HTTPError as e:
            res = {"name": name, "exists": False, "error": "Not found on PyPI" if e.code == 404 else str(e)}
            if e.code == 404:
                self.info_cache[key] = res
            return res
        except Exception as e:
            return {"name": name, "exists": False, "error": "Could not reach PyPI: %s" % e}
        inf = d.get("info", {})
        files = d.get("urls", [])
        wheels = [f["filename"] for f in files if f.get("packagetype") == "bdist_wheel"]
        kind = "source"
        if any(w.endswith("-none-any.whl") for w in wheels):
            kind = "pure"
        elif any("win_amd64" in w or "win32" in w for w in wheels):
            kind = "windows"
        elif wheels:
            kind = "other"
        rels = d.get("releases", {})
        versions = [v for v, fl in rels.items() if fl and not all(f.get("yanked") for f in fl)]
        versions.sort(key=_vkey, reverse=True)
        res = {
            "name": inf.get("name") or name,
            "exists": True,
            "summary": (inf.get("summary") or "").strip(),
            "version": inf.get("version"),
            "versions": versions[:80],
            "requires_python": inf.get("requires_python") or "",
            "kind": kind,
            "wheels": wheels[:200],
            "url": inf.get("project_url") or inf.get("package_url") or "https://pypi.org/project/%s/" % name,
        }
        r = self.top_rank.get(key)
        if r:
            res["rank"], res["downloads"] = r
        self.info_cache[key] = res
        return res


def _vkey(v):
    parts = []
    for p in re.split(r"[.+-]", v):
        m = re.match(r"(\d+)(.*)", p)
        if m:
            parts.append((1, int(m.group(1)), 0 if m.group(2) else 1, m.group(2)))
        else:
            parts.append((0, 0, 0, p))
    return parts


# =========================================================================================
#  Scanning a file for the packages it needs
# =========================================================================================

IMPORT_TO_PIP = {
    "cv2": "opencv-python", "PIL": "Pillow", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "docx": "python-docx", "pptx": "python-pptx",
    "dateutil": "python-dateutil", "dotenv": "python-dotenv", "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodomex", "OpenSSL": "pyOpenSSL", "jwt": "PyJWT", "magic": "python-magic",
    "serial": "pyserial", "usb": "pyusb", "fitz": "PyMuPDF", "pymupdf": "PyMuPDF", "attr": "attrs",
    "MySQLdb": "mysqlclient", "psycopg2": "psycopg2-binary", "mysql": "mysql-connector-python",
    "pymysql": "PyMySQL", "telegram": "python-telegram-bot", "discord": "discord.py",
    "git": "GitPython", "github": "PyGithub", "Levenshtein": "python-Levenshtein",
    "nacl": "PyNaCl", "pkg_resources": "setuptools", "OpenGL": "PyOpenGL", "wx": "wxPython",
    "gi": "PyGObject", "qtpy": "QtPy", "kivy": "Kivy", "zmq": "pyzmq", "ldap": "python-ldap",
    "websocket": "websocket-client", "socketio": "python-socketio", "engineio": "python-engineio",
    "multipart": "python-multipart", "jose": "python-jose", "slugify": "python-slugify",
    "Bio": "biopython", "mpl_toolkits": "matplotlib", "pylab": "matplotlib",
    "xlsxwriter": "XlsxWriter", "pyautogui": "PyAutoGUI", "wmi": "WMI",
    "speech_recognition": "SpeechRecognition", "gtts": "gTTS", "pyaudio": "PyAudio",
    "vlc": "python-vlc", "barcode": "python-barcode", "fpdf": "fpdf2", "camelot": "camelot-py",
    "tabula": "tabula-py", "webdriver_manager": "webdriver-manager", "scrapy": "Scrapy",
    "flask": "Flask", "jinja2": "Jinja2", "markupsafe": "MarkupSafe", "werkzeug": "Werkzeug",
    "django": "Django", "rest_framework": "djangorestframework", "babel": "Babel",
    "charset_normalizer": "charset-normalizer", "smb": "pysmb", "cx_Oracle": "cx-Oracle",
    "clr": "pythonnet", "apscheduler": "APScheduler", "exifread": "ExifRead",
    "pillow_heif": "pillow-heif", "cairosvg": "CairoSVG", "pygments": "Pygments",
    "pymdownx": "pymdown-extensions", "markdown": "Markdown", "Cython": "Cython",
    "PyInstaller": "pyinstaller", "nuitka": "Nuitka", "cx_Freeze": "cx-Freeze", "faker": "Faker",
    "IPython": "ipython", "tables": "tables", "webview": "pywebview", "eel": "Eel",
    "pyvisa": "PyVISA", "can": "python-can", "snap7": "python-snap7", "paho": "paho-mqtt",
    "googleapiclient": "google-api-python-client", "google_auth_oauthlib": "google-auth-oauthlib",
    "osgeo": "GDAL", "sqlalchemy": "SQLAlchemy", "pyqtgraph": "pyqtgraph", "tkcalendar": "tkcalendar",
    "dns": "dnspython", "lz4": "lz4", "snappy": "python-snappy", "zstandard": "zstandard",
    "ruamel": "ruamel.yaml", "toml": "toml", "simple_salesforce": "simple-salesforce",
    "mpl_interactions": "mpl-interactions", "pythoncom": "pywin32", "pywintypes": "pywin32",
    "winerror": "pywin32", "adodbapi": "pywin32", "win32com": "pywin32", "win32comext": "pywin32",
    "pywin": "pywin32", "win32ui": "pywin32", "dde": "pywin32", "pyrevit": None, "Autodesk": None,
    "rhinoscriptsyntax": None, "Rhino": None, "System": None, "RevitServices": None,
    "RevitAPI": None, "RevitAPIUI": None, "ProtoGeometry": None, "DSCore": None,
    "Grasshopper": None, "scriptcontext": None, "arcpy": None, "bpy": None, "maya": None,
}
GOOGLE_SUB = {
    "protobuf": "protobuf", "auth": "google-auth", "generativeai": "google-generativeai",
    "genai": "google-genai", "oauth2": "google-auth", "api_core": "google-api-core",
}

FALLBACK_STDLIB = set("""__future__ abc aifc argparse array ast asynchat asyncio asyncore atexit audioop
base64 bdb binascii bisect builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop
collections colorsys compileall concurrent configparser contextlib contextvars copy copyreg cProfile
crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils doctest email encodings
ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch fractions ftplib functools gc
getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http idlelib imaplib imghdr imp
importlib inspect io ipaddress itertools json keyword lib2to3 linecache locale logging lzma mailbox
mailcap marshal math mimetypes mmap modulefinder msilib msvcrt multiprocessing netrc nis nntplib
numbers operator optparse os ossaudiodev pathlib pdb pickle pickletools pipes pkgutil platform plistlib
poplib posix posixpath pprint profile pstats pty pwd py_compile pyclbr pydoc queue quopri random re
readline reprlib resource rlcompleter runpy sched secrets select selectors shelve shlex shutil signal
site smtpd smtplib sndhdr socket socketserver spwd sqlite3 ssl stat statistics string stringprep
struct subprocess sunau symtable sys sysconfig syslog tabnanny tarfile telnetlib tempfile termios
textwrap threading time timeit tkinter token tokenize tomllib trace traceback tracemalloc tty turtle
turtledemo types typing unicodedata unittest urllib uu uuid venv warnings wave weakref webbrowser
winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo nt ntpath
_thread _winapi""".split())
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | FALLBACK_STDLIB

REQ_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(\[[A-Za-z0-9,._ -]*\])?"
    r"\s*((===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+(\s*,\s*(===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_-]+)*)?$")
PIP_OPTS_WITH_VALUE = {
    "-r", "--requirement", "-c", "--constraint", "-e", "--editable", "-i", "--index-url",
    "--extra-index-url", "-f", "--find-links", "-t", "--target", "--prefix", "--root",
    "--trusted-host", "--proxy", "--python-version", "--platform", "--only-binary",
    "--no-binary", "--upgrade-strategy", "--implementation", "--abi", "--cert",
    "--client-cert", "--cache-dir", "--src", "--log", "--timeout", "--retries",
    "--progress-bar", "--python", "--config-settings", "-C", "--global-option",
}
PIP_LINE_RE = re.compile(r"(?i)(?:\bpip3?(?:\.exe)?|-m\s+pip|\buv\s+pip)[\"']?\s+install\b(.*)")
SHELL_STOP = {"&&", "||", "|", ";", "&", ">", ">>", "2>", "2>&1", ")", "`", "}"}


def _split_cmd(s):
    toks = re.findall(r'"[^"]*"|\'[^\']*\'|[^\s]+', s)
    out = []
    for t in toks:
        if t[:1] in "\"'" and t[-1:] == t[:1]:
            t = t[1:-1]
        out.append(t)
    return out


def _req_file_name(t):
    """'%~dp0requirements.txt' -> 'requirements.txt' (just the file name, without script variables)."""
    t = re.sub(r"%~[a-zA-Z]*\d|%\w+%|\$\{?\w+\}?|\{[^}]*\}", "", t)
    return os.path.basename(t.replace("\\", "/")) or t


_COMMENT_START = re.compile(r"(?i)^\s*(?:@?echo(?:\.|\s|$)|rem(?:\s|$)|::|#+|//|write-(?:host|output)\b|print\b)")
_PY_EXE_END = re.compile(r"""(?ix)(?:^|[\s;&|(])(?:"[^"]*"|'[^']*'|[^\s"']*python[\w.]*|py|%\w+%|\$\{?\w+\}?|\{[^}]*\})
                              (?:\s+-[\w.]+)*\s*$""")
_LEAD_WORDS = re.compile(r"(?i)(?:^|[.:;!?,(\-*>]\s*)(?:please\s+)?(?:run|try|use|type|execute|with|do|install|command|cmd|then|or|and)\s*:?\s*$")


def pip_command_rest(line, code=False):
    """The text after 'pip install' when the line really is an install command, else None.

    code=True: a line of a .bat/.ps1/.sh that runs (anything may come before the command).
    Otherwise (comments, docstrings, messages, echo lines) the command must start the text, follow a
    ':', '$' or '>' prompt, a python path ('python -m pip install', '"%PY%" -m pip install'), or a
    word like 'Run' / 'try' - so a sentence such as "you can pip install anything" is ignored."""
    m = PIP_LINE_RE.search(line)
    if not m:
        return None
    before = line[:m.start()]
    comment = _COMMENT_START.match(before)
    if code and not comment and not _COMMENT_START.match(line):
        return m.group(1)
    if comment:
        before = before[comment.end():]
    before = before.strip()
    matched = m.group(0)[:len(m.group(0)) - len(m.group(1))]
    if not before or before[-1] in ":$>!%(`'\"&|;" or _LEAD_WORDS.search(before):
        return m.group(1)
    if re.search(r"(?i)-m\s+pip", matched) and _PY_EXE_END.search(before + " "):
        return m.group(1)
    return None


def pip_line_packages(rest):
    """Packages named after 'pip install' on one line.

    All or nothing: if anything after 'pip install' isn't an option, a path or a valid package name,
    the line is ordinary text that happens to contain the words "pip install" (a sentence, a label in
    code...), so nothing is taken from it."""
    pkgs, notes = [], []
    rest = rest.rstrip()
    if rest[:1] in ("'", '"', "`", ")", ",", ".", ":") or not rest[:1].isspace() and rest:
        return [], []          # "pip install" was quoted or part of a sentence: 'pip install' on one line
    toks = _split_cmd(rest)
    skip = None
    for n, t in enumerate(toks):
        if skip:
            if skip == "req":
                notes.append("installs from %s (add that file too)" % _req_file_name(t))
            skip = None
            continue
        if t in SHELL_STOP or t.startswith(("&&", "||", ">", "|", "#", "REM ", "rem ", "::", "2>")):
            break
        last = n == len(toks) - 1
        tt = t.rstrip(";)&|`,\"'")
        if last:
            tt = tt.rstrip(".")   # "... pip install requests." at the end of a sentence
        if not tt:
            continue
        if tt.startswith("-"):
            opt = tt.split("=", 1)[0]
            if opt in ("-r", "--requirement") and "=" not in tt:
                skip = "req"
            elif opt in PIP_OPTS_WITH_VALUE and "=" not in tt:
                skip = "opt"
            continue
        if tt in (".", "..") or any(c in tt for c in "%$\\/{}") or tt.endswith((".whl", ".zip", ".tar.gz")):
            continue           # a folder, a file or a script variable
        if not REQ_RE.match(tt):
            return [], []      # ordinary text, not a command
        if canon(re.split(r"[\[<>=!~]", tt, 1)[0]) not in ("pip", "setuptools", "wheel"):
            pkgs.append(tt)
    return pkgs, notes


# Calls whose text is usually a real command or an instruction for the user:
# os.system("pip install x"), print("Run: pip install x"), raise ImportError("pip install x") ...
_CMD_CALL_WORDS = ("system", "popen", "run", "call", "check", "spawn", "exec", "shell", "print", "write",
                   "warn", "error", "info", "message", "msg", "exit", "critical", "fatal", "alert", "show")


def _py_pip_texts(src, tree):
    """(text, line) pairs in Python code that may hold an install command: comments, docstrings and
    the text given to commands/messages. Plain strings elsewhere (labels, dictionary keys...) are skipped."""
    import io
    import tokenize
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.string.lstrip("#").strip(), tok.start[0]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        for i, ln in enumerate(src.splitlines(), 1):
            if ln.lstrip().startswith("#"):
                out.append((ln.lstrip().lstrip("#").strip(), i))
    if tree is None:
        return out

    def strings_in(node):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub

    def add_str(c):
        for k, ln in enumerate(c.value.splitlines()):
            out.append((ln.strip(), c.lineno + k))

    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            add_str(node.value)                       # docstrings / bare text blocks
        elif isinstance(node, ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else "").lower()
            if any(w in name for w in _CMD_CALL_WORDS) or name.endswith(("error", "exception", "warning")):
                for a in list(node.args) + [k.value for k in node.keywords]:
                    for c in strings_in(a):
                        add_str(c)
    return out


def _import_roots(tree):
    found = []  # (module path, line, optional)

    class V(ast.NodeVisitor):
        def __init__(self):
            self.try_depth = 0

        def visit_Try(self, node):
            handles_import_error = any(
                isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError", "Exception")
                or h.type is None
                or isinstance(h.type, ast.Tuple) and any(isinstance(e, ast.Name) and e.id in ("ImportError", "ModuleNotFoundError") for e in h.type.elts)
                for h in node.handlers)
            if handles_import_error:
                self.try_depth += 1
                for n in node.body:
                    self.visit(n)
                self.try_depth -= 1
                for n in node.handlers + node.orelse + node.finalbody:
                    self.visit(n)
            else:
                self.generic_visit(node)
        visit_TryStar = visit_Try

        def visit_Import(self, node):
            for a in node.names:
                found.append((a.name, node.lineno, self.try_depth > 0))

        def visit_ImportFrom(self, node):
            if node.level == 0 and node.module:
                found.append((node.module, node.lineno, self.try_depth > 0))

        def visit_Call(self, node):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if fname in ("import_module", "__import__") and node.args:
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and not a.value.startswith("."):
                    found.append((a.value, node.lineno, True))
            self.generic_visit(node)

    V().visit(tree)
    return found


def _pip_lists_in_ast(tree):
    """subprocess.run([sys.executable, "-m", "pip", "install", "requests", ...])"""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else None for e in node.elts]
            for i in range(len(vals) - 1):
                if vals[i] and vals[i].lower().endswith(("pip", "pip3", "pip.exe")) and vals[i + 1] == "install":
                    rest = " ".join(v if v is not None else "%VAR%" for v in vals[i + 2:])
                    out.append((rest, node.lineno))
                    break
    return out


def _module_to_pip(mod):
    parts = mod.split(".")
    root = parts[0]
    if root == "google":
        if len(parts) > 1:
            if parts[1] == "cloud" and len(parts) > 2:
                return "google-cloud-" + parts[2].replace("_", "-")
            return GOOGLE_SUB.get(parts[1], "google-" + parts[1].replace("_", "-"))
        return None
    if root.startswith("win32") or root.startswith("_win32"):
        return "pywin32"
    if root in IMPORT_TO_PIP:
        return IMPORT_TO_PIP[root]
    return root


SCAN_EXTS = (".py", ".pyw", ".bat", ".cmd", ".ps1", ".psm1", ".ipynb")
SKIP_DIRS = {"__pycache__", "site-packages", "node_modules", "build", "dist", "wheels", "venv",
             "pyinstaller offline", "package downloader library"}


def is_scannable(fn):
    low = os.path.basename(fn).lower()
    return low.endswith(SCAN_EXTS) or low.endswith(".txt") and low.startswith("requirement")


def folder_files(root, limit=3000, max_size=4 << 20):
    """Every script / requirements file under a folder (skips virtual envs, caches, build output,
    hidden folders). Returns (paths, module names that live in the folder, how many were skipped)."""
    out, local, skipped = [], set(), 0
    for d, dirs, files in os.walk(root):
        keep = []
        for x in dirs:
            full = os.path.join(d, x)
            if x.startswith(".") or x.lower() in SKIP_DIRS or os.path.exists(os.path.join(full, "pyvenv.cfg")):
                continue
            keep.append(x)
            if os.path.exists(os.path.join(full, "__init__.py")):
                local.add(x)             # a package folder of the app itself: "import mypkg" is local
        dirs[:] = sorted(keep, key=str.lower)
        for f in sorted(files, key=str.lower):
            if not is_scannable(f):
                continue
            full = os.path.join(d, f)
            try:
                if os.path.getsize(full) > max_size:
                    skipped += 1
                    continue
            except OSError:
                continue
            if len(out) >= limit:
                skipped += 1
                continue
            out.append(full)
            local.add(os.path.splitext(f)[0])
    return out, local, skipped


def scan_files(files, pypi, local_names=()):
    """files: list of {name, content, label?}. Returns detected packages + notes.
    local_names: module names that are the app's own (files and package folders found next to it)."""
    found = {}   # canon -> item
    ignored_std, local, notes = set(), set(), []
    stems = {os.path.splitext(os.path.basename(f["name"]))[0] for f in files} | set(local_names)

    def add(pkg, how, file, line, optional=False, mapped_from=None):
        if not pkg:
            return
        key = canon(re.split(r"[\[<>=!~ ;]", pkg, 1)[0])
        it = found.get(key)
        where = "%s line %s" % (file, line) if line else file
        if it is None:
            it = found[key] = {"name": pkg, "how": [], "where": [], "optional": optional,
                               "import": mapped_from, "files": []}
        elif ("==" in pkg or ">" in pkg or "<" in pkg) and pkg != it["name"]:
            it["name"] = pkg  # keep the version pin if a pip line gives one
        if how not in it["how"]:
            it["how"].append(how)
        if where not in it["where"] and len(it["where"]) < 5:
            it["where"].append(where)
        if file not in it["files"]:
            it["files"].append(file)
        it["optional"] = it["optional"] and optional

    for f in files:
        fname = f.get("label") or os.path.basename(f["name"])
        text = f.get("content") or ""
        ext = os.path.splitext(fname)[1].lower()
        sources = []
        if ext == ".ipynb":
            try:
                nb = json.loads(text)
                cells = nb.get("cells", [])
                code = []
                for c in cells:
                    if c.get("cell_type") == "code":
                        src = c.get("source", "")
                        src = "".join(src) if isinstance(src, list) else src
                        lines = []
                        for ln in src.split("\n"):
                            s = ln.strip()
                            if s.startswith(("!", "%")):
                                m = PIP_LINE_RE.search(s)
                                if m:
                                    p, n = pip_line_packages(m.group(1))
                                    for x in p:
                                        add(x, "pip install in notebook", fname, None)
                                lines.append("pass")
                            else:
                                lines.append(ln)
                        code.append("\n".join(lines))
                sources.append(("py", "\n".join(code)))
            except Exception:
                notes.append("%s: could not read the notebook." % fname)
        elif ext in (".py", ".pyw"):
            sources.append(("py", text))
        elif os.path.basename(fname).lower().startswith("requirements") or ext == ".txt" and "requirement" in fname.lower():
            sources.append(("req", text))
        else:
            sources.append(("shell", text))

        for kind, src in sources:
            if kind == "req":
                for i, ln in enumerate(src.splitlines(), 1):
                    s = ln.split("#", 1)[0].strip()
                    if not s or s.startswith("-"):
                        if s.startswith(("-r", "--requirement")):
                            notes.append("%s line %d refers to another requirements file: %s" % (fname, i, s))
                        continue
                    s = s.split(";", 1)[0].strip()
                    if REQ_RE.match(s):
                        add(s, "requirements file", fname, i)
                continue

            # pip install lines: every line of a .bat/.ps1/.sh, but only comments, docstrings and
            # command/message text in Python (other strings in code are labels, not commands)
            if kind == "py":
                try:
                    texts = _py_pip_texts(src, ast.parse(src))
                except SyntaxError:
                    texts = _py_pip_texts(src, None)
            else:
                texts = [(ln, i) for i, ln in enumerate(src.splitlines(), 1)]
            for ln, i in texts:
                rest = pip_command_rest(ln, code=kind == "shell")
                if rest is not None:
                    pk, nt = pip_line_packages(rest)
                    for x in pk:
                        add(x, "pip install line", fname, i)
                    for n in nt:
                        msg = "%s line %d: %s" % (fname, i, n)
                        if msg not in notes:
                            notes.append(msg)

            if kind == "py":
                tree = None
                try:
                    tree = ast.parse(src)
                except SyntaxError as e:
                    notes.append("%s: Python syntax error at line %s, used a simpler text scan." % (fname, e.lineno))
                mods = []
                if tree is not None:
                    mods = _import_roots(tree)
                    for rest, line in _pip_lists_in_ast(tree):
                        for x in pip_line_packages(rest)[0]:
                            add(x, "pip install in code", fname, line)
                    # PEP 723 inline metadata
                    m = re.search(r"(?ms)^# /// script\s*$(.*?)^# ///\s*$", src)
                    if m:
                        block = "\n".join(l[2:] if l.startswith("# ") else l[1:] for l in m.group(1).splitlines())
                        dm = re.search(r"dependencies\s*=\s*\[(.*?)\]", block, re.S)
                        if dm:
                            for dep in re.findall(r"[\"']([^\"']+)[\"']", dm.group(1)):
                                add(dep.split(";")[0].strip(), "script metadata", fname, None)
                else:
                    for i, ln in enumerate(src.splitlines(), 1):
                        for mm in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+(?:\s*,\s*[\w.]+)*))", ln):
                            if mm.group(1):
                                mods.append((mm.group(1), i, False))
                            else:
                                for x in mm.group(2).split(","):
                                    mods.append((x.strip(), i, False))
                for mod, line, optional in mods:
                    root = mod.split(".")[0]
                    if not root or root in STDLIB or root.startswith("_") and root not in ("_win32sysloader",):
                        if root:
                            ignored_std.add(root)
                        continue
                    if root in stems:
                        local.add(root)
                        continue
                    pkg = _module_to_pip(mod)
                    if pkg is None:
                        notes.append("%s line %s: '%s' comes with its host program (Revit/Dynamo/Rhino/...), not from PyPI." % (fname, line, root))
                        continue
                    add(pkg, "import %s" % root if mod == root else "import %s" % mod, fname, line,
                        optional=optional, mapped_from=root)
            elif kind == "shell":
                # python -c "import x" inside .bat / .ps1
                for i, ln in enumerate(src.splitlines(), 1):
                    if re.search(r"(?i)\bpy(thon)?(3)?(\.exe)?\b.*\s-c\s", ln):
                        for mm in re.finditer(r"(?:^|[\s;\"'(])(?:import|from)\s+([A-Za-z_][\w]*)", ln):
                            root = mm.group(1)
                            if root in STDLIB:
                                ignored_std.add(root)
                                continue
                            add(_module_to_pip(root), "python -c import", fname, i, mapped_from=root)

    # "installs from requirements.txt (add that file too)" is pointless when that file is already loaded
    loaded = {os.path.basename(f["name"]).lower() for f in files}
    notes = [n for n in notes if not any("installs from %s (" % b in n.lower() for b in loaded)]

    # check every candidate on PyPI (in parallel)
    items = list(found.values())
    pypi.load_top()

    def check(it):
        base = re.split(r"[\[<>=!~ ;]", it["name"], 1)[0]
        info = pypi.info(base)
        it["exists"] = bool(info.get("exists"))
        it["error"] = info.get("error")
        if it["exists"]:
            it["summary"] = info.get("summary", "")
            it["kind"] = info.get("kind")
            it["latest"] = info.get("version")
            if base == it["name"]:
                it["name"] = info["name"]
        if it.get("import") and not it["exists"]:
            it["hint"] = "No PyPI package is called '%s'. It may be one of your own files or use a different install name." % base
        elif (it["exists"] and it.get("import") and it["import"] not in IMPORT_TO_PIP and it["how"] == ["import %s" % it["import"]]
              and len(pypi.top_rank) > 1000 and canon(base) not in pypi.top_rank):
            # an import that matches a little-known PyPI name: often the user's own helper module
            it["unsure"] = True
            it["hint"] = "A PyPI package with this name exists, but it isn't a popular one. If '%s' is one of your own .py files, leave it unticked." % it["import"]
    ths = [threading.Thread(target=check, args=(it,)) for it in items]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    items.sort(key=lambda x: (not x.get("exists"), x["optional"], x["name"].lower()))
    return {"packages": items, "stdlib": sorted(ignored_std), "local": sorted(local), "notes": notes}


# =========================================================================================
#  Download + build job
# =========================================================================================

class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.log = []
        self.state = "idle"
        self.result = None
        self.stage = ""
        self.started = None
        self.progress = None

    def add(self, line):
        with self.lock:
            self.log.append(line.rstrip("\n"))
            if len(self.log) > 4000:
                del self.log[:1000]

    def snapshot(self, since):
        with self.lock:
            return {"state": self.state, "stage": self.stage, "lines": self.log[since:],
                    "count": len(self.log), "result": self.result, "progress": self.progress}


JOB = Job()


def find_stub():
    """The exe used as the installer base: the frozen exe itself, or a built one next to the .py."""
    if FROZEN:
        return sys.executable
    for p in (os.path.join(app_dir(), "dist", APP_NAME + ".exe"), os.path.join(app_dir(), APP_NAME + ".exe")):
        if os.path.exists(p):
            return p
    return None


def _run_pip_logged(args, job, resolving=False):
    cmd = pip_base_cmd() + args
    side = os.path.join(data_dir(), "pip-%d-%d.log" % (os.getpid(), threading.get_ident()))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PIP_NO_INPUT="1", PYTHONUNBUFFERED="1", PD_PIP_OUT=side)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=env, **_no_window())
    lines = iter(p.stdout)
    got_any = [False]

    def all_lines():
        for ln in lines:
            got_any[0] = True
            yield ln
        p.wait()
        if not got_any[0] and os.path.exists(side):
            with open(side, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    yield ln
        try:
            os.remove(side)
        except OSError:
            pass
    tail = []
    for line in all_lines():
        line = line.rstrip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-30:]
        st = line.strip()
        if "metadata" in st or st.startswith(("Saved ", "File was already")):
            continue
        if resolving:
            if st.startswith("Collecting"):
                job.resolved = getattr(job, "resolved", 0) + 1
                job.stage = "Finding dependencies… %d packages so far" % job.resolved
                job.add("    " + st)
            elif st.startswith(("ERROR", "WARNING")):
                job.add("    " + st)
            continue
        if st.startswith(("Collecting", "Downloading", "ERROR", "WARNING", "Successfully",
                          "Requirement")) or "error" in st.lower():
            job.add("    " + st)
    p.wait()
    return p.returncode, tail


def target_args(ver, plat):
    nodot = ver.replace(".", "")
    return ["--only-binary=:all:", "--platform", plat, "--python-version", ver,
            "--implementation", "cp", "--abi", "cp" + nodot, "--abi", "abi3", "--abi", "none"]


class Progress:
    """Byte counter with speed and time-left, read by the portal while downloading."""

    def __init__(self, job, phase, total_bytes=0, files_total=0):
        self.job, self.phase = job, phase
        self.total, self.done, self.files_total, self.files_done = total_bytes, 0, files_total, 0
        self.net = 0                 # bytes really fetched (not from cache), for the speed
        self.samples = [(time.time(), 0)]
        self.current = []
        self.lock = threading.Lock()
        self.last_pub = 0
        self.publish(force=True)

    def add(self, n, net=True):
        with self.lock:
            self.done += n
            if net:
                self.net += n
        self.publish()

    def file_done(self):
        with self.lock:
            self.files_done += 1
        self.publish(force=True)

    def publish(self, force=False):
        now = time.time()
        if not force and now - self.last_pub < 0.25:
            return
        with self.lock:
            self.last_pub = now
            self.samples.append((now, self.net))
            while len(self.samples) > 2 and now - self.samples[0][0] > 5:
                self.samples.pop(0)
            t0, b0 = self.samples[0]
            speed = (self.net - b0) / (now - t0) if now - t0 > 0.3 else 0
            left = max(self.total - self.done, 0)
            eta = left / speed if speed > 1 else None
            self.job.progress = {
                "phase": self.phase, "total": self.total, "done": min(self.done, self.total or self.done),
                "files_total": self.files_total, "files_done": self.files_done,
                "speed": speed, "eta": eta, "current": list(self.current)[:4],
            }


def _resolve(pkgs, t, plat, job, extra=()):
    """Ask pip which files are needed (without downloading the wheels themselves).
    Returns (rc, tail, {filename: {url, sha256, name, version}})."""
    rdir = tempfile.mkdtemp(prefix="res_", dir=data_dir())
    report = os.path.join(rdir, "report.json")
    try:
        rc, tail = _run_pip_logged(["install", "--dry-run", "--ignore-installed", "--report", report,
                                    "--target", os.path.join(rdir, "t"), "--progress-bar", "off"]
                                   + list(extra) + target_args(t, plat) + pkgs, job, resolving=True)
        files = {}
        if rc == 0 and os.path.exists(report):
            with open(report, "r", encoding="utf-8") as f:
                rep_ = json.load(f)
            for it in rep_.get("install", []):
                di = it.get("download_info") or {}
                url = di.get("url", "")
                fn = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("#")[0])
                if not fn.endswith(".whl"):
                    continue
                md = it.get("metadata") or {}
                files[fn] = {"url": url, "sha256": ((di.get("archive_info") or {}).get("hashes") or {}).get("sha256"),
                             "name": md.get("name"), "version": md.get("version")}
        return rc, tail, files
    finally:
        shutil.rmtree(rdir, ignore_errors=True)


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()




# =========================================================================================
#  Settings and the package library
# =========================================================================================

SETTINGS_DEFAULTS = {
    "theme": "system",          # system | light | dark
    "accent": "Blue",
    "library_dir": "",          # "" = default_library_dir()
    "output_dir": "",           # "" = <app folder>\Output
    "newer_policy": "ask",      # ask | saved | newest
    "parallel": 4,
    "bundle": [], "targets": [], "name": "Python Packages", "plat": "win_amd64", "offline_only": False,
}


class Settings(dict):
    def __init__(self):
        super().__init__(SETTINGS_DEFAULTS)
        self.path = os.path.join(data_dir(), "settings.json")
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.update({k: v for k, v in json.load(f).items()})
        except Exception:
            pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass


SETTINGS = Settings()


def _documents_dir():
    if IS_WIN:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                return os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0])
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def default_library_dir():
    """Documents\\Package Downloader Library, unless Documents is synced by OneDrive
    (gigabytes of wheels shouldn't be uploaded) - then the user folder itself."""
    docs = _documents_dir()
    if "onedrive" in docs.lower() or not os.path.isdir(docs):
        return os.path.join(os.path.expanduser("~"), "Package Downloader Library")
    return os.path.join(docs, "Package Downloader Library")


def output_dir():
    d = SETTINGS.get("output_dir") or os.path.join(app_dir(), "Output")
    os.makedirs(d, exist_ok=True)
    return d


def parse_wheel(fn):
    """'numpy-2.1.0-cp312-cp312-win_amd64.whl' -> dict(name, version, py, abi, plat) or None."""
    if not fn.endswith(".whl"):
        return None
    parts = fn[:-4].split("-")
    if len(parts) not in (5, 6):
        return None
    return {"name": parts[0], "version": parts[1], "py": parts[-3], "abi": parts[-2], "plat": parts[-1]}


def wheel_fits(fn, pyver, plat):
    """Can this wheel be installed on CPython <pyver> for <plat>?"""
    w = parse_wheel(fn)
    if not w:
        return False
    plats = w["plat"].split(".")
    if "any" not in plats and plat not in plats:
        return False
    nod = pyver.replace(".", "")
    minor = int(nod[1:]) if nod[1:].isdigit() else 0
    pys, abis = w["py"].split("."), w["abi"].split(".")
    for p in pys:
        if p in ("py3", "py" + nod) and "none" in abis:
            return True
        if p == "cp" + nod and ("cp" + nod in abis or "abi3" in abis or "none" in abis):
            return True
        if p.startswith("cp3") and "abi3" in abis and p[3:].isdigit() and int(p[3:]) <= minor:
            return True
    return False


class Library:
    """A plain folder of .whl files kept forever and reused for every bundle.
    <library>\\Packages\\*.whl   - the files, exactly as PyPI serves them
    <library>\\library.json      - name, version, checksum, size and date of each file"""

    def __init__(self, root=None):
        self.lock = threading.RLock()
        self.set_root(root or SETTINGS.get("library_dir") or default_library_dir())

    def set_root(self, root):
        with self.lock:
            self.root = root
            self.files_dir = os.path.join(root, "Packages")
            os.makedirs(self.files_dir, exist_ok=True)
            self.index_path = os.path.join(root, "library.json")
            self.index = {}
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self.index = json.load(f).get("files", {})
            except Exception:
                pass
            self._sync()
            readme = os.path.join(root, "README.txt")
            if not os.path.exists(readme):
                try:
                    with open(readme, "w", encoding="utf-8") as f:
                        f.write("Package Downloader keeps every Python package it downloads here (Packages folder),\n"
                                "exactly as published on PyPI, so the next bundle can reuse them without\n"
                                "downloading again. library.json lists name, version and checksum of each file.\n"
                                "You can delete files here, or manage them in the app's Library tab.\n")
                except OSError:
                    pass

    def _sync(self):
        """Pick up files added or removed by hand."""
        on_disk = {f for f in os.listdir(self.files_dir) if f.endswith(".whl")}
        changed = False
        for fn in list(self.index):
            if fn not in on_disk:
                del self.index[fn]
                changed = True
        for fn in on_disk - set(self.index):
            w = parse_wheel(fn)
            if not w:
                continue
            p = os.path.join(self.files_dir, fn)
            self.index[fn] = {"name": w["name"], "version": w["version"], "sha256": None,
                              "size": os.path.getsize(p), "added": _dt.datetime.fromtimestamp(
                                  os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")}
            changed = True
        if changed:
            self.save()

    def save(self):
        with self.lock:
            try:
                tmp = self.index_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"app": APP_NAME, "files": self.index}, f, indent=1, sort_keys=True)
                os.replace(tmp, self.index_path)
            except OSError:
                pass

    def path(self, fn):
        return os.path.join(self.files_dir, fn)

    def has(self, fn, sha256=None):
        with self.lock:
            e = self.index.get(fn)
            if not e or not os.path.exists(self.path(fn)):
                return False
            if sha256:
                if not e.get("sha256"):
                    e["sha256"] = _sha256(self.path(fn))
                    self.save()
                return e["sha256"] == sha256
            return True

    def add(self, fn, src_path, sha256=None, name=None, version=None):
        w = parse_wheel(fn) or {}
        dst = self.path(fn)
        if os.path.abspath(src_path) != os.path.abspath(dst):
            os.replace(src_path, dst)
        with self.lock:
            self.index[fn] = {"name": name or w.get("name"), "version": version or w.get("version"),
                              "sha256": sha256, "size": os.path.getsize(dst),
                              "added": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
            self.save()
        return dst

    def delete(self, fns):
        with self.lock:
            for fn in fns:
                try:
                    os.remove(self.path(fn))
                except OSError:
                    pass
                self.index.pop(fn, None)
            self.save()

    def versions(self, name):
        """{version: [filenames]} saved for a package."""
        c = canon(name)
        out = {}
        with self.lock:
            for fn, e in self.index.items():
                if canon(e.get("name") or "") == c:
                    out.setdefault(e.get("version"), []).append(fn)
        return out

    def best_saved(self, name, targets, plat):
        """Newest saved version that has a fitting file for every target Python."""
        vs = self.versions(name)
        ok = [v for v, fns in vs.items() if all(any(wheel_fits(f, t, plat) for f in fns) for t in targets)]
        ok.sort(key=_vkey, reverse=True)
        return ok[0] if ok else None

    def packages(self):
        """[(display name, {version: [files]})] sorted by name."""
        g = {}
        with self.lock:
            for fn, e in self.index.items():
                key = canon(e.get("name") or fn)
                d = g.setdefault(key, {"name": e.get("name") or key, "versions": {}})
                d["versions"].setdefault(e.get("version"), []).append(fn)
        return sorted(g.values(), key=lambda d: d["name"].lower())

    def stats(self):
        with self.lock:
            n = len(self.index)
            size = sum(e.get("size") or 0 for e in self.index.values())
            pk = len({canon(e.get("name") or "") for e in self.index.values()})
        return {"files": n, "size": size, "packages": pk}

    def migrate_old_cache(self):
        old = os.path.join(data_dir(), "wheel-cache")
        if not os.path.isdir(old):
            return
        for fn in os.listdir(old):
            if fn.endswith(".whl") and fn not in self.index:
                try:
                    self.add(fn, os.path.join(old, fn))
                except OSError:
                    pass
        shutil.rmtree(old, ignore_errors=True)


LIBRARY = None


def library():
    global LIBRARY
    if LIBRARY is None:
        LIBRARY = Library()
        try:
            LIBRARY.migrate_old_cache()
        except Exception:
            pass
    return LIBRARY


def check_newer(bundle, targets, plat, pypi, lib):
    """Bundle entries (no version picked) whose saved version is older than PyPI's newest.
    Returns [{name, saved, latest}]."""
    out = []
    for b in bundle:
        if b.get("ver") or b.get("pin"):
            continue
        saved = lib.best_saved(b["name"], targets, plat)
        if not saved:
            continue
        info = pypi.info(b["name"])
        latest = info.get("version")
        if latest and _vkey(latest) > _vkey(saved):
            out.append({"name": b["name"], "saved": saved, "latest": latest})
    return out


def _download_files(items, job, lib):
    """items: {filename: {url, sha256, name, version}} -> ({filename: path in library}, {filename: error}).
    Files already in the library are reused; new ones are downloaded into it, with live progress."""
    names = sorted(items)
    paths, failed = {}, {}

    job.stage = "Checking the library"
    have = [fn for fn in names if lib.has(fn, items[fn].get("sha256"))]
    todo = [fn for fn in names if fn not in have]

    # sizes first, so the bar knows the total (one request per project to PyPI's index)
    sizes = {fn: (lib.index.get(fn) or {}).get("size") or 0 for fn in have}
    projects = {}
    for fn in todo:
        sizes[fn] = 0
        if items[fn]["url"].startswith("file:"):
            try:
                sizes[fn] = os.path.getsize(urllib.request.url2pathname(urllib.parse.urlparse(items[fn]["url"]).path))
            except OSError:
                pass
        else:
            projects.setdefault(items[fn].get("name") or fn.split("-")[0], []).append(fn)

    def index_sizes(proj):
        try:
            raw = PyPI.get("https://pypi.org/simple/%s/" % urllib.parse.quote(canon(proj)),
                           accept="application/vnd.pypi.simple.v1+json", timeout=10)
            got = {f["filename"]: int(f.get("size") or 0) for f in json.loads(raw).get("files", [])}
            return {fn: got.get(fn, 0) for fn in projects[proj]}
        except Exception:
            return {}
    if projects:
        job.stage = "Checking file sizes"
        with ThreadPoolExecutor(8) as ex:
            for d in ex.map(index_sizes, list(projects)):
                sizes.update(d)

    prog = Progress(job, "download", sum(sizes.values()), len(names))
    prog.reused = len(have)
    for fn in have:
        paths[fn] = lib.path(fn)
        prog.add(sizes[fn], net=False)
        prog.file_done()
        job.add("    ✓ %s  (from the library)" % fn)
    job.stage = ("Downloading %d new file%s (%s)" % (len(todo), "" if len(todo) == 1 else "s",
                                                   _human(sum(sizes[f] for f in todo)))) if todo else "All files were already in the library"

    def get(fn):
        it = items[fn]
        tmp = lib.path(fn) + ".part"
        with prog.lock:
            prog.current.append(fn)
        last_err = None
        for attempt in range(3):
            got = 0
            try:
                req = urllib.request.Request(it["url"], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
                    if not sizes.get(fn):
                        sizes[fn] = int(r.headers.get("Content-Length") or 0)
                        with prog.lock:
                            prog.total += sizes[fn]
                    while True:
                        b = r.read(256 * 1024)
                        if not b:
                            break
                        out.write(b)
                        got += len(b)
                        prog.add(len(b))
                sha = _sha256(tmp)
                if it.get("sha256") and sha != it["sha256"]:
                    raise IOError("checksum mismatch")
                paths[fn] = lib.add(fn, tmp, sha, it.get("name"), it.get("version"))
                last_err = None
                break
            except Exception as e:
                last_err = e
                prog.add(-got, net=False)
                time.sleep(1 + attempt * 2)
        if last_err is not None:
            # last resort: let pip fetch it (it knows proxy settings from pip.ini)
            d = tempfile.mkdtemp(prefix="pipdl_", dir=data_dir())
            rc, tail = _run_pip_logged(["download", "--no-deps", "-d", d, it["url"]], job)
            f2 = os.path.join(d, fn)
            if rc == 0 and os.path.exists(f2):
                paths[fn] = lib.add(fn, f2, _sha256(f2), it.get("name"), it.get("version"))
                prog.add(sizes.get(fn, 0), net=False)
                job.add("    ✓ %s  (downloaded by pip)" % fn)
            else:
                failed[fn] = str(last_err)
                job.add("    ✗ %s: %s" % (fn, last_err))
            shutil.rmtree(d, ignore_errors=True)
        else:
            job.add("    ↓ %s  (%s)" % (fn, _human(os.path.getsize(paths[fn]))))
        try:
            os.remove(tmp)
        except OSError:
            pass
        with prog.lock:
            if fn in prog.current:
                prog.current.remove(fn)
        prog.file_done()

    if todo:
        with ThreadPoolExecutor(max(1, min(8, int(SETTINGS.get("parallel") or 4)))) as ex:
            list(ex.map(get, todo))
    prog.publish(force=True)
    return paths, failed


def _human(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%.0f %s" if u in ("B", "KB") else "%.1f %s") % (n, u)
        n /= 1024


def build_job(req):
    job = JOB
    lib = library()
    try:
        name = safe_filename(req.get("name") or "Python Packages")
        pkgs = []
        for p in req.get("packages", []):
            s = (p.get("spec") or p.get("name") or "").strip()
            if s and REQ_RE.match(s):
                pkgs.append(s)
        targets = [t for t in req.get("targets", []) if re.match(r"^3\.\d{1,2}$", t)]
        plat = req.get("platform") or "win_amd64"
        if not re.match(r"^[a-z0-9_]{3,40}$", plat):
            plat = "win_amd64"
        offline = bool(req.get("offline_only"))
        here = req.get("install_into")      # {"exe": python.exe, "user": bool}: install on this PC, no installer
        if here:
            info = _probe_python(here["exe"])
            if not info:
                raise ValueError("That Python didn't start: %s" % here["exe"])
            here = dict(info, user=bool(here.get("user")))
            targets = [info["version"]]
            plat = info["platform"]
            if plat.startswith("linux_"):          # pip only accepts manylinux tags for Linux wheels
                plat = "manylinux_2_17_" + plat[len("linux_"):]
        if not pkgs:
            raise ValueError("Add at least one package to the bundle.")
        if not targets:
            raise ValueError("Pick the Python version of the offline PC.")

        job.stage = "Checking pip"
        ok, ver = pip_available()
        if not ok:
            raise RuntimeError("pip could not start: %s" % ver)
        job.add("Using %s" % (ver.split(" from ")[0] or "the bundled pip"))
        job.add("Library: %s" % lib.root)
        extra = ["--no-index", "--find-links", lib.files_dir] if offline else []
        if offline:
            job.add("Offline mode: only packages already in the library are used.")

        good, failed = [], {}
        needed = {}          # filename -> {url, sha256, name, version}
        use_report = True
        for t in targets:
            job.add("")
            job.add("== Python %s, %s ==" % (t, plat))
            job.resolved = 0
            job.progress = {"phase": "resolve"}
            job.stage = "Finding dependencies for Python %s…" % t
            todo = [p for p in pkgs if p not in failed]
            rc, tail, files = _resolve(todo, t, plat, job, extra)
            if rc != 0 and any("no such option: --report" in l or "--dry-run" in l and "no such option" in l for l in tail):
                use_report = False
                break
            if rc == 0:
                needed.update(files)
                for p in todo:
                    if p not in good:
                        good.append(p)
                continue
            job.add("  Some packages failed together. Checking them one at a time to find which...")
            for p in todo:
                rc, tail, files = _resolve([p], t, plat, job, extra)
                if rc == 0:
                    needed.update(files)
                    if p not in good:
                        good.append(p)
                else:
                    reason = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else "unknown error")
                    if offline:
                        reason += " (not in the library - turn off 'Only use saved packages')"
                    failed.setdefault(p, []).append("Python %s: %s" % (t, reason.replace("ERROR: ", "")))
                    if p in good:
                        good.remove(p)
                    job.add("  ✗ %s: %s" % (p, reason.replace("ERROR: ", "")))
        good = [p for p in good if p not in failed]

        wheel_paths, boot_paths = {}, {}
        if use_report:
            pipfiles = {}
            if not here:
                job.progress = {"phase": "resolve"}
                job.stage = "Adding pip (used only if the offline PC has no pip)"
                rc, tail, pipfiles = _resolve(["pip"], targets[0], plat, job, extra + ["--no-deps"])
            if needed:
                job.add("")
                job.add("== %d files needed ==" % len(needed))
                wheel_paths, bad = _download_files(needed, job, lib)
                if bad:
                    raise RuntimeError("%d file(s) could not be downloaded: %s" % (len(bad), ", ".join(bad)))
                if pipfiles:
                    boot_paths, _ = _download_files(pipfiles, job, lib)
        else:
            # old pip without --report: let pip download directly (no byte progress)
            job.add("  (this pip is too old for the progress bar; downloading the simple way)")
            job.progress = {"phase": "resolve"}
            tmpd = tempfile.mkdtemp(prefix="pipdl_", dir=data_dir())
            for t in targets:
                job.stage = "Downloading for Python %s (%s)" % (t, plat)
                todo = [p for p in pkgs if p not in failed]
                rc, tail = _run_pip_logged(["download", "-d", tmpd, "--find-links", lib.files_dir]
                                           + extra + target_args(t, plat) + todo, job)
                if rc == 0:
                    good = list(dict.fromkeys(good + todo))
                    continue
                for p in todo:
                    rc, tail = _run_pip_logged(["download", "-d", tmpd, "--find-links", lib.files_dir]
                                               + extra + target_args(t, plat) + [p], job)
                    if rc == 0:
                        if p not in good:
                            good.append(p)
                    else:
                        reason = next((l for l in reversed(tail) if "ERROR" in l), tail[-1] if tail else "unknown error")
                        failed.setdefault(p, []).append("Python %s: %s" % (t, reason.replace("ERROR: ", "")))
                        job.add("  ✗ %s: %s" % (p, reason.replace("ERROR: ", "")))
            good = [p for p in good if p not in failed]
            for fn in os.listdir(tmpd):
                if fn.endswith(".whl"):
                    wheel_paths[fn] = lib.path(fn) if lib.has(fn) else lib.add(fn, os.path.join(tmpd, fn), _sha256(os.path.join(tmpd, fn)))
            shutil.rmtree(tmpd, ignore_errors=True)

        files = sorted(wheel_paths)
        if not good or not files:
            raise RuntimeError("Nothing could be downloaded. See the details.")

        if here:
            _install_here(job, lib, here, good, failed, wheel_paths)
            return

        # ---- pack: the zip is built straight from the library files ----
        job.stage = "Packing"
        work = tempfile.mkdtemp(prefix="pkgdl_", dir=data_dir())
        manifest = {
            "name": name, "packages": good, "failed": failed, "targets": targets, "platform": plat,
            "files": files, "created": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "app_version": APP_VERSION,
        }
        extras = {
            "manifest.json": json.dumps(manifest, indent=2),
            "requirements.txt": "\n".join(good) + "\n",
            "Install.bat": INSTALL_BAT.replace("\n", "\r\n"),
        }
        entries = [("wheels/" + fn, p) for fn, p in sorted(wheel_paths.items())]
        entries += [("bootstrap/" + fn, p) for fn, p in sorted(boot_paths.items())]
        total = sum(os.path.getsize(p) for _, p in entries)
        stub = find_stub()
        prog = Progress(job, "pack", total * (2 if stub else 1), len(entries))
        zpath = os.path.join(work, "payload.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for arc, txt in extras.items():
                z.writestr(arc, txt)
            for arc, p in entries:
                z.write(p, arc)
                prog.add(os.path.getsize(p))
                prog.file_done()

        out_dir = output_dir()
        result = {"name": name, "packages": good, "failed": failed, "files": len(entries),
                  "size_mb": round(total / 1048576, 1), "folder": out_dir,
                  "reused": sum(1 for l in job.log if "(from the library)" in l)}
        if stub:
            out = os.path.join(out_dir, name + INSTALL_SUFFIX + ".exe")
            job.stage = "Writing the installer"
            write_installer(stub, zpath, out, on_bytes=lambda n: prog.add(n))
            result["kind"] = "exe"
        else:
            out = os.path.join(out_dir, name + INSTALL_SUFFIX + ".zip")
            shutil.copyfile(zpath, out)
            result["kind"] = "zip"
            job.add("")
            job.add("Note: no built 'Package Downloader.exe' was found (you are running the .py),")
            job.add("so a .zip was made instead: unzip it on the offline PC and run Install.bat.")
        result["path"] = out
        shutil.rmtree(work, ignore_errors=True)
        job.add("")
        job.add("Done: %s" % out)
        if failed:
            job.add("Could not get: %s" % ", ".join(failed))
        job.progress = None
        job.result = result
        job.state = "done"
        job.stage = "Done"
    except Exception as e:
        job.add("")
        job.add("FAILED: %s" % e)
        if not isinstance(e, (ValueError, RuntimeError)):
            job.add(traceback.format_exc())
        job.progress = None
        job.state = "error"
        job.stage = "Failed"
        job.result = {"error": str(e)}
    finally:
        job.running = False




INSTALL_BAT = r"""@echo off
REM Offline install of the packages in the "wheels" folder (fallback for the .exe installer).
setlocal
cd /d "%~dp0"
set "PY="
for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY if exist "%PY%" goto :found
set "PY="
for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%P"
if defined PY if exist "%PY%" goto :found
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*") do (
    if exist "%%~D\python.exe" set "PY=%%~D\python.exe"
)
if defined PY goto :found
echo Python 3 was not found on this PC. Install Python first, then run this again.
pause
exit /b 1
:found
echo Using Python: %PY%
"%PY%" -m pip --version >nul 2>nul || "%PY%" -m ensurepip --default-pip
"%PY%" -m pip install --no-index --find-links "%~dp0wheels" --disable-pip-version-check -r "%~dp0requirements.txt"
echo.
pause
"""



def _run_logged(cmd, job):
    """Runs a command, sends its output to the job log, returns (exit code, all output)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
               PIP_NO_INPUT="1", PYTHONUNBUFFERED="1")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", env=env, **_no_window())
    out = []
    for line in p.stdout:
        line = line.rstrip()
        out.append(line)
        if line.strip():
            job.add("    " + line)
            st = line.strip()
            if st.startswith("Installing collected packages"):
                job.stage = "Installing %d packages…" % len(st.split(":", 1)[-1].split(","))
    p.wait()
    return p.returncode, "\n".join(out)


def _install_here(job, lib, py, good, failed, wheel_paths):
    """'Install now': installs the downloaded files into a Python on this PC (pip, no internet needed)."""
    exe = py["exe"]
    job.add("")
    job.add("== Installing into Python %s (%s) ==" % (py["full"], exe))
    job.progress = {"phase": "resolve"}
    job.stage = "Checking pip in Python %s…" % py["full"]
    r = subprocess.run([exe, "-m", "pip", "--version"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, **_no_window())
    if r.returncode != 0:
        job.add("pip is missing in this Python. Adding it…")
        _run_logged([exe, "-m", "ensurepip", "--default-pip"], job)
    # the exact versions that were just resolved, so pip installs those and nothing else
    work = tempfile.mkdtemp(prefix="pkgin_", dir=data_dir())
    pins = []
    for fn in wheel_paths:
        w = parse_wheel(fn)
        if w:
            pins.append("%s==%s" % (w["name"].replace("_", "-"), w["version"]))
    cfile = os.path.join(work, "constraints.txt")
    with open(cfile, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(set(pins))) + "\n")
    cmd = [exe, "-m", "pip", "install", "--no-index", "--find-links", lib.files_dir, "-c", cfile,
           "--disable-pip-version-check"] + good
    if py.get("user"):
        cmd.append("--user")
    job.stage = "Installing %d package%s into Python %s…" % (len(good), "" if len(good) == 1 else "s", py["full"])
    code, out = _run_logged(cmd, job)
    if code != 0 and not py.get("user") and not py.get("venv") and re.search(
            r"(?i)access is denied|permission denied|--user|WinError 5|not writeable", out):
        job.add("")
        job.add("No permission to write into that Python. Trying again for your user only…")
        code, out = _run_logged(cmd + ["--user"], job)
    shutil.rmtree(work, ignore_errors=True)
    if code != 0:
        raise RuntimeError("pip could not install the packages into Python %s. See the details." % py["full"])
    total = sum(os.path.getsize(p) for p in wheel_paths.values() if os.path.exists(p))
    job.add("")
    job.add("Installed into %s" % exe)
    if failed:
        job.add("Could not get: %s" % ", ".join(failed))
    job.progress = None
    job.result = {"kind": "installed", "packages": good, "failed": failed, "files": len(wheel_paths),
                  "size_mb": round(total / 1048576, 1), "python": exe, "python_version": py["full"],
                  "reused": sum(1 for l in job.log if "(from the library)" in l)}
    job.state = "done"
    job.stage = "Done"


def _probe_python(exe):
    code = ("import sys,sysconfig,json;print(json.dumps(['%d.%d'%sys.version_info[:2],"
            "sysconfig.get_platform(),sys.prefix!=getattr(sys,'base_prefix',sys.prefix),sys.version.split()[0]]))")
    try:
        r = subprocess.run([exe, "-c", code], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL, **_no_window())
        if r.returncode == 0:
            v, plat, venv, full = json.loads(r.stdout.strip().splitlines()[-1])
            return {"exe": exe, "version": v, "platform": plat.replace("-", "_").replace(".", "_"),
                    "venv": venv, "full": full}
    except Exception:
        pass
    return None


def find_pythons():
    cands = []

    def add(p):
        if not p:
            return
        p = os.path.normpath(p.strip().strip('"'))
        if "WindowsApps" in p or not os.path.isfile(p):
            return
        if os.path.basename(p).lower() == "pythonw.exe":
            p = os.path.join(os.path.dirname(p), "python.exe")
        if os.path.normcase(p) not in [os.path.normcase(c) for c in cands]:
            cands.append(p)

    if IS_WIN:
        try:
            r = subprocess.run(["py", "-0p"], capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL, **_no_window())
            for m in re.finditer(r"([A-Za-z]:\\[^\r\n*]*?python[w]?\.exe)", r.stdout):
                add(m.group(1))
        except Exception:
            pass
        try:
            r = subprocess.run(["where", "python"], capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL, **_no_window())
            for ln in r.stdout.splitlines():
                add(ln)
        except Exception:
            pass
        import glob
        pats = []
        for env in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = os.environ.get(env)
            if base:
                pats.append(os.path.join(base, "Programs", "Python", "Python3*", "python.exe"))
                pats.append(os.path.join(base, "Python3*", "python.exe"))
        pats += [r"C:\Python3*\python.exe", os.path.join(os.path.expanduser("~"), ".venv", "Scripts", "python.exe"),
                 os.path.join(os.path.expanduser("~"), "anaconda3", "python.exe"),
                 os.path.join(os.path.expanduser("~"), "miniconda3", "python.exe")]
        for pat in pats:
            for p in sorted(glob.glob(pat)):
                add(p)
    else:
        for n in ("python3", "python"):
            add(shutil.which(n))
    found = []
    for c in cands:
        info = _probe_python(c)
        if info:
            found.append(info)
    return found




# =========================================================================================
#  Offline installer logic (no UI; used by the installer window and by --yes)
# =========================================================================================

class OfflineInstall:
    def __init__(self, payload_file, log, progress=None):
        self.payload_file = payload_file
        self.log = log                       # log(text)
        self.progress = progress or (lambda frac, text: None)
        self.tmp = None
        self.man = None

    def unpack(self, dest=None):
        self.tmp = dest or tempfile.mkdtemp(prefix="npi_")
        os.makedirs(self.tmp, exist_ok=True)
        extract_payload(self.payload_file, self.tmp)
        with open(os.path.join(self.tmp, "manifest.json"), "r", encoding="utf-8") as f:
            self.man = json.load(f)
        return self.man

    def matches(self, p):
        return p["version"] in self.man["targets"] and p["platform"] == self.man["platform"]

    def ranked_pythons(self, extra=None):
        pys = find_pythons()
        if extra:
            info = _probe_python(extra)
            if info and all(os.path.normcase(info["exe"]) != os.path.normcase(p["exe"]) for p in pys):
                pys.insert(0, info)
        # matching first, then normal installs before virtual envs, newest first
        pys.sort(key=lambda p: (not self.matches(p), p["venv"], [-int(x) for x in p["version"].split(".")]))
        if extra:   # the one asked for on the command line comes first
            pys.sort(key=lambda p: os.path.normcase(p["exe"]) != os.path.normcase(os.path.abspath(extra)))
        return pys

    def _run(self, cmd):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
                   PIP_NO_INPUT="1", PYTHONUNBUFFERED="1")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", env=env, **_no_window())
        out = []
        nfiles = max(len(self.man.get("files", [])), 1)
        seen = 0
        for line in p.stdout:
            line = line.rstrip()
            out.append(line)
            if not line.strip():
                continue
            self.log(line)
            st = line.strip()
            if st.startswith(("Processing ", "Looking in links")):
                if st.startswith("Processing "):
                    seen += 1
                self.progress(0.05 + 0.6 * min(seen / nfiles, 1.0), "Reading packages… %d of %d" % (seen, nfiles))
            elif st.startswith("Installing collected packages"):
                self.progress(0.7, "Installing %d packages…" % len(st.split(":", 1)[-1].split(",")))
        p.wait()
        return p.returncode, "\n".join(out)

    def ensure_pip(self, py):
        r = subprocess.run([py, "-m", "pip", "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, **_no_window())
        if r.returncode == 0:
            return True
        self.log("pip is missing in this Python. Adding it (offline)…")
        r = subprocess.run([py, "-m", "ensurepip", "--default-pip"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, **_no_window())
        if r.returncode != 0:
            boot = os.path.join(self.tmp, "bootstrap")
            whl = None
            if os.path.isdir(boot):
                whl = next((os.path.join(boot, f) for f in os.listdir(boot) if f.startswith("pip-")), None)
            if whl:
                self._run([py, os.path.join(whl, "pip"), "install", "--no-index", whl])
        r = subprocess.run([py, "-m", "pip", "--version"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, **_no_window())
        return r.returncode == 0

    def install(self, pyinfo, user=False):
        py = pyinfo["exe"]
        self.progress(0.03, "Checking pip…")
        if not self.ensure_pip(py):
            self.log("Could not set up pip in this Python.")
            return 3
        cmd = [py, "-m", "pip", "install", "--no-index", "--find-links", os.path.join(self.tmp, "wheels"),
               "--disable-pip-version-check", "-r", os.path.join(self.tmp, "requirements.txt")]
        if user:
            cmd.append("--user")
        self.progress(0.05, "Installing…")
        code, out = self._run(cmd)
        if code != 0 and not user and not pyinfo.get("venv") and re.search(
                r"(?i)access is denied|permission denied|--user|WinError 5|not writeable", out):
            self.log("")
            self.log("No permission to write into that Python. Trying again for your user only…")
            code, out = self._run(cmd + ["--user"])
        return 0 if code == 0 else 4

    def cleanup(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)


def headless_install(payload_file, argv):
    """'... Install.exe --yes [--python PATH] [--user] [--extract DIR]' : no window."""
    logp = os.path.join(tempfile.gettempdir(), os.path.splitext(os.path.basename(payload_file))[0] + ".log")
    lf = open(logp, "w", encoding="utf-8")

    def log(t):
        lf.write(t + "\n")
        lf.flush()
        if sys.stdout:
            try:
                print(t)
            except Exception:
                pass
    opt = lambda k: argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else None
    inst = OfflineInstall(payload_file, log)
    try:
        if opt("--extract"):
            inst.unpack(opt("--extract"))
            log("Extracted to %s" % inst.tmp)
            return 0
        man = inst.unpack()
        log("Packages: %s" % ", ".join(man["packages"]))
        if opt("--python"):
            pys = [p for p in [_probe_python(opt("--python"))] if p]
        else:
            pys = [p for p in inst.ranked_pythons() if inst.matches(p)]
        if not pys:
            log("No matching Python %s found." % "/".join(man["targets"]))
            return 2
        log("Using %s" % pys[0]["exe"])
        rc = inst.install(pys[0], user="--user" in argv)
        log("Done." if rc == 0 else "Failed (code %d)." % rc)
        return rc
    except Exception:
        log(traceback.format_exc())
        return 5
    finally:
        if not opt("--extract"):
            inst.cleanup()
        lf.close()




def _message(title, text):
    """Show a message even without a console (windowed build) and without the window toolkit."""
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
            return
        except Exception:
            pass
    try:
        sys.__stderr__.write(text + "\n")
    except Exception:
        pass


def _ui():
    """The windows live in package_downloader_ui.py (PySide6)."""
    try:
        import package_downloader_ui as ui
    except ImportError as e:
        _message(APP_NAME, "Package Downloader needs PySide6 for its window:\n\n%s\n\nRun 'Run Package "
                           "Downloader.bat' (it installs it), or: python -m pip install PySide6" % e)
        sys.exit(1)
    ui.init(sys.modules[__name__])
    return ui


def main():
    argv = sys.argv[1:]
    if sys.stdout is None or sys.stderr is None:          # windowed build
        if argv[:1] != ["--pip"]:
            nul = open(os.devnull, "w")
            sys.stdout = sys.stdout or nul
            sys.stderr = sys.stderr or nul
    if IS_WIN and FROZEN:
        # PyInstaller points the DLL search path at its temp folder; don't let the
        # Python we start for pip/installing inherit that.
        try:
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        except Exception:
            pass
    if argv[:1] == ["--pip"]:
        return run_bundled_pip(argv[1:])

    # Which file carries the packages? (this exe, or an installer dropped onto it)
    payload = None
    if argv[:1] == ["--run-installer"] and len(argv) > 1:
        payload, argv = argv[1], argv[2:]
    elif argv and os.path.isfile(argv[0]) and read_footer(argv[0]):
        payload, argv = os.path.abspath(argv[0]), argv[1:]
    elif FROZEN:
        me = sys.executable
        if not read_footer(me):
            for alt in (sys.argv[0], sys.argv[0] + ".exe"):
                alt = os.path.abspath(alt)
                if os.path.isfile(alt) and read_footer(alt):
                    me = alt
                    break
        if read_footer(me):
            payload = me
        elif looks_like_installer(me):
            _message(os.path.basename(me),
                     "This installer has no packages inside it (file size %s).\n\nIt was probably cut short while "
                     "copying. Copy it again from the PC with internet and run it again.\n\nDetails: %s"
                     % (_human(os.path.getsize(me)), me))
            return 6

    if payload:
        if "--yes" in argv or "--extract" in argv or "-y" in argv:
            return headless_install(payload, argv)
        return _ui().run_installer(payload, argv)

    files = [os.path.abspath(a) for a in argv if os.path.isfile(a) or os.path.isdir(a)]
    return _ui().run_downloader(files)


if __name__ == "__main__":
    sys.exit(main())
