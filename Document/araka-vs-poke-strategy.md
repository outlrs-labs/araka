# Araka vs Poke — Brutally Honest Competitive & Strategy Report

*Prepared: 2026-06-12 · Author: strategy pass for Araka (FollowUp Bot) · Audience: founder/PM ("Akshay")*

> Currency: USD and INR shown together at ≈ ₹83/$ (round figures). All ARR
> numbers are **illustrative models with stated assumptions**, not forecasts.

---

## 0. TL;DR — the brutal version

Poke and Araka both look like "an AI you text," but they're playing different sports.

- **Poke** is a *horizontal, proactive, premium* life-assistant for affluent **iMessage** users in the US. ~$25M raised, **~$300M valuation**, ~6,000 insider users, a recipes/integration marketplace, multi-model routing, and a cult brand.
- **Araka** is a *narrow, reactive, near-free* **WhatsApp** scheduling utility, India-first, pre-revenue, built by ~1 person on a $0 Always-Free VM.

**The brutal part:** as a *product*, Araka today is maybe 3–5% of Poke's surface area, with none of its proactivity, marketplace, multi-model brain, capital, or distribution. If your plan is "Poke, but on WhatsApp," you lose — they have ~100× the resources and a year's head start, and they'd out-execute you the moment WhatsApp opened up.

**The non-obvious part (your actual opening):** Poke *structurally cannot follow you*.
1. **Meta bars general-purpose chatbots on WhatsApp** — that's *why* Poke is iMessage-first and only "limited" on WhatsApp. A *narrow utility* (scheduling/reminders) is exactly the kind of bot Meta tolerates.
2. **Poke is single-player.** It acts for *you*. Araka's one genuinely novel mechanic — **bilateral coordination** (it also messages, confirms, and reminds the *other* person, with their own consent/STOP) — is something a personal-assistant architecture doesn't naturally do.
3. **WhatsApp is the OS of communication** for ~2.8B people, dominant in exactly the markets (India, LATAM, SEA, MENA, Africa) Poke isn't built for.

So the winnable game is narrow and specific: **"the dependable WhatsApp scheduling concierge that gets *both* people to show up."** Everything below is about not squandering that wedge by drifting into Poke's lane.

### Where you win / where you lose

