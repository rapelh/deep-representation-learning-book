#!/usr/bin/env python3
"""Post-processor for make4ht HTML output.

Responsibilities:
  1. Rename files to expected URL structure (Ch1.html, Chx1.html, A1.html, etc.)
  2. Inject <head> resources (viewport, CSS, JS)
  3. Add id="top" to body
  4. Build mini-TOC from section headings
  5. Fix image paths
  6. Remove make4ht default navigation
  7. Generate search-index.json
  8. Copy processed files to website/html/
"""

import argparse
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup, Comment, Tag

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Resources to inject into <head>
COMMON_CSS = "common.css"
CHAPTER_CSS = "chapter.css"
COMMON_COMPONENTS_JS = "common_components.js"
COMMON_JS = "common.js"
CHAPTER_JS = "chapter.js"

# Theorem types with their color palettes (matching chapter.css)
THEOREM_TYPES = [
    "theorem", "definition", "lemma", "corollary", "proposition",
    "example", "remark", "exercise", "proof", "assumption", "axiom",
    "notation", "keyidea", "method", "model", "claim", "conjecture",
    "idea", "fact", "problem", "question",
]


# ---------------------------------------------------------------------------
# File renaming
# ---------------------------------------------------------------------------

def detect_file_mapping(build_dir: Path) -> dict[str, str]:
    """Detect make4ht output files and map to expected URL structure.

    make4ht with option '2' (chapter splitting) produces files like:
      book-main.html (title page / TOC)
      book-mainch1.html, book-mainch2.html, ...  (chapters)
      book-mainli1.html, book-mainli2.html, ...  (front matter: preface, etc.)
      book-mainap1.html, book-mainap2.html, ...  (appendices)
      book-mainbi1.html  (bibliography)

    We map these to:
      Ch1.html, Ch2.html, ...
      Chx1.html, Chx2.html, ...  (front matter)
      A1.html, A2.html, ...
      bib.html
    """
    mapping = {}
    html_files = sorted(build_dir.glob("*.html"))
    base = _detect_base_name(html_files)

    for f in html_files:
        name = f.name

        # Chapter files: {base}ch{N}.html
        m = re.match(rf"^{re.escape(base)}ch(\d+)\.html$", name, re.IGNORECASE)
        if m:
            mapping[name] = f"Ch{m.group(1)}.html"
            continue

        # Front matter (list items): {base}li{N}.html
        m = re.match(rf"^{re.escape(base)}li(\d+)\.html$", name, re.IGNORECASE)
        if m:
            mapping[name] = f"Chx{m.group(1)}.html"
            continue

        # Appendices: {base}ap{N}.html
        m = re.match(rf"^{re.escape(base)}ap(\d+)\.html$", name, re.IGNORECASE)
        if m:
            mapping[name] = f"A{m.group(1)}.html"
            continue

        # Bibliography: {base}bi{N}.html
        m = re.match(rf"^{re.escape(base)}bi\d*\.html$", name, re.IGNORECASE)
        if m:
            mapping[name] = "bib.html"
            continue

        # Main/index file: {base}.html
        if name.lower() == f"{base}.html":
            # Skip or map to index — the main page is usually the TOC
            # We don't include this in the final output
            continue

    return mapping


def _detect_base_name(html_files: list[Path]) -> str:
    """Detect the common base name of make4ht output files."""
    names = [f.stem for f in html_files]
    if not names:
        return "book-main"

    # Look for a pattern like {base}ch1
    for name in names:
        m = re.match(r"^(.+?)ch\d+$", name, re.IGNORECASE)
        if m:
            return m.group(1)

    # Fallback: use the shortest name as base
    return min(names, key=len) if names else "book-main"


def rename_files(build_dir: Path, mapping: dict[str, str]):
    """Rename files and update all internal href links."""
    # First pass: rename files
    for old_name, new_name in mapping.items():
        old_path = build_dir / old_name
        new_path = build_dir / new_name
        if old_path.exists():
            if new_path.exists() and old_path != new_path:
                new_path.unlink()
            old_path.rename(new_path)

    # Second pass: update hrefs in all HTML files
    for html_file in build_dir.glob("*.html"):
        text = html_file.read_text(encoding="utf-8")
        for old_name, new_name in mapping.items():
            text = text.replace(old_name, new_name)
        html_file.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Head injection
# ---------------------------------------------------------------------------

