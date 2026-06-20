# **DBMS GROUP CASE STUDY** — *Revised Clone*

## **Case Study 2: Recruitment and Hiring Management System**

> **About this clone.** This is an updated copy of your schema document. The **tables in Section 3 are your latest design and are left intact.** Sections **1, 2, 4, 5, and 6** have been rewritten so they actually match those tables — the previous versions still described the older schema and in a few places contradicted the new columns. Every change carries an inline note like the one below explaining *why*.
>
> 📝 **Why a note like this appears:** so you can see exactly what was changed against the tables and defend it in your submission.

### **Revision summary (what changed and why)**

| Section | What changed | Why |
| :---- | :---- | :---- |
| 1 · Assumptions | Refined 6 originals + added 5 new business rules | New columns (`profile_type`, `source`, `total_exp_years`, `job_type`, `Waitlist`, `On_Hold`, `Backfill_Action_Taken`) imply rules the old assumptions never stated |
| 2 · Relationship Walkthrough | Added 4 missing relationships | Your tables have FKs (`hiring_manager_id`, `recruiter_id`, `EmployeeID`, `DecisionByID`) that the walkthrough never listed |
| 4 · Constraints | Rebuilt the whole table; corrected stale values | The old table said `OfferStatus` had `'Declined'` and `DecisionStatus` only had `'Selected','Rejected'` — both now wrong |
| 5 · Normalization | Updated examples; added a profile-modeling note | New `profile_url` / `other_profile_url` columns raise a real 1NF point worth addressing |
| 6 · Indexing Strategy | Kept all 6 indexes; updated justifications + portability/nuance notes | `Waitlist` and the relocated withdrawal state change how two queries behave |

---

**Group Number:** [Enter Group Number]

### **Student Names & Roll Numbers**

| # | Student Name | Roll Number | Email |
| :---- | :---- | :---- | :---- |
| 1 | [Name] | [Roll No] | [Email] |
| 2 | [Name] | [Roll No] | [Email] |
| 3 | [Name] | [Roll No] | [Email] |
| 4 | [Name] | [Roll No] | [Email] |
| 5 | [Name] | [Roll No] | [Email] |

---

## **1. Assumptions**

The following business rules and design assumptions were made while interpreting the case study brief. They now reflect every constraint present in the Section 3 tables.

1. **Application uniqueness.** A candidate may apply to many distinct job openings over time, but at most **one application per (candidate, job)** pair. This is enforced at the database level by a composite `UNIQUE (candidate_id, job_id)` on `CANDIDATE_APPLICATION`, not just by application logic.
2. **Application states.** An application moves through a controlled set of states — `Active`, `Waitlist`, `Hired`, `Rejected`, `Withdrawn`. `Waitlist` is an interim holding state for strong candidates kept in reserve while a decision is pending elsewhere.
3. **Candidate profiles & sourcing.** Every candidate has exactly one **primary** professional link (`profile_url`) tagged by `profile_type` (`LinkedIn`, `Resume`, or `Portfolio`), and may optionally add one **secondary** link (`other_profile_url`). The **sourcing channel** (`source`: `Job_portal`, `Referral`, `Agency`, `Other`) and **total years of experience** (`total_exp_years ≥ 0`) are recorded for screening and reporting.
4. **Panel interviews.** A single interview round can be conducted by a panel of multiple employees. Each interviewer submits their own individual `score` (0–10) and `feedback`.
5. **Job openings.** Each opening declares an **employment type** (`job_type`: Full-Time, Part-Time, Contract, Internship, Temporary) and a **valid salary band** (`0 ≤ min_salary ≤ max_salary`), and tracks the number of seats to fill (`open_positions ≥ 1`, default `1`).
6. **Hiring decisions.** A single application results in exactly one final decision (1:1). A decision is `Selected`, `Rejected`, or `On_Hold` (deferred — kept open without committing either way).
7. **Rejection rule.** If `DecisionStatus = 'Rejected'`, a `RejectionReason` is required.
8. **Offer generation.** Offer letters are created **only** for applications whose hiring decision is `Selected` (linked 1:1 through `DecisionID`).
9. **Offer outcomes & withdrawal.** An offer is `Pending` until the candidate `Accept`s. If the candidate does not take it, the offer is recorded as `Withdrawn` together with a `CandidateWithdrawalReason`. *Candidate-declined and company-rescinded offers are both captured under the single `Withdrawn` state.*
10. **Backfill workflow.** When a `Selected` candidate withdraws, the seat may need re-opening. `Backfill_Action_Taken` (default `FALSE`) records whether a backfill was initiated, tying the withdrawal back to `open_positions` planning.
11. **Historical tracking.** Every change in the application pipeline is logged as a separate timestamped row in `APPLICATION_STAGE_HISTORY`, so stage durations and "time-to-offer" can be computed accurately.

