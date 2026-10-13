#!/usr/bin/env sh
#
# Build every published Marp deck into the Antora attachments directory.
#
# Run it from site/, or through `npm run build:slides`, which is what CI does.
# The output is build output: modules/ROOT/attachments/ is gitignored and
# nothing here should ever be committed. Committing the HTML is how a stale
# deck ships after someone edits the Markdown and forgets to rebuild.

set -eu

MARP="./node_modules/.bin/marp"
SRC="../docs/slides"
OUT="modules/ROOT/attachments/slides"

# Published decks. Each name is a file in docs/slides/ without its .md suffix.
# webinar-overview.md is the proposal the deck was written from, not a deck.
DECKS="webinar-overview-slides"

# Start from empty, so a renamed or removed deck cannot keep serving.
rm -rf "$OUT"
mkdir -p "$OUT"

for deck in $DECKS; do
  echo "  building $deck"
  "$MARP" --allow-local-files "$SRC/$deck.md" -o "$OUT/$deck.html"
done

echo "done. $(find "$OUT" -name '*.html' | wc -l | tr -d ' ') decks in $OUT"