def should_include_chapter_styling(filename: str) -> bool:
    """Check if this file should get chapter-specific CSS/JS."""
    name = filename.lower()
    return bool(
        re.match(r"^chx?\d+\.html$", name)
        or re.match(r"^ax?\d+\.html$", name)
        or name == "bib.html"
    )


def _asset_href(prefix: str, filename: str) -> str:
    """Build an asset path, optionally relative to a parent output dir."""
    return f"{prefix}/{filename}" if prefix else filename


def inject_head_resources(soup: BeautifulSoup, filename: str, shared_asset_prefix: str = ""):
    """Inject viewport meta, CSS, and JS into <head>."""
    head = soup.find("head")
    if not head:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)

    # Viewport meta
    if not head.find("meta", {"name": "viewport"}):
        meta = soup.new_tag("meta", attrs={
            "name": "viewport",
            "content": "width=device-width, initial-scale=1.0",
        })
        head.insert(0, meta)

    # Common CSS
    common_css_href = _asset_href(shared_asset_prefix, COMMON_CSS)
    if not head.find("link", {"href": common_css_href}):
        head.append(soup.new_tag("link", rel="stylesheet", href=common_css_href))

    # Chapter-specific resources
    if should_include_chapter_styling(filename):
        chapter_css_href = _asset_href(shared_asset_prefix, CHAPTER_CSS)
        if not head.find("link", {"href": chapter_css_href}):
            head.append(soup.new_tag("link", rel="stylesheet", href=chapter_css_href))

        # JS files (defer)
        js_sources = [
            COMMON_COMPONENTS_JS,
            _asset_href(shared_asset_prefix, COMMON_JS),
            _asset_href(shared_asset_prefix, CHAPTER_JS),
        ]
        for js_src in js_sources:
            if not head.find("script", {"src": js_src}):
                script = soup.new_tag("script", src=js_src, defer=True)
                head.append(script)


# ---------------------------------------------------------------------------
# Body fixes
# ---------------------------------------------------------------------------

def ensure_body_top_anchor(soup: BeautifulSoup):
    """Add id='top' to body if not present."""
    body = soup.find("body")
    if body and isinstance(body, Tag) and not body.get("id"):
        body["id"] = "top"


def remove_make4ht_navigation(soup: BeautifulSoup):
    """Remove make4ht's default prev/next navigation links."""
    # make4ht typically puts navigation in a div at the top and bottom
    for nav in soup.find_all("div", class_="crosslinks"):
        nav.decompose()

    # Also remove any top/bottom navigation tables
    for nav in soup.find_all("nav", class_="crosslinks"):
        nav.decompose()


# ---------------------------------------------------------------------------
# Theorem / algorithm class tagging
# ---------------------------------------------------------------------------

# make4ht wraps amsthm environments in <div class="newtheorem"> or similar.
# We detect them by looking for the bold heading text ("Theorem", "Definition", ...)
# and add our own styling classes.

_THEOREM_HEAD_RE = re.compile(
    r"^(Theorem|Definition|Lemma|Corollary|Proposition|Example|Remark|"
    r"Exercise|Proof|Assumption|Axiom|Notation|Key Idea|Method|Model|"
    r"Claim|Conjecture|Idea|Fact|Problem|Question)\b",
    re.IGNORECASE,
)

_THEOREM_NAME_MAP = {
    "key idea": "keyidea",
}


def tag_theorem_environments(soup: BeautifulSoup):
    """Add .theorem-env and .theorem-{type} classes to theorem-like environments.

    make4ht emits theorem envs as <div class="newtheorem"> with a bold heading
    like "Theorem 1.1." or wrapped in a <span class="head">.  We scan for these
    and add our styling classes.
    """
    # Strategy 1: look for divs with class "newtheorem"
    for div in soup.find_all("div", class_="newtheorem"):
        _classify_theorem_div(div)

    # Strategy 2: look for divs whose first bold/span child matches a theorem name
    # (make4ht sometimes uses different class names depending on config)
    for div in soup.find_all("div"):
        if "theorem-env" in (div.get("class") or []):
            continue  # already tagged
        head = div.find(["span", "strong", "b"], class_=lambda c: c and "head" in str(c))
        if not head:
            head = div.find(["strong", "b"])
        if head:
            _classify_theorem_div(div, head)

    # Tag proof environments (make4ht uses class "proof")
    for div in soup.find_all("div", class_="proof"):
        classes = div.get("class", [])
        if "theorem-env" not in classes:
            div["class"] = classes + ["theorem-env", "theorem-proof"]


