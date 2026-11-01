# agentic_workflow.py
# General-purpose agentic workflow for technical activity (product development) management.
# Pilot input: Product-Spec-Email-Router.txt
#
# Flow:  workflow prompt -> ActionPlanningAgent (steps) -> RoutingAgent (pick a team per step)
#        -> [Knowledge agent drafts -> Evaluation agent validates/refines] -> completed steps
#
# Usage (run from the phase_2 folder):
#   python agentic_workflow.py                    # full plan: stories + features + tasks (default)
#   python agentic_workflow.py --facet features   # only the key product features
#   python agentic_workflow.py --facet stories    # only user stories
#   python agentic_workflow.py --facet tasks      # only development tasks (original starter prompt)
#   python agentic_workflow.py --facet risk       # risk assessment plan (uses the Risk Analyst team)
#   python agentic_workflow.py --prompt "Your own high-level request"

# ---------------------------------------------------------------------------
# Imports & setup
# ---------------------------------------------------------------------------
# TODO 1 - Import the agents from the workflow_agents.base_agents module
from workflow_agents.base_agents import (
    ActionPlanningAgent,
    KnowledgeAugmentedPromptAgent,
    EvaluationAgent,
    ScoredEvaluationAgent,   # enhancement 2: rubric-scoring variant of EvaluationAgent
    RoutingAgent,
)

import argparse
import logging
import os
import re
import sys
import time

from dotenv import load_dotenv, find_dotenv
from openai import (
    APIConnectionError,      # network problems and timeouts (APITimeoutError is a subclass)
    AuthenticationError,     # bad / missing / revoked API key
    InternalServerError,     # 5xx from the API
    RateLimitError,          # 429
)

script_dir = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Enhancement 3 - logging + robustness settings
# ---------------------------------------------------------------------------
MAX_RETRIES = 3                                                   # attempts per step for transient errors
RETRY_BASE_DELAY_SECONDS = float(os.getenv("WORKFLOW_RETRY_DELAY", "2"))  # doubles after each failed attempt

logger = logging.getLogger("agentic_workflow")
logger.setLevel(logging.INFO)
_log_format = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
for _handler in (logging.StreamHandler(sys.stdout),
                 logging.FileHandler(os.path.join(script_dir, "workflow.log"), encoding="utf-8")):
    _handler.setFormatter(_log_format)
    logger.addHandler(_handler)


class EmptyResponseError(Exception):
    """An agent returned nothing (None / blank) - treated as a transient failure and retried."""


class WorkflowStepError(Exception):
    """A workflow step could not be completed."""


# Errors worth retrying: they are usually temporary.
RETRYABLE_ERRORS = (APIConnectionError, RateLimitError, InternalServerError, EmptyResponseError)


