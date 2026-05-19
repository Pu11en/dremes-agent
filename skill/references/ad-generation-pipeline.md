# Ad Generation Pipeline — Reference

This document describes how the Dremes ad generation pipeline works. It is a reference for understanding the flow, not instructions for manual prompt writing. The pipeline is fully automatic — `dremes_agent.py` handles everything.

## Pipeline Steps (automatic)

1. **Reverse Analysis** — The reference ad is fed to Gemini 2.5 Pro, which extracts: subject, product count, composition (3×3 grid), text (every word + font + position), decorative elements, background, lighting, mood.

2. **Product Picking** — Matches product count to the reference, then picks least-used products from the brand tally. Produce hints from the ref act as tiebreakers.

3. **Composer — PRESERVE / REPLACE / ADAPT** — The core model:
   - **PRESERVE**: Composition, framing, camera angle, lighting direction, text zone positions, subject pose
   - **REPLACE**: Product → our exact product image. Text → our approved headlines. Colors → brand palette. Logo → our logo
   - **ADAPT**: Decorative elements keep their role but get brand aesthetic. Produce/ingredients become our product's real ingredients. Nothing gets deleted — every element gets adapted

4. **Forbidden Text Scan** — The composed prompt is scanned for forbidden words (URLs, pricing, medical claims, etc.). Errors abort, warnings proceed.

5. **Image Generation** — Primary: Gemini 3 Pro Image Preview with reference + product images + logo. Fallback: OpenAI GPT Image 2. Always 4:5 portrait.

6. **Save & Sync** — Sidecar saved (analyses + prompt), tally updated, ad copied to website gallery, sent via Telegram.

## On-Demand Research

Add `--research "query"` to inject web research (via Jina) into the composer prompt. Research is on-demand only — never automatic. Requires JINA_API_KEY on Railway.

## Brand Configuration

All product knowledge lives in `brands/{slug}.json`. The JSON structure is a template:
- `identity` — brand voice, palette, headlines, vibe, format
- `products` — name, label file, container type, cap rules, keywords, forbidden text, real claims
- `ad_creative_rules` — brand-specific rules for generation
- `global_forbidden_text` — words that must never appear in any ad

## Image Model Inputs

The image model receives, in order:
1. Reference ad image (INPUT 1) — for composition/framing/lighting reference only
2. Product label image(s) (INPUT 2-N) — the exact product to paste
3. Logo image (last INPUT) — for small corner badge

The model is instructed to paste product images pixel-for-pixel, never redraw or reinterpret them.
