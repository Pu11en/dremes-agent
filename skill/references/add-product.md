# Flow: Add a Product to an Existing Brand

Add a product to a brand that's already onboarded. Handles brand JSON update,
product image, reference pool, and Pinterest board drain if needed.

## Trigger

User says:
- "add a product" / "new product"
- "add another product to [brand]"
- "I have a new product for [brand]"

## Before Starting

```bash
ls brands/{slug}.json  # Verify brand exists
cat brands/{slug}.json | python3 -c "import sys,json; d=json.load(sys.stdin); print([p['name'] for p in d['products']])"
```

If product name already exists in the list, tell user and skip.

## The Fast Interview (4 Questions)

### 1. Which Brand?
"Which brand are we adding to?"
→ Auto-resolve if only one brand. Show list if multiple.

### 2. Product Name
"What's the new product called?"

### 3. Container Type
"What kind of container? (jar, bottle, soap-bar, spray-bottle, twist-up stick, tube)"
→ Sets `container` field. Impacts how product is framed in ads.

### 4. Product Image
"Send the product photo."
→ User sends image. Save to `brand_assets/{slug}/products/{filename}.png`
→ Update brand JSON with label_file path.

### 5. Pool Strategy
"Same pool as another product, or its own pool?"
→ If shared: reuse existing pool_slug.
→ If own pool: create new pool_slug (auto from name: "rejuvenating cream" → "rejuvenating-cream").

### STOP HERE. Add to brand JSON.

## Manual JSON Update

The onboard script doesn't have an "add product" subcommand. Edit the brand JSON directly:

```python
# Example: adding "Bug Repellent" to cinco-h-ranch
import json

with open('brands/cinco-h-ranch.json') as f:
    brand = json.load(f)

new_product = {
    "name": "Bug Repellent",
    "label_file": "bug-repellent.png",
    "pool_slug": "bug-repellent",
    "container": "spray-bottle",
    "cap_rule": "dark cap on spray bottle",
    "triggers": ["bug", "repellent", "spray", "outdoor"],
    "keywords": ["bug", "repellent", "spray", "outdoor", "citronella", "natural"],
    "forbidden_text": [
        {"pattern": "DEET", "severity": "error", "reason": "no synthetic chemicals"},
        {"pattern": "kills bugs", "severity": "error", "reason": "repels, does not kill"}
    ],
    "voice_note": "Bug Repellent: Beef Tallow base with Citronella, Cedarwood, Eucalyptus. Claims: Natural outdoor protection, Ranch-tested defense, Chemical-free.",
    "real_claims": ["Natural outdoor protection", "Ranch-tested defense", "Chemical-free"],
    "real_ingredients": "Tallow base / Citronella / Cedarwood / Eucalyptus / Lemongrass"
}

brand['products'].append(new_product)

with open('brands/cinco-h-ranch.json', 'w') as f:
    json.dump(brand, f, indent=2)
```

## After Adding — Create Ref Folders

```bash
mkdir -p brand_assets/{slug}/references/{pool_slug}
```

If product-required brand, the pool will auto-discover on next gallery load.

## Offer Pinterest Drain

"Product added. Want to drain a Pinterest board for refs?"

If yes → `python3 skill/scripts/drain_board.py --brand {slug} --board-url "{url}" --pool {pool_slug}`

## Post-Add Polish (Optional, Later)

User can add voice, claims, ingredients, keywords later by editing the JSON or asking:
"Add voice note to Honey Vanilla Soap: ..."
"Add claims to Face Cream: ..."
"Add forbidden text to Sunscreen Stick: no SPF numbers"

## Troubleshooting

- **Product already exists**: Show list, skip
- **Brand not found**: Offer onboarding first
- **JSON syntax after edit**: Run `python3 -m json.tool brands/{slug}.json` to verify
