# Accessibility

curate.d should be usable by anyone who runs it, operates it or reads its documentation,
whatever assistive technology they use. A barrier is a bug.

## What curate.d exposes today

curate.d has no graphical interface yet; the web interface is deferred. People use it
through:

- **The control-plane HTTP API.** Responses are structured JSON, so any client or
  assistive tool can consume them.
- **Logs.** JSON lines in deployment; in a terminal, the development renderer colours
  output only when stderr is a TTY, and every level and event name is spelled out in text,
  never conveyed by colour alone.
- **Make targets and operator commands**, which report through plain text and exit codes.
- **Documentation.** Markdown in the repository, plus self-contained HTML diagrams under
  `docs/diagrams/`.

## Commitments

- Information is never conveyed by colour alone, in logs, terminal output or diagrams.
- The HTML diagrams carry text labels and ARIA roles for their parts, follow the system's
  light or dark preference, and honour `prefers-reduced-motion`.
- A diagram doesn't carry information that only it holds: the README's
  [How it works](README.md#how-it-works) describes the pipeline in text, and a new diagram
  comes with a text description of what it shows.
- When the web interface is built, it targets [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
  level AA, and its OpenSpec change will say how that is tested.

## Known limitations

- The HTML diagrams are dense, interactive SVG. A screen reader can reach their labels,
  but following the connections between components is easier from the surrounding text.
- Error responses and log events are written for operators and assume familiarity with
  the system's terms (placement, quarantine, workflow); [`docs/recovery.md`](docs/recovery.md)
  explains the named events.

## Reporting a barrier

Open an [accessibility issue](https://github.com/jorgejch/curate-d/issues/new?template=accessibility.yml),
or use the [contact form](https://hddd.dev/contact/) if you'd rather not report it
publicly. Say what you were trying to do, where, and what got in the way.