> 📝 **Why updated:** Assumptions 1, 4, 7, 8, 11 are your originals (1 and 6 refined). Assumptions **2, 3, 5, 9, 10 are new** — they were forced by columns you added to the tables (`Waitlist`, the candidate profile/source/experience block, `job_type` + salary checks, the `Pending/Accepted/Withdrawn` offer states, and `Backfill_Action_Taken`). Without these, the brief wouldn't explain why those columns exist.

---

## **2. ER Diagram**

*(Insert ER diagram image here.)*

### **Relationship Walkthrough**

* **1:N (One-to-Many):**
  * **Department (1) → Employees (N):** one department employs many people.
  * **Department (1) → Job_Openings (N):** one department posts many openings.
  * **Employees (1) → Job_Openings (N) — as Hiring Manager:** via `job_openings.hiring_manager_id`.
  * **Employees (1) → Job_Openings (N) — as Recruiter:** via `job_openings.recruiter_id`.
  * **Candidates (1) → Candidate_Application (N):** one candidate applies to many jobs.
  * **Job_Openings (1) → Candidate_Application (N):** one opening receives many applications.
  * **Candidate_Application (1) → Application_Stage_History (N):** one application moves through many stages.
  * **Candidate_Application (1) → Interview_Rounds (N):** one application has many interview rounds.
  * **Candidate_Application (1) → Communication_Logs (N):** one application generates many messages.
  * **Employees (1) → Communication_Logs (N):** via `communication_logs.EmployeeID` — one employee handles many messages.
  * **Interview_Rounds (1) → Interview_Panel (N):** one round has a panel of many interviewers.
  * **Employees (1) → Interview_Panel (N):** one employee sits on many panels.
  * **Employees (1) → Hiring_Decision (N) — as Decision-maker:** via `hiring_decision.DecisionByID`.
* **1:1 (One-to-One):**
  * **Candidate_Application (1) → Hiring_Decision (1):** one decision per application (enforced by `UNIQUE` on `ApplicationID`).
  * **Hiring_Decision (1) → Offer_Letters (0..1):** at most one offer per decision (enforced by `UNIQUE` on `DecisionID`), only when `Selected`.

> 📝 **Why updated:** Four relationships your tables actually contain were **missing** from the old walkthrough and have been added: *Employees → Job_Openings* (twice — hiring manager and recruiter), *Employees → Communication_Logs*, and *Employees → Hiring_Decision*. Each one corresponds to a real foreign key, so leaving them out understated the model.
>
> ⚠️ **Naming flag (carried over from earlier review):** the walkthrough and all foreign keys refer to **`Employees`**, but the actual table in Section 3 is named **`HIRING_PANNEL`**. Pick one name and use it everywhere: either rename the table back to `EMPLOYEES`, or change every FK reference to `HIRING_PANNEL(employee_id)`. Also note the spelling (`PANNEL` → `PANEL`) and that `HIRING_PANNEL` reads confusingly next to the separate `INTERVIEW_PANEL` table.

---

## **3. Relational Schema**

Eleven tables were derived from the ER model. *(Your latest tables, reproduced as-is.)*

### **Table: DEPARTMENT**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| department_id | INT | PRIMARY KEY |
| department_name | VARCHAR | NOT NULL, UNIQUE |