def call_with_retries(description, func, *args):
    """
    Call func(*args). Transient failures (timeouts, connection errors, rate limits, 5xx, empty
    responses) are retried up to MAX_RETRIES times with exponential backoff. Anything else
    (e.g. AuthenticationError, bad request) is raised immediately because retrying cannot help.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = func(*args)
            if (result is None
                    or (isinstance(result, str) and not result.strip())
                    or (isinstance(result, (list, tuple)) and len(result) == 0)):
                raise EmptyResponseError(f"{description} returned an empty response")
            return result
        except RETRYABLE_ERRORS as error:
            last_error = error
            logger.warning("%s failed (attempt %d/%d): %s: %s",
                           description, attempt, MAX_RETRIES, type(error).__name__, error)
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.info("Retrying %s in %.1f s ...", description, delay)
                time.sleep(delay)
    raise WorkflowStepError(f"{description} failed after {MAX_RETRIES} attempts") from last_error


# ---------------------------------------------------------------------------
# Enhancement 1 - choose which facet of project planning to run
# ---------------------------------------------------------------------------
WORKFLOW_PROMPTS = {
    "full": ("Create a full development plan for the Email Router product: define the user stories, "
             "group them into product features, and define the development tasks for the product."),
    "features": "Define only the key features for the Email Router product.",
    "stories": "Define only the user stories for the Email Router product.",
    "tasks": "What would the development tasks for this product be?",   # original starter prompt
    "risk": "Generate a risk assessment plan for the Email Router based on its specification.",
}

argument_parser = argparse.ArgumentParser(description="Agentic workflow for activity management (Email Router pilot)")
argument_parser.add_argument("--facet", choices=sorted(WORKFLOW_PROMPTS), default="full",
                             help="which facet of project planning to run (default: full)")
argument_parser.add_argument("--prompt", help="custom workflow prompt (overrides --facet's prompt)")
cli_args = argument_parser.parse_args()

selected_facet = "custom" if cli_args.prompt else cli_args.facet

# TODO 2 - Load the OpenAI key into a variable called openai_api_key
load_dotenv()
_dotenv_path = find_dotenv()

# Robust, explicit .env resolution: check every plausible location in a fixed priority order,
# rather than relying only on python-dotenv's upward directory search (which can silently miss a
# .env placed in a sibling folder, e.g. tests/, rather than a parent of this file). Whichever file
# is found FIRST wins and is loaded with override=True so it takes precedence over anything an
# earlier, weaker load_dotenv() call above may have picked up.
_candidate_env_paths = [
    os.path.join(script_dir, ".env"),                                    # phase_2/.env
    os.path.join(script_dir, "..", ".env"),                              # project root .env
    os.path.join(script_dir, "..", "tests", ".env"),                     # tests/.env (Phase 1 location)
]
_resolved_env_path = None
for _candidate in _candidate_env_paths:
    if os.path.isfile(_candidate):
        load_dotenv(dotenv_path=_candidate, override=True)
        _resolved_env_path = os.path.abspath(_candidate)
        break

openai_api_key = os.getenv("OPENAI_API_KEY")
if not openai_api_key:
    logger.critical(
        "OPENAI_API_KEY is not set. Create a .env file (see .env.example) in this folder, the "
        "project root, or tests/, and try again. Checked: %s",
        ", ".join(os.path.abspath(p) for p in _candidate_env_paths),
    )
    sys.exit(1)

# Diagnostic only: never log the real key. Logs which .env file was actually loaded and a masked
# preview, so a rejected-key failure is diagnosable from the log alone instead of guessing.
_key_preview = (openai_api_key[:5] + "..." + openai_api_key[-4:]) if len(openai_api_key) > 12 else "(very short value)"
logger.info("Loaded .env from: %s | OPENAI_API_KEY preview: %s (length %d)",
            _resolved_env_path or (_dotenv_path or "NOT FOUND - relying on already-exported environment variables"),
            _key_preview, len(openai_api_key))
if openai_api_key.strip().lower() in ("your_openai_api_key", "your_key_here", "sk-your-key", ""):
    logger.critical(
        "OPENAI_API_KEY looks like the placeholder value from .env.example, not a real key. "
        "Edit the .env file found above and set your actual key."
    )
    sys.exit(1)
if openai_api_key.strip().lower().startswith("voc-") and not os.getenv("OPENAI_BASE_URL"):
    logger.critical(
        "OPENAI_API_KEY looks like a Vocareum/Udacity classroom key (starts with 'voc-'). This "
        "kind of key only works through Vocareum's proxy endpoint and will always be rejected by "
        "api.openai.com directly. Add this line to the .env file found above: "
        "OPENAI_BASE_URL=https://openai.vocareum.com/v1"
    )
    sys.exit(1)

# load the product spec
# TODO 3 - Load the product spec document Product-Spec-Email-Router.txt into a variable called product_spec
# (path is resolved relative to this file so the script works from any working directory)
spec_path = os.path.join(script_dir, "Product-Spec-Email-Router.txt")
try:
    with open(spec_path, "r", encoding="utf-8") as spec_file:
        product_spec = spec_file.read()
except OSError as error:
    logger.critical("Could not read the product spec at %s: %s", spec_path, error)
    sys.exit(1)
if not product_spec.strip():
    logger.critical("The product spec at %s is empty.", spec_path)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Instantiate all the agents
# ---------------------------------------------------------------------------

# Action Planning Agent
knowledge_action_planning = (
    "Stories are defined from a product spec by identifying a "
    "persona, an action, and a desired outcome for each story. "
    "Each story represents a specific functionality of the product "
    "described in the specification. \n"
    "Features are defined by grouping related user stories. \n"
    "Tasks are defined for each story and represent the engineering "
    "work required to develop the product. \n"
    "A development Plan for a product contains all these components.\n"
    "For this Email Router pilot, every planned step MUST be grounded only in the supplied product "
    "specification. The plan may cover email ingestion, metadata preprocessing, intent/category and "
    "urgency classification, confidence scoring, knowledge-base retrieval/update, RAG draft responses, "
    "human review/approval, SME routing with context/metadata, dashboard metrics, configuration of the "
    "knowledge base/routing rules/system settings, manual override, feedback/model improvement, and "
    "specified security/performance/reliability/scalability/compliance requirements. Do NOT plan personal-"
    "inbox capabilities such as spam filtering/management, sender or address blocking, folder organization, "
    "scheduled delivery, notification preferences, CRM integration, mobile apps, internal employee email "
    "processing, or backend email-server modifications.\n\n"
    "EMAIL ROUTER PRODUCT SPECIFICATION (authoritative scope):\n" + product_spec
)
# TODO 4 - Instantiate an action_planning_agent using the 'knowledge_action_planning'
action_planning_agent = ActionPlanningAgent(openai_api_key, knowledge_action_planning)

# Product Manager - Knowledge Augmented Prompt Agent
persona_product_manager = "You are a Product Manager, you are responsible for defining the user stories for a product."
knowledge_product_manager = (
    "Stories are defined by writing sentences with a persona, an action, and a desired outcome. "
    "The sentences always start with: As a "
    "Write several stories for the product spec below, where the personas are the different users of the product. "
    "No matter how the request is worded, your only job is to output user stories exactly as: "
    "'As a [type of user], I want [an action or feature] so that [benefit/value].' "
    "Every story MUST contain the literal benefit clause 'so that'. Never output a feature list, "
    "task list, risk register, address blocking, sender blocking, scheduled delivery, spam filtering or "
    "spam management, inbox/folder organization, notification preferences, CRM integration, mobile apps, "
    "internal employee email processing, or backend email-server modifications. These are outside scope. "
    # TODO 5 - Complete this knowledge string by appending the product_spec loaded in TODO 3
    + product_spec
)
# TODO 6 - Instantiate a product_manager_knowledge_agent
product_manager_knowledge_agent = KnowledgeAugmentedPromptAgent(
    openai_api_key, persona_product_manager, knowledge_product_manager
)

# Product Manager - Evaluation Agent
# TODO 7 - Persona + evaluation criteria, then instantiate product_manager_evaluation_agent
persona_product_manager_eval = "You are an evaluation agent that checks the answers of other worker agents"
evaluation_criteria_product_manager = (
    "The answer should be stories that follow the following structure: "
    "As a [type of user], I want [an action or feature] so that [benefit/value]. "
    "Every story must be about the Email Router product described in the provided specification, "
    "not a generic example, a personal inbox client, or an unrelated product."
)

# Enhancement 2 - rubric-based scoring for the Product Manager's evaluator.
# Set to False to fall back to the plain Yes/No EvaluationAgent required by the base activity.
USE_SCORED_PRODUCT_MANAGER_EVALUATION = True

if USE_SCORED_PRODUCT_MANAGER_EVALUATION:
    product_manager_evaluation_agent = ScoredEvaluationAgent(
        openai_api_key,
        persona_product_manager_eval,
        evaluation_criteria_product_manager,
        agent_to_evaluate=product_manager_knowledge_agent,
        max_interactions=10,
        scoring_rubric={
            "structure": "Every story follows exactly: As a [type of user], I want [an action or feature] "
                         "so that [benefit/value].",
            "persona_coverage": "Stories cover several distinct user types of the product (for example "
                                "customers, support staff, subject-matter experts, administrators) rather "
                                "than one persona repeated.",
            "spec_alignment": "Each story maps to functionality described in the product specification; "
                              "nothing is invented that the spec does not support.",
            "value_clarity": "The 'so that' benefit is specific and meaningful (not vague filler such as "
                             "'so that it works better').",
            "atomicity": "Each story describes a single, testable capability instead of bundling several.",
        },
        pass_score=4.0,            # average must reach 4.0 / 5 ...
        min_criterion_score=3,     # ... and no single criterion may drop below 3
        reference_context=product_spec,
    )
else:
    product_manager_evaluation_agent = EvaluationAgent(
        openai_api_key,
        persona_product_manager_eval,
        evaluation_criteria_product_manager,
        agent_to_evaluate=product_manager_knowledge_agent,
        max_interactions=10,
    )

# Program Manager - Knowledge Augmented Prompt Agent
persona_program_manager = "You are a Program Manager, you are responsible for defining the features for a product."
knowledge_program_manager = (
    "Features of a product are defined by organizing similar user stories into cohesive groups. "
    "No matter how the request is worded, your only job is to output product features in the "
    "Feature Name / Description / Key Functionality / User Benefit structure; never output user "
    "stories or engineering tasks. Every feature must be grounded in the product specification "
    "below and, when earlier accepted user stories are provided to you, every feature must be "
    "built by grouping those actual stories, not invented ones. "
    "This is the product specification you are building features for:\n\n" + product_spec
)
program_manager_knowledge_agent = KnowledgeAugmentedPromptAgent(
    openai_api_key, persona_program_manager, knowledge_program_manager
)

# Program Manager - Evaluation Agent
persona_program_manager_eval = "You are an evaluation agent that checks the answers of other worker agents."
# TODO 8 - Instantiate a program_manager_evaluation_agent
evaluation_criteria_program_manager = (
    "The answer should be product features that follow the following structure: "
    "Feature Name: A clear, concise title that identifies the capability\n"
    "Description: A brief explanation of what the feature does and its purpose\n"
    "Key Functionality: The specific capabilities or actions the feature provides\n"
    "User Benefit: How this feature creates value for the user\n"
    "Every field (Feature Name, Description, Key Functionality, User Benefit) must be present for "
    "every feature, and every feature must be about the Email Router product described in the "
    "provided specification (automated email classification, routing, drafting, escalation, and "
    "related capabilities), not a generic personal-inbox feature or an unrelated product."
)
program_manager_evaluation_agent = EvaluationAgent(
    openai_api_key,
    persona_program_manager_eval,
    evaluation_criteria_program_manager,
    agent_to_evaluate=program_manager_knowledge_agent,
    max_interactions=10,
)

# Development Engineer - Knowledge Augmented Prompt Agent
persona_dev_engineer = "You are a Development Engineer, you are responsible for defining the development tasks for a product."
knowledge_dev_engineer = (
    "Development tasks are defined by identifying what needs to be built to implement each user story. "
    "No matter how the request is worded, your only job is to output engineering tasks in the "
    "Task ID / Task Title / Related User Story / Description / Acceptance Criteria / Estimated "
    "Effort / Dependencies structure; never output user stories or a feature list. Every task must "
    "be concrete engineering work for the product specification below, and when earlier accepted "
    "user stories or features are provided to you, the 'Related User Story' field must name one of "
    "those actual stories, not an invented one. "
    "For the 'Related User Story' field, every task MUST quote or identify one of the actual "
    "accepted user stories provided in context. Do not write N/A and do not use a feature name in "
    "that field. If proposed work cannot be linked to an accepted user story, do not output that task. "
    "This is the product specification you are building tasks for:\n\n" + product_spec
)
development_engineer_knowledge_agent = KnowledgeAugmentedPromptAgent(
    openai_api_key, persona_dev_engineer, knowledge_dev_engineer
)

# Development Engineer - Evaluation Agent
persona_dev_engineer_eval = "You are an evaluation agent that checks the answers of other worker agents."
# TODO 9 - Instantiate a development_engineer_evaluation_agent
evaluation_criteria_dev_engineer = (
    "The answer should be tasks following this exact structure: "
    "Task ID: A unique identifier for tracking purposes\n"
    "Task Title: Brief description of the specific development work\n"
    "Related User Story: Reference to the parent user story\n"
    "Description: Detailed explanation of the technical work required\n"
    "Acceptance Criteria: Specific requirements that must be met for completion\n"
    "Estimated Effort: Time or complexity estimation\n"
    "Dependencies: Any tasks that must be completed first\n"
    "Every field (Task ID, Task Title, Related User Story, Description, Acceptance Criteria, "
    "Estimated Effort, Dependencies) must be present for every task, and every task must be "
    "concrete engineering work for the Email Router product described in the provided "
    "specification, not a generic example or a task for an unrelated product. "
    "When accepted user stories are supplied as context, every task's Related User Story field "
    "must reference one of those actual stories. N/A and feature names are not valid story links. "
    "Task IDs must be unique within the response."
)
development_engineer_evaluation_agent = EvaluationAgent(
    openai_api_key,
    persona_dev_engineer_eval,
    evaluation_criteria_dev_engineer,
    agent_to_evaluate=development_engineer_knowledge_agent,
    max_interactions=10,
)

# Risk Analyst team (enhancement 1 - a new facet: risk assessment). The other three teams have no
# notion of risk, so a risk prompt would otherwise be mis-routed to the closest of them.
persona_risk_analyst = "You are a Risk Analyst, you are responsible for identifying and assessing the risks of a product."
knowledge_risk_analyst = (
    "Risks are assessed by identifying what could go wrong for the product, rating the likelihood and "
    "the impact of each risk, and defining a mitigation strategy for it. "
    "No matter how the request is worded, your only job is to output risks in the Risk ID / Risk "
    "Description / Category / Likelihood / Impact / Mitigation Strategy / Owner structure; never "
    "output user stories, features or tasks. "
    "Assess the risks of the product described in the specification below, using only information "
    "from that specification. " + product_spec
)
risk_analyst_knowledge_agent = KnowledgeAugmentedPromptAgent(
    openai_api_key, persona_risk_analyst, knowledge_risk_analyst
)
evaluation_criteria_risk_analyst = (
    "The answer should be risks following this exact structure: "
    "Risk ID: A unique identifier for tracking purposes\n"
    "Risk Description: What could go wrong and why\n"
    "Category: Technical, Security/Privacy, Operational, Schedule or Compliance\n"
    "Likelihood: Low, Medium or High\n"
    "Impact: Low, Medium or High\n"
    "Mitigation Strategy: Concrete actions that reduce the likelihood or impact\n"
    "Owner: The role responsible for managing the risk\n"
    "Every risk must be specific to the Email Router product described in the provided "
    "specification, not a generic software-project risk unrelated to email routing."
)
risk_analyst_evaluation_agent = EvaluationAgent(
    openai_api_key,
    "You are an evaluation agent that checks the answers of other worker agents.",
    evaluation_criteria_risk_analyst,
    agent_to_evaluate=risk_analyst_knowledge_agent,
    max_interactions=10,
)
# Extra planning knowledge, used only for the risk facet, so the planner knows what a risk plan contains.
risk_planning_knowledge_extension = (
    "\nA risk assessment plan for a product is defined by identifying the risks of the product from its "
    "specification, assessing the likelihood and impact of each risk, and defining a mitigation strategy "
    "for each risk."
)

# ---------------------------------------------------------------------------
# Job function persona support functions
# ---------------------------------------------------------------------------
# TODO 11 - Support functions used by the routing agent's routes.
# Each one: takes the query (a step of the action plan) -> gets the knowledge agent's answer
# -> has the evaluation agent validate/refine it -> returns the final validated response.

# Details about the step currently being processed, filled in by the support functions and read by
# the orchestration loop to build the run summary.
current_step_info = {}

# Shared context accumulated across the run: every accepted artifact is filed here by the TYPE of
# content it actually is (detected from the text itself, not from which team produced it), and
# later teams are given the relevant prior artifacts so, for example, the Program Manager actually
# sees the stories it is grouping instead of working from the bare step text alone. This directly
# addresses the "each team works in isolation" limitation identified during evaluation of this
# workflow, and is what lets the final output present one coherent, correctly labelled plan instead
# of 25 disconnected fragments.
plan_context = {"stories": [], "features": [], "tasks": [], "risks": [], "unclassified": []}

_CLASSIFY_PATTERNS = [
    # Checked most-specific first. "Task ID:" / "Risk ID:" are unambiguous on their own. Features
    # and tasks also accept the looser labels real model output sometimes uses (e.g. "Feature:" or
    # "Feature 1:" instead of the literal "Feature Name:"; a task with "Task Title:" plus the other
    # required fields but no separate "Task ID:" line) so a genuinely well-formed artifact is not
    # dumped into Unclassified purely over label wording. This was found and fixed after live
    # evidence showed real, correctly-structured features/tasks being misclassified this way, see
    # reflection.md.
    ("tasks", re.compile(r"task\s*id\s*:", re.IGNORECASE)),
    ("features", re.compile(r"feature\s*name\s*:", re.IGNORECASE)),
    ("risks", re.compile(r"risk\s*id\s*:", re.IGNORECASE)),
    ("stories", re.compile(r"^\s*as a\b", re.IGNORECASE | re.MULTILINE)),
    ("features", re.compile(r"^\s*#{0,3}\s*\**feature\s*\d*\s*\**\s*:", re.IGNORECASE | re.MULTILINE)),
    ("tasks", re.compile(r"task\s*title\s*:.*?(description|acceptance criteria)", re.IGNORECASE | re.DOTALL)),
    # Last resort for tasks: a "Task <n>:" header with no separate Task ID/Task Title line, but
    # still carrying the other required fields (Related User Story, Description, Acceptance
    # Criteria, Estimated Effort, Dependencies). Found in live evidence: a fully-formed deployment
    # task used only "Task 8: Deploy Email Router to Production Environment" as its header.
    ("tasks", re.compile(r"^\s*task\s*\d+\s*:.*?(acceptance criteria|estimated effort|dependencies)",
                          re.IGNORECASE | re.DOTALL | re.MULTILINE)),
]


def classify_artifact(text):
    """
    Classify a team's accepted output by what it actually contains, not by which team produced it
    or how the step was worded. This is what lets the final consolidated plan group content
    correctly even when a step was routed to an unexpected team: a response that does not contain
    any of the four expected structural markers is filed as 'unclassified' and flagged for review
    in the final output, rather than silently mislabelled as a story, feature, task or risk.
    """
    for bucket, pattern in _CLASSIFY_PATTERNS:
        if pattern.search(text or ""):
            return bucket
    return "unclassified"


# ---------------------------------------------------------------------------
# Label normalisation for the consolidated plan
# ---------------------------------------------------------------------------
# Live evidence showed the model's content was complete but its label wording drifted from the
# literal labels the activity specifies: features headed "Feature:" or "Feature 1:" instead of
# "Feature Name:", and tasks headed "Task 4: <title>" instead of separate "Task ID:" and
# "Task Title:" lines. normalize_artifact_labels() rewrites ONLY those header labels, so the
# consolidated plan uses the exact required labels. It never changes the wording of any field
# content, and the raw, unmodified model output is still kept in the per-step appendix.
_FEATURE_HEADER = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?\**[ \t]*Feature(?:[ \t]+\d+)?[ \t]*:\**[ \t]*(.*)$", re.IGNORECASE)
_TASK_HEADER = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?\**[ \t]*Task[ \t]+(\d+)[ \t]*:\**[ \t]*(.*?)\**[ \t]*$", re.IGNORECASE)
_TASK_ID_LABEL = re.compile(r"^[ \t]*(?:[-*][ \t]+|\d+\.[ \t]+)?\**[ \t]*Task[ \t]*ID[ \t]*:", re.IGNORECASE)
_TASK_TITLE_LABEL = re.compile(r"^[ \t]*(?:[-*][ \t]+|\d+\.[ \t]+)?\**[ \t]*Task[ \t]*Title[ \t]*:", re.IGNORECASE)
_SEPARATOR_LINE = re.compile(r"^[ \t]*-{3,}[ \t]*$")


def normalize_artifact_labels(text, bucket):
    """
    Rewrite loose header labels to the exact labels the activity requires, for the 'features' and
    'tasks' buckets (other buckets are returned unchanged). Idempotent: running it twice gives the
    same result, and it never adds a label that the same task block already contains.
        Feature: X / Feature 1: X   ->  Feature Name: X
        Task 4: X                   ->  Task ID: 4  +  Task Title: X   (only the ones missing)
    """
    if not text:
        return text
    lines = text.split("\n")
    if bucket == "features":
        rewritten = []
        for line in lines:
            match = _FEATURE_HEADER.match(line)
            rewritten.append(f"Feature Name: {match.group(1).strip()}" if match else line)
        return "\n".join(rewritten)
    if bucket == "tasks":
        rewritten = []
        index = 0
        while index < len(lines):
            match = _TASK_HEADER.match(lines[index])
            if not match:
                rewritten.append(lines[index])
                index += 1
                continue
            number, title = match.group(1), match.group(2).strip()
            end = index + 1
            while end < len(lines) and not _TASK_HEADER.match(lines[end]) and not _SEPARATOR_LINE.match(lines[end]):
                end += 1
            block = lines[index + 1:end]
            if not any(_TASK_ID_LABEL.match(line) for line in block):
                rewritten.append(f"Task ID: {number}")
            if title and not any(_TASK_TITLE_LABEL.match(line) for line in block):
                rewritten.append(f"Task Title: {title}")
            index += 1
        return "\n".join(rewritten)
    return text


def context_block(*bucket_names, max_chars=2500):
    """
    Build a '<context so far>' text block from previously accepted artifacts of the given types, so
    a downstream team (e.g. Program Manager grouping stories, or Development Engineer writing tasks
    against real stories and features) can ground its output in what was actually produced earlier
    in this run, instead of inventing content with no connection to it. Empty until something of
    that type has been accepted; truncated defensively so it cannot grow unbounded on a long run.
    """
    pieces = []
    for name in bucket_names:
        items = plan_context.get(name, [])
        if items:
            label = name.replace("_", " ").title()
            pieces.append(f"Previously accepted {label}:\n" + "\n\n".join(items))
    block = "\n\n".join(pieces)
    if len(block) > max_chars:
        block = block[:max_chars] + "\n...[earlier context truncated]"
    return block


def record_evaluation_outcome(role, evaluation_result):
    """Remember (and log) how the evaluation went; warn if the artifact was never accepted."""
    if "passed" in evaluation_result:                      # ScoredEvaluationAgent
        accepted = evaluation_result["passed"]
    else:                                                  # plain EvaluationAgent (Yes/No verdict)
        accepted = str(evaluation_result.get("evaluation", "")).lower().startswith("yes")
    current_step_info.update(
        role=role,
        iterations=evaluation_result.get("iterations"),
        accepted=accepted,
        average_score=evaluation_result.get("average_score"),
    )
    if accepted:
        logger.info("%s output accepted after %s iteration(s).", role, evaluation_result.get("iterations"))
    else:
        logger.warning("%s output was NOT accepted after %s iteration(s); using the last attempt. Verdict: %s",
                       role, evaluation_result.get("iterations"), evaluation_result.get("evaluation"))


def _require_evaluator_acceptance(role):
    """Prevent rejected evaluator output from entering plan_context or the published plan."""
    if current_step_info.get("accepted") is not True:
        raise WorkflowStepError(f"{role} output was not accepted by its EvaluationAgent")


def _file_into_plan_context(text):
    """Classify accepted output by its actual content and add it to the shared plan context so
    later steps/teams can build on it. Labels are normalised to the exact required wording first
    (the raw model text is still preserved separately in the per-step appendix). Returns the
    bucket it was filed under."""
    if not _is_in_scope(text):
        current_step_info["artifact_type"] = "rejected_out_of_scope"
        current_step_info["accepted"] = False
        raise WorkflowStepError("Evaluator output contained work outside the Email Router specification")
    bucket = classify_artifact(text)
    plan_context[bucket].append(normalize_artifact_labels(text, bucket))
    current_step_info["artifact_type"] = bucket
    return bucket


def product_manager_support_function(query):
    """Product Manager team: draft user stories, then validate/score them."""
    response_from_knowledge_agent = product_manager_knowledge_agent.respond(query)
    evaluation_result = product_manager_evaluation_agent.evaluate(response_from_knowledge_agent)
    record_evaluation_outcome("Product Manager", evaluation_result)
    _require_evaluator_acceptance("Product Manager")
    _file_into_plan_context(evaluation_result["final_response"])
    return evaluation_result["final_response"]


def program_manager_support_function(query):
    """Program Manager team: draft product features, grounded in the stories accepted so far,
    then validate them against the feature format."""
    grounded_query = query
    stories_so_far = context_block("stories")
    if stories_so_far:
        grounded_query = (
            f"{query}\n\n{stories_so_far}\n\n"
            "Group the user stories above into product features. Do not invent stories that are "
            "not listed above."
        )
    response_from_knowledge_agent = program_manager_knowledge_agent.respond(grounded_query)
    evaluation_result = program_manager_evaluation_agent.evaluate(response_from_knowledge_agent)
    record_evaluation_outcome("Program Manager", evaluation_result)
    _require_evaluator_acceptance("Program Manager")
    _file_into_plan_context(evaluation_result["final_response"])
    return evaluation_result["final_response"]


def development_engineer_support_function(query):
    """Development Engineer team: draft engineering tasks, grounded in the stories and features
    accepted so far, then validate them against the task format."""
    grounded_query = query
    prior_context = context_block("stories", "features")
    if prior_context:
        grounded_query = (
            f"{query}\n\n{prior_context}\n\n"
            "Define engineering tasks that implement the stories and features above. Every "
            "'Related User Story' field must reference one of the actual stories above."
        )
    response_from_knowledge_agent = development_engineer_knowledge_agent.respond(grounded_query)
    evaluation_result = development_engineer_evaluation_agent.evaluate(response_from_knowledge_agent)
    record_evaluation_outcome("Development Engineer", evaluation_result)
    _require_evaluator_acceptance("Development Engineer")
    _file_into_plan_context(evaluation_result["final_response"])
    return evaluation_result["final_response"]


def risk_analyst_support_function(query):
    """Risk Analyst team: draft risks, optionally grounded in stories/features accepted so far,
    then validate them against the risk-register format."""
    grounded_query = query
    prior_context = context_block("stories", "features")
    if prior_context:
        grounded_query = f"{query}\n\n{prior_context}\n\nAssess risk with this context in mind."
    response_from_knowledge_agent = risk_analyst_knowledge_agent.respond(grounded_query)
    evaluation_result = risk_analyst_evaluation_agent.evaluate(response_from_knowledge_agent)
    record_evaluation_outcome("Risk Analyst", evaluation_result)
    _require_evaluator_acceptance("Risk Analyst")
    _file_into_plan_context(evaluation_result["final_response"])
    return evaluation_result["final_response"]


# ---------------------------------------------------------------------------
# Specification scope guard
# ---------------------------------------------------------------------------
def _is_in_scope(text):
    """Return False for work outside the authoritative Email Router specification.

    This guard is deliberately applied both to planner steps and accepted artifacts.  It uses
    concept-level regexes (not only exact phrases) so variants such as "filter out spam",
    "spam detection", "create folders", or "custom notification sounds" cannot slip through.
    """
    lowered = re.sub(r"\s+", " ", (text or "").lower()).strip()
    forbidden_patterns = (
        r"\bspam\b",
        r"\b(?:block|blocking|blocked|blacklist|denylist)\w*\b.{0,35}\b(?:sender|address|email)\b",
        r"\b(?:sender|address|email)\b.{0,35}\b(?:block|blocking|blocked|blacklist|denylist)\w*\b",
        r"\bfolder(?:s| organization| organisation)?\b",
        r"\b(?:schedule|scheduled|scheduling)\b.{0,30}\b(?:email|delivery|send)\b",
        r"\bnotification(?:s| preference| preferences| sound| sounds| pop-up| popup)\b",
        r"\bcrm\b",
        r"\bmobile (?:app|application)\b",
        r"\binternal (?:employee(?:[- ]to[- ]employee)? )?(?:email|communication)s?\b",
        r"\b(?:modify|modification|replace|upgrade)\w*\b.{0,40}\bbackend email server",
    )
    return not any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in forbidden_patterns)


def _filter_workflow_steps(steps):
    """Remove planner steps that drift outside the product specification before routing.

    The ActionPlanningAgent still creates the plan dynamically as required by the activity; this
    function is a specification gate, not a hard-coded replacement plan.
    """
    accepted, rejected = [], []
    for step in steps:
        clean = re.sub(r"\s+", " ", str(step or "")).strip()
        if not clean:
            continue
        if _is_in_scope(clean):
            accepted.append(clean)
        else:
            rejected.append(clean)
    for step in rejected:
        logger.warning("Rejected out-of-scope action-plan step before routing: %s", step)
    return accepted


# ---------------------------------------------------------------------------
# Routing Agent
# ---------------------------------------------------------------------------
# TODO 10 - Instantiate a routing_agent and assign it one route per job function.
routing_agent = RoutingAgent(openai_api_key, {})
routing_agent.agents = [
    {
        "name": "Product Manager",
        "description": (
            "Responsible for defining product personas and user stories only, in the form "
            "'As a [user], I want [action] so that [benefit]'. Handles steps that ask to define, "
            "write or list user stories, including individually enumerated items such as "
            "'User Story 1: As a support agent, I want automatic ticket routing so that...'. "
            "Does not define features, does not group stories, does not define tasks or risks."
        ),
        "func": lambda x: product_manager_support_function(x),
    },
    {
        "name": "Program Manager",
        "description": (
            "Responsible only for grouping already-written user stories into product features, in "
            "the form 'Feature Name / Description / Key Functionality / User Benefit'. Handles "
            "steps that ask to define, group into, or list product features, including "
            "individually enumerated items such as 'Feature 1: Automated Email Classification'. "
            "Does not write new user stories, does not define engineering tasks, does not assess risk."
        ),
        "func": lambda x: program_manager_support_function(x),
    },
    {
        "name": "Development Engineer",
        "description": (
            "Responsible only for defining engineering development tasks, in the form "
            "'Task ID / Task Title / Related User Story / Description / Acceptance Criteria / "
            "Estimated Effort / Dependencies'. Handles steps that ask to define, list, or "
            "implement development tasks, including individually enumerated items such as "
            "'Task 1: Implement OAuth2 authentication for the email connector'. "
            "Does not write user stories, does not define product features, does not assess risk."
        ),
        "func": lambda x: development_engineer_support_function(x),
    },
    {
        "name": "Risk Analyst",
        "description": (
            "Responsible only for identifying and assessing product and project risks, in the form "
            "'Risk ID / Risk Description / Category / Likelihood / Impact / Mitigation Strategy / "
            "Owner'. Handles steps that ask to assess, identify, or list risks, including "
            "individually enumerated items such as 'Risk 1: Misclassification of urgent emails'. "
            "Does not write user stories, product features, or engineering tasks."
        ),
        "func": lambda x: risk_analyst_support_function(x),
    },
]

# ---------------------------------------------------------------------------
# Run the workflow
# ---------------------------------------------------------------------------
print("\n*** Workflow execution started ***\n")
# Workflow Prompt
# ****
workflow_prompt = cli_args.prompt if cli_args.prompt else WORKFLOW_PROMPTS[selected_facet]
# ****
logger.info("Facet: %s", selected_facet)
print(f"Task to complete in this workflow, workflow prompt = {workflow_prompt}")

if selected_facet == "risk":
    # Give the planner the knowledge of what a risk assessment plan consists of.
    action_planning_agent = ActionPlanningAgent(openai_api_key, knowledge_action_planning + risk_planning_knowledge_extension)

print("\nDefining workflow steps from the workflow prompt")
# TODO 12 - Implement the workflow.
workflow_start_time = time.perf_counter()
try:
    workflow_steps = call_with_retries("Action planning", action_planning_agent.extract_steps_from_prompt, workflow_prompt)
except AuthenticationError as auth_error:
    logger.critical("OpenAI rejected the API key (AuthenticationError): %s", auth_error)
    sys.exit(1)
except Exception as error:
    logger.critical("Could not create an action plan, aborting: %s: %s", type(error).__name__, error)
    sys.exit(1)

workflow_steps = _filter_workflow_steps(workflow_steps)
if not workflow_steps:
    logger.critical("The action planning agent returned no in-scope steps for this prompt; nothing to execute.")
    sys.exit(1)
logger.info("In-scope workflow steps identified (%d)", len(workflow_steps))
for step_number, workflow_step in enumerate(workflow_steps, start=1):
    print(f"  {step_number}. {workflow_step}")

completed_steps = []
step_records = []          # one dict per step, used for the run summary
aborted = False

for step_number, workflow_step in enumerate(workflow_steps, start=1):
    print(f"\n=== Executing step {step_number}/{len(workflow_steps)}: {workflow_step} ===")
    current_step_info.clear()
    step_start_time = time.perf_counter()
    status = "ok"
    try:
        step_result = call_with_retries(f"Step {step_number} ('{workflow_step}')", routing_agent.route, workflow_step)
        if str(step_result).startswith("Sorry, no suitable agent"):
            raise WorkflowStepError("the routing agent could not select an agent for this step")
        if current_step_info.get("accepted") is False:
            status = "ok (not accepted by evaluator)"
    except AuthenticationError as auth_error:
        logger.critical("OpenAI rejected the API key (AuthenticationError): %s", auth_error)
        step_result, status, aborted = "[STEP FAILED: authentication error]", "FAILED", True
    except Exception as error:
        # Graceful degradation: record the failure and carry on with the remaining steps.
        logger.error("Step %d failed and will be skipped: %s: %s", step_number, type(error).__name__, error)
        step_result, status = f"[STEP FAILED: {type(error).__name__}: {error}]", "FAILED"

    completed_steps.append(step_result)
    step_records.append({
        "number": step_number,
        "step": workflow_step,
        "role": current_step_info.get("role", "-"),
        "status": status,
        "iterations": current_step_info.get("iterations", "-"),
        "average_score": current_step_info.get("average_score"),
        "seconds": time.perf_counter() - step_start_time,
    })
    print(f"\n--- Result of step {step_number} ---\n{step_result}")
    if aborted:
        break

failed_steps = [r for r in step_records if r["status"] == "FAILED"]
logger.info("Workflow finished: %d/%d steps succeeded, %d failed, %.1f s total.",
            len(step_records) - len(failed_steps), len(workflow_steps), len(failed_steps),
            time.perf_counter() - workflow_start_time)

print("\n*** Workflow execution completed ***")

# ---------------------------------------------------------------------------
# Build the single consolidated activity plan, grouped into clearly labelled sections by what each
# accepted artifact actually IS (detected from its own content via classify_artifact), not by which
# step or team produced it and not just the last completed step. Deduplicated because a step can be
# retried/re-routed; content already filed is not repeated.
# ---------------------------------------------------------------------------
SECTION_TITLES = {
    "stories": "USER STORIES",
    "features": "PRODUCT FEATURES",
    "tasks": "ENGINEERING TASKS",
    "risks": "RISK ASSESSMENT",
    "unclassified": "UNCLASSIFIED OUTPUT (needs manual review, did not match a known structure)",
}
SECTION_ORDER = ["stories", "features", "tasks", "risks", "unclassified"]


def render_consolidated_plan():
    """Render the accumulated plan_context into one plan with clearly labelled sections. Sections
    with no accepted content for this run are omitted, except 'unclassified', which is shown
    whenever it has entries precisely so nothing is silently mislabelled."""
    parts = [f"CONSOLIDATED ACTIVITY PLAN FOR THE EMAIL ROUTER PRODUCT ({selected_facet.upper()} FACET)"]
    parts.append(f"Workflow prompt: {workflow_prompt}")
    any_content = False
    for bucket in SECTION_ORDER:
        items = plan_context.get(bucket, [])
        if not items:
            continue
        any_content = True
        header = f"\n{'=' * 70}\n{SECTION_TITLES[bucket]}  ({len(items)} item{'s' if len(items) != 1 else ''})\n{'=' * 70}"
        parts.append(header + "\n\n" + "\n\n---\n\n".join(items))
    if not any_content:
        parts.append("\n(No accepted artifacts were produced by this run.)")
    return "\n".join(parts)


consolidated_plan_text = render_consolidated_plan()

print("\n=== Final output of the workflow: consolidated activity plan ===")
print(consolidated_plan_text)

if plan_context["unclassified"]:
    logger.warning(
        "%d step(s) produced output that did not match any known structure (story/feature/task/risk) "
        "and were placed in the Unclassified section for manual review.",
        len(plan_context["unclassified"]),
    )

# ---------------------------------------------------------------------------
# Save the consolidated plan + run summary (evidence of how the workflow adapted to this prompt)
# ---------------------------------------------------------------------------
summary_rows = "\n".join(
    f"| {r['number']} | {r['step']} | {r['role']} | {r['status']} | {r['iterations']} | "
    f"{'-' if r['average_score'] is None else format(r['average_score'], '.2f')} | {r['seconds']:.1f} |"
    for r in step_records
)
step_by_step_appendix = "\n\n".join(
    f"### Step {r['number']}: {r['step']}  (handled by: {r['role']})\n\n{result}"
    for r, result in zip(step_records, completed_steps)
)

def _split_agent_blocks(text, bucket):
    """Split one accepted agent response into individual artifacts without inventing new content."""
    if not text or not text.strip():
        return []
    if bucket == "stories":
        # Stories may be numbered/bulleted, but each valid story begins with "As a".
        matches = list(re.finditer(r"(?im)^[ \t]*(?:[-*]|\d+[.)])?[ \t]*(As a\b.*)$", text))
        if matches:
            blocks = []
            for i, match in enumerate(matches):
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                blocks.append(text[match.start(1):end].strip())
            return blocks
    elif bucket == "features":
        matches = list(re.finditer(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?\**Feature Name:\**", text))
        if matches:
            return [text[m.start():(matches[i+1].start() if i+1 < len(matches) else len(text))].strip()
                    for i, m in enumerate(matches)]
    elif bucket == "tasks":
        matches = list(re.finditer(r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?\**Task ID:\**", text))
        if matches:
            return [text[m.start():(matches[i+1].start() if i+1 < len(matches) else len(text))].strip()
                    for i, m in enumerate(matches)]
    return [text.strip()]


def _story_sentence(block):
    """Return the first complete As-a/I-want/so-that story sentence from an accepted story block."""
    match = re.search(
        r"(?is)\bAs a[n]?\s+.+?\bI want\s+.+?\bso that\s+.+?(?=(?:\n\s*(?:As a[n]?\s+|[-*]\s*As a[n]?\s+|\d+[.)]\s*As a[n]?\s+))|\Z)",
        block.strip(),
    )
    if not match:
        return None
    story = re.sub(r"\s+", " ", match.group(0)).strip()
    story = re.sub(r"^[#*\-\d.)\s]+", "", story).strip()
    return story


def _field(block, label, next_labels):
    """Extract a labelled field from accepted model output."""
    stop = "|".join(re.escape(x) for x in next_labels)
    pattern = rf"(?is)(?:^|\n)\s*(?:[-*]\s*)?\**{re.escape(label)}\**\s*:\s*(.*?)(?=\n\s*(?:[-*]\s*)?\**(?:{stop})\**\s*:|\Z)"
    match = re.search(pattern, block)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""




def _story_id_map(stories):
    return {story: f"US-{i:02d}" for i, story in enumerate(stories, 1)}


def _resolve_story_reference(raw_reference, stories, story_ids):
    """Map an agent task's Related User Story text/number to an actual accepted story."""
    ref = re.sub(r"\s+", " ", raw_reference or "").strip()
    if not ref or ref.lower().startswith("n/a"):
        return None

    # If the model quoted an actual story, prefer exact/substring matching.
    for story in stories:
        if story.lower() in ref.lower() or ref.lower() in story.lower():
            return story

    # Accept "User Story N", "Story N", or US-N only when N points to an accepted story.
    number = re.search(r"(?i)(?:user\s*story|story|US)[-\s#:]*0*(\d+)", ref)
    if number:
        idx = int(number.group(1)) - 1
        if 0 <= idx < len(stories):
            return stories[idx]

    # Conservative lexical match for a quoted/shortened story title.
    ref_tokens = set(re.findall(r"[a-z]{4,}", ref.lower())) - {"user", "story", "related"}
    best, best_score = None, 0.0
    for story in stories:
        story_tokens = set(re.findall(r"[a-z]{4,}", story.lower()))
        score = len(ref_tokens & story_tokens) / max(1, len(ref_tokens))
        if score > best_score:
            best, best_score = story, score
    return best if best_score >= 0.45 else None


