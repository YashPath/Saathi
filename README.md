# Saathi — role-adaptive agentic AI in an Android app

One core agent (LangGraph + Gemini) loads a different role pack for each user:
- **Shopkeeper** — credit ledger, stock, GST bills, supplier orders, payment reminders, evening summary
- **Professor** — classes, attendance, marks with Excel export, assignments with automatic reminders, notes, quizzes
- **Student** — their own attendance and marks, and doubts answered only from their professor's notes

Everything happens in the **Saathi Android app** (Kotlin + Jetpack Compose): chat by text or voice, approve with
one tap, open bills and Excel files, and see a dashboard. A FastAPI backend runs the agent. An **evaluation harness**
measures the agent with the real model.

## How the app works
- **Log in with phone + 6-digit PIN.** Shopkeepers and professors create their own account. A student account is
  created by the professor (adding the student with a phone); the student gets a one-time **setup code** (valid
  7 days) from the professor and uses it once to set a PIN. 5 wrong PINs lock the account for 15 minutes.
- **Chat tab:** type or hold the mic. The reply shows "🎙 Heard: …" for voice. When something needs approval
  (big payment, supplier order, reminder, class message, UPI change, bill cancel) **Haan / Nahi** buttons appear.
- **Files:** a bill PDF or marks Excel appears as a card; tap to open or share it.
- **People who don't use the app** (suppliers, customers): Saathi prepares the message and shows a card with
  **WhatsApp** and **SMS** buttons (and the UPI QR for reminders). The owner taps to send it from their own phone,
  so nothing is ever sent without them.
- **Other app users** (students of a class) get messages in their app inbox; the app checks every 20 seconds.
- **Dashboard tab:** charts and records for your role, read-only.
- **Account tab:** "Log out on all phones" stops every login and dashboard link.

## Features by phase
**Phase 1 — ledger and agent core.** Credit ledger by chat (add credit, record payment, balance, list dues, undo) in
English / Hindi / Hinglish; several tasks in one message; fuzzy customer names; approval gate for payments above
₹5,000 (expires in 10 minutes); role enforcement in code; every tool call is logged; undo adds a reversing entry.

**Phase 2 — shop.** Voice; products and stock with GST slabs 0/5/18/40; GST bills (PDF) from a spoken order, cash or
on credit; supplier orders and payment reminders with UPI link and QR (both wait for YES); shop UPI ID and GSTIN;
evening summary at 9 pm IST in the inbox. Messages for others are only released after the database commit (outbox).

**Phase 3 — professor and student.** Attendance ("CSE-3 attendance: 2 aur 4 absent"), below-75% report; marks with
summary and Excel; assignments with reminders the evening before (hourly 6–10 pm IST, never twice); notes stored in
Postgres and indexed in ChromaDB, quizzes and student doubts answered only from them (notes passed as data inside
`<notes>` tags). A student must accept the class invite ("join CSE-3") before seeing anything.

**Phase 4 — dashboard.** Shopkeeper: today's numbers, 7-day sales, top dues, low stock, recent bills. Professor:
attendance per class, students below 75%, average marks. Student: own records. Everyone: "What Saathi did".

**Phase 5 — evaluation**, and the gap fixes: cancel a bill (stock and udhaar reversed, bill kept as cancelled),
student consent, logout of all links.

**App phase.** WhatsApp removed; PIN accounts, inbox, share cards, Android app, GitHub Actions build.

## Project layout
```
app/
  config.py            settings from .env
  main.py              FastAPI app (/health, /api/app/*, /api/dashboard*, /dashboard)
  db/                  models.py, session.py
  agent/
    graph.py           handle_message(): the single entry point
    llm.py             get_llm(): change the model here
    approval.py        approval gate
    action_log.py      audit log
    outbox.py          messages queued by tools, delivered after commit
    knowledge.py       lecture notes in ChromaDB
    roles/             base.py, registry.py, dashboard_link.py, professor.py, student.py, shopkeeper.py (ledger),
                       inventory.py, billing.py, messaging.py, summary.py
  mobile/              api.py (app API), accounts.py (PIN login), delivery.py (inbox / share cards), voice.py
  dashboard/           auth.py (signed tokens), api.py (read-only API)
  static/              dashboard.html (React + Chart.js, no build step)
  scheduler.py         9 pm shop summary and evening assignment reminders (IST)
android/               the Android app (Kotlin, Jetpack Compose)
.github/workflows/     build.yml: builds the APK / Play bundle and runs the backend tests
migrations/            Alembic
cli.py                 chat in the terminal
seed.py                demo users
tests/                 pytest (offline: the LLM is faked)
eval/                  cases.jsonl + run_eval.py
PROMPTS.md             the prompt used for each build step
```

