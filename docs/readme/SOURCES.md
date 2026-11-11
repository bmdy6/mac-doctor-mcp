# README image sources

The hero is a real stdio MCP run using only disposable synthetic data.
The image reads selected scalar fields and numeric metrics from stdout; it
does not display a fabricated transcript. All four PNG variants are rendered
by the proposed `art` showcase-banner template. The manifest records each hash.

Reproduce from the target checkout with Python and uv installed. From the
readme-from-code skill checkout, run:

```sh
node scripts/run.mjs capture --repo TARGET --work WORK --id demo --role core --timeout-ms 120000 -- 'uv run --quiet python docs/readme/showcase/demo.py'
node scripts/run.mjs art --repo TARGET --work WORK --spec TARGET/docs/readme/art.json --out TARGET/docs/readme
```

Replace TARGET with this checkout and WORK with a disposable output directory.
The rendering source is art.json; the operation source is showcase/demo.py.
Capture receipts and task reports are not part of this repository.
The checked-in images were generated on 2026-11-11 with the unmerged banner
feature and caption fix in skill-readme-from-code; upstream v0.4.0 cannot render
this template yet. No model/API call or real user data is part of the demo.
