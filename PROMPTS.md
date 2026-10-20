# Saathi — Phase 1 build prompts

Each step was specified as a prompt, then built and tested before the next one started.
Steps 1–3 (structure, database, shopkeeper tools) are in the chat history; steps 4–8 follow.

## Step 4: Core agent (LangGraph)
Build on steps 1–3; do not restructure.
- app/agent/llm.py: get_llm() returns ChatGoogleGenerativeAI from settings (one place to swap models).
- app/agent/roles/base.py: Role(name, system_prompt, tools). build_tools(role, session, user_id) turns each
  plain tool function into a LangChain StructuredTool, hiding the session/owner_id parameters from the LLM.
- app/agent/roles/professor.py: placeholder role with one "coming soon" tool.
- app/agent/roles/registry.py: role name -> Role.
- app/agent/graph.py: handle_message(session, phone, text, llm=None) -> reply text.
  Unknown phone -> "not registered". Loads the last HISTORY_LIMIT messages, runs LangGraph's prebuilt
  ReAct agent with ONLY the user's role tools (role enforcement in code), saves both turns, returns the reply.
- System prompt: reply in the user's language (English / Hindi in Roman script / Hinglish), short WhatsApp-style
  replies, always repeat names and amounts the tools return, never invent numbers.
Tests (fake LLM, offline): one message -> two tool calls (credit + balance) in one reply; professor gets
"coming soon" and has no ledger tools; history is saved and reloaded; unknown number is rejected.

## Step 5: Local CLI
cli.py --phone <number>: terminal chat loop using handle_message and the real LLM. "exit" quits.

## Step 6: Approval gate
- app/agent/approval.py. A role can mark a tool as needing approval with a rule on its args.
  Shopkeeper: record_payment above APPROVAL_THRESHOLD.
- Instead of running, such a call saves a PendingAction (expires after APPROVAL_TTL_MINUTES), logs an
  "approval_request" ActionLog with status pending_approval, and replies with a summary + "Reply YES to confirm or NO to cancel".
- Before the LLM runs, a YES/haan/ok or NO/nahi reply resolves the latest pending action: YES runs the tool,
  NO marks it rejected, expired ones are rejected with a clear message. One pending action per user.
Tests: >5000 creates pending and writes no entry; yes executes; no rejects; expired yes does nothing; <=5000 runs directly.

## Step 7: WhatsApp webhook
- app/whatsapp/client.py: send_text(to, body) via the Cloud API (errors logged, never crash).
- app/whatsapp/routes.py: GET /webhook verification; POST /webhook checks X-Hub-Signature-256 with the app secret
  (fail closed if unset), returns 200 at once, handles text messages in a background task, skips duplicate
  message ids, replies "text only for now" to other message types.
Tests: verification ok/forbidden, bad signature -> 403, good signature -> processed once even if sent twice.

## Step 8: README
Setup, docker compose, migrations, seed, tests, CLI, server, ngrok, Meta webhook setup, acceptance checklist.

---

# Phase 2 — Shopkeeper role complete + voice

Same workflow: prompt → build → tests → independent reviewer → next. Build on Phase 1; do not restructure.
Reviewer checks after steps 2, 4 and 6 (money paths first).

## Step P2-0: Close the new_customer gap (reviewer round 3)
When add_credit asks "Did you mean X?", record it (ActionLog tool "name_check", input = the asked name).
add_credit(new_customer=true) is accepted only if this owner got a name_check for that same normalised name
in the last 10 minutes; otherwise it behaves as if the flag were false. Test: flag on the first call cannot split a balance.

## Step P2-1: Outbox + inventory
- Outbox: tools never send messages themselves. They append {"to", "text"} or {"to", "document", "filename"} to
  session.info["outbox"]; the channel sends them only after the DB commit (webhook) or prints them (CLI).
  On agent failure the outbox is cleared with the rollback.
- Models + migration: Supplier(owner, name, phone), Product(owner, name, unit, price, gst_rate, stock,
  reorder_level, supplier). Names unique per shop. Money Numeric(12,2), quantities Numeric(12,3).
- Tools: add_product (create or update the given fields), add_stock (stock received), check_stock (one product,
  or all items at/below reorder level). Product names use the same matching as customers.
- Validation: price > 0, gst_rate one of the GST 2.0 slabs {0, 5, 18, 40} (in force since 22 Sep 2025; 12% and 28% were removed), quantities > 0 with max 3 decimals.

## Step P2-2: Bills (GST)
- Bill(owner, bill_no per shop, customer?, on_credit, subtotal/taxable, gst_total, total) + BillItem snapshot
  (name, qty, price, gst_rate, amount, gst_amount).
- create_bill(items[{product_name, qty}], customer_name?, on_credit=false): validate EVERYTHING first (all products
  found, enough stock, valid qty), then write. Prices are MRP (GST-inclusive): taxable = amount×100/(100+rate),
  CGST = SGST = half the GST. Reduce stock. on_credit requires a customer and adds a ledger credit "Bill #n".
