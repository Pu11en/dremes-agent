---
name: dremes
version: 2.1.0
description: Dremes Ad Generation Pipeline — Telegram bot for non-technical brand owners
---

# Dremes Pipeline

## ⚠️ SELF-SYNC RULE — READ FIRST

This file is the canonical source of truth for how the Dremes pipeline works. If you modify `dremes_agent.py` or any pipeline logic, you MUST update this file to match before committing. The generation model, brand JSON structure, CLI flags, and critical rules described here must stay in sync with the code. Never push a code change without checking whether this skill needs updating.

## RESPONSE STYLE

Talk like a terse human. One or two sentences. No bullet points, no markdown formatting. Never show tool names, file paths, code, or terminal output. Never say "I searched the files" or "Let me look" — just give the answer.

When the user asks for the gallery: immediately reply with `https://dremes-agent-v2-production.up.railway.app/`. Do not run commands.

When the user asks to see refs, ads, or the gallery: send an inline keyboard button using the Telegram API. Example Python to send a button with web_app URL:

```python
import requests, os, json
token = os.environ['TELEGRAM_BOT_TOKEN']
chat_id = CHAT_ID  # the chat/thread ID
payload = {
  'chat_id': chat_id,
  'text': 'Review your images:',
  'reply_markup': json.dumps({'inline_keyboard': [[{'text': 'Open Gallery', 'web_app': {'url': 'https://dremes-agent-v2-production.up.railway.app/island-splash/refs'}}]]})
}
requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload)
```

Use the correct brand URL path. Brand URLs:
- Island Splash refs: https://dremes-agent-v2-production.up.railway.app/island-splash/refs
- Island Splash ads: https://dremes-agent-v2-production.up.railway.app/island-splash/ads
- Cinco H Ranch refs: https://dremes-agent-v2-production.up.railway.app/cinco-h-ranch/refs
- Cinco H Ranch ads: https://dremes-agent-v2-production.up.railway.app/cinco-h-ranch/ads

After any action, end with the direct link:
- Refs: `https://dremes-agent-v2-production.up.railway.app/{brand}/refs`
- Ads: `https://dremes-agent-v2-production.up.railway.app/{brand}/ads`
- Posts: `https://dremes-agent-v2-production.up.railway.app/{brand}/posts`

## HOW GENERATION WORKS

The generate pipeline uses a PRESERVE / REPLACE / ADAPT triage model. Every element from the reference ad is sorted into one of three buckets:

**PRESERVE** — keep exactly from reference:
- Composition, framing, 3×3 grid layout, visual hierarchy
- Camera angle, depth of field, perspective
- Lighting direction, quality, highlight and shadow behavior
- Text zone positions (where text appears, not what it says)
- Subject pose, expression, product-to-subject spatial relationship

**REPLACE** — swap with our brand-native equivalent:
- Product → our exact product image (pasted pixel-for-pixel, never redrawn)
- Text → our approved brand headlines, verbatim, in the same text zones
- Colors → entire scene repainted with brand palette only, 60-30-10 rule
- Logo → our logo, small, single corner, no banner or box

**ADAPT** — keep structural intent, make brand-native:
- Decorative elements (splashes, sparkles, borders, badges) → keep their role, adapt aesthetic
- Produce / ingredients → replace with our product's real ingredients
- Background structure → preserve solid/gradient/scene type, re-theme to brand world
- Any element that doesn't fit → find the closest brand-native equivalent. NEVER delete wholesale.

The old rule "Drop ALL produce/ingredient elements" is GONE. The new rule is: ADAPT, don't delete.

## ON-DEMAND RESEARCH

You can add web research to any ad generation. The agent uses Jina Search + Reader API (auth via JINA_API_KEY env var) to search the web and inject findings into the composer prompt — grounding ads in real-world accuracy.

Add `--research "your query"` to the generate command. Research is on-demand only, never automatic. If JINA_API_KEY isn't set, research is skipped silently (no error).

## WHAT THE USER SAYS → WHAT YOU RUN

### "add a brand" / "new brand" / "onboard"
→ Follow `references/onboard-brand.md` — 5-question fast interview, logo palette extraction, Pinterest drain

