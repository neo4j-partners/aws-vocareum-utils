# Slides

Presentation decks for this repository, written in [Marp](https://marp.app/) Markdown. They publish to the microsite at https://neo4j-partners.github.io/aws-vocareum-utils.

## Decks

| File | What it is |
| --- | --- |
| `webinar-overview.md` | The webinar proposal: pitch, audience, takeaways, and the slide-by-slide outline. It is the source the deck was written from, not a deck. |
| `webinar-overview-slides.md` | The webinar deck: Neo4j Courses on Vocareum. It covers how Neo4j hosts its courses on Vocareum and the tools built to run them. |

## Preview while editing

Marp CLI fails on Node 25 and later. Use Node 22 (`brew install node@22`), and run `npm install` in `site/` once.

```bash
cd site
export PATH=/opt/homebrew/opt/node@22/bin:$PATH
npm run preview:slides
```

This opens a live-reload preview of every deck in this directory at http://localhost:8080/.

## Build and publish

```bash
cd site
npm run build:slides   # renders the decks into site/modules/ROOT/attachments/slides/
npm run build          # builds the Antora site into site/build/site/
npm run serve          # serves it at http://localhost:8080
```

**Do not commit the built HTML.** `site/modules/ROOT/attachments/` is gitignored. On every push to `main` that touches `docs/slides/` or `site/`, `.github/workflows/deploy-site.yml` rebuilds the decks and the site and deploys them to GitHub Pages.

## Add a deck

1. **Write the deck:** Add `NAME-slides.md` here. Copy the frontmatter and `<style>` block from `webinar-overview-slides.md`.
2. **Register it:** Add `NAME-slides` to `DECKS` in `site/build-slides.sh`. Only decks listed there are published.
3. **Give it a page:** Add `site/modules/ROOT/pages/slides/slides-NAME.adoc`, modeled on `slides-webinar-overview.adoc`.
4. **Link it:** Add the page to `site/modules/ROOT/nav.adoc` and to the table in `site/modules/ROOT/pages/slides.adoc`.

## Slide conventions

- **Speaker notes:** Put full sentences in `<!-- ... -->` comments. The slide shows short phrases.
- **Dense slides:** Add `<!-- _class: dense -->` to a slide with a long table or many lines. It shrinks the text to fit.
- **Title and closing slides:** Use `<!-- _class: lead -->` to center the content.
- **Real identifiers:** Never put account, organization, or SCP identifiers on a slide. Write them as `${PLACEHOLDER}`.