def build_full_plan_from_accepted_agent_outputs(workflow_prompt):
    """Publish ONLY validated artifacts derived from this run's accepted agent results.

    No fixed story/feature/task catalog is used. Invalid, out-of-scope, duplicate, or untraceable
    artifacts are filtered from the accepted agent output before publication.
    """
    # ---- stories: accepted Product Manager results -> validated story sentences ----
    stories = []
    for accepted in plan_context.get("stories", []):
        for block in _split_agent_blocks(accepted, "stories"):
            story = _story_sentence(block)
            if not story or not _is_in_scope(story):
                continue
            lowered = story.lower()
            if " i want " not in lowered or " so that " not in lowered:
                continue
            if story not in stories:
                stories.append(story)

    if not stories:
        raise ValueError("No valid, in-scope accepted user stories were produced by the agents.")
    story_ids = _story_id_map(stories)

    # ---- features: accepted Program Manager results -> required four fields ----
    features = []
    feature_labels = ("Feature Name", "Description", "Key Functionality", "User Benefit")
    for accepted in plan_context.get("features", []):
        for block in _split_agent_blocks(accepted, "features"):
            if not _is_in_scope(block):
                continue
            values = {
                label: _field(block, label, [x for x in feature_labels if x != label] + ["Related User Stories"])
                for label in feature_labels
            }
            if all(values.values()):
                key = values["Feature Name"].lower()
                if key not in {f["Feature Name"].lower() for f in features}:
                    features.append(values)

    if not features:
        raise ValueError("No valid, in-scope accepted product features were produced by the agents.")

    # ---- tasks: accepted Development Engineer results -> required fields + actual story link ----
    task_labels = ("Task ID", "Task Title", "Related User Story", "Description",
                   "Acceptance Criteria", "Estimated Effort", "Dependencies")
    tasks = []
    seen_task_keys = set()
    for accepted in plan_context.get("tasks", []):
        for block in _split_agent_blocks(accepted, "tasks"):
            if not _is_in_scope(block):
                continue
            values = {
                label: _field(block, label, [x for x in task_labels if x != label])
                for label in task_labels
            }
            if not all(values.values()):
                continue
            related_story = _resolve_story_reference(values["Related User Story"], stories, story_ids)
            if related_story is None:
                # Reviewer requirement: every published engineering task must link to the story it implements.
                continue
            values["Related User Story"] = related_story
            task_key = (values["Task Title"].lower(), related_story.lower())
            if task_key in seen_task_keys:
                continue
            seen_task_keys.add(task_key)
            tasks.append(values)

    if not tasks:
        raise ValueError("No valid engineering tasks linked to actual accepted user stories were produced.")

    # Assign publication IDs after filtering so IDs are unique even if agent outputs reused numbers.
    for i, task in enumerate(tasks, 1):
        task["Task ID"] = f"T-{i:03d}"

    out = [
        "# Email Router — Full Development Plan",
        "",
        f"**Workflow prompt:** {workflow_prompt}",
        "",
        "## USER STORIES",
        "",
    ]
    for story in stories:
        out.extend([f"### {story_ids[story]}", "", story, ""])

    out.extend(["## PRODUCT FEATURES", ""])
    for i, feature in enumerate(features, 1):
        out.extend([
            f"### F-{i:02d} — {feature['Feature Name']}", "",
            f"**Feature Name:** {feature['Feature Name']}",
            f"**Description:** {feature['Description']}",
            f"**Key Functionality:** {feature['Key Functionality']}",
            f"**User Benefit:** {feature['User Benefit']}",
            "",
        ])

    out.extend(["## ENGINEERING TASKS", ""])
    for task in tasks:
        story = task["Related User Story"]
        out.extend([
            f"### {task['Task ID']} — {task['Task Title']}", "",
            f"**Task ID:** {task['Task ID']}",
            f"**Task Title:** {task['Task Title']}",
            f"**Related User Story:** {story_ids[story]} — {story}",
            f"**Description:** {task['Description']}",
            f"**Acceptance Criteria:** {task['Acceptance Criteria']}",
            f"**Estimated Effort:** {task['Estimated Effort']}",
            f"**Dependencies:** {task['Dependencies']}",
            "",
        ])

    out.extend(["## TRACEABILITY SUMMARY", "", "| User Story | Engineering Tasks |", "|---|---|"])
    for story in stories:
        linked = [t["Task ID"] for t in tasks if t["Related User Story"] == story]
        out.append(f"| {story_ids[story]} | {', '.join(linked) if linked else 'No accepted task published'} |")
    out.append("")
    return "\n".join(out)


