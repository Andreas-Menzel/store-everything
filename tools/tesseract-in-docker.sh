#!/bin/sh
# A `tesseract` that runs in the OCR extractor's own image.
#
# The OCR tests need the engine on PATH. CI installs the Debian package, and on Linux so can a
# developer — but on macOS the choice is a large Homebrew formula whose Tesseract is a different
# build from the one the instance actually ships. This is the third option: the engine from
# `extractors/Dockerfile.ocr`, invoked exactly as the extractor invokes it, so what passes locally
# passed against the image that will run in production.
#
#   ln -s "$PWD/tools/tesseract-in-docker.sh" /some/dir/on/your/path/tesseract
#
# or, for one test run:
#
#   mkdir -p .shim && ln -sf "$PWD/tools/tesseract-in-docker.sh" .shim/tesseract
#   PATH="$PWD/.shim:$PATH" uv run --directory server pytest tests/test_document_text.py
#
# The image is built on first use and reused after that. It is a *test* convenience: nothing in the
# core, the extractors or the deployment refers to this file.
set -eu

IMAGE="${SE_OCR_IMAGE:-store-everything-extractors-ocr:dev}"
HERE=$(cd "$(dirname "$0")/.." && pwd -P)

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "building $IMAGE (first use)…" >&2
    docker build -q -f "$HERE/extractors/Dockerfile.ocr" -t "$IMAGE" "$HERE/extractors" >&2
fi

# `tesseract --version` and friends take no paths and need nothing mounted.
case "${1:-}" in
    -*|"")
        exec docker run --rm --entrypoint tesseract "$IMAGE" "$@"
        ;;
esac

# Otherwise argument 1 is an input file and argument 2 an output base, both in one directory the
# caller made. That directory is mounted **at the path the caller used**, not at its resolved one:
# on macOS a temporary directory is reached through /var, which is a symlink to /private/var, and a
# container that only had the resolved path would be handed arguments naming a directory it cannot
# see. Tesseract's response to that is to print a complaint and exit *zero*.
given=$(dirname "$1")
room=$(cd "$given" && pwd -P)
exec docker run --rm \
    --user "$(id -u):$(id -g)" \
    --volume "$room:$given" \
    --workdir "$given" \
    --env OMP_THREAD_LIMIT=1 \
    --entrypoint tesseract \
    "$IMAGE" "$@"
