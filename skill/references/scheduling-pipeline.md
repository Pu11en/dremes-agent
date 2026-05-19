# Flow: Schedule Posts (Stage 2 — Not Built)

Future flow. Here for planning, not yet implemented.

## Goal

- 2 posts/day per brand (AM + PM).
- 5-slide carousels.
- User reviews once → agent schedules automatically.
- Fresh caption + hashtags each time.

## Dependencies

- Blotato MCP (already connected — see `.hermes/mcp_settings.json`).
- Per-brand connected social accounts in `brands/<slug>.json`.
- Enough generated ads in the pool to avoid repetition.

## Flow (planned)

1. Agent picks 5 fresh ads from the pool.
2. Generates caption + hashtags from the brand voice.
3. Shows user the bundle → "approve to schedule?"
4. On approval, calls Blotato to schedule at the user's AM/PM slots.
5. Marks those ads as `status: scheduled` in `ads.json`.
6. Writes next scheduled post to `website/public/data/schedule.json` for dashboard visibility.

## Out of scope for MVP

Not in MVP. Build after the 3 core flows (onboard, add-refs, generate) work
end-to-end through the website chat.