### "add a product" / "new product"
→ Follow `references/add-product.md` — add product to existing brand JSON, create ref pool, offer Pinterest drain

### "clear the refs" / "delete all refs" / "remove refs"
→ `python3 skill/scripts/dremes_cli.py clear-refs --brand {brand} --pool {pool}`

### "generate ads" / "make ads" / "run the pipeline"
→ `python3 dremes_agent.py --brand {brand} --pool --category {pool}`
If user says a number like "2 ads": add `--count 2`
If user wants research with generation: add `--research "query"` (e.g. `--research "cinco h ranch tallow skincare benefits"`)
Generated ads are automatically sent to Telegram as photos by `dremes_agent.py` after they sync to the gallery. Do not send a duplicate generated-ad photo manually unless the user explicitly asks.
Generated ads have human labels in the ads page and Telegram caption, like `Cinco Cream 014`. Use those labels when the user asks to edit a specific ad.

### "whats the status" / "how many refs" / "check"
→ `python3 skill/scripts/dremes_cli.py status --brand {brand}`

### "scrape this board" / "drain" / "get refs from Pinterest"
→ `python3 skill/scripts/drain_board.py --brand {brand} --board-url "{url}" --pool {pool}`
   (defaults to 500 images. For large boards, user can re-run to get more — duplicates auto-skip)
   User can say "just 200" → add `--max-images 200`
   User can say "get more from that board" → run same command again, new-only images get added

### "research" / "look up" / "search for"
→ `python3 skill/scripts/jina_research.py research "{topic}" --deep`
   Or search only: `python3 skill/scripts/jina_research.py search "{query}" --num 5`
   Requires JINA_API_KEY on Railway. Falls back to DuckDuckGo scraper (skill/scripts/search.py) if Jina isn't available.

### "compose posts" / "write captions"
→ `python3 skill/scripts/compose_posts.py --brand {brand} --min-ads 3`
This only creates review drafts. It must never schedule or post anything.

### "schedule" / "post to instagram"
→ `python3 skill/scripts/schedule_post.py --brand {brand} --from-composed`
This schedules only posts that were explicitly approved in the posts review UI. Unapproved drafts are skipped.

### "populate the site" / "put ads on the website" / "update the client dashboard" / "sync ads to drewpullen.com"
→ `python3 skill/scripts/populate_client_site.py --brand {brand}`
If user says "both brands": add `--all`
After running, reply with the site link: `https://www.drewpullen.com/brands`
This copies approved ad PNGs from the Railway volume into the client site repo, rebuilds the manifest, commits, and pushes. Vercel auto-deploys within ~30 seconds.

## CRITICAL RULES

- Pinterest scraping: ONLY use drain_board.py. Never try to scrape Pinterest HTML yourself.
  Run: python3 skill/scripts/drain_board.py --brand {brand} --board-url "{url}" --pool {pool}
  The script uses Playwright Chromium for reliable board scraping. Default 500 images.
  Duplicates are auto-skipped — safe to re-run. User can say "get more from that board".
  If Playwright not installed: pip install playwright && playwright install chromium
- Commit and push only code, config, or instruction changes. Do not commit/push just because runtime refs or ads were generated on the Railway volume.
- Generated ads auto-post to Telegram from inside `dremes_agent.py`; if Telegram sending fails, the ad still succeeds.
- NEVER skip approval gates — humans approve refs and ads in the gallery before next step
- No fake promotions: FREE, % OFF, GIVEAWAY are forbidden in generated ads
- Product labels must be pixel-faithful — paste exact product image, never redraw
- BLACK CAPS for Island Splash products
- Every reference element gets a bucket: PRESERVE, REPLACE, or ADAPT. Do NOT delete elements — adapt them to the brand.
- All product knowledge lives in structured brand JSON (`brands/{slug}.json`). Never hardcode brand details in prompts.
- `JINA_API_KEY` is an env var on Railway — never hardcode it.

## BRANDS

Island Splash (island-splash) — Florida Caribbean juice. Pools: drinks
Cinco H Ranch (cinco-h-ranch) — Texas tallow skincare. Pools: cream, soap, sunscreen-stick

## GALLERY URL

https://dremes-agent-v2-production.up.railway.app/
