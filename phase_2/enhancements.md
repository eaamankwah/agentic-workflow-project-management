# Phase 2 enhancements

All commands are run from the `phase_2/` folder. Every run writes `email_router_plan_<facet>.md`
(run summary table + generated artifacts) and appends to `workflow.log`.

---

## 1. Exploring different facets of project planning

`agentic_workflow.py` now takes a facet (or any custom prompt) on the command line:

| Command | Workflow prompt |
|---|---|
| `python agentic_workflow.py` | *Create a full development plan… user stories, features, development tasks* (default) |
| `python agentic_workflow.py --facet stories` | *Define only the user stories for the Email Router product.* |
| `python agentic_workflow.py --facet features` | *Define only the key features for the Email Router product.* |
| `python agentic_workflow.py --facet tasks` | *What would the development tasks for this product be?* (original starter prompt) |
| `python agentic_workflow.py --facet risk` | *Generate a risk assessment plan for the Email Router based on its specification.* |
| `python agentic_workflow.py --prompt "..."` | any prompt you write |

### How the workflow adapts (mechanism)

Nothing in the orchestration loop changes between facets. The adaptation comes from three layers:

1. **Action Planning Agent decides the scope.** It extracts only the steps its knowledge says the
   prompt needs, so the number of steps shrinks or grows with the prompt (a "features only" prompt
   should yield a features step, not stories and tasks).
2. **Routing Agent sends each step to the team whose description is semantically closest**, so a
   features step should go to the Program Manager and a risk step to the Risk Analyst.
3. **Each team's Evaluation Agent enforces that team's own output format** (stories, features,
   tasks, risk register), so the artifact type follows the route.

### The risk facet required extending the system, which shows how extensible it is

The original three teams have no notion of risk, so a risk prompt would have been routed to whichever
of them is nearest and produced the wrong artifact. Supporting it took three additions and *no change
to the loop*:

- a **Risk Analyst team** (knowledge agent grounded in the product spec + an evaluator requiring
  Risk ID, Description, Category, Likelihood, Impact, Mitigation Strategy, Owner);
- a fourth **route** in `routing_agent.agents`;
- an **extra sentence of planner knowledge** (used only for `--facet risk`) explaining what a risk
  assessment plan consists of, because the planner is told to return only steps found in its knowledge.

### Expected behaviour per facet (design analysis, not measured output)

| Facet | Expected steps | Expected routing | Expected artifact |
|---|---|---|---|
| full | stories, features, tasks | Product Manager → Program Manager → Development Engineer | all three artifact types |
| stories | user stories | Product Manager | user stories in *As a… I want… so that…* form (scored) |
| features | features | Program Manager | features (Name / Description / Key Functionality / User Benefit) |
| tasks | tasks | Development Engineer | tasks (ID / Title / Related User Story / … / Dependencies) |
| risk | identify risks, assess likelihood/impact, define mitigations | Risk Analyst | risk-register entries |

**Known weaknesses that show up in the narrower facets**

- `features` and `tasks` run a team that never sees the product spec or the user stories (only the
  step text reaches it), so its output is likely to be generic rather than about the Email Router.
  The Product Manager and Risk Analyst are grounded in the spec; the other two are not. See
  `reflection.md` for the proposed fix.
- Routing is by embedding similarity of a short step against a short description. Steps whose wording
  overlaps another role (e.g. *"Group user stories into features"* mentions "user stories") can be
  misrouted. The router prints every similarity score, so this is visible in the console.

### Recording what actually happened

I could not call the OpenAI API while building this, so the table above is what the design should do,
not observed results. Run each facet once; the generated `email_router_plan_<facet>.md` files contain a
**run summary table** (steps the planner chose, which team each step was routed to, evaluation
iterations, average score, seconds) that documents the real adaptation. `bash ../run_all.sh` runs all
of them and saves the terminal output under `../evidence/`.

---

## 2. Scored evaluation for the Product Manager (`ScoredEvaluationAgent`)

`ScoredEvaluationAgent` (in `base_agents.py`) subclasses `EvaluationAgent` and replaces the single
Yes/No verdict with a rubric of 1-5 scores. The Product Manager's evaluator uses five criteria:

| Criterion | What it checks |
|---|---|
| structure | every story follows *As a [user], I want [action] so that [benefit]* |
| persona_coverage | several distinct user types, not one persona repeated |
| spec_alignment | each story maps to the product spec (the spec is passed to the evaluator as reference) |
| value_clarity | the "so that" benefit is specific, not filler |
| atomicity | one testable capability per story |

**Acceptance rule:** average ≥ 4.0 **and** no criterion below 3 (both configurable). If a story set
fails, the weakest criteria and the evaluator's feedback become correction instructions and the worker
retries, up to `max_interactions`. The returned dictionary keeps the base keys (`final_response`,
`evaluation`, `iterations`) and adds `scores`, `average_score` and `passed`, so the support function
and the rest of the workflow are unchanged. If the evaluator's reply is not valid JSON the answer is
never accepted (it is retried rather than trusted).

To switch back to the plain Yes/No evaluator, set `USE_SCORED_PRODUCT_MANAGER_EVALUATION = False`
near the top of the Product Manager section.

---

## 3. Error handling and logging

| Situation | Behaviour |
|---|---|
| `OPENAI_API_KEY` missing, or spec file missing/empty | clear `CRITICAL` log line, exit code 1 |
| Request hangs | every agent's OpenAI client has a **60 s timeout** (`OPENAI_REQUEST_TIMEOUT` to change) so it raises instead of waiting forever |
| Timeout, connection error, rate limit (429), 5xx, empty response | **retried up to 3 times** with exponential backoff (2 s, 4 s; `WORKFLOW_RETRY_DELAY` to change) |
| Planner returns no steps | retried, then the workflow stops with a clear message |
| A step still fails after retries | logged as `ERROR`, recorded as `[STEP FAILED: …]`, **remaining steps still run**; the run summary shows `FAILED` and the exit code is 1 |
| Router cannot choose an agent | that step fails with a clear reason |
| Authentication error (bad/revoked key) | not retried; remaining steps are skipped because they would all fail |
| Evaluator never accepts an artifact within the iteration limit | the last attempt is used and a `WARNING` says it was **not accepted**; the summary shows "ok (not accepted by evaluator)" |

Logs go to the console and to `workflow.log` (timestamped, with level).

**Caveats.** A retry re-runs the whole step including its evaluation loop, so it is not free. The
provided agents' internal `print` traces are unchanged; only the workflow-level events use `logging`.
