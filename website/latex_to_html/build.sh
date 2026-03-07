#!/usr/bin/env bash
# build.sh — Build PDF + HTML from a TeX file in an isolated temp directory.
#
# Usage: bash website/latex_to_html/build.sh [tex_file] [output_dir]
#
# Arguments:
#   tex_file    TeX source file (default: book-main.tex)
#   output_dir  Final output directory for HTML + PDF (default: website/html)
#
# The build runs in a temp directory named after the TeX file, allowing
# parallel builds of different files without collisions. The PDF is placed
# at $output_dir/$tex_base.pdf alongside the HTML output.
#
# Set KEEP_BUILD_DIR=1 to preserve the temp directory for debugging.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PIPELINE_DIR="$SCRIPT_DIR"

TEX_FILE="${1:-book-main.tex}"
if [[ "$TEX_FILE" = /* ]]; then
    case "$TEX_FILE" in
        "$REPO_ROOT"/*) TEX_REL="${TEX_FILE#$REPO_ROOT/}" ;;
        *)
            echo "Error: tex_file must be inside the repository: $TEX_FILE" >&2
            exit 1
            ;;
    esac
else
    TEX_REL="$TEX_FILE"
fi
TEX_BASE="$(basename "$TEX_REL" .tex)"

OUTPUT_DIR="${2:-$REPO_ROOT/website/html}"
[[ "$OUTPUT_DIR" != /* ]] && OUTPUT_DIR="$REPO_ROOT/$OUTPUT_DIR"

# Create temp build directory named after the tex file (parallel-safe)
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/build-${TEX_BASE}-XXXXXX")"
cleanup() {
    if [ "${KEEP_BUILD_DIR:-}" = "1" ]; then
        echo "Build dir preserved: $BUILD_DIR"
    else
        rm -rf "$BUILD_DIR"
    fi
}
trap cleanup EXIT

SOURCE_DIR="$BUILD_DIR/source"
MAKE4HT_DIR="$BUILD_DIR/make4ht_raw"
MACROS_DIR="$BUILD_DIR/macros"
MATHJAX_DIR="$BUILD_DIR/mathjax_injected"
POST_INPUT_DIR="$BUILD_DIR/postprocess_input"
POST_OUTPUT_DIR="$BUILD_DIR/postprocess_output"

echo "=========================================="
echo "Build Pipeline (PDF + HTML)"
echo "=========================================="
echo "  Repo root:   $REPO_ROOT"
echo "  TeX file:    $TEX_REL"
echo "  Build dir:   $BUILD_DIR"
echo "  Output dir:  $OUTPUT_DIR"
echo ""

# ---------------------------------------------------------------------------
# Stage 0: Create source snapshot
# ---------------------------------------------------------------------------
echo "[Stage 0] Creating source snapshot..."
mkdir -p "$SOURCE_DIR" "$MAKE4HT_DIR" "$MACROS_DIR" \
         "$MATHJAX_DIR" "$POST_INPUT_DIR" "$POST_OUTPUT_DIR"

rsync -a \
    --exclude=".git/" \
    --exclude="website/_build_*/" \
    --exclude="website/html/" \
    "$REPO_ROOT/" "$SOURCE_DIR/"

SNAPSHOT_TEX="$SOURCE_DIR/$TEX_REL"
if [ ! -f "$SNAPSHOT_TEX" ]; then
    echo "Error: TeX file not found: $SNAPSHOT_TEX" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Pre-build: ensure XeTeX format has enough main_memory
# ---------------------------------------------------------------------------
export TEXMFCNF="$SOURCE_DIR:"

NEEDED_MEM=$(kpsewhich --var-value main_memory 2>/dev/null || echo 0)
PROBE_DIR=$(mktemp -d)
ACTUAL_MEM=$(cd "$PROBE_DIR" \
    && xelatex -interaction=batchmode '\tracingstats=1 \stop' >/dev/null 2>&1 \
    && grep -o 'out of [0-9]*' texput.log 2>/dev/null | grep -o '[0-9]*' || echo 0)
rm -rf "$PROBE_DIR"

if [ "$ACTUAL_MEM" -lt "$NEEDED_MEM" ] 2>/dev/null; then
    echo "[Pre-build] Rebuilding xelatex format (main_memory $ACTUAL_MEM -> $NEEDED_MEM)..."
    fmtutil-user --byfmt xelatex >/dev/null 2>&1
fi

# ---------------------------------------------------------------------------
# Stage 1: Build PDF
# ---------------------------------------------------------------------------
echo "[Stage 1] Building PDF..."
(
    cd "$SOURCE_DIR"
    latexmk -pdf -interaction=nonstopmode -shell-escape -f "$TEX_REL" || true
)

PDF_FILE="$SOURCE_DIR/${TEX_BASE}.pdf"
if [ -f "$PDF_FILE" ]; then
    echo "  PDF built successfully"
else
    echo "  Warning: PDF was not produced"
fi

# ---------------------------------------------------------------------------
# Stage 2: Capture reference AUX (from PDF build) for eqref fallback
# ---------------------------------------------------------------------------
echo "[Stage 2] Capturing reference AUX..."
SNAPSHOT_AUX="$(dirname "$SNAPSHOT_TEX")/${TEX_BASE}.aux"
REF_AUX="$BUILD_DIR/reference.aux"

if [ -f "$SNAPSHOT_AUX" ] && ! grep -q '\\ifx\\rEfLiNK\\UnDef' "$SNAPSHOT_AUX"; then
    cp "$SNAPSHOT_AUX" "$REF_AUX"
    echo "  Using AUX from PDF build"
else
    echo "  Warning: no usable AUX; eqref fallback may be limited"
fi

# ---------------------------------------------------------------------------
# Stage 3: make4ht
# ---------------------------------------------------------------------------
echo "[Stage 3] Running make4ht..."
CFG_FILE="$PIPELINE_DIR/book.cfg"
MK4_FILE="$PIPELINE_DIR/book.mk4"
(
    cd "$SOURCE_DIR"
    make4ht -x -u -s \
        -c "$CFG_FILE" \
        -e "$MK4_FILE" \
        -d "$MAKE4HT_DIR/" \
        "$SNAPSHOT_TEX" \
        "html,mathjax,2,fn-in" \
        "" \
        "" \
        "-shell-escape"
)

echo "  make4ht output: $(ls "$MAKE4HT_DIR"/*.html 2>/dev/null | wc -l) HTML files"
echo ""

# ---------------------------------------------------------------------------
# Stage 4: Generate MathJax macros
# ---------------------------------------------------------------------------
echo "[Stage 4] Generating MathJax macros..."
MACROS_JSON="$MACROS_DIR/macros.json"

(cd "$REPO_ROOT" && uv run python3 "$PIPELINE_DIR/generate_macros.py" \
    "$MACROS_JSON" \
    "$SOURCE_DIR/math-macros.sty" \
    "$SOURCE_DIR/book-macros.sty" \
    "$SOURCE_DIR/chapters")

echo "  Macros written to $MACROS_JSON"
echo ""

# ---------------------------------------------------------------------------
# Stage 5: Inject macros into copied HTML
# ---------------------------------------------------------------------------
echo "[Stage 5] Injecting MathJax macros..."

rsync -a --delete "$MAKE4HT_DIR/" "$MATHJAX_DIR/"

(cd "$REPO_ROOT" && uv run python3 "$PIPELINE_DIR/inject_mathjax_macros.py" \
    "$MATHJAX_DIR" "$MACROS_JSON")

echo ""

# ---------------------------------------------------------------------------
# Stage 6: Post-processing
# ---------------------------------------------------------------------------
echo "[Stage 6] Post-processing..."

rsync -a --delete "$MATHJAX_DIR/" "$POST_INPUT_DIR/"

(cd "$REPO_ROOT" && uv run python3 "$PIPELINE_DIR/postprocess.py" \
    --input "$POST_INPUT_DIR" \
    --output "$POST_OUTPUT_DIR" \
    --aux "$REF_AUX" \
    --shared-asset-prefix "")

echo ""

# ---------------------------------------------------------------------------
# Stage 7: Publish to output directory
# ---------------------------------------------------------------------------
echo "[Stage 7] Publishing to $OUTPUT_DIR..."
mkdir -p "$OUTPUT_DIR"
rsync -a "$POST_OUTPUT_DIR/" "$OUTPUT_DIR/"

# Copy PDF to output directory
if [ -f "$PDF_FILE" ]; then
    cp "$PDF_FILE" "$OUTPUT_DIR/${TEX_BASE}.pdf"
    echo "  PDF: $OUTPUT_DIR/${TEX_BASE}.pdf"
fi

# Keep chapter assets resolvable when publishing to non-default output paths.
SHARED_ASSETS_DIR="$REPO_ROOT/website/html"
for file in common.css chapter.css common.js chapter.js; do
    if [ -f "$SHARED_ASSETS_DIR/$file" ] && [ "$SHARED_ASSETS_DIR/$file" != "$OUTPUT_DIR/$file" ]; then
        cp "$SHARED_ASSETS_DIR/$file" "$OUTPUT_DIR/$file"
    fi
done
if [ -d "$SHARED_ASSETS_DIR/assets" ] && [ "$SHARED_ASSETS_DIR/assets" != "$OUTPUT_DIR/assets" ]; then
    mkdir -p "$OUTPUT_DIR/assets"
    rsync -a "$SHARED_ASSETS_DIR/assets/" "$OUTPUT_DIR/assets/"
fi

echo "=========================================="
echo "Build complete!"
echo "  HTML: $OUTPUT_DIR/"
if [ -f "$PDF_FILE" ]; then
    echo "  PDF:  $OUTPUT_DIR/${TEX_BASE}.pdf"
fi
echo "=========================================="