def _classify_theorem_div(div: Tag, head: Tag | None = None):
    """Try to classify a div as a specific theorem type from its heading text."""
    if head is None:
        head = div.find(["span", "strong", "b"], class_="head")
    if head is None:
        head = div.find(["strong", "b"])
    if head is None:
        return

    text = head.get_text(strip=True)
    m = _THEOREM_HEAD_RE.match(text)
    if not m:
        return

    env_name = m.group(1).lower()
    env_name = _THEOREM_NAME_MAP.get(env_name, env_name)

    classes = div.get("class", [])
    if "theorem-env" not in classes:
        div["class"] = classes + ["theorem-env", f"theorem-{env_name}"]


def tag_algorithm_environments(soup: BeautifulSoup):
    """Add .algorithm-container class to algorithm float environments."""
    # make4ht wraps algorithm floats in <div class="float"> or <div class="algorithm">
    for div in soup.find_all("div", class_="algorithm"):
        classes = div.get("class", [])
        if "algorithm-container" not in classes:
            div["class"] = classes + ["algorithm-container"]

    # Also check for float divs containing "Algorithm" in caption
    for div in soup.find_all("div", class_="float"):
        cap = div.find(class_="caption")
        if cap and "Algorithm" in cap.get_text():
            classes = div.get("class", [])
            if "algorithm-container" not in classes:
                div["class"] = classes + ["algorithm-container"]


# ---------------------------------------------------------------------------
# Mini-TOC
# ---------------------------------------------------------------------------

def build_mini_toc(soup: BeautifulSoup, filename: str):
    """Build a mini table-of-contents from section headings."""
    is_chapter = bool(re.match(r"^Chx?\d+\.html$", filename, re.IGNORECASE))
    is_appendix = bool(re.match(r"^Ax?\d+\.html$", filename, re.IGNORECASE))

    if not is_chapter and not is_appendix:
        return

    # Check if mini-toc already exists
    if soup.find("div", class_="mini-toc"):
        return

    # Find sections (make4ht uses h2 for sections, h3 for subsections in chapters)
    sections = soup.find_all(["h2", "h3", "h4"])

    # Filter to section-level headings (h2) and subsection-level (h3)
    section_entries = []
    for heading in sections:
        # Get the section id from the heading or its parent
        section_id = heading.get("id")
        if not section_id:
            parent = heading.parent
            if parent and isinstance(parent, Tag):
                section_id = parent.get("id")
        if not section_id:
            continue

        level = heading.name  # h2, h3, h4

        # Extract heading text with spaces between child elements
        # (e.g. <span>B.1</span>Title becomes "B.1 Title")
        text = heading.get_text(" ", strip=True)
        # Insert colon after section number: "B.1 Title" -> "B.1: Title"
        text = re.sub(
            r"^(\s*(?:"
            r"(?:Appendix|Chapter)\s+[A-Z0-9]+(?:\.\d+)*\.?"
            r"|[A-Z](?:\.\d+)+\.?"
            r"|[A-Z]?\d+(?:\.\d+)*\.?"
            r"))\s+",
            r"\1: ",
            text,
        )
        if not text:
            continue

        section_entries.append({
            "id": section_id,
            "level": level,
            "text": text,
        })

    if not section_entries:
        return

    # Build the mini-toc HTML
    toc_title = "In this chapter" if is_chapter else "In this section"
    toc_div = soup.new_tag("div", attrs={"class": "mini-toc"})

    title_div = soup.new_tag("div", attrs={"class": "mini-toc-title"})
    title_div.string = toc_title
    toc_div.append(title_div)

    ul = soup.new_tag("ul")

    current_li = None
    for entry in section_entries:
        if entry["level"] == "h2":
            # Top-level section
            current_li = soup.new_tag("li")
            a = soup.new_tag("a", href=f"#{entry['id']}")
            a.string = entry["text"]
            current_li.append(a)
            ul.append(current_li)
        elif entry["level"] == "h3" and current_li is not None:
            # Subsection under current section
            sub_div = current_li.find("div", class_="mini-toc-sub")
            if not sub_div:
                sub_div = soup.new_tag("div", attrs={"class": "mini-toc-sub"})
                current_li.append(sub_div)
            a = soup.new_tag("a", href=f"#{entry['id']}")
            a.string = entry["text"]
            sub_div.append(a)

    toc_div.append(ul)

    # Insert after the first h1 (chapter title)
    h1 = soup.find("h1")
    if h1:
        h1.insert_after(toc_div)
    else:
        body = soup.find("body")
        if body:
            body.insert(0, toc_div)


