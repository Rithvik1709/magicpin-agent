# 🚀 Vera Bot - Deployment Guide (Railway.app)

## Quick Start (5 minutes)

### Step 1: Push to GitHub
```bash
cd /Users/rithvik/Desktop/magic/magicpin-agent
git init
git add .
git commit -m "Vera Bot - ready for production"
git remote add origin https://github.com/YOUR_USERNAME/magicpin-agent.git
git branch -M main
git push -u origin main
```

### Step 2: Deploy to Railway
1. Go to [railway.app](https://railway.app)
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your `magicpin-agent` repository
4. Railway will auto-detect the Python project and start building
5. Click on the service and go to **"Settings"**
6. Add the following environment variables:
   ```
   LLM_PROVIDER=openai (or leave blank for fallback)
   LLM_API_KEY=sk-... (your OpenAI key, optional)
   PORT=8080
   ```
7. Railway will auto-generate a public URL like: `https://magicpin-agent-prod.up.railway.app`

### Step 3: Verify Deployment
```bash
# Test the public endpoints
curl https://YOUR_RAILWAY_URL/v1/healthz
curl https://YOUR_RAILWAY_URL/v1/metadata
```

---

## What Railway Provides

✅ **Automatic deployment** on every git push  
✅ **Auto-scaling** - handles traffic spikes  
✅ **Logs & monitoring** - built-in  
✅ **Custom domain support** (if needed)  
✅ **Free tier** - up to 5GB bandwidth/month  

---

## Required Endpoints (Judge Will Test These)

```bash
# 1. Health check
GET /v1/healthz
Response: {"status": "ok", "uptime_seconds": 123, "contexts_loaded": {...}}

# 2. Bot metadata
GET /v1/metadata
Response: {"team_name": "Vera Prime", "model": "...", "approach": "..."}

# 3. Push context
POST /v1/context
Body: {"scope": "trigger", "context_id": "...", "version": 1, "payload": {...}}
Response: {"accepted": true, "ack_id": "...", "stored_at": "..."}

# 4. Compose message
POST /v1/tick
Body: {"now": "2026-04-29T10:00:00Z", "available_triggers": ["t1", "t2"]}
Response: {"actions": [{...}, {...}]}

# 5. Handle reply
POST /v1/reply
Body: {"conversation_id": "...", "merchant_id": "...", "from_role": "merchant", "message": "...", "turn_number": 1}
Response: {"action": "send", "body": "...", "cta": "open_ended"}
```

---

## Testing Your Deployment

After Railway gives you a public URL:

```bash
# Set your URL
VERA_URL="https://your-railway-app.up.railway.app"

# Test healthz
curl $VERA_URL/v1/healthz

# Test metadata
curl $VERA_URL/v1/metadata

# Test context push
curl -X POST $VERA_URL/v1/context \
  -H "Content-Type: application/json" \
  -d '{
    "scope": "category",
    "context_id": "restaurants",
    "version": 1,
    "payload": {
      "slug": "restaurants",
      "voice": {"tone": "friendly"}
    }
  }'

# Test tick
curl -X POST $VERA_URL/v1/tick \
  -H "Content-Type: application/json" \
  -d '{
    "now": "2026-04-29T10:00:00Z",
    "available_triggers": []
  }'
```

---

## Troubleshooting

### Bot not starting?
Check logs in Railway dashboard:
1. Select your service
2. Click "Logs"
3. Look for error messages

### Port issue?
Railway automatically sets `PORT` env var. The bot reads it with `int(os.getenv("PORT", "8080"))`

### LLM not working?
The bot has built-in fallback templates. It works perfectly without LLM.
Optional: Add `LLM_API_KEY` in Railway environment to enable LLM-powered responses.

---

## Final Submission Format

Share with the judge:
```
Team: Vera Prime
Bot URL: https://your-railway-app.up.railway.app
Endpoints available:
  - GET  /v1/healthz
  - GET  /v1/metadata  
  - POST /v1/context
  - POST /v1/tick
  - POST /v1/reply
```

---

## Alternative: Test Locally First

Before deploying, verify the bot works locally:

```bash
# Terminal 1: Start the bot
export LLM_PROVIDER=openai
export LLM_API_KEY=your_key_or_leave_empty
python bot.py

# Terminal 2: Run tests
python judge_simulator.py
```

The bot will be on `http://localhost:8080`

---

**✅ Ready to deploy! Your bot is production-ready.**