- PDF bill (reportlab) sent to the owner through the outbox. Shop GSTIN shown when set.
Tests: totals and GST split, all-or-nothing on a bad line, stock reduced, credit bill hits the ledger, PDF created.

## Step P2-3: Supplier reorder
add_supplier(name, phone); order_from_supplier(supplier_name, items) ALWAYS needs approval; on YES the order text
goes to the supplier through the outbox. Low-stock items are listed by check_stock so the model can propose an order.

## Step P2-4: Payment reminders (UPI)
set_shop_details(upi_id?, gstin?) with format checks; set_customer_phone(customer, phone);
send_payment_reminder(customer) needs approval, sends a polite text with the due amount, a upi://pay link and a
QR code image (when the shop's UPI ID is set).

## Step P2-5: Daily summary
daily_summary tool (today in IST: bills, cash vs credit sales, payments received, credit given, total dues,
low stock) + a scheduler job at SUMMARY_HOUR (default 21:00 IST) that sends it to every shopkeeper.

## Step P2-6: Voice notes
Webhook audio → download media from the Cloud API → Gemini transcribes (original language, Roman script) →
handle_message. Reply starts with what was heard. Size cap 10 MB; failures get a clear "please type it" reply.

## Step P2-W: Wire messaging, summary and voice
Approval rules take (session, user_id, args); the messaging rules return True only for valid requests (shared
check_* validators), so invalid requests error at once and valid ones always wait for YES. Registry adds all shop
modules. Webhook: audio -> download -> transcribe -> handle_message, reply "🎙 Heard: …"; voice failure -> "please
type it"; other types -> "text and voice only". Tests through the agent for every approval path and the voice path.

## Step P2-7: README, PROMPTS, final review, zip

---

# Phase 3 — Professor and Student roles

Same workflow. The professor role replaces the "coming soon" placeholder; a new Student role shows role switching:
the same core agent serves a shopkeeper, a professor and a student with different tools and permissions.

## Step P3-1: Classes, students, attendance
- Models + migration: ClassGroup(owner professor, name), Student(class, roll_no, name, phone?, user?),
  Attendance(class, student, day, present) unique per student per day. users.role gains "student".
- Students added with a phone get a User row with role "student" (one per phone; a phone that already belongs to
  another role is not linked, and the reply says so).
- Tools: add_class, add_students(class, [{roll_no, name, phone?}]) (upsert by roll number),
  mark_attendance(class, absent_rolls, date?) (everyone else present; re-marking a day overwrites it; no future
  dates; unknown roll numbers refuse the whole call), attendance_report(class, below_percent=75).
- A professor only ever sees their own classes.

## Step P3-2: Marks, assignments, reminders
- Mark(class, student, assessment, max_marks, score) unique per student per assessment; Assignment(class, title,
  due_date, reminded).
- record_marks(class, assessment, max_marks, [{roll_no, score}]) all-or-nothing (0 <= score <= max);
  marks_summary(class, assessment): average/highest/lowest + an Excel sheet (openpyxl) sent to the professor.
- add_assignment(class, title, due_date); remind_class(class, message) needs approval and messages every student
  with a phone through the outbox. Scheduler: at 18:00 IST, assignments due tomorrow get one automatic reminder.

## Step P3-3: Notes, quiz, Student role (ChromaDB)
- add_notes(class, title, text): stored in Postgres and chunked into ChromaDB with embeddings (Gemini embeddings in
  production, a deterministic fake in tests; never the default model download).
- generate_quiz(class, topic, n=5): retrieves the closest note chunks and asks the model for MCQs grounded in them.
- Student role (students messaging the bot): ask_doubt(question) answers only from their classes' notes and says
  so when the notes do not cover it; my_attendance() shows their own attendance. Students never see other students.

---

# Phase 4 — Web dashboard

- Login without passwords: the dashboard_link tool (every role) sends a signed link valid DASHBOARD_LINK_HOURS.
  The token sits in the URL #fragment (never sent to servers) and travels only through the outbox to the user's own
  phone, so the model, the conversation history and the action log never contain it. No DASHBOARD_SECRET = off.
- Read-only API (/api/me, /shop, /classes, /student, /activity), every query filtered by the signed-in user;
  role walls return 403. All changes still go through the WhatsApp agent and its approvals.
- One static page (React 18 + htm + Chart.js from a CDN, no build step): KPI tiles, 7-day sales, dues, low stock,
  recent bills; class attendance and marks; a student's own record; and "What Saathi did" (the agent activity log).

# Phase 5 — Evaluation

eval/cases.jsonl holds 34 commands across roles and languages (English, Hindi, Hinglish), including approvals,
multi-task messages and things a role must NOT be able to do. eval/run_eval.py replays them with the real model on a
fresh copy of the same demo world and reports task success, intent accuracy and response time, by role and language.

# After Phase 5 — closing the known limits

cancel_bill (approval; stock back, credit reversed, bill kept and marked cancelled; undo never reaches past a bill or a
cancellation) · student invites and join_class (nothing visible or sent before the student accepts; phone "none"
removes a number; one number on two rolls warns the professor) · dashboard_logout via users.token_version ·
assignment reminders hourly 18–22 IST with a retry when every send failed.