### **Table: CANDIDATES**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| candidate_id | INT | PRIMARY KEY |
| first_name | VARCHAR | NOT NULL |
| last_name | VARCHAR | NOT NULL |
| email | VARCHAR | NOT NULL, UNIQUE |
| phone_number | VARCHAR | UNIQUE |
| profile_url | VARCHAR | NOT NULL |
| profile_type | VARCHAR | NOT NULL, CHECK (profile_type IN ('LinkedIn', 'Resume', 'Portfolio')) |
| other_profile_url | VARCHAR |  |
| source | VARCHAR | NOT NULL, CHECK (source IN ('Job_portal', 'Referral', 'Agency', 'Other')) |
| total_exp_years | DECIMAL(3,1) | CHECK (total_exp_years >= 0) |

### **Table: HIRING_PANNEL**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| employee_id | INT | PRIMARY KEY |
| first_name | VARCHAR | NOT NULL |
| last_name | VARCHAR | NOT NULL |
| email | VARCHAR | NOT NULL, UNIQUE |
| phone_number | VARCHAR | UNIQUE |
| department_id | INT | FOREIGN KEY → Department(department_id) |
| role | VARCHAR | NOT NULL, CHECK (role IN ('Recruiter','HiringManager','Interviewer','HRAdmin')) |

### **Table: JOB_OPENINGS**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| job_id | INT | PRIMARY KEY |
| job_title | VARCHAR | NOT NULL |
| department_id | INT | NOT NULL, FOREIGN KEY → Department(department_id) |
| hiring_manager_id | INT | FOREIGN KEY → Employees(employee_id) |
| recruiter_id | INT | FOREIGN KEY → Employees(employee_id) |
| job_type | VARCHAR | NOT NULL, CHECK (job_type IN ('Full-Time', 'Part-Time', 'Contract', 'Internship', 'Temporary')) |
| min_salary | DECIMAL | NOT NULL, CHECK (min_salary >= 0) |
| max_salary | DECIMAL | NOT NULL, CHECK (max_salary >= min_salary) |
| open_positions | INT | NOT NULL, Default: 1, CHECK (open_positions > 0) |
| posting_date | DATE | NOT NULL |
| closing_date | DATE |  |
| status | VARCHAR | NOT NULL, CHECK (status IN ('Open','On-Hold','Closed')) |

### **Table: CANDIDATE_APPLICATION**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| application_id | INT | PRIMARY KEY |
| candidate_id | INT | NOT NULL, FOREIGN KEY → Candidates(candidate_id) |
| job_id | INT | NOT NULL, FOREIGN KEY → Job_Openings(job_id) |
| application_date | DATE | NOT NULL |
| current_status | VARCHAR | NOT NULL, CHECK (current_status IN ('Active','Rejected','Withdrawn','Hired','Waitlist')) |
| (table-level) | - | UNIQUE (candidate_id, job_id) — one application per candidate per job |

### **Table: APPLICATION_STAGE_HISTORY**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| history_id | INT | PRIMARY KEY |
| application_id | INT | NOT NULL, FOREIGN KEY → Candidate_Application(application_id) |
| stage_name | VARCHAR | NOT NULL |
| entered_date | TIMESTAMP | NOT NULL |
| remarks | TEXT |  |

### **Table: INTERVIEW_ROUNDS**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| interview_id | INT | PRIMARY KEY |
| application_id | INT | NOT NULL, FOREIGN KEY → Candidate_Application(application_id) |
| round_number | INT | NOT NULL, CHECK (round_number > 0) |
| interview_type | VARCHAR | NOT NULL, CHECK (interview_type IN ('Technical','HR','Managerial','Other')) |
| interview_date | TIMESTAMP | NOT NULL |
| status | VARCHAR | NOT NULL, CHECK (status IN ('Scheduled','Completed','Cancelled','PendingFeedback')) |
| (table-level) |  | UNIQUE (application_id, round_number) |

### **Table: INTERVIEW_PANEL**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| panel_id | INT | PRIMARY KEY |
| interview_id | INT | NOT NULL, FOREIGN KEY → Interview_Rounds(interview_id) |
| interviewer_id | INT | NOT NULL, FOREIGN KEY → Employees(employee_id) |
| feedback | TEXT |  |
| score | INT | CHECK (score BETWEEN 0 AND 10) |
| (table-level) |  | UNIQUE (interview_id, interviewer_id) |