| You genuinely WIN | You genuinely LOSE (don't pretend otherwise) |
|---|---|
| WhatsApp-native (Poke can't be) | Breadth — Poke does 50 things, you do ~3 |
| Bilateral / multiplayer scheduling | Proactivity — Poke acts unprompted; you wait |
| India + emerging markets reach | Capital & distribution ($300M val vs $0) |
| Reliability via deterministic guards | Model brain — single small model vs routed frontier |
| ~$0 infra → can be cheap/free | Brand & hype — Poke is a *movement*; you're unknown |
| Appointment-business ROI (no-shows) | Marketplace / integrations ecosystem |

---

## 1. Tech comparison

| Dimension | **Poke** | **Araka** | Honest read |
|---|---|---|---|
| Primary surface | iMessage (via Linq) + SMS/Telegram; WhatsApp *limited* | **WhatsApp Cloud API** (native) | Different oceans. Yours is bigger by headcount, theirs is richer per user. |
| Scope | Horizontal: planning, email, finance, travel, health, smart home, dev | Vertical: scheduling, reminders, follow-ups, GCal/Meet, contacts | They're a platform; you're a feature. That's fine *if you stay a great feature*. |
| Initiative | **Proactive** (nudges you unprompted) | **Reactive** (responds when messaged) | Biggest product gap. Proactivity is hard + risky on WhatsApp (24h window, opt-in). |
| Multiplayer | Single-player (acts for you) | **Bilateral** (coordinates both sides) | Your one architectural edge. Lean into it hard. |
| Model strategy | Routes to best model per task (frontier + OSS) | Single Groq `llama-4-scout` + tool-calling loop | They're smarter per query; you're cheaper and more predictable. |
| Reliability approach | Capable but probabilistic | **Deterministic guards** (anti-hallucination, confirmation gates, field accumulation) | Genuinely differentiated for a *scheduling* tool where a wrong time = lost trust. |
| Integrations | Recipes marketplace (Gmail, Notion, Linear, Strava, Oura, Hue…) | Google Calendar/Meet/Contacts only | You're 1 integration deep; they're 20+. Don't try to catch up — you don't need to. |
| Infra / cost | Funded cloud, multi-model spend | 1× OCI Always-Free VM, SQLite WAL, APScheduler, Caddy → **~$0/mo** | Your cost structure is a weapon. Use it for an aggressive free tier. |
| Scalability ceiling | High (capitalized) | ~100s now; SQLite/single-VM caps at low thousands DAU | Fine for a pilot/wedge. Plan the Postgres/queue migration *only when revenue demands it*. |
| Compliance posture | iMessage = fewer content rules | WhatsApp Business Policy: opt-in, 24h window, template cost, 3rd-party messaging | Real constraint **and** your moat — narrow utility survives where general bots get barred. |

**Honest gaps you must own:** no proactivity, no marketplace, one small model, single-VM ceiling, and a live policy risk (the prior audit flagged third-party messaging + opt-in as P0 — these will bite at scale if not tightened). **Honest edges:** WhatsApp reach, bilateral coordination, deterministic reliability, and a ~$0 cost base.

---

## 2. Business comparison

| | **Poke** | **Araka** |
|---|---|---|
| Target audience | US/SV professionals, "AI-native" early adopters | India-first WhatsApp users; appointment/coordination-heavy SMBs |
| Ideal user | Busy affluent knowledge worker who lives in iMessage | A person/business that schedules a lot *with other people* over WhatsApp |
| GTM | Hype + exclusivity + insider waitlist + press | Bottom-up: bilateral exposure loop + SMB outreach |
| Funding | ~$25M (GC, Spark, marquee angels) | Bootstrapped |
| Valuation | ~$300M | n/a (pre-revenue) |
| Traction | ~6,000 users, ~200K msgs/mo, 10× growth | ~100-user pilot |
| Unit economics | High ARPU (negotiated $5–75/mo), high model cost | Low cost/user (utility-class WhatsApp + cheap Groq); ARPU TBD |
| Moat | Brand, capital, ecosystem, talent | Channel (WhatsApp), bilateral mechanic, reliability, price |

### Potential ARR — three illustrative models

> Assumptions stated inline. The point isn't the exact number — it's *which motion produces real money*. Spoiler: not consumer freemium.

**Scenario A — India consumer freemium** *(Pro ₹199/mo ≈ $2.4)*
Reach 50,000 free users in year 1 (plausible *only* because the bilateral loop markets to a non-user on every meeting), 4% → Pro:
`2,000 × ₹199 × 12 ≈ ₹4.8M ≈ $57K ARR`.
→ Low absolute ARR, but the **growth loop** is the real asset here, not the revenue.

**Scenario B — SMB per-seat** *(₹399/seat ≈ $4.8 India; $8 global)*
300 teams × 5 seats = 1,500 seats:
`1,500 × ₹399 × 12 ≈ ₹7.2M ≈ $86K ARR` (higher with global $8 seats).
→ Better retention, clearer ROI (fewer no-shows), defensible.

**Scenario C — B2B2C concierge / white-label** *(clinics, salons, tuition, brokers @ ₹2,500/mo ≈ $30)*
200 businesses:
`200 × ₹2,500 × 12 = ₹6M ≈ $72K`; scales to **₹25–50M (~$300–600K) ARR at 800–1,600 SMBs**.
→ Highest ACV, stickiest, budgeted pain ("no-shows cost me money"). **This is where the ARR actually lives.**

**Verdict:** Use **A's free tier as the acquisition engine**, monetize through **B and especially C**. Consumer scheduling willingness-to-pay in India is low; *appointment businesses* will pay because no-shows are a line-item cost.

---

## 3. Brand positioning

**Poke owns:** "your delightful, proactive AI friend (or *nemesis*)." Personality-forward, premium, exclusive. Whimsy is a feature because it's a *companion*.

**Your whitespace is the opposite axis — trust, not whimsy.** Nobody wants a quirky "nemesis" rescheduling their dentist appointment. For a scheduling tool, **dependability is the brand**.

Candidate positioning statements:

1. **(Recommended)** *"Araka is the WhatsApp scheduling concierge that gets both people to show up — confirmations, reminders, and reschedules handled for you and the person you're meeting."*
2. *"The reliable way to schedule over WhatsApp. No app, no logins for the other person — just messages that make meetings actually happen."*
3. *(SMB cut)* *"Cut no-shows. Araka confirms and reminds your customers on WhatsApp automatically."*

**Voice:** warm, crisp, competent — a great executive assistant, not a comedian. You can borrow *one* drop of Poke's personality in onboarding ("Hi, I'm Araka 👋"), but keep the scheduling core boringly trustworthy. **Name:** pick **one** — "Araka" (brandable) over "FollowUp Bot" (descriptive). Use a descriptor tagline so the brand name doesn't have to do the explaining.

---

## 4. Akshay's PM brainstorm — where the real upside is

*(Applying the `product-management:brainstorm` lens: find the wedge, name the bets, surface the riskiest assumptions, and decide what NOT to build.)*

**The wedge (one sentence):** Be the *default scheduling layer inside WhatsApp* for people and small businesses who coordinate with others — starting in India, where WhatsApp is the OS and Poke can't go.

**3 strategic bets:**
1. **The bilateral loop is your CAC=₹0 engine.** Every meeting Araka books exposes the *counterparty* to Araka ("Araka confirmed your 4 PM with Rahul — reply STOP to opt out"). Instrument this as the #1 growth metric: *non-user → user conversions per booked meeting*. If that loop works, you have something Poke structurally doesn't: built-in virality on the world's biggest messaging network.
2. **No-shows are the ROI wedge for SMBs.** Clinics, salons, tuition centers, brokers, repair services — they lose money when customers don't show. "Automatic WhatsApp confirm + remind" is a *budgeted* pain, not a nice-to-have. This is your paid motion (Scenario C).
3. **Reliability is the brand.** Your deterministic guards aren't just engineering hygiene — they're the marketing. "Araka never invents a time, never double-books, always confirms before booking." Make that a public promise.

**Riskiest assumptions (test these before scaling):**
- **(Existential) Meta tolerance.** Your whole moat assumes WhatsApp lets a narrow scheduling utility message third parties. Mitigate: rock-solid opt-in capture, approved utility templates, respect the 24h window, and plan to go through a BSP / get the business verified early. *This is the single thing that can kill the company — treat it as P0.*
- **Consent friction.** Will people let a bot message their contacts? The STOP flow helps, but watch opt-out rates obsessively.
- **WTP in India.** Consumers may not pay; that's *why* the money plan is SMB/concierge, not consumer Pro.
- **Reliability at scale** on a single small model + single VM. Fine for the pilot; have the Postgres/queue migration costed but *don't build it until revenue forces it*.

**What to deliberately NOT build (resist the Poke envy):**
- ❌ A recipes/integrations marketplace. ❌ Proactive health/travel/finance. ❌ Multi-model routing (yet). ❌ "General assistant" scope creep.
- Every one of those drags you onto Poke's turf where you lose. Stay narrow until the wedge is undeniably working.

**30 / 60 / 90 focus:**
- **30:** Ship the 100-user pilot clean. Instrument the bilateral loop + no-show reduction. Lock down opt-in/templates (de-risk Meta).
- **60:** Recruit 5–10 SMBs (Scenario C). Prove "Araka cut my no-shows by X%." Get 2 testimonials with numbers.
- **90:** Stand up paid SMB tier; turn on consumer freemium to feed the loop; decide global-SMB expansion based on retention data.

---

## 5. First 5 pilot users — exact profiles

Recruit people who *already feel* the "did they confirm? will they show up?" pain **and** live in WhatsApp. Skew toward appointment/coordination-heavy roles — they validate the bilateral + reminder core, which is your only real differentiator.

| # | Archetype | Why them | Where to find them |
|---|---|---|---|
| 1 | **Over-scheduled solo founder / freelancer** (recruiter, consultant, designer) | Books many calls with *others*; feels reschedule pain daily | Your own network, LinkedIn, founder WhatsApp/Slack groups |
| 2 | **Real-estate / property broker** | Constant site-visit scheduling over WhatsApp; no-shows = wasted trips | Local broker WhatsApp groups, 99acres/MagicBricks agents, NoBroker network |
| 3 | **Tuition teacher / small coaching-center owner** | Class reminders + parent coordination; WhatsApp-native already | Coaching-center FB/WhatsApp communities, local listings, your alumni network |
| 4 | **Independent clinic / dentist / physio receptionist** | No-shows are a direct revenue loss; appointment reminders are budgeted | Practo/local clinic outreach, dental/physio associations, cold WhatsApp |
| 5 | **Salon/spa owner OR community/event organizer** | Bookings + reminders; high WhatsApp usage; visible ROI | Local salon directories, Instagram DMs, meetup/event organizer groups |

**Copy-paste outreach (WhatsApp/DM):**
> *"Hi [name] — I built Araka, a little WhatsApp assistant that automatically confirms and reminds people about your meetings/appointments, so fewer no-shows and less back-and-forth. It also messages the other person (with their consent) so both sides show up. I'm onboarding 5 people free this week and want brutal feedback — can I set you up in 5 minutes?"*

**Selection rule:** pick people who (a) schedule ≥5 things/week *with others*, (b) already use WhatsApp for it, and (c) will actually give you blunt feedback. One from each row beats five of row 1.

---

## 6. Pricing strategy

**Principle:** scheduling is a *utility* — pricing must be **predictable and low-friction**. Poke's negotiated "bouncer" pricing is brilliant marketing for a *companion*, but wrong for a utility (users want to know the price, not haggle for their dentist reminders). Borrow Poke's *personality*, not its *pricing model*.

| Model | What | Best for | Verdict |
|---|---|---|---|
| **Freemium + Pro** | Free: N meetings/reminders/mo; Pro **₹199 / $5** unlimited + GCal/Meet + bilateral reminders | Consumers / solo | ✅ Use free tier as the **growth engine**; Pro is upside, not the plan |
| **Per-seat SMB** | **₹399 / $8** per seat/mo | Small teams (sales, recruiting) | ✅ Clean expansion revenue |
| **Usage / credits** | Pay per reminder/notification sent | Low-frequency businesses | ⚠️ Aligns with WhatsApp msg cost, but adds friction; offer as add-on |
| **Concierge / white-label** | **₹1,500–5,000 / $30–60** per month, branded to the business | Clinics, salons, tuition, brokers | ✅✅ **Primary ARR engine** — budgeted no-show pain |
| **Poke-style negotiated** | Chat-to-set-price | — | ❌ For the utility core. Maybe a fun *referral/perk* gimmick only |

**Recommendation:**
1. **Pilot (now → 100 users): free.** Don't price during validation; buy feedback and loop data.
2. **Consumer: generous freemium, Pro at ₹199/$5.** Your ~$0 infra means a fat free tier is cheap and it *fuels the bilateral loop* — the free users are the marketing.
3. **Monetize seriously on SMB/concierge (Scenario C).** Land clinics/salons/tuition/brokers at **₹2,500/$30 per month**, expand to per-seat for teams. Anchor the pitch on **no-show reduction %**, not features.
4. **Global SMBs:** same model, **$30–60/mo**, once India retention proves the wedge.

**Why this beats copying Poke:** Poke monetizes *scarcity + delight* among rich early adopters. You monetize *measurable ROI* among businesses that lose money to no-shows — a larger, more durable, less hype-dependent base, on a channel Poke can't touch.

---

## Sources
- [Poke makes using AI agents as easy as sending a text — TechCrunch (2026-04-08)](https://techcrunch.com/2026/04/08/poke-makes-ai-agents-as-easy-as-sending-a-text/)
- [Poke.com launches iMessage AI assistant with $15M seed at $100M valuation — Tech Startups (2025-09-08)](https://techstartups.com/2025/09/08/poke-com-launches-imessage-ai-assistant-with-15m-seed-funding-at-100m-valuation-now-used-by-6000-vc-insiders/)
- [Poke launches with $15M from General Catalyst — TechFundingNews](https://techfundingnews.com/poke-launches-15m-seed-imessage-ai-assistant/)
- [Apple approves Poke as first iMessage AI agent — TechTimes (2026-06-05)](https://www.techtimes.com/articles/317863/20260605/apple-approves-poke-first-imessage-ai-agent-charging-per-user-before-wwdc.htm)
- [Alex Kaplan on negotiating Poke's price down to $29/mo — X](https://x.com/alexkaplan0/status/1965158155002020019)
- [Poke AI Company Profile — PitchBook](https://pitchbook.com/profiles/company/902683-27)
- [Poke — official site](https://poke.com/)
- Araka internals: this repo (`bot/agent.py`, `bot/tools.py`, `futurePlan.md` technical audit) + this session's production deploy.
