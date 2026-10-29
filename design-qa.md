# Factory console design QA

Use this guide to verify a console change against [DESIGN.md](DESIGN.md).
It describes checks to run and evidence to retain; it is not a current test
result or a deployment approval.

## Visual references

Factory uses Linear's public [My issues screen](https://linear.app/docs/my-issues)
and [application redesign](https://linear.app/now/how-we-redesigned-the-linear-ui)
as references for navigation, list density, status icons, and surface hierarchy.
Keep Factory's identity, read-only evidence semantics, and explicit operator
approvals. Do not copy unrelated branding, avatars, editing controls, or data.

When comparing screenshots, record the source URL and capture date. Preserve
image proportions and distinguish a product screenshot from a known CSS viewport.
The public reference images do not establish exact application dimensions.

## Run the checks

Follow the dependency and browser setup in the [console guide](factory/README.md).
Build the actual React assets before testing. The browser checks use disposable
fixtures and local servers; do not point them at a production installation.

From the repository root, after installing the documented dependencies:

```bash
npm run build --prefix factory/console
export FACTORY_CONSOLE_EVIDENCE_DIR="$(mktemp -d)"
export NODE_PATH=/tmp/factory-console-checks/node_modules
node tools/test_factory_console_browser.cjs
node tools/test_factory_issue_control_browser.cjs
node tools/test_factory_issue_control_backend_browser.cjs
npm run check:ui --prefix factory/console
node tools/test_factory_ui_package.cjs
npm run build-storybook --prefix factory/console
node tools/test_factory_storybook.cjs
```

The backend browser check also requires the bridge's pinned Python dependencies;
set `FACTORY_CONSOLE_TEST_PYTHON` when using a separate Python environment.
Set `FACTORY_CONSOLE_CHROMIUM` only when selecting an existing compatible browser.
See [the design system guide](docs/design-system.md) for catalog checks and the
[quality workflow](.github/workflows/hatchet-quality.yml) for CI dependency setup.

Screenshots are written to `FACTORY_CONSOLE_EVIDENCE_DIR` and contain fictional
test data. Preserve the generated files with the reviewed commit and test
results when sharing evidence. A temporary path on one developer's machine is
not a durable public artifact.

## Inspect the result

Review Overview, Work, and issue inspection on desktop and mobile. Include
320, 390, 768, 1024, and 1440px widths, intermediate layouts, and 200% text scaling.
Check keyboard focus, navigation, filters, URL reload/history, loading and
disabled states, errors, empty views, and long identifiers.

- Typography and spacing follow the compact hierarchy in `DESIGN.md`.
- Narrow layouts preserve readable records, visible search, usable filters,
  and touch targets without page-level horizontal overflow.
- Color supplements icons and text; supporting text and controls remain legible.
- Local icons and static styles preserve the console's content security policy.
- Stale snapshots, dry runs, missing evidence, source limits, and historical
  verification remain explicit.
- Unknown outcomes never imply safe takeover, successful execution, or permission
  to retry. Summary counts do not imply an unsupported detail view.
- Browsing stays read-only. Ownership changes retain their separate reason,
  preview, confirmation, and audit behavior.

Inspect the generated screenshots alongside the references. Automated assertions
and screenshot review complement each other; neither alone establishes full
assistive-technology or accessibility compliance.

## Record the outcome

Report the exact commit, checks run, results, browser and viewport information,
evidence location, unresolved findings, and any checks that could not run.
Fix consequential findings and rerun the affected checks before reporting a pass.
Do not carry a previous change's pass result forward as evidence for a new revision.
Production deployment remains a separate operation.