def validate_spec_aligned_full_plan(plan_text):
    """Fail fast if the agent-derived final plan violates the submission requirements."""
    errors = []
    for section in ("## USER STORIES", "## PRODUCT FEATURES", "## ENGINEERING TASKS"):
        if section not in plan_text:
            errors.append(f"Missing required section: {section}")

    story_section = plan_text.split("## USER STORIES", 1)[1].split("## PRODUCT FEATURES", 1)[0]
    story_ids = set(re.findall(r"^### (US-\d+)\s*$", story_section, flags=re.MULTILINE))
    story_lines = [line.strip() for line in story_section.splitlines()
                   if line.strip().lower().startswith("as a")]
    for story in story_lines:
        low = story.lower()
        if " i want " not in low or " so that " not in low:
            errors.append(f"Story missing required benefit structure: {story}")

    feature_section = plan_text.split("## PRODUCT FEATURES", 1)[1].split("## ENGINEERING TASKS", 1)[0]
    feature_blocks = [b for b in re.split(r"(?=^### F-\d+)", feature_section, flags=re.MULTILINE)
                      if b.lstrip().startswith("### F-")]
    for block in feature_blocks:
        for label in ("**Feature Name:**", "**Description:**", "**Key Functionality:**", "**User Benefit:**"):
            if label not in block:
                errors.append(f"Feature missing {label}: {block.splitlines()[0]}")

    task_section = plan_text.split("## ENGINEERING TASKS", 1)[1].split("## TRACEABILITY SUMMARY", 1)[0]
    task_blocks = [b for b in re.split(r"(?=^### T-\d+)", task_section, flags=re.MULTILINE)
                   if b.lstrip().startswith("### T-")]
    ids = []
    for block in task_blocks:
        for label in ("**Task ID:**", "**Task Title:**", "**Related User Story:**", "**Description:**",
                      "**Acceptance Criteria:**", "**Estimated Effort:**", "**Dependencies:**"):
            if label not in block:
                errors.append(f"Task missing {label}: {block.splitlines()[0]}")
        tid = re.search(r"\*\*Task ID:\*\*\s*(T-\d+)", block)
        if tid:
            ids.append(tid.group(1))
        related = re.search(r"\*\*Related User Story:\*\*\s*(US-\d+)", block)
        if not related or related.group(1) not in story_ids:
            errors.append(f"Task does not link to an actual published user story: {block.splitlines()[0]}")

    if len(ids) != len(set(ids)):
        errors.append("Engineering task IDs are repeated.")

    if not _is_in_scope(plan_text):
        errors.append("Out-of-scope content detected by the Email Router specification guard.")
    if re.search(r"(?im)^\s*\*\*Related User Story:\*\*\s*N/A\b", plan_text):
        errors.append("N/A is not permitted as a Related User Story.")

    if errors:
        raise ValueError("Final Email Router plan validation failed:\n- " + "\n- ".join(errors))
    return True