### **Table: HIRING_DECISION**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| DecisionID | INT | PRIMARY KEY |
| ApplicationID | INT | NOT NULL, UNIQUE, FOREIGN KEY → Candidate_Application(application_id) |
| DecisionByID | INT | NOT NULL, FOREIGN KEY → Employees(employee_id) |
| DecisionDate | DATE | NOT NULL |
| DecisionStatus | VARCHAR | NOT NULL, CHECK (DecisionStatus IN ('Selected', 'Rejected', 'On_Hold')) |
| RejectionReason | VARCHAR | Required when DecisionStatus = 'Rejected' |

### **Table: OFFER_LETTERS**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| OfferID | INT | PRIMARY KEY |
| DecisionID | INT | NOT NULL, UNIQUE, FOREIGN KEY → Hiring_Decision(DecisionID) |
| BaseCompensation | DECIMAL | NOT NULL |
| OfferDate | DATE | NOT NULL |
| ExpectedJoiningDate | DATE |  |
| OfferStatus | VARCHAR | CHECK (OfferStatus IN ('Pending', 'Accepted', 'Withdrawn')) |
| CandidateWithdrawalReason | VARCHAR |  |
| Backfill_Action_Taken | BOOLEAN | Default: FALSE |

### **Table: COMMUNICATION_LOGS**

| Attribute | Data Type | Key / Constraint |
| :---- | :---- | :---- |
| LogID | INT | PRIMARY KEY |
| ApplicationID | INT | NOT NULL, FOREIGN KEY → Candidate_Application(application_id) |
| EmployeeID | INT | NOT NULL, FOREIGN KEY → Employees(employee_id) |
| Direction | VARCHAR | NOT NULL, CHECK (Direction IN ('Inbound', 'Outbound')) |
| CommunicationType | VARCHAR | CHECK (CommunicationType IN ('Email', 'Phone Call', 'SMS', 'Portal Message')) |
| CommunicationDate | TIMESTAMP | NOT NULL |
| MessageSummary | TEXT |  |

