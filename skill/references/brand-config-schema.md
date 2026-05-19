# Brand Config Schema

Every brand has a file at `brands/<slug>.json`. This is the single source of
truth for voice, claims, products, and forbidden language.

## Shape

```jsonc
{
  "slug": "island-splash",
  "name": "Island Splash",
  "tagline": "...",
  "voice": {
    "tone": "...",
    "brief": "...",
    "headlines": ["...", "..."]
  },
  "products": [
    {
      "slug": "mango-passion",
      "name": "Mango Passion",
      "allowed_claims": ["...", "..."],
      "ref_dir": "brand_assets/island-splash/references/mango-passion"
    }
  ],
  "forbidden": ["no medical claims", "no SPF numbers", "..."],
  "social": {
    "instagram": "islandsplashjuice",
    "facebook": "...",
    "connected_accounts": ["islandsplashjuice IG"]
  }
}
```

## Field rules

- `slug` — lowercase, hyphenated, matches filename.
- `voice.headlines` — at least 3 approved headlines the generator can draw from.
- `products[].allowed_claims` — whitelist; generator picks from this list only.
- `products[].ref_dir` — path relative to repo root; the ref pool lives here.
- `forbidden` — freeform list of things the generator must never say.

## Real examples

- `brands/island-splash.json`
- `brands/cinco-h-ranch.json`