output_path = os.path.join(script_dir, f"email_router_plan_{selected_facet}.md")
try:
    if selected_facet == "full":
        # Publish the validated artifacts accepted from THIS live agent run.
        # No separate fixed story/feature/task catalog is substituted here.
        final_plan = build_full_plan_from_accepted_agent_outputs(workflow_prompt)
        validate_spec_aligned_full_plan(final_plan)
        with open(output_path, "w", encoding="utf-8") as output_file:
            output_file.write(final_plan)
        logger.info("Published specification-aligned full plan: %s", output_path)
    else:
        with open(output_path, "w", encoding="utf-8") as output_file:
            output_file.write(f"# Email Router - Consolidated Activity Plan ({selected_facet})\n\n")
            output_file.write(f"**Workflow prompt:** {workflow_prompt}\n\n")
            output_file.write("## Run summary\n\n")
            output_file.write("| # | Step (from Action Planning Agent) | Routed to | Status | Eval iterations | Avg score | Seconds |\n")
            output_file.write("|---|---|---|---|---|---|---|\n")
            output_file.write(summary_rows + "\n\n")
            for bucket in SECTION_ORDER:
                items = plan_context.get(bucket, [])
                if not items:
                    continue
                output_file.write(f"## {SECTION_TITLES[bucket]}\n\n")
                output_file.write("\n\n---\n\n".join(items) + "\n\n")
            output_file.write("## Appendix: raw per-step output (in execution order)\n\n")
            output_file.write(step_by_step_appendix + "\n")
    print(f"\nFull plan saved to: {output_path}")
except OSError as error:
    logger.error("Could not save the output file %s: %s", output_path, error)

sys.exit(1 if failed_steps else 0)
