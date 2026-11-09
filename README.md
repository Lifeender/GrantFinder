# AI Grant Search Interviewer & Active RFP Web Crawler

A two-phase AI program powered by your preferred AI provider (**Google Gemini**, **OpenAI / ChatGPT**, or **Anthropic / Claude**) that:
1. **Phase 1 (AI Search-Query Narrowing Interview)**: Interviews the user with a 4-stage funnel to narrow down their grant search queries, applicant eligibility, and exclusion rules before crawling.
2. **Phase 2 (Active Grant RFP Web Crawler & Daily Notifier)**: Crawls **Grants.gov REST API**, **Web Open RFPs**, and **Auto-Discovered Grant Portals**, evaluates each opportunity with AI (`0–100%` match score + 1-sentence reason), and sends daily alerts via **Email (SMTP)** and **Free Carrier SMS Gateways** (`@vtext.com`, `@txt.att.net`, `@tmomail.net`, etc.).

---

## Local Quickstart (Windows / PowerShell)

```powershell
pip install -r requirements.txt
.\start.ps1
```
Open **http://127.0.0.1:8050** in your browser. Connect your preferred API key (**Gemini** `AIza...`, **OpenAI / ChatGPT** `sk-...`, or **Anthropic Claude** `sk-ant-...`) directly in the UI. To stop the local server, press `Ctrl+C`, click **⏻ Shut Off Server** in the header, or run `.\stop.ps1`.

---

## Hosting to Share or Run 24/7

### Option 1: Railway (Recommended — 1-Click Cloud Hosting)
1. Push this folder to a GitHub repository (your local `grants_crawler.db` is automatically excluded by `.gitignore` so your personal API keys and phone number are never uploaded).
2. On [Railway](https://railway.app), click **New Project &rarr; Deploy from GitHub repo**.
3. Railway will automatically detect `railway.json` and `Dockerfile`, assign a public URL, and start the server.
4. *(Optional)* Add `GEMINI_API_KEY`, `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` in Railway's **Variables** tab so visitors can try the AI Interview immediately without pasting their own key.

### Option 2: Render.com (Free Docker Web Service)
1. Push this folder to GitHub.
2. On [Render](https://render.com), click **New &rarr; Blueprint** (uses `render.yaml`) or **New Web Service** and connect your repo.

### Option 3: Any Linux VPS / Home Server with Docker
```bash
docker compose up -d --build
```
The app will run 24/7 on port `8050` with automatic restart and persistent SQLite storage.
