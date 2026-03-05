#!/usr/bin/env bash
# build.sh — Orchestrates an isolated, stage-pure make4ht + MathJax pipeline.
#
# Usage: bash website/latex_to_html/build.sh [tex_file] [output_dir]
#
# Arguments:
#   tex_file    TeX source file (default: book-main.tex)
#   output_dir  Final HTML output directory (default: website/html)
#
# Stages:
#   0. Create isolated source snapshot (no writes to repo source tree)
#   1. Capture reference AUX in stage dir
#   2. Run make4ht → 20_make4ht_raw/
#   3. Generate MathJax macros → 30_macros/macros.json
#   4. Inject macros into copied HTML → 40_mathjax_injected/
#   5. Post-process copied HTML → 50_postprocess_output/
#   6. Publish stage output into output_dir/

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PIPELINE_DIR="$SCRIPT_DIR"
RUN_STARTED_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

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
# Make output_dir absolute if relative
[[ "$OUTPUT_DIR" != /* ]] && OUTPUT_DIR="$REPO_ROOT/$OUTPUT_DIR"

BUILD_ROOT="$REPO_ROOT/website/_build_${TEX_BASE}"
RUN_DIR="$BUILD_ROOT/latest"
SOURCE_SNAPSHOT="$RUN_DIR/00_source_snapshot"
STAGE_AUX="$RUN_DIR/10_aux"
STAGE_MAKE4HT="$RUN_DIR/20_make4ht_raw"
STAGE_MACROS="$RUN_DIR/30_macros"
STAGE_MATHJAX="$RUN_DIR/40_mathjax_injected"
STAGE_POST_INPUT="$RUN_DIR/45_postprocess_input"
STAGE_POST_OUTPUT="$RUN_DIR/50_postprocess_output"
MANIFEST_PATH="$RUN_DIR/manifest.json"

SNAPSHOT_TEX_PATH="$SOURCE_SNAPSHOT/$TEX_REL"
SNAPSHOT_AUX_PATH="$(dirname "$SNAPSHOT_TEX_PATH")/${TEX_BASE}.aux"
REF_AUX="$STAGE_AUX/reference.aux"

CFG_FILE="$PIPELINE_DIR/book.cfg"
MK4_FILE="$PIPELINE_DIR/book.mk4"
MACROS_JSON="$STAGE_MACROS/macros.json"
SHARED_ASSETS_DIR="$REPO_ROOT/website/html"

echo "=========================================="
echo "Isolated make4ht + MathJax Pipeline"
echo "=========================================="
echo "  Repo root:   $REPO_ROOT"
echo "  TeX file:    $TEX_REL"
echo "  Build root:  $BUILD_ROOT"
echo "  Run dir:     $RUN_DIR"
echo "  Output dir:  $OUTPUT_DIR"
echo ""

# ---------------------------------------------------------------------------
# Stage 0: create isolated run workspace and source snapshot
# ---------------------------------------------------------------------------
echo "[Stage 0] Preparing isolated workspace..."
rm -rf "$BUILD_ROOT"
mkdir -p \
    "$RUN_DIR" \
    "$STAGE_AUX" \
    "$STAGE_MAKE4HT" \
    "$STAGE_MACROS" \
    "$STAGE_MATHJAX" \
    "$STAGE_POST_INPUT" \
    "$STAGE_POST_OUTPUT"

echo "[Stage 0] Creating source snapshot..."
rsync -a \
    --exclude=".git/" \
    --exclude="website/_build_*/" \
    --exclude="website/html/" \
    "$REPO_ROOT/" "$SOURCE_SNAPSHOT/"

if [ ! -f "$SNAPSHOT_TEX_PATH" ]; then
    echo "Error: TeX file not found in source snapshot: $SNAPSHOT_TEX_PATH" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Stage 1: capture a full LaTeX .aux for eqref fallback
# ---------------------------------------------------------------------------
# tex4ht can miss some align labels (and older CI tex4ht can miss many refs).
# Keep a standard LaTeX-generated .aux snapshot before make4ht runs so the
# postprocessor can recover unresolved \eqref entries.
echo "[Stage 1] Capturing reference AUX..."
if [ -f "$SNAPSHOT_AUX_PATH" ] && ! grep -q '\\ifx\\rEfLiNK\\UnDef' "$SNAPSHOT_AUX_PATH"; then
    cp "$SNAPSHOT_AUX_PATH" "$REF_AUX"
    echo "  Using existing LaTeX aux from snapshot: $SNAPSHOT_AUX_PATH"
else
    if [ -f "$SNAPSHOT_AUX_PATH" ]; then
        echo "  Existing aux appears tex4ht-generated; regenerating via latexmk..."
    else
        echo "  No aux found; generating via latexmk..."
    fi

    if (
        cd "$SOURCE_SNAPSHOT" \
            && latexmk -pdf -interaction=nonstopmode -shell-escape -f \
                -output-directory="$STAGE_AUX" \
                "$SNAPSHOT_TEX_PATH" >/dev/null 2>&1
    ); then
        GENERATED_AUX="$STAGE_AUX/${TEX_BASE}.aux"
        if [ -f "$GENERATED_AUX" ]; then
            cp "$GENERATED_AUX" "$REF_AUX"
            echo "  Generated reference aux: $REF_AUX"
        else
            echo "  Warning: latexmk completed but $GENERATED_AUX was not found."
        fi
    else
        echo "  Warning: latexmk aux generation failed; eqref fallback may be limited."
    fi
fi

# ---------------------------------------------------------------------------
# Pre-build: ensure XeTeX format has enough main_memory
# ---------------------------------------------------------------------------
# The XeTeX format bakes main_memory at build time; the repo texmf.cnf
# sets a larger value.  Rebuild a user-level xelatex.fmt if needed so
# make4ht (which uses xelatex) has enough memory for CJK + tex4ht.
# Uses a temp dir for the probe so parallel builds don't collide.
export TEXMFCNF="$SOURCE_SNAPSHOT:"

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
# Stage 2: make4ht
# ---------------------------------------------------------------------------
echo "[Stage 2] Running make4ht in snapshot..."

# Run make4ht with XeTeX engine, mathjax passthrough, chapter splitting
(
    cd "$SOURCE_SNAPSHOT"
    make4ht -x -u -s \
        -c "$CFG_FILE" \
        -e "$MK4_FILE" \
        -d "$STAGE_MAKE4HT/" \
        "$SNAPSHOT_TEX_PATH" \
        "html,mathjax,2,fn-in" \
        "" \
        "" \
        "-shell-escape"
)

echo "  make4ht output in $STAGE_MAKE4HT/"
echo "  Files: $(ls "$STAGE_MAKE4HT"/*.html 2>/dev/null | wc -l) HTML files"
echo ""

# ---------------------------------------------------------------------------
# Stage 3: Generate MathJax macros
# ---------------------------------------------------------------------------
echo "[Stage 3] Generating MathJax macros..."

uv run python3 "$PIPELINE_DIR/generate_macros.py" \
    "$MACROS_JSON" \
    "$SOURCE_SNAPSHOT/math-macros.sty" \
    "$SOURCE_SNAPSHOT/book-macros.sty" \
    "$SOURCE_SNAPSHOT/chapters"

echo "  Macros written to $MACROS_JSON"
echo ""

# ---------------------------------------------------------------------------
# Stage 4: Inject macros into copied HTML
# ---------------------------------------------------------------------------
echo "[Stage 4] Injecting MathJax macros..."

rsync -a --delete "$STAGE_MAKE4HT/" "$STAGE_MATHJAX/"

uv run python3 "$PIPELINE_DIR/inject_mathjax_macros.py" "$STAGE_MATHJAX" "$MACROS_JSON"

echo ""

# ---------------------------------------------------------------------------
# Stage 5: Post-processing from copied input
# ---------------------------------------------------------------------------
echo "[Stage 5] Post-processing..."

rsync -a --delete "$STAGE_MATHJAX/" "$STAGE_POST_INPUT/"

uv run python3 "$PIPELINE_DIR/postprocess.py" \
    --input "$STAGE_POST_INPUT" \
    --output "$STAGE_POST_OUTPUT" \
    --aux "$REF_AUX" \
    --shared-asset-prefix ""

echo ""

# ---------------------------------------------------------------------------
# Stage 6: Publish to output directory
# ---------------------------------------------------------------------------
echo "[Stage 6] Publishing stage output to $OUTPUT_DIR..."
mkdir -p "$OUTPUT_DIR"
rsync -a "$STAGE_POST_OUTPUT/" "$OUTPUT_DIR/"

# Keep chapter assets resolvable when publishing to non-default output paths.
for file in common.css chapter.css common.js chapter.js common_components.js; do
    if [ -f "$SHARED_ASSETS_DIR/$file" ]; then
        if [ "$SHARED_ASSETS_DIR/$file" != "$OUTPUT_DIR/$file" ]; then
            cp "$SHARED_ASSETS_DIR/$file" "$OUTPUT_DIR/$file"
        fi
    fi
done
if [ -d "$SHARED_ASSETS_DIR/assets" ]; then
    if [ "$SHARED_ASSETS_DIR/assets" != "$OUTPUT_DIR/assets" ]; then
        mkdir -p "$OUTPUT_DIR/assets"
        rsync -a "$SHARED_ASSETS_DIR/assets/" "$OUTPUT_DIR/assets/"
    fi
fi

RUN_FINISHED_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
GIT_COMMIT="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat >"$MANIFEST_PATH" <<EOF
{
  "started_utc": "$RUN_STARTED_UTC",
  "finished_utc": "$RUN_FINISHED_UTC",
  "git_commit": "$GIT_COMMIT",
  "tex_file": "$TEX_REL",
  "output_dir": "$OUTPUT_DIR",
  "stages": {
    "source_snapshot": "$SOURCE_SNAPSHOT",
    "aux": "$STAGE_AUX",
    "make4ht_raw": "$STAGE_MAKE4HT",
    "macros": "$STAGE_MACROS",
    "mathjax_injected": "$STAGE_MATHJAX",
    "postprocess_input": "$STAGE_POST_INPUT",
    "postprocess_output": "$STAGE_POST_OUTPUT"
  }
}
EOF

echo "=========================================="
echo "Pipeline complete!"
echo "Output: $OUTPUT_DIR/"
echo "Run cache: $RUN_DIR/"
echo "Manifest: $MANIFEST_PATH"
echo "=========================================="
