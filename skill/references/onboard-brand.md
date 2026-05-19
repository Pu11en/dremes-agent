# Flow: Onboard a New Brand (Fast)

Onboard a brand in ~5 questions. Logo palette auto-extracted with colorthief.
Ends with Pinterest board drain. No fluff, no optional detours.

## Trigger

User says any of:
- "add a brand" / "new brand" / "onboard"
- "set up my brand" / "I want to create ads for my..."

## Before Starting

```bash
ls brands/  # If slug exists, skip to add-product or add-refs
```

## The Fast Interview (5 Questions Max)

Ask ONE question at a time. Keep answers moving.

### 1. Brand Name
"What's your brand called?"
→ Set `display_name`. Auto-derive slug (lowercase, hyphens).

### 2. Products
"What products do you sell? (comma-sep names)"
→ e.g. "Soap, Face Cream, Sunscreen Stick"

### 3. Pool Strategy
"One reference pool per product, or one pool for the whole brand?"
→ If per-product: use `--product-required`. Each product gets its own Pinterest board and ref folder.
→ If shared: products share one pool.

### 4. Logo
"Send your logo — I'll extract your brand colors automatically."
→ User sends photo. Run colorthief to extract palette.
→ Show extracted colors: "Palette: navy #204050, cream #F0E0B0, red #B03030. Look right?"
→ User confirms or overrides with `--colors "#hex1,#hex2"`

### 5. Product Images
"Send one product photo per product."
→ User sends photos. Save to `brand_assets/{slug}/products/`.
→ Match by order or filename to product names.

### STOP HERE. Build the config.

Run without dry-run immediately — this is the build step:
```bash
python3 skill/scripts/onboard_brand.py \
  --name "Brand Name" \
  --slug "brand-slug" \
  --products "Product 1,Product 2" \
  --colors "#hex1,#hex2,#hex3" \
  --palette-desc "description" \
  --logo-file "/tmp/logo.png" \
  --product-files "/tmp/product1.png,/tmp/product2.png" \
  [--product-required]
```

### After Config Is Built — Offer Pinterest Drain

"Brand is set. Want to drain a Pinterest board for refs? Send the URL."

If yes → `python3 skill/scripts/drain_board.py --brand {slug} --board-url "{url}" --pool {pool}`

## What Gets Created

1. `brands/{slug}.json` — Full brand config with colors, products, pool setup
2. `brand_assets/{slug}/logo/` — Logo saved
3. `brand_assets/{slug}/products/` — Product images saved
4. `brand_assets/{slug}/references/` — Per-product ref folders (if product-required)
5. `output/{slug}/` — Generated ad output folder
6. Gallery pages auto-discover new brand

## Vibe/Voice/Headlines Are OPTIONAL

Don't ask during fast onboarding. User can add later or run:
```bash
python3 skill/scripts/onboard_brand.py --name "Brand" --vibe "..." --voice "..." --headlines "..."
```

## Adding a Product Later

See `add-product.md` for adding a product to an existing brand (separate flow).

## Script Reference

```bash
# Fast onboard (what we use)
python3 skill/scripts/onboard_brand.py \
  --name "Brand Name" \
  --slug "brand-slug" \
  --products "Product 1,Product 2" \
  --colors "#hex1,#hex2,#hex3" \
  --logo-file "path/to/logo.png" \
  --product-files "path/p1.png,path/p2.png"

# Per-product pools
python3 skill/scripts/onboard_brand.py ... --product-required

# Full options (vibe, voice, etc — post-onboard polish)
python3 skill/scripts/onboard_brand.py \
  --name "Brand" --vibe "description" --voice "description" \
  --headlines "H1,H2,H3" --vibe-phrases "V1,V2" \
  --prop-themes "theme1,theme2" --forbidden-props "prop1,prop2" \
  --platforms "instagram,facebook" --time-slots "12:00,17:00"
```

## Troubleshooting

- **Brand exists**: Skip onboarding, offer add-product or add-refs
- **colorthief fails**: Install with `pip install colorthief` or ask user for hex codes
- **Product image count mismatch**: Match images to products by send order
