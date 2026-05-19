# Flow: Add References to a Brand

Intake real product photos into a product's reference pool. The generator
will pull from this pool when creating ads.

## Two Ways to Add References

### 1. User Sends Images Directly
When user sends images in chat, the agent saves them to the pool.

### 2. Drain a Pinterest Board (Auto-Categorize)
Pull images from a Pinterest board and auto-sort into product pools.

---

## Method 1: User Sends Images

### Trigger

User sends images and says:
- "add these to [brand] [product]"
- "here are reference photos for my [product]"
- "add these as refs"

### Steps

1. Identify brand + product
2. Run the script:

```bash
python3 skill/scripts/add_refs.py \
  --brand <slug> \
  --product "Product Name" \
  --image /path/to/image.jpg
```

Or batch:

```bash
python3 skill/scripts/add_refs.py \
  --brand <slug> \
  --product "Product Name" \
  --images img1.jpg img2.jpg img3.jpg
```

3. Confirm:

```
"✅ Added X ref photo(s) to [Brand] / [Product].
Pool now has Y reference images."
```

---

## Method 2: Drain Pinterest Board

### When to Use

- User wants to quickly bulk-add reference photos
- They have a Pinterest board with product images
- Brand has multiple product categories (like Cinco H Ranch with soap, sunscreen, etc.)

### Trigger

User says:
- "drain my Pinterest board"
- "pull refs from this board"
- "get reference photos from Pinterest"

### Brand Pool Strategies

**Single Pool (Island Splash style):**
All products go to one pool because it's just drinks.

```bash
python3 skill/scripts/drain_board.py \
  --brand island-splash \
  --board-url "https://pinterest.com/user/board/abc123"
```

**Multi Pool (Cinco H Ranch style):**
AI auto-categorizes images into different product pools.

```bash
python3 skill/scripts/drain_board.py \
  --brand cinco-h-ranch \
  --board-url "https://pinterest.com/user/board/abc123" \
  --auto-categorize
```

### How Auto-Categorization Works

1. Download image from Pinterest
2. Send to Gemini Vision
3. AI looks at the image and decides: "This is soap" or "This is sunscreen"
4. Image saved to the correct `brand_assets/<brand>/references/<product>/` folder

### Example: Cinco H Ranch Products

```
Products:
- Soap (keywords: soap, lather, bar)
- Sunscreen (keywords: sunscreen, SPF, beach)
- Lip Balm (keywords: lip, balm, chap)

Board has mixed images:
- Image of soap → goes to Soap pool
- Image of sunscreen → goes to Sunscreen pool
- Image of lips → goes to Lip Balm pool
```

### Preview First (Dry Run)

Always preview before downloading:

```bash
python3 skill/scripts/drain_board.py \
  --brand cinco-h-ranch \
  --board-url "..." \
  --dry-run
```

Shows what would be categorized without downloading.

### Full Command

```bash
python3 skill/scripts/drain_board.py \
  --brand cinco-h-ranch \
  --board-url "https://pinterest.com/user/board/xyz" \
  --auto-categorize \
  --max-images 50 \
  --delay 2
```

Options:
- `--max-images`: Limit how many to download (default: 50)
- `--delay`: Seconds between downloads (default: 2)
- `--dry-run`: Preview without downloading
- `--skip-existing`: Skip if already in pool

---

## Pinterest URL Formats

The script handles various Pinterest URL formats:

```
https://pinterest.com/username/board-name/abc123/
https://pin.it/short-url
https://pinterest.com/pin/123456789/
```

For boards, you need the full board URL (not individual pins).

---

## Finding the Board URL

1. Go to Pinterest
2. Navigate to your board
3. Click the board name
4. Copy the URL from browser address bar

---

## Reference Photo Guidelines

Tell the user what makes a good reference:

**Good references:**
- ✅ Product in a lifestyle setting (beach, kitchen, table)
- ✅ Natural lighting
- ✅ Product clearly visible
- ✅ Multiple angles
- ✅ Shows the product being enjoyed

**Bad references:**
- ❌ Blurry or dark photos
- ❌ Product alone on white background (too sterile)
- ❌ Other brands visible
- ❌ Cluttered backgrounds
- ❌ Stock photos (look fake)

---

## Non-Negotiables

- **Never** pull from `output/` or `website/public/images/ads/`
- **Never** feed generated ads back as references
- Don't overwrite existing refs — always increment index
- Refs must be real product photos, not AI-generated

---

## After Adding References

**Generation happens automatically** — `add_refs.py` calls `asset_ads.py` for each ref immediately after storing it. You do NOT need to run generation separately.

```
"✅ Added X ref photo(s) to [Brand] / [Product].

Pool now has Y reference images.
Z ad(s) generated and added to the ad pool.

Ad pool status: [N] unused ads
Scheduling triggers automatically at 10+ unused ads."
```

---

## Troubleshooting

### "No images found"
- Pinterest may require login
- Board may be private
- Try individual pin URLs instead

### "Unclassified" images
- AI couldn't identify the product
- Manually add to correct pool using `add_refs.py`

### Pinterest rate limiting
- Use `--delay` to slow down
- Try smaller batches with `--max-images 20`