# ---------------------------------------------------------------------------
# Image fixes
# ---------------------------------------------------------------------------

_DASH_SUFFIX_RE = re.compile(r"-(\.\w+)$")
_RASTER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAGICK_CMD_CACHE: Optional[str] = None
_MAGICK_LOOKED_UP = False


def _strip_dash_suffix(name: str) -> str:
    """Strip make4ht's trailing dash from filenames: 'foo-.png' → 'foo.png'."""
    return _DASH_SUFFIX_RE.sub(r"\1", name)


def _find_magick_cmd() -> Optional[str]:
    """Locate ImageMagick executable once, if available."""
    global _MAGICK_CMD_CACHE, _MAGICK_LOOKED_UP
    if _MAGICK_LOOKED_UP:
        return _MAGICK_CMD_CACHE

    _MAGICK_LOOKED_UP = True
    _MAGICK_CMD_CACHE = shutil.which("magick") or shutil.which("convert")
    return _MAGICK_CMD_CACHE


def _image_size(path: Path, trim: bool = False) -> Optional[tuple[int, int]]:
    """Read image dimensions via ImageMagick (optionally after trim)."""
    magick = _find_magick_cmd()
    if not magick:
        return None

    cmd = [magick, str(path)]
    if trim:
        cmd.extend(["-fuzz", "1%", "-trim", "+repage"])
    cmd.extend(["-format", "%w %h", "info:"])

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return None

    out = result.stdout.strip().split()
    if len(out) != 2:
        return None
    try:
        return int(out[0]), int(out[1])
    except ValueError:
        return None


