# 🎬 BookMyShow Seat Alert

A simple web app that monitors BookMyShow and sends you a WhatsApp notification the instant your preferred seats become available.

---

## How it works

1. Open the app in your browser
2. Search for your movie → pick theatre → pick showtime
3. Choose your preferred row (A, B, C...) and optional specific seats
4. Enter your WhatsApp number and hit **Notify Me**
5. The app monitors BookMyShow in the background
6. The moment seats open → you get a WhatsApp message with the booking link

---

## Deploy to Railway (cloud, runs 24/7)

### Step 1 — Get Twilio credentials (~5 min)

1. Sign up free at **[twilio.com](https://www.twilio.com)**
2. In your console, note your **Account SID** and **Auth Token**
3. Go to **Messaging → Try it out → Send a WhatsApp message**
4. Send the join message (e.g. `join bright-lion`) from your phone to the sandbox number

### Step 2 — Upload to GitHub (~3 min)

1. Go to **[github.com/new](https://github.com/new)** → name it `bms-alert`
2. Upload these files: `app.py`, `templates/index.html`, `Dockerfile`, `requirements.txt`, `railway.json`, `.gitignore`
3. Commit

### Step 3 — Deploy on Railway (~5 min)

1. Go to **[railway.app](https://railway.app)** → Login with GitHub
2. **New Project → Deploy from GitHub repo** → select `bms-alert`
3. Add these environment variables:

| Variable | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | Your Twilio SID |
| `TWILIO_AUTH_TOKEN` | Your Twilio Auth Token |
| `TWILIO_FROM_NUMBER` | `whatsapp:+14155238886` |
| `PORT` | `5000` |

4. Railway will build and deploy. Click the generated URL to open your app!

### Step 4 — Use it

Open the Railway URL → search movie → pick show → enter phone → Notify Me.

---

## Run locally (optional)

```bash
pip install -r requirements.txt
playwright install chromium

export TWILIO_ACCOUNT_SID=your_sid
export TWILIO_AUTH_TOKEN=your_token
export TWILIO_FROM_NUMBER=whatsapp:+14155238886

python app.py
```

Open http://localhost:5000

---

*Built for personal use. Not affiliated with BookMyShow.*
