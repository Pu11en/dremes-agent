# Flow: Schedule or Post an Ad

Post approved ads to social platforms (Instagram, TikTok, etc.) via Blotato.

## Trigger

User says:
- "post this ad"
- "publish it"
- "schedule this for tomorrow"
- "show my scheduled posts"
- "cancel that scheduled post"

## Before Starting: Check Brand Setup

Make sure the brand has a connected social account:

```bash
python3 skill/scripts/schedule_post.py --list-accounts
```

This shows all connected accounts and their IDs.

If no accounts are connected, tell the user:
```
"To post to Instagram, you need to connect an account in Blotato.
Go to https://blotato.com → Settings → Connected Accounts → Connect Instagram.
Once connected, run --list-accounts again and I'll get the account ID."
```

## Command: List Accounts

```bash
python3 skill/scripts/schedule_post.py --list-accounts
```

Output:
```
=== Connected Social Accounts ===

  ID: 27011
  Platform: instagram
  Name: @islandsplashjuice

  ID: 14209
  Platform: instagram
  Name: @drew__pullen
```

Save the ID for the brand config.

## Command: Post Immediately

```bash
python3 skill/scripts/schedule_post.py \
  --post \
  --brand island-splash \
  --ad-id island-splash-1715000000-004.png
```

What happens:
1. Loads ad from `website/public/data/ads.json`
2. Uploads image to Blotato
3. Creates Instagram post
4. Polls until published
5. Updates ad status to "published"

Output:
```
=== Posting Ad: island-splash-1715000000-004.png to instagram ===

📤 Uploading image...
   Uploaded: https://media.blotato.com/abc123.png
📤 Posting to Instagram (account: 27011)...
   Submission ID: sub_xyz789
⏳ Waiting for post to publish...
   Status: in-progress
   Status: in-progress
   Status: published

✅ POSTED! https://www.instagram.com/p/DXBn-KlFFqH/
```

## Command: Show Scheduled Posts

```bash
python3 skill/scripts/schedule_post.py --show-scheduled --brand island-splash
```

Output:
```
=== Scheduled Posts: island-splash ===

  ⏳ scheduled-1715000000
     Platform: instagram
     Scheduled: 2026-04-25T09:00:00Z
     Caption: Tropical vibes only...

  ✅ scheduled-1715000001
     Platform: instagram
     Scheduled: 2026-04-24T09:00:00Z
     Caption: Mango Monday...
```

## Command: Cancel Scheduled Post

```bash
python3 skill/scripts/schedule_post.py \
  --cancel \
  --brand island-splash \
  --post-id scheduled-1715000000
```

## Command: List All Commands

```bash
python3 skill/scripts/schedule_post.py
```

Shows help with all options.

## Setting Up Account ID

After listing accounts, update the brand config:

Edit `brands/island-splash.json`:
```json
{
  "scheduling": {
    "instagram_account_id": "27011"
  }
}
```

## How It Works

### 1. Image Upload
```
POST /media/uploads → get presigned URL
PUT <presigned URL> → upload image binary
```

### 2. Post Creation
```
POST /posts → creates Instagram post
```

### 3. Status Polling
```
GET /posts/:id → check status
Polls every 5 seconds until "published" or "failed"
```

## Platform Support

| Platform | Status |
|----------|--------|
| Instagram | ✅ Working |
| Facebook | ✅ Should work |
| TikTok | ⚠️ Blotato support varies |
| LinkedIn | ⚠️ Blotato support varies |
| Twitter/X | ⚠️ Blotato support varies |

## Troubleshooting

### "Instagram account not configured"
Edit `brands/<slug>.json` and set `scheduling.instagram_account_id`

### "Image not found"
Check the ad exists in `website/public/data/ads.json` and the image path is correct.

### "Post failed"
Check Blotato dashboard at https://blotato.com for error details.
Common issues: Instagram policy violations, account permissions.

### "No accounts connected"
User needs to connect accounts in Blotato first.

## Non-Negotiables

- Always confirm with user before posting
- Respect Instagram's 5 hashtag limit
- Never post without approval (unless user enables auto-post)
- Track all posts in `website/public/data/ads.json`

## Future Features

- True scheduling (Blotato API scheduling)
- Multi-platform posting (one approval, publish everywhere)
- Post analytics tracking
