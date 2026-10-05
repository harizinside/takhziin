---
title: takhziin Dashboard
---

# Dashboard overrides

The dashboard is the entry point for a DBA. Aspect:

- Stats grid is the first thing the eye lands on → use accent green for OK, destructive for failed.
- "Recent runs" is the most-touched file → row hover, accessible live badge announcements (role="status" aria-atomic).
- Telegram pill on the right of stats must NOT steal the OK/failed attention.
- Empty state must explain what to do next.

Pattern adherence:
- Status badge uses atomic role="status" with full phrase (e.g. "Backup succeeded 2 minutes ago"), not bare "OK" or "2".
- Tables overflow horizontally on mobile (overflow-x: auto wrapper).
- 16px base body; 1.5 line-height.
- Animations respect prefers-reduced-motion.
- Min 44×44 px touch targets on the action buttons.

Color accents override:
- `--color-status-ok`: same as accent (#22C55E)
- `--color-status-fail`: same as destructive (#EF4444)
- `--color-status-running`: muted amber (#F59E0B)

Layout:
- Stats grid collapses to 1-column at <768px, 2-column at 768–1024px, 4-column at >=1024px.
- Recent runs table wraps in overflow-x:auto; cells use whitespace-nowrap.