def _copy_with_optional_trim(src: Path, dst: Path, from_dash_suffix: bool) -> bool:
    """Copy file and trim heavy border whitespace for make4ht-converted rasters."""
    if not from_dash_suffix or src.suffix.lower() not in _RASTER_IMAGE_EXTS:
        shutil.copy2(src, dst)
        return False

    orig_size = _image_size(src)
    trim_size = _image_size(src, trim=True)
    if not orig_size or not trim_size:
        shutil.copy2(src, dst)
        return False

    ow, oh = orig_size
    tw, th = trim_size
    if tw >= ow or th >= oh or ow <= 0 or oh <= 0:
        shutil.copy2(src, dst)
        return False

    dx = ow - tw
    dy = oh - th
    area_reduction = 1.0 - ((tw * th) / (ow * oh))
    should_trim = area_reduction >= 0.05 or dx >= 80 or dy >= 80
    if not should_trim:
        shutil.copy2(src, dst)
        return False

    magick = _find_magick_cmd()
    if not magick:
        shutil.copy2(src, dst)
        return False

    try:
        subprocess.run(
            [magick, str(src), "-fuzz", "1%", "-trim", "+repage", str(dst)],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        shutil.copy2(src, dst)
        return False


def _copytree_strip_dash(src: Path, dst: Path) -> tuple[int, int]:
    """Copy a directory tree, stripping dash suffix and trimming converted rasters."""
    files_copied = 0
    files_trimmed = 0
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_dir():
            sub_copied, sub_trimmed = _copytree_strip_dash(item, dst / item.name)
            files_copied += sub_copied
            files_trimmed += sub_trimmed
        else:
            dest_name = _strip_dash_suffix(item.name)
            dest_path = dst / dest_name
            from_dash_suffix = dest_name != item.name
            if _copy_with_optional_trim(item, dest_path, from_dash_suffix):
                files_trimmed += 1
            files_copied += 1
    return files_copied, files_trimmed


def fix_images(soup: BeautifulSoup, build_dir: Path):
    """Fix image paths and ensure they resolve correctly."""
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue

        # If it's a relative path, check it exists in build dir
        if not src.startswith(("http://", "https://", "/")):
            img_path = build_dir / src
            if not img_path.exists():
                # Try without directory prefix
                basename = Path(src).name
                alt_path = build_dir / basename
                if alt_path.exists():
                    img["src"] = basename

        # Strip make4ht's trailing dash from image src (foo-.png → foo.png)
        src = img.get("src", "")
        dirname = str(Path(src).parent)
        basename = _strip_dash_suffix(Path(src).name)
        img["src"] = f"{dirname}/{basename}" if dirname != "." else basename

        # Ensure responsive attributes
        if not img.get("loading"):
            img["loading"] = "lazy"


# ---------------------------------------------------------------------------
# Equation reference recovery
# ---------------------------------------------------------------------------

_EQ_LABEL_RE = re.compile(r"\\label\s*\{([^}]+)\}")
_EQREF_MISSING_RE = re.compile(
    r"<!--\s*(?:POSTPROC_EQREF|EQREF):\s*([^>]*?)\s*-->\s*"
    r"\(\s*<span[^>]*>\?\?</span>\s*\)",
    re.IGNORECASE,
)
_EQREF_MARKER_RE = re.compile(
    r"<!--\s*(?:POSTPROC_EQREF|EQREF):\s*[^>]*?-->",
    re.IGNORECASE,
)
_MARKED_REF_MARKER_RE = re.compile(
    r"<!--\s*POSTPROC_(?:CREF|REF):\s*([^>]*?)\s*-->",
    re.IGNORECASE,
)
_TEX4HT_REF_RE = re.compile(r"tex4ht:\s*ref:\s*(\S+)", re.IGNORECASE)
_CREF_TOKEN_RE = re.compile(
    r"<!--\s*POSTPROC_(?:CREF|REF):\s*([^>]*?)\s*-->"
    r"|<!--\s*tex4ht:\s*ref:\s*([^>]*?)\s*-->"
    r"|<span[^>]*>\?\?</span>",
    re.IGNORECASE,
)


def _anchor_id_for_label(label: str) -> str:
    """Create a stable synthetic anchor id for a TeX label."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-")[:40] or "label"
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
    return f"eqref-{safe}-{digest}"


def _extract_brace_group(s: str, start: int) -> tuple[Optional[str], int]:
    """Extract { ... } content and return (content, next_index)."""
    if start < 0 or start >= len(s) or s[start] != "{":
        return None, start

    depth = 0
    group_start = start + 1
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "{" and (i == 0 or s[i - 1] != "\\"):
            depth += 1
        elif ch == "}" and (i == 0 or s[i - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return s[group_start:i], i + 1
    return None, len(s)


def _normalize_aux_ref_text(text: str) -> str:
    """Normalize first newlabel field to plain display text."""
    out = text.strip()
    # tex4ht aux uses \rEfLiNK{anchor}{display}
    for _ in range(4):
        m = re.search(r"\\rEfLiNK\{[^{}]*\}\{([^{}]*)\}", out)
        if not m:
            break
        out = m.group(1).strip()

    out = out.replace("\\relax", "").replace("\\ignorespaces", "").strip()
    if out.startswith("{") and out.endswith("}"):
        out = out[1:-1].strip()
    return out


def load_aux_ref_numbers(aux_path: Optional[Path]) -> dict[str, str]:
    """Load label -> display-number map from a LaTeX .aux file."""
    if aux_path is None or not aux_path.exists():
        return {}

    mapping: dict[str, str] = {}
    for line in aux_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.startswith("\\newlabel{"):
            continue

        label_start = line.find("{")
        label, idx = _extract_brace_group(line, label_start)
        if not label:
            continue

        payload_start = line.find("{", idx)
        payload, _ = _extract_brace_group(line, payload_start)
        if payload is None:
            continue

        # payload typically looks like: {<display>}{<page>}...
        if not payload.startswith("{"):
            continue
        display_raw, _ = _extract_brace_group(payload, 0)
        if display_raw is None:
            continue

        display = _normalize_aux_ref_text(display_raw)
        if display:
            mapping[label] = display

    return mapping


def build_eq_label_to_file_map(html_files: list[Path]) -> dict[str, str]:
    """Map TeX equation labels found in mathjax blocks to the containing file."""
    mapping: dict[str, str] = {}
    for html_file in html_files:
        text = html_file.read_text(encoding="utf-8")
        for label in _EQ_LABEL_RE.findall(text):
            mapping.setdefault(label, html_file.name)
    return mapping


def build_label_href_map(html_files: list[Path]) -> dict[str, str]:
    """Map tex4ht label comments to href targets from resolved links."""
    mapping: dict[str, str] = {}
    for html_file in html_files:
        soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "html.parser")
        for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
            m = _TEX4HT_REF_RE.search(str(comment))
            if not m:
                continue
            label = m.group(1).strip()
            parent = comment.parent
            if not isinstance(parent, Tag) or parent.name != "a":
                continue
            href = parent.get("href")
            if href:
                mapping.setdefault(label, href)
    return mapping


def add_equation_label_anchors(soup: BeautifulSoup):
    """Insert synthetic anchors for all \\label{...} found in mathjax blocks."""
    for block in soup.find_all("div", class_="mathjax-env"):
        tex = block.get_text()
        labels = _EQ_LABEL_RE.findall(tex)
        if not labels:
            continue
        for label in labels:
            anchor_id = _anchor_id_for_label(label)
            if soup.find(id=anchor_id):
                continue
            anchor = soup.new_tag("a", id=anchor_id)
            block.insert_before(anchor)


def fix_unresolved_eqrefs(
    html: str,
    current_file: str,
    label_to_file: dict[str, str],
    label_href_map: dict[str, str],
    aux_ref_numbers: dict[str, str],
) -> tuple[str, int, int]:
    """Replace marked '(??)' eqrefs with numbered links using AUX + label map."""
    repaired = 0
    still_unresolved = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal repaired, still_unresolved
        label = match.group(1).strip()

        number = aux_ref_numbers.get(label)
        href = label_href_map.get(label)
        if not href:
            target_file = label_to_file.get(label)
            if target_file:
                anchor_id = _anchor_id_for_label(label)
                href = f"#{anchor_id}" if target_file == current_file else f"{target_file}#{anchor_id}"

        if not href or not number:
            still_unresolved += 1
            return _EQREF_MARKER_RE.sub("", match.group(0), count=1)

        repaired += 1
        return f'(<a href="{href}">{html_lib.escape(number)}</a>)'

    html = _EQREF_MISSING_RE.sub(_replace, html)
    html = _EQREF_MARKER_RE.sub("", html)
    return html, repaired, still_unresolved


def fix_unresolved_marked_refs(
    html: str,
    current_file: str,
    label_to_file: dict[str, str],
    label_href_map: dict[str, str],
    aux_ref_numbers: dict[str, str],
) -> tuple[str, int, int]:
    """Repair unresolved Cref/ref placeholders using queued label markers."""
    repaired = 0
    still_unresolved = 0
    pending_labels: list[tuple[str, int]] = []
    out_parts: list[str] = []
    cursor = 0

    for m in _CREF_TOKEN_RE.finditer(html):
        out_parts.append(html[cursor:m.start()])
        cursor = m.end()
        pos = m.start()

        # Avoid stale markers leaking too far.
        pending_labels = [(lbl, p) for (lbl, p) in pending_labels if pos - p <= 2000]

        cref_marker = m.group(1)
        resolved_ref = m.group(2)

        if cref_marker is not None:
            label = cref_marker.strip()
            if label:
                pending_labels.append((label, pos))
            continue

        if resolved_ref is not None:
            label = resolved_ref.strip()
            for idx, (pending, _) in enumerate(pending_labels):
                if pending == label:
                    pending_labels.pop(idx)
                    break
            out_parts.append(m.group(0))
            continue

        # Unresolved "??" span
        if not pending_labels:
            out_parts.append(m.group(0))
            continue

        pick_idx = 0
        if len(pending_labels) > 1:
            # If some earlier labels in the same marker batch already rendered,
            # the unresolved placeholder usually corresponds to the last label.
            marker_window_start = pending_labels[0][1]
            between = html[marker_window_start:m.start()]
            if "<a " in between.lower() or re.search(r">\s*[0-9A-Za-z]", between):
                pick_idx = len(pending_labels) - 1

        label, _ = pending_labels.pop(pick_idx)
        number = aux_ref_numbers.get(label)
        href = label_href_map.get(label)

        if not href:
            target_file = label_to_file.get(label)
            if target_file:
                anchor_id = _anchor_id_for_label(label)
                href = f"#{anchor_id}" if target_file == current_file else f"{target_file}#{anchor_id}"

        if number and href:
            out_parts.append(f'<a href="{html_lib.escape(href, quote=True)}">{html_lib.escape(number)}</a>')
            repaired += 1
        elif number:
            out_parts.append(html_lib.escape(number))
            repaired += 1
        else:
            out_parts.append(m.group(0))
            still_unresolved += 1

    out_parts.append(html[cursor:])
    html = "".join(out_parts)
    html = _MARKED_REF_MARKER_RE.sub("", html)
    return html, repaired, still_unresolved


# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------

_MATH_INLINE_RE = re.compile(r"\\\(.*?\\\)", re.DOTALL)
_MATH_DISPLAY_RE = re.compile(r"\\begin\{.*?\}.*?\\end\{.*?\}", re.DOTALL)
_MATH_LEFTOVER_RE = re.compile(r"\\[a-zA-Z]+(?:\{[^}]*\})*")


def _strip_math(text: str) -> str:
    """Remove LaTeX math markup from text so it doesn't pollute the search index."""
    text = _MATH_DISPLAY_RE.sub(" ", text)
    text = _MATH_INLINE_RE.sub(" ", text)
    text = _MATH_LEFTOVER_RE.sub(" ", text)
    return text


def _collect_section_text(heading_tag: Tag) -> str:
    """Collect body text following a heading until the next heading of any level.

    make4ht doesn't wrap sections in <section> elements — headings and their
    content are siblings.  Walk forward from the heading, stopping at the next
    heading so that each paragraph is only indexed under its most specific
    section heading.  LaTeX math is stripped to keep the index clean.
    """
    parts: list[str] = []

    for sib in heading_tag.next_siblings:
        if not isinstance(sib, Tag):
            continue
        # Stop at any heading
        if sib.name in ("h1", "h2", "h3", "h4", "h5"):
            break
        text = sib.get_text(" ", strip=True)
        if text:
            parts.append(text)

    raw = " ".join(parts)
    raw = _strip_math(raw)
    return re.sub(r"\s+", " ", raw).strip()


def generate_search_index(output_dir: Path):
    """Generate search-index.json from all HTML files.

    For each section heading, the snippet contains the actual body text that
    follows the heading (paragraphs, lists, theorems, etc.) so that Lunr can
    perform full-text search across all content.
    """
    entries = []
    seen_ids: set[str] = set()

    html_files = sorted(
        [p for p in output_dir.glob("*.html") if p.name != "index.html"]
    )

    for html_path in html_files:
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
        filename = html_path.name

        # Page label from <title>
        title_tag = soup.find("title")
        page_label = title_tag.get_text(strip=True) if title_tag else filename

        # Section headings — the main searchable units
        for heading in soup.find_all(["h2", "h3", "h4"]):
            el_id = heading.get("id")
            if not el_id or el_id in seen_ids:
                continue
            seen_ids.add(el_id)

            title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip()[:200]
            if not title:
                continue

            snippet = _collect_section_text(heading)

            entries.append({
                "page": page_label,
                "href": f"{filename}#{el_id}",
                "title": title,
                "snippet": snippet,
            })

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(entries),
        "entries": entries,
    }

    index_path = output_dir / "search-index.json"
    index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Search index: {len(entries)} entries → {index_path}")