> 🛠️ **Recommended table corrections (not applied — your tables, your call).** These are small DDL fixes that will otherwise cost marks or fail to run. I rendered the `CHECK` clauses above in their *intended* form, but in your master copy they currently read:
> - **Wrong column names inside `CHECK`:** `current_status` uses `CHECK (Status IN …)` and `interview_type` uses `CHECK (RoundType IN …)` — those identifiers don't exist. Use the real column names.
> - **Malformed `CHECK IN (...)`** in `HIRING_DECISION`, `OFFER_LETTERS`, `COMMUNICATION_LOGS` — valid SQL is `CHECK (column IN (...))`. Also `job_type`'s `CHECK` is **missing its closing parenthesis**.
> - **Smart quotes** around `'On_Hold'` and `'Waitlist'` won't parse — use straight `'`.
> - **`(CandidateID, JobID)`** in the application table-level UNIQUE should be the actual columns **`(candidate_id, job_id)`**.
> - **Naming consistency:** `HIRING_DECISION`, `OFFER_LETTERS`, `COMMUNICATION_LOGS` still use PascalCase columns while every other table uses snake_case.

---

## **4. Constraints**

| Category | Examples in this schema |
| :---- | :---- |
| **NOT NULL** | `Department.department_name`; `Candidates.email`, `Candidates.profile_url`, `Candidates.profile_type`, `Candidates.source`; `Job_Openings.job_title`, `job_type`, `min_salary`, `max_salary`, `open_positions`, `posting_date`, `status`; `Candidate_Application.application_date`, `current_status`; `Application_Stage_History.stage_name`, `entered_date`; `Offer_Letters.BaseCompensation`, `OfferDate`. |
| **UNIQUE** | `Department.department_name`; `Candidates.email`, `Candidates.phone_number`; `Employees.email`, `Employees.phone_number`; **composite** `Candidate_Application (candidate_id, job_id)`; **composite** `Interview_Rounds (application_id, round_number)`; **composite** `Interview_Panel (interview_id, interviewer_id)`; `Hiring_Decision.ApplicationID`; `Offer_Letters.DecisionID`. |
| **CHECK** | `Candidates.profile_type IN ('LinkedIn','Resume','Portfolio')`; `Candidates.source IN ('Job_portal','Referral','Agency','Other')`; `Candidates.total_exp_years >= 0`; `Employees.role IN ('Recruiter','HiringManager','Interviewer','HRAdmin')`; `Job_Openings.job_type IN ('Full-Time','Part-Time','Contract','Internship','Temporary')`; `min_salary >= 0`; `max_salary >= min_salary`; `open_positions > 0`; `Job_Openings.status IN ('Open','On-Hold','Closed')`; `Candidate_Application.current_status IN ('Active','Rejected','Withdrawn','Hired','Waitlist')`; `Interview_Rounds.round_number > 0`; `Interview_Rounds.interview_type IN ('Technical','HR','Managerial','Other')`; `Interview_Rounds.status IN ('Scheduled','Completed','Cancelled','PendingFeedback')`; `Interview_Panel.score BETWEEN 0 AND 10`; `Hiring_Decision.DecisionStatus IN ('Selected','Rejected','On_Hold')`; `Offer_Letters.OfferStatus IN ('Pending','Accepted','Withdrawn')`; `Communication_Logs.Direction IN ('Inbound','Outbound')`; `Communication_Logs.CommunicationType IN ('Email','Phone Call','SMS','Portal Message')`. |
| **DEFAULT** | `Job_Openings.open_positions` DEFAULT `1`; `Offer_Letters.Backfill_Action_Taken` DEFAULT `FALSE`. |
| **Referential Integrity** | Foreign keys prevent orphaned records — e.g. `Candidate_Application.candidate_id` must exist in `Candidates`, `Offer_Letters.DecisionID` must exist in `Hiring_Decision`, `Interview_Panel.interviewer_id` must exist in `Employees`. |
| **Business Rules** | (i) An offer is linked 1:1 to a single `Selected` hiring decision via the `UNIQUE` `DecisionID`. (ii) `RejectionReason` is required when `DecisionStatus = 'Rejected'`. (iii) A candidate can hold at most one application per job (`UNIQUE (candidate_id, job_id)`). (iv) Salary band integrity: `max_salary >= min_salary >= 0`. (v) On candidate withdrawal, `Backfill_Action_Taken` records whether the vacated seat was re-opened. |

> 📝 **Why updated:** the old Constraints table was **out of date and contradicted the tables** — it listed `Offer_Letters.OfferStatus IN ('Pending','Accepted','Declined','Withdrawn')` (your table dropped `'Declined'`) and `Hiring_Decision.DecisionStatus IN ('Selected','Rejected')` (your table added `'On_Hold'`). It also missed every new `CHECK` (profiles, source, experience, role, job type, salary, positions, statuses), the new composite `UNIQUE`, the phone `UNIQUE`s, and both `DEFAULT`s. All of that is now reflected, and a new **DEFAULT** row was added since you now use defaults.

---

## **5. Normalization**

* **1NF (First Normal Form):** Every table has a primary key and all attributes are atomic. Multi-valued facts — multiple interviewers per round, multiple stages, multiple messages — live in dedicated child tables (`Interview_Panel`, `Application_Stage_History`, `Communication_Logs`) rather than comma-separated lists. Candidate links are kept atomic as a single primary `profile_url` (typed by `profile_type`) plus one optional `other_profile_url`.
* **2NF (Second Normal Form):** Surrogate single-column primary keys (INT IDs) are used throughout, so no non-key attribute can depend on only part of a key. Where a natural composite is needed (`Interview_Panel`, and the `(candidate_id, job_id)` uniqueness on `Candidate_Application`), the attributes depend on the full combination.
* **3NF (Third Normal Form):** No transitive dependencies. Candidate profiles hold no department or job data; departments hold no employee data; the candidate↔job link is bridged through `Candidate_Application`. Lookup-style values (statuses, types, sources) are constrained by `CHECK` enums rather than duplicated descriptive columns.
* **BCNF (Boyce-Codd Normal Form):** Every determinant is a candidate key. This is explicit in the 1:1 tables — `Hiring_Decision.ApplicationID` and `Offer_Letters.DecisionID` carry `UNIQUE`, guaranteeing strict one-to-one determinacy.

> 📝 **Why updated:** the 1NF and 3NF examples were rewritten to reference your **new** candidate-profile and enum columns instead of the old `resume_url`/`linkedin_url`. The 2NF note now also mentions the new composite `UNIQUE (candidate_id, job_id)`.
>
> 💡 **One genuine normalization consideration (optional):** storing a primary `profile_url` *and* an `other_profile_url` is a mild repeating-group pattern. If a candidate could have an arbitrary number of links, the stricter design is a separate `candidate_profiles` child table (`candidate_id`, `profile_type`, `url`). With just two fixed slots it's a reasonable pragmatic choice — but worth a sentence in your defense so the examiner sees you considered it.

---

## **6. Indexing Strategy**

| Frequent Query | Suggested Index & Justification |
| :---- | :---- |
| Candidates currently pending interview feedback. | **Index:** `CREATE INDEX idx_pending_feedback ON Interview_Panel(interview_id) WHERE feedback IS NULL;` **Justification:** a partial index stores only the small subset of rows missing feedback, avoiding the large `TEXT` column and making the lookup near-instant. |
| Open positions with the highest number of active applications. | **Index 1:** `CREATE INDEX idx_job_status ON Job_Openings(status);` **Index 2:** `CREATE INDEX idx_app_job_status ON Candidate_Application(job_id, current_status);` **Justification:** the composite index covers both the grouping (`job_id`) and the `current_status = 'Active'` filter, preventing full-table scans during aggregation. |
| Average time taken to move candidates from application to offer. | **Index:** `CREATE INDEX idx_offer_dates ON Offer_Letters(OfferDate);` plus `idx_app_date ON Candidate_Application(application_date)`. **Justification:** both endpoints of the duration are indexed, so cohorts of applications and offers can be pulled and differenced without scanning. |
| Interviewer workload by round and department. | **Index:** `CREATE INDEX idx_panel_interviewer ON Interview_Panel(interviewer_id);` **Justification:** supports the `GROUP BY` / `JOIN` on the employee identifier, removing the need to scan all panel rows. |
| Offer acceptance rates by job role. | **Index:** `CREATE INDEX idx_offer_status ON Offer_Letters(OfferStatus, DecisionID);` **Justification:** filters offers by status and carries the key needed to join upward through `Hiring_Decision → Candidate_Application → Job_Openings` to the role. |
| Candidates rejected or withdrawn in the last quarter. | **Index:** `CREATE INDEX idx_decision_status_date ON Hiring_Decision(DecisionStatus, DecisionDate);` **Justification:** placing the status and date adjacent in one composite index makes the time-boxed `'Rejected'` scan highly performant. |

> 📝 **Why updated:** all six indexes still apply, so they were kept; justifications were tightened and a second endpoint index was added for the "time-to-offer" query. Two behaviour changes from your new tables are worth flagging:
> - **`Active` now excludes `Waitlist`.** The "active applications" query filters `current_status = 'Active'`; waitlisted candidates are a separate state, so `idx_app_job_status` still serves it correctly.
> - **Withdrawals moved.** Rejection lives in `Hiring_Decision`, but withdrawal is now recorded in `Candidate_Application.current_status = 'Withdrawn'` and `Offer_Letters.OfferStatus = 'Withdrawn'`. The index above only time-boxes *rejections*. To bound *withdrawals* by date, lean on `Application_Stage_History.entered_date` (or `Offer_Letters.OfferDate`), since the application row itself has no status-change timestamp — consider `idx_history_app_time ON Application_Stage_History(application_id, entered_date)`.
>
> ⚙️ **Portability note:** the partial index (`WHERE feedback IS NULL`) is PostgreSQL syntax. On MySQL, partial indexes aren't supported — drop the `WHERE` clause and index `Interview_Panel(interview_id, feedback)` instead.