## 1. Run the backend
Needs Python 3.11+ and Docker.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in the values (see below)

docker compose up -d               # Postgres 16 on localhost:5432
alembic upgrade head               # create tables
python seed.py                     # demo shopkeeper and professor, PIN = SEED_PIN
uvicorn app.main:app --port 8000   # check: http://localhost:8000/health
```

`.env` values you must set:
| Key | What |
|---|---|
| `GOOGLE_API_KEY` | https://aistudio.google.com → Get API key |
| `SEED_SHOPKEEPER_PHONE`, `SEED_PROFESSOR_PHONE` | Phone numbers, digits only with country code, e.g. `919876543210` |
| `SEED_PIN` | 6-digit PIN for the seeded users |
| `DASHBOARD_SECRET` | 32+ random characters (signs logins; also turns the dashboard on) |
| `PUBLIC_BASE_URL` | Your public https URL (step 2) |

Optional: `APP_LOGIN_DAYS`, `BILLS_DIR`, `CHROMA_DIR`, `SUMMARY_HOUR`, `ASSIGNMENT_REMINDER_HOUR`, `SCHEDULER_ENABLED`.

## 2. Make the backend reachable from the phone
The app only talks **https** (plain http is allowed only for the Android emulator at `http://10.0.2.2:8000`).
For a demo, use ngrok:
```bash
ngrok http 8000          # copy the https://….ngrok-free.app URL
```
Put that URL in `PUBLIC_BASE_URL`, restart uvicorn, and type the same URL in the app's **Server** field.
Keep the laptop and ngrok running during the demo. For a permanent server, deploy to any host with Postgres
(Render, Railway, a VPS) and use its https URL.

## 3. Get the APK
The app is built by GitHub Actions on every push to `main` (workflow "Build app and test backend").
1. Open the repo → **Actions** → latest run → **Artifacts** → download `saathi-apk` and unzip it.
2. Copy `app-debug.apk` to the phone and open it (allow "Install unknown apps" once).

To build on your own machine instead: install Android Studio, open the `android/` folder, and press Run.

## 4. Demo script
Log in as the shopkeeper (seed phone + `SEED_PIN`):
- `Sharma ji ka 500 udhaar likh do aur Gupta ka balance batao`
- `Gupta ne 6000 de diye` → tap **Haan**
- `Maggi add karo, price 14, GST 18, stock 24, reorder 10` → `2 maggi ka bill banao` (PDF card) 
- `Ramesh Traders supplier add karo 9876543210` → `Ramesh se 24 maggi mangwao` → **Haan** → WhatsApp/SMS card
- `mera UPI yash@okaxis hai` → **Haan**; `Sharma ka number 9811122233` → `Sharma ko yaad dilao` → **Haan** (QR)
- `aaj ka hisaab`, `undo`, `bill 1 cancel karo` → **Haan**; Dashboard tab

Log in as the professor:
- `CSE-3 class banao` → `CSE-3 me students add karo: 1 Ravi 98111xxxxx, 2 Neha, 3 Aman` (reply lists Ravi's
  setup code)
- `CSE-3 attendance: 2 absent`, `kiski attendance kam hai?`
- `CSE-3 quiz 1 out of 10: roll 1 ko 8, roll 2 ko 6, roll 3 ko 9` → `quiz 1 ka summary` (Excel card)
- `CSE-3 notes save karo, title Photosynthesis: ...` → `photosynthesis pe 5 sawaal ka quiz banao`