# ---------------------------------------------------------------------------
# Main processing pipeline
# ---------------------------------------------------------------------------

def process_file(
    html_path: Path,
    build_dir: Path,
    label_to_file: dict[str, str],
    label_href_map: dict[str, str],
    aux_ref_numbers: dict[str, str],
    shared_asset_prefix: str = "",
) -> tuple[int, int, int, int]:
    """Process a single HTML file through all post-processing steps."""
    text = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(text, "html.parser")
    filename = html_path.name

    inject_head_resources(soup, filename, shared_asset_prefix)
    ensure_body_top_anchor(soup)
    remove_make4ht_navigation(soup)
    tag_theorem_environments(soup)
    tag_algorithm_environments(soup)
    fix_images(soup, build_dir)
    build_mini_toc(soup, filename)
    add_equation_label_anchors(soup)

    # Serialize and clean up
    html = str(soup)
    html, eq_repaired, eq_unresolved = fix_unresolved_eqrefs(
        html, filename, label_to_file, label_href_map, aux_ref_numbers
    )
    html, cref_repaired, cref_unresolved = fix_unresolved_marked_refs(
        html, filename, label_to_file, label_href_map, aux_ref_numbers
    )
    html = re.sub(r"\n[\t \r\f\v]*\n+", "\n", html)

    html_path.write_text(html, encoding="utf-8")
    return eq_repaired, eq_unresolved, cref_repaired, cref_unresolved


