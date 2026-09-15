# Factory console design QA

Final result: passed

## Visual target and scope

The user authorized a redesign using Linear's public product screenshots after
Mobbin's signed-in app library was inaccessible. This is a Factory adaptation
of those screens, not a pixel-identical clone of Linear's features or branding.

Source visual truth:

- [Linear My issues](https://linear.app/docs/my-issues), captured from its public
  product image: `/tmp/linear-my-issues-reference.png` (1864 × 842).
- [Linear application redesign](https://linear.app/now/how-we-redesigned-the-linear-ui),
  its dark “After” product image: `/tmp/linear-dark-app-reference.png` (2352 × 1380).
- Full comparison: `/tmp/factory-linear-comparison.png`. References and rendered
  Factory views were placed together in one browser-rendered comparison board
  and opened for inspection. Each image was scaled proportionally to 704px wide.

Reference CSS viewport sizes are not published. The My issues reference is a
magnified partial app capture; the After reference contains desktop-window
chrome. They are used for the approved visual language, not precise coordinate
matching. Factory intentionally uses its existing full-width list and issue
views, without Linear's avatars, editing controls, or inbox data.

## Implementation evidence

Browser: isolated Chromium / Playwright, actual built React assets, disposable
API fixtures, no production credentials or dispatches. Desktop viewport:
1440 × 1080 CSS pixels, device scale 1. Mobile: 390 × 844, scale 1. Full-page
screenshots retain natural content height rather than stretching to viewport.

- `/tmp/factory-dashboard-overview.png`: 1440 × 1400, review and active work.
- `/tmp/factory-dashboard-work-list.png`: 1440 × 1080, Active scope selected
  with keyboard focus visible.
- `/tmp/factory-dashboard-work.png`: 1440 × 1080, recorded verified issue.
- `/tmp/factory-dashboard-mobile.png`: mobile issue inspection.
- `/tmp/factory-dashboard-mobile-work.png`: mobile filters and returned attempt.
- `/tmp/factory-dashboard-mobile-overview.png`: mobile summary and active work.

Focused inspection used the original My issues capture for navigation, icons,
status and text treatment, and full-size Factory Work and mobile captures for
control alignment, filter states, wrapping, and evidence readability. These
were opened alongside the sources, in addition to the comparison board.

## Findings and iteration history

1. **Resolved P2: mobile quick filters used two rows, and Clear occupied a
   separate row.** The first mobile Work capture placed the first record below
   a long toolbar. Quick scopes now scroll inside their navigation region,
   while Filters and Clear share a row. The post-fix mobile Work capture shows
   the returned record within the initial viewport. Keyboard activation,
   filter expansion and viewport overflow checks pass.
2. **Expected adaptation: Factory has more evidence qualifiers than Linear.**
   Returned-set limits, stale snapshots, dry runs, and recorded verification
   remain explicit. They are necessary for operator decisions.
3. **Expected adaptation: system status is a summary, not a detail link.**
   The API supplies counts; no unsupported system-detail destination was added.

No actionable P0/P1/P2 visual findings remain.

## Fidelity surfaces

- **Typography:** platform sans-serif, compact 24px title, 14px section labels,
  13px controls/records, secondary metadata, and monospace issue references.
  The hierarchy follows the reference without bundling a remote font. Long
  technical strings wrap; 200% text sizing does not overflow the page.
- **Spacing:** narrow persistent sidebar, compact top toolbar, contiguous
  review summary, fine list separators, and earlier active work. Mobile uses
  top navigation, stacked review rows, and expandable attempt metadata.
- **Color:** neutral dark surfaces and selected navigation replace the prior
  paper/green/gold theme. Lavender is restrained. Status colors accompany
  distinct icons and text; tested supporting-text/status/navigation contrast
  is at least 4.5:1.
- **Assets:** real Radix React icons are bundled locally. No raster artwork is
  needed for this workspace. Factory's existing F mark remains; Linear logos,
  avatars, screenshots, and unrelated product assets are not shipped.
- **Copy:** Factory tasks and evidence retain their meaning. No invented
  progress, live acceptance, actions, people, or metrics were added.

## Validation

- React build passed.
- Browser regression passed: existing overview/work/issue navigation, sidebar
  review shortcuts, keyboard quick scopes, filter preservation, URL reload and
  history, safe links/text, unknown status, stale retry, race handling,
  empty/error states, visibility refresh, and GET-only requests.
- Overview width checks: 320, 700, 850, 1024, 1280 CSS px; Work also checks 560.
- 200% text sizing, mobile expandable metadata, keyboard filter toggle, and
  primary mobile touch targets checked.
- Four FastAPI console tests passed in a disposable Factory image with source
  mounted read-only: authentication boundary, sanitized API/security headers,
  issue/static assets/schema, invalid issue rejection.
- Browser page-error collection is empty.

These checks are not a comprehensive assistive-technology or accessibility
certification. A production deployment is outside this redesign PR.