On a second phone (or log out): **Create account → Student**, Ravi's phone, the setup code, a new PIN. Then
`join CSE-3`, `meri attendance?`, `photosynthesis kahan hota hai?`.

Without a phone, the same agent works in the terminal: `python cli.py --phone <seed phone>`.

## 5. Publish on Google Play (internal testing)
Internal testing is the fastest track: up to 100 testers by email, no review wait, and no 12-tester / 14-day rule
(that rule applies only to closed testing before production on new personal accounts).
1. Create a **Google Play developer account** (one-time fee) at https://play.google.com/console.
2. Make an upload key once (keep the file and passwords safe, never commit them):
   ```bash
   keytool -genkeypair -v -keystore upload.jks -alias upload -keyalg RSA -keysize 2048 -validity 10000
   base64 -w0 upload.jks > upload.b64       # macOS: base64 -i upload.jks > upload.b64
   ```
3. GitHub repo → Settings → Secrets and variables → Actions → add:
   `SIGNING_KEYSTORE_BASE64` (contents of upload.b64), `SIGNING_STORE_PASSWORD`, `SIGNING_KEY_ALIAS` (`upload`),
   `SIGNING_KEY_PASSWORD`.
4. Re-run the workflow. The `saathi-apk` artifact now also contains `app-release.aab`.
5. Play Console → Create app "Saathi" → Testing → **Internal testing** → Create release → upload the `.aab`
   (accept Play App Signing). Fill the required store listing, content rating, data safety (phone number, audio
   sent to your server) and a privacy policy URL.
6. Testers tab → add your Gmail (and your guide's) → copy the opt-in link → open it on the phone → install from Play.

Every push gives a higher `versionCode` (the run number), so new uploads are accepted.

## Run the tests
Tests use a separate database because they drop all tables. Never point it at `saathi`.
```bash
docker compose exec db createdb -U saathi saathi_test
TEST_DATABASE_URL=postgresql+psycopg://saathi:saathi@localhost:5432/saathi_test pytest
```
Without `TEST_DATABASE_URL` the database tests are skipped, not failed. GitHub Actions runs them on every push.

## Evaluate the agent (numbers for the report)
```bash
docker compose exec db createdb -U saathi saathi_eval
EVAL_DATABASE_URL=postgresql+psycopg://saathi:saathi@localhost:5432/saathi_eval python eval/run_eval.py
```
Replays 34 commands (three languages, all roles, approvals, forbidden actions) and writes `eval/results/<time>.md`
and `.csv`: task success, intent accuracy and response time.

## Known limits
- The phone needs the backend online (ngrok or a server); there is no offline mode.
- New inbox messages appear within 20 seconds while the app is open; there are no push notifications.
- Messages to suppliers and customers are sent by the owner tapping WhatsApp / SMS; Saathi cannot see whether they
  were actually sent.
- Notes stay in the vector store if the step that saved them is rolled back; search ignores them (Postgres decides).
- A bill can only be cancelled as a whole. The tax invoice shows CGST/SGST for the whole bill, not per GST rate.
- Voice up to 10 MB. A spoken "haan" confirms like a tap (the reply shows what was heard).
- The scheduler runs inside the web server: run one server process.
- Customer names ending in "ji" are trimmed ("Balaji" is stored as "Bala").
- Approvals: a short reply such as "haan kar do" or 👍 confirms; any other message cancels and says so.

## Independent review
A separate reviewer agent checked the code after every few steps. It found and fixed real bugs, among them: parallel
tool calls sharing one DB session, a double "yes" recording a payment twice, a currency regex that misread
"500rs.50", "ok Gupta 500" confirming an old payment, restocking during a sale undoing the sale's stock change, a
migration that could not be rolled back, and a forged token causing a server error. Every fix has a test.

## Troubleshooting
- App says "Can't reach the server": the Server field must be the exact https URL; check ngrok is running.
- "Wrong number or PIN": use digits only with country code, same as in `.env`; after 5 tries wait 15 minutes.
- Dashboard tab is blank: the page needs internet for its CDN scripts; check `DASHBOARD_SECRET` is 32+ characters.