def main():
    parser = argparse.ArgumentParser(description="Post-process make4ht HTML output")
    parser.add_argument("--input", "-i", required=True, help="Build directory with make4ht output")
    parser.add_argument("--output", "-o", required=True, help="Final output directory (website/html/)")
    parser.add_argument("--aux", help="Reference .aux file for eqref recovery", default=None)
    parser.add_argument(
        "--shared-asset-prefix",
        default=None,
        help=(
            "Override prefix for shared assets (common.css/js, chapter.css/js). "
            "Use empty string for same-directory assets."
        ),
    )
    args = parser.parse_args()

    build_dir = Path(args.input).resolve()
    output_dir = Path(args.output).resolve()
    if args.shared_asset_prefix is not None:
        shared_asset_prefix = args.shared_asset_prefix
    else:
        shared_asset_root = Path(__file__).resolve().parent.parent / "html"
        shared_asset_prefix = os.path.relpath(shared_asset_root, output_dir)
        if shared_asset_prefix == ".":
            shared_asset_prefix = ""

    if not build_dir.exists():
        print(f"Error: build directory {build_dir} does not exist", file=sys.stderr)
        sys.exit(1)

    # Step 1: Detect and rename files
    print("Detecting file mapping...")
    mapping = detect_file_mapping(build_dir)
    if mapping:
        print(f"File mapping ({len(mapping)} files):")
        for old, new in sorted(mapping.items()):
            print(f"  {old} → {new}")
        rename_files(build_dir, mapping)
    else:
        print("Warning: no file mapping detected, files may already be renamed")

    # Step 2: Remove unwanted files (TOC page, etc.)
    for pattern in ["book-main*.html", "Ptx*.html"]:
        for f in build_dir.glob(pattern):
            # Only remove if it wasn't mapped to something
            if f.name not in [m for m in mapping.values()]:
                print(f"Removing unwanted: {f.name}")
                f.unlink()

    # Step 3: Process each HTML file
    html_files = sorted(build_dir.glob("*.html"))
    label_to_file = build_eq_label_to_file_map(html_files)
    label_href_map = build_label_href_map(html_files)
    aux_ref_numbers = load_aux_ref_numbers(Path(args.aux).resolve()) if args.aux else {}

    print(f"\nEquation labels detected: {len(label_to_file)}")
    print(f"Resolved href labels detected: {len(label_href_map)}")
    if args.aux and aux_ref_numbers:
        print(f"Reference numbers loaded from AUX: {len(aux_ref_numbers)}")
    elif args.aux:
        print("Warning: AUX map empty; unresolved eqrefs may remain")

    print(f"\nProcessing {len(html_files)} HTML files...")
    total_eq_repaired = 0
    total_eq_unresolved = 0
    total_cref_repaired = 0
    total_cref_unresolved = 0
    for html_file in html_files:
        print(f"  {html_file.name}")
        eq_repaired, eq_unresolved, cref_repaired, cref_unresolved = process_file(
            html_file,
            build_dir,
            label_to_file,
            label_href_map,
            aux_ref_numbers,
            shared_asset_prefix,
        )
        total_eq_repaired += eq_repaired
        total_eq_unresolved += eq_unresolved
        total_cref_repaired += cref_repaired
        total_cref_unresolved += cref_unresolved

    print(
        f"Eqref recovery: repaired={total_eq_repaired}, "
        f"still-unresolved={total_eq_unresolved}"
    )
    print(
        f"Cref/ref recovery: repaired={total_cref_repaired}, "
        f"still-unresolved={total_cref_unresolved}"
    )

    # Step 4: Copy to output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\nCopying files to {output_dir}...")

    # Copy HTML files
    for html_file in build_dir.glob("*.html"):
        shutil.copy2(html_file, output_dir / html_file.name)

    # Copy flat images and assets from build root
    for ext in ["*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.css"]:
        for f in build_dir.glob(ext):
            shutil.copy2(f, output_dir / f.name)

    # Copy image subdirectories, stripping make4ht's "-" suffix from filenames
    # (e.g. diagram-.png → diagram.png)
    for subdir in ["chapters"]:
        src = build_dir / subdir
        dst = output_dir / subdir
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            copied_count, trimmed_count = _copytree_strip_dash(src, dst)
            print(
                f"  Copied {subdir}/ ({copied_count} files, "
                f"{trimmed_count} converted rasters whitespace-trimmed)"
            )

    # Step 5: Generate search index
    print("\nGenerating search index...")
    generate_search_index(output_dir)

    print("\nPost-processing complete.")


if __name__ == "__main__":
    main()